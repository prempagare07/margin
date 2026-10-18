"""Integration tests on a disposable LOCAL database named margin_test.

Set TEST_DATABASE_URL explicitly. This suite never loads .env or falls back to
DATABASE_URL. It truncates its test tables between cases; see docs/HANDOFF.md.
Timescale aggregate creation is excluded; tables and the spending function come
directly from the production schema, including all constraints and indexes.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet, InvalidToken
from psycopg.conninfo import conninfo_to_dict

from db import plaid as plaid_store, repo
from shared.protocol import Item

pytestmark = pytest.mark.asyncio


@pytest.fixture(scope="module")
def test_dsn():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is unset; local PostgreSQL integration tests skipped")
    info = conninfo_to_dict(dsn)
    if (info.get("host") not in {"127.0.0.1", "localhost", "::1"}
            or info.get("dbname") != "margin_test" or "hostaddr" in info or "service" in info):
        pytest.fail("Integration tests require a local database named margin_test")
    schema = (Path(__file__).parents[1] / "db/schema.sql").read_text()
    tables = schema.split("-- Convert transactions to a TimescaleDB hypertable.")[0]
    tables = tables.replace("CREATE EXTENSION IF NOT EXISTS timescaledb;", "")
    function = "CREATE FUNCTION public.flexible_spent_this_month" + schema.split(
        "CREATE FUNCTION public.flexible_spent_this_month", 1
    )[1].split("COMMIT;", 1)[0]
    with psycopg.connect(dsn, autocommit=True) as conn:
        if conn.execute("SELECT to_regclass('public.users')").fetchone()[0] is None:
            conn.execute(tables + function + "COMMIT;", prepare=False)
    return dsn


@pytest_asyncio.fixture(autouse=True)
async def database(test_dsn, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", test_dsn)
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    try:
        async with (await repo.get_pool()).connection() as conn:
            await conn.execute("TRUNCATE public.users CASCADE")
        await repo.upsert_user("alex")
        await repo.upsert_user("other")
        yield
    finally:
        await repo.close_pool()


def event(item_id="event", user_id="alex", **changes):
    return dict({
        "item_id": item_id, "user_id": user_id, "source": "gcal", "source_id": item_id,
        "kind": "event", "title": "Study block", "starts_at": "2026-10-19T10:00:00-07:00",
        "ends_at": "2026-10-19T11:00:00-07:00", "location": "BYAC 110",
    }, **changes)


def actions(action_id="a1"):
    return [{"action_id": action_id, "type": "create_event", "params": {"title": "[Margin] Study"}}]


async def test_pool_lifecycle_and_separate_agent_loops():
    first = await repo.get_pool()
    assert await repo.get_pool() is first

    async def worker():
        pool = await repo.get_pool()
        assert (await repo.get_user("alex"))["user_id"] == "alex"
        await repo.close_pool()
        return pool

    other = await asyncio.to_thread(lambda: asyncio.run(worker()))
    assert other is not first and other.closed
    await repo.close_pool()
    assert first.closed
    assert await repo.get_pool() is not first


async def test_user_updates_are_explicit_and_preserve_preferences():
    await repo.upsert_user("alex", tz="America/New_York", plan="pro")
    await repo.upsert_user("alex")
    assert (await repo.get_user("alex"))["tz"] == "America/New_York"
    assert await repo.get_user("missing") is None
    with pytest.raises(ValueError):
        await repo.upsert_user("alex", phone_verified=True)


async def test_phone_hash_expiry_relink_and_verified_uniqueness():
    future = datetime.now(timezone.utc) + timedelta(minutes=10)
    await repo.set_phone("alex", "+14805551234", "hash-one", future)
    assert await repo.get_user_by_phone("+14805551234") is None
    assert not await repo.verify_phone("alex", "wrong-hash")
    await repo.set_phone("alex", "+14805551234", "hash-two", future)
    assert not await repo.verify_phone("alex", "hash-one")
    assert await repo.verify_phone("alex", "hash-two")
    user = await repo.get_user_by_phone("+14805551234")
    assert user["user_id"] == "alex" and user["phone_code_hash"] is None
    assert user["phone_code_expires_at"] is None
    await repo.set_phone("other", "+14805551234", "other-hash", future)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.verify_phone("other", "other-hash")
    await repo.set_phone("other", "+14805551235", "expired", future - timedelta(days=1))
    assert not await repo.verify_phone("other", "expired")
    assert (await repo.get_user("other"))["phone_code_hash"] is None


async def test_connections_encrypted_and_scoped(monkeypatch):
    secret = {"token": "fictional-demo-token", "ical_url": "https://example.test/private-feed"}
    await repo.save_connection("alex", "canvas", secret=secret)
    async with (await repo.get_pool()).connection() as conn:
        raw = await (await conn.execute("SELECT secret_enc FROM public.connections")).fetchone()
    assert b"fictional-demo-token" not in bytes(raw["secret_enc"])
    saved = await repo.get_connection("alex", "canvas")
    assert saved["secret"] == secret and "secret_enc" not in saved
    assert await repo.get_connection("other", "canvas") is None
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(InvalidToken):
        await repo.get_connection("alex", "canvas")


async def test_connection_status_guard_preserves_account_selection_and_encrypted_settings():
    settings = {"settings": {"calendar_ids": ["study@example.edu"], "write_calendar_id": "study@example.edu"}}
    await repo.save_connection("alex", "gcal", composio_account_id="ca_old", secret=settings)
    await repo.save_connection("other", "gcal", composio_account_id="ca_other")
    await repo.save_connection("alex", "gmail", composio_account_id="ca_gmail")
    await repo.set_connection_status("alex", "gcal", "ca_old", "relink_required")
    selected = await repo.get_connection("alex", "gcal")
    assert selected["status"] == "relink_required" and selected["secret"] == settings
    assert (await repo.get_connection("other", "gcal"))["status"] == "active"
    assert (await repo.get_connection("alex", "gmail"))["status"] == "active"
    # A delayed failure for the old account cannot revoke its replacement.
    await repo.save_connection("alex", "gcal", composio_account_id="ca_new", secret=settings)
    await repo.set_connection_status("alex", "gcal", "ca_old", "relink_required")
    selected = await repo.get_connection("alex", "gcal")
    assert selected["composio_account_id"] == "ca_new" and selected["status"] == "active"
    assert selected["secret"] == settings
    with pytest.raises(ValueError):
        await repo.set_connection_status("alex", "gcal", "ca_new", "arbitrary-status")


async def test_source_item_lookup_requires_authenticated_owner_and_preserves_namespace():
    from integrations.composio.models import source_key

    key = source_key("ca_calendar", "study@example.edu", "event-native")
    await repo.upsert_items([event("owned", source_id=key), event("other-owned", "other")])
    saved = await repo.get_item("alex", "owned")
    assert saved["source_id"] == key and saved["user_id"] == "alex"
    assert saved["starts_at"] == "2026-10-19T17:00:00Z"
    assert await repo.get_item("other", "owned") is None
    assert await repo.get_item("alex", "other-owned") is None
    assert await repo.get_item("alex", "missing") is None


@pytest_asyncio.fixture
async def composio_execution(monkeypatch):
    """Real local approval/claim/receipt storage; only provider boundaries are fake."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from integrations.composio import actions as dispatcher
    from integrations.composio.models import CalendarOption, ConnectionState, ReadBatch, ToolResult
    from shared.protocol import ActionRequest

    params = {"account_id": "ca_calendar", "calendar_id": "study@example.edu", "title": "Study physics",
              "starts_at": "2026-10-19T10:00:00-07:00", "ends_at": "2026-10-19T11:30:00-07:00"}
    settings = {"calendar_ids": ["study@example.edu"], "write_calendar_id": "study@example.edu"}
    await repo.save_connection("alex", "gcal", composio_account_id="ca_calendar", secret={"settings": settings})
    await repo.insert_plan("composio-plan", "alex", [{"action_id": "write", "type": "create_event", "params": params}])
    await repo.approve_plan("composio-plan", "alex")

    async def require(user_id, provider):
        saved = await repo.get_connection(user_id, provider)
        return ConnectionState(provider, "active", saved["composio_account_id"], saved["secret"]["settings"])

    monkeypatch.setattr(dispatcher.connections, "require_connection", AsyncMock(side_effect=require))
    monkeypatch.setattr(dispatcher.calendar, "list_calendars", AsyncMock(return_value=ReadBatch(records=[
        CalendarOption("study@example.edu", "Study", "ca_calendar", writable=True),
    ])))
    monkeypatch.setattr(dispatcher.calendar, "fetch_calendar_events", AsyncMock(return_value=ReadBatch()))

    async def create(*args, **kwargs):
        await asyncio.sleep(.02)
        return ToolResult({"response_data": {"id": "created-event",
                          "htmlLink": "https://calendar.google.com/calendar/event?eid=fixture"}}, "log_db_fixture")

    execute = AsyncMock(side_effect=create)
    monkeypatch.setattr(dispatcher.runtime, "execute", execute)
    request = ActionRequest(user_id="alex", plan_id="composio-plan", action_id="write", type="create_event",
                            params={"account_id": "unreviewed", "title": "Must not execute"})
    return SimpleNamespace(dispatcher=dispatcher, execute=execute, request=request)


async def test_composio_concurrent_dispatch_uses_one_durable_claim_and_done_replays(composio_execution):
    setup = composio_execution
    results = await asyncio.gather(*(setup.dispatcher.execute_approved_action(setup.request) for _ in range(8)))
    assert any(result.ok for result in results) and setup.execute.await_count == 1
    assert all(result.data["execution_status"] in {"running", "succeeded"} for result in results)
    executions = await repo.list_action_executions("composio-plan", "alex")
    assert len(executions) == 1
    saved = executions[0]
    assert saved["status"] == "succeeded" and saved["completed_at"] is not None
    assert saved["result"]["event_id"] == "created-event" and saved["result"]["log_id"] == "log_db_fixture"
    assert "Must not execute" not in str(saved["result"])
    assert setup.execute.call_args.args[3]["summary"] == "[Margin] Study physics"
    assert await repo.update_plan_status("composio-plan", "alex", "done")
    setup.dispatcher.connections.require_connection.side_effect = AssertionError("Replay must not contact provider")
    replay = await setup.dispatcher.execute_approved_action(setup.request)
    assert replay.ok and replay.data == saved["result"]
    assert setup.execute.await_count == 1


async def test_composio_unknown_receipt_survives_replay_and_blocks_plan_completion(composio_execution):
    from integrations.composio.models import IntegrationError

    setup = composio_execution
    setup.execute.side_effect = IntegrationError("OUTCOME_UNKNOWN", "gcal", may_have_written=True, log_id="log_timeout")
    result = await setup.dispatcher.execute_approved_action(setup.request)
    assert not result.ok and result.error == "OUTCOME_UNKNOWN"
    saved = await repo.get_action_execution("composio-plan", "write", "alex")
    assert saved["status"] == "unknown" and saved["completed_at"] is None
    assert saved["result"]["log_id"] == "log_timeout"
    assert not await repo.update_plan_status("composio-plan", "alex", "done")
    replay = await setup.dispatcher.execute_approved_action(setup.request)
    assert replay.error == "OUTCOME_UNKNOWN" and replay.data == saved["result"]
    setup.execute.assert_awaited_once()


async def test_composio_receipt_failure_stores_unknown_with_created_id(composio_execution, monkeypatch):
    setup = composio_execution
    finish = repo.finish_action_execution

    async def interrupted_success(*args, **kwargs):
        if kwargs["status"] == "succeeded":
            raise psycopg.OperationalError("Simulated receipt transaction failure")
        await finish(*args, **kwargs)

    monkeypatch.setattr(repo, "finish_action_execution", interrupted_success)
    result = await setup.dispatcher.execute_approved_action(setup.request)
    assert not result.ok and result.error == "RECEIPT_UNAVAILABLE"
    saved = await repo.get_action_execution("composio-plan", "write", "alex")
    assert saved["status"] == "unknown" and saved["result"]["event_id"] == "created-event"
    assert saved["result"]["remote_outcome"] == "succeeded"
    assert not await repo.update_plan_status("composio-plan", "alex", "done")
    replay = await setup.dispatcher.execute_approved_action(setup.request)
    assert replay.error == "RECEIPT_UNAVAILABLE"
    setup.execute.assert_awaited_once()


async def test_composio_crashed_running_claim_and_other_owner_cannot_dispatch(composio_execution):
    from shared.protocol import ActionRequest

    setup = composio_execution
    forged = ActionRequest(**dict(setup.request.model_dump(), user_id="other"))
    assert (await setup.dispatcher.execute_approved_action(forged)).error == "INVALID_ACTION"
    assert await repo.claim_action_execution("composio-plan", "write", "alex")
    result = await setup.dispatcher.execute_approved_action(setup.request)
    assert result.error == "EXECUTION_RUNNING" and result.data["execution_status"] == "running"
    assert not await repo.update_plan_status("composio-plan", "alex", "done")
    setup.execute.assert_not_awaited()


async def test_canvas_connect_encrypts_feed_and_gather_uses_actual_cache(monkeypatch):
    import re
    import httpx
    from fastapi import FastAPI
    from integrations import canvas_client
    from web.canvas_connect import router
    from web.connect_state import make_state
    from tests.test_canvas_client import ASSIGNMENT, FEED, ics, parse, request

    monkeypatch.setenv("STATE_SIGNING_KEY", "local-test-signing-key-" * 3)
    async def feed(url, user_id, **kwargs):
        assert url == FEED
        return parse(ics(ASSIGNMENT), user_id)
    monkeypatch.setattr(canvas_client, "fetch_feed", feed)
    app = FastAPI()
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://margin.test") as client:
        token = make_state("new-student")
        page = await client.get("/connect/canvas", params={"token": token})
        csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
        result = await client.post("/connect/canvas", data={"token": token, "csrf": csrf, "ical_url": FEED})
    assert result.status_code == 200 and "Canvas is connected" in result.text
    saved = await repo.get_connection("new-student", "canvas")
    assert saved["secret"] == {"ical_url": FEED}
    assert await repo.get_connection("other", "canvas") is None
    async with (await repo.get_pool()).connection() as conn:
        raw = await (await conn.execute(
            "SELECT secret_enc FROM public.connections WHERE user_id = %s", ("new-student",)
        )).fetchone()
    assert FEED.encode() not in bytes(raw["secret_enc"])
    assert Fernet(os.environ["TOKEN_ENCRYPTION_KEY"].encode()).decrypt(bytes(raw["secret_enc"]))
    gathered = await canvas_client.gather(request(user_id="new-student"))
    assert gathered.error is None and len(gathered.items) == 1
    item = gathered.items[0].model_dump()
    item["status"] = "done"
    await repo.upsert_items([item])
    assert (await canvas_client.gather(request(user_id="new-student"))).items[0].status == "done"
    async def unavailable(*args, **kwargs):
        raise canvas_client.CanvasFeedError("offline")
    monkeypatch.setattr(canvas_client, "fetch_feed", unavailable)
    cached = await canvas_client.gather(request(user_id="new-student"))
    assert cached.stale_since and cached.error and cached.items[0].status == "done"


async def test_connection_snapshot_can_clear_secret_without_encryption_key(monkeypatch):
    await repo.save_connection("alex", "canvas", secret={"token": "example"})
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY")
    await repo.save_connection("alex", "canvas", status="disconnected")
    assert (await repo.get_connection("alex", "canvas"))["secret"] is None
    await repo.save_connection("alex", "gmail", composio_account_id="ca_demo")
    assert (await repo.get_connection("alex", "gmail"))["composio_account_id"] == "ca_demo"


async def test_item_refresh_keeps_ids_state_and_utc_event_fields():
    await repo.upsert_items([event(status="done", cost_usd=15)])
    await repo.upsert_items([event("new-id", source_id="event", title="Updated", status="open", cost_usd=20)])
    await repo.upsert_items([event("other-event", "other", source_id="event")])
    rows = await repo.last_items_by_source("alex", "gcal")
    assert len(rows) == 1
    row = rows[0]
    assert row["item_id"] == "event" and row["title"] == "Updated" and row["status"] == "done"
    assert row["starts_at"] == "2026-10-19T17:00:00Z" and row["ends_at"] == "2026-10-19T18:00:00Z"
    assert row["location"] == "BYAC 110" and row["cost_usd"] == 20
    assert Item.model_validate(row).starts_at == row["starts_at"]


async def test_item_batch_rolls_back_on_database_error():
    await repo.upsert_items([event("taken", "other")])
    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.upsert_items([event("first"), event("taken")])
    assert await repo.last_items_by_source("alex", "gcal") == []


async def test_invalid_event_range_does_not_partially_save():
    with pytest.raises(ValueError):
        await repo.upsert_items([event("first"), event("bad", ends_at="2026-10-19T09:00:00-07:00")])
    assert await repo.last_items_by_source("alex", "gcal") == []


async def test_window_overlap_boundaries_and_user_scope():
    await repo.upsert_items([
        event("inside"), event("other", "other"),
        event("spanning", starts_at="2026-10-18T00:00:00Z", ends_at="2026-10-21T00:00:00Z"),
        event("zero", starts_at="2026-10-19T17:00:00Z", ends_at="2026-10-19T17:00:00Z"),
        event("ends-at-start", starts_at="2026-10-19T16:00:00Z", ends_at="2026-10-19T17:00:00Z"),
        event("starts-at-end", starts_at="2026-10-19T18:00:00Z", ends_at=None),
        event("deadline", starts_at=None, ends_at=None, due_at="2026-10-19T17:00:00Z"),
        event("end-deadline", starts_at=None, ends_at=None, due_at="2026-10-19T18:00:00Z"),
        event("undated", starts_at=None, ends_at=None),
    ])
    rows = await repo.items_in_window("alex", "2026-10-19T17:00:00Z", "2026-10-19T18:00:00Z")
    assert {r["item_id"] for r in rows} == {"inside", "spanning", "zero", "deadline"}


async def test_cached_results_expose_staleness_without_inventing_free_slots():
    await repo.upsert_items([event(), event("canvas", source="canvas_ical")])
    result = await repo.cached_gather_result(
        "request", "margin-planner", "alex", ["gcal"], "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z",
        error="Calendar unavailable",
    )
    assert len(result.items) == 1 and result.items[0].source == "gcal"
    assert result.stale_since and result.error == "Calendar unavailable" and result.free_slots == []
    empty = await repo.cached_gather_result(
        "request", "margin-planner", "other", ["gcal"], "2026-10-19T00:00:00Z", "2026-10-20T00:00:00Z",
        error="Calendar unavailable",
    )
    assert empty.items == [] and empty.stale_since is None and empty.error


async def test_concurrent_approvals_and_claims_have_exactly_one_winner():
    await repo.insert_plan("p", "alex", actions())
    assert not await repo.claim_action_execution("p", "a1", "alex")
    assert sum(await asyncio.gather(*(repo.approve_plan("p", "alex") for _ in range(12)))) == 1
    assert len(await repo.list_action_executions("p", "alex")) == 1
    assert sum(await asyncio.gather(*(repo.claim_action_execution("p", "a1", "alex") for _ in range(12)))) == 1
    assert (await repo.get_action_execution("p", "a1", "alex"))["started_at"] is not None


async def test_approval_rolls_back_if_execution_creation_fails():
    await repo.insert_plan("p", "alex", actions())
    async with (await repo.get_pool()).connection() as conn:
        await conn.execute("INSERT INTO public.action_executions (plan_id, action_id, user_id) VALUES ('p', 'a1', 'alex')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        await repo.approve_plan("p", "alex")
    plan = await repo.get_plan("p", "alex")
    assert plan["status"] == "pending" and plan["approved_at"] is None


async def test_plan_replay_is_immutable_and_scoped():
    await repo.insert_plan("p", "alex", actions())
    assert (await repo.pending_plan_for("alex"))["plan_id"] == "p"
    assert await repo.get_plan("p", "other") is None
    assert not await repo.approve_plan("p", "other")
    assert not await repo.update_plan_status("p", "other", "cancelled")
    await repo.approve_plan("p", "alex")
    await repo.insert_plan("p", "alex", actions())
    assert (await repo.get_plan("p", "alex"))["status"] == "approved"
    with pytest.raises(ValueError):
        await repo.insert_plan("p", "alex", actions("changed"))
    with pytest.raises(ValueError):
        await repo.insert_plan("p", "other", actions())
    assert await repo.pending_plan_for("alex") is None
    assert await repo.list_action_executions("p", "other") == []
    assert not await repo.claim_action_execution("p", "a1", "other")
    with pytest.raises(LookupError):
        await repo.finish_action_execution("p", "a1", "other", status="succeeded")


async def test_claim_checks_action_membership_and_plan_status():
    await repo.insert_plan("p", "alex", actions())
    await repo.approve_plan("p", "alex")
    async with (await repo.get_pool()).connection() as conn:
        await conn.execute("INSERT INTO public.action_executions (plan_id, action_id, user_id) VALUES ('p', 'fake', 'alex')")
    assert not await repo.claim_action_execution("p", "fake", "alex")
    await repo.insert_plan("cancel", "alex", actions())
    assert await repo.update_plan_status("cancel", "alex", "cancelled")
    assert not await repo.approve_plan("cancel", "alex")


async def test_execution_unknown_requires_reconciliation_and_receipts_are_immutable():
    await repo.insert_plan("p", "alex", actions())
    await repo.approve_plan("p", "alex")
    with pytest.raises(ValueError):
        await repo.finish_action_execution("p", "a1", "alex", status="succeeded")
    assert not await repo.update_plan_status("p", "alex", "done")
    await repo.claim_action_execution("p", "a1", "alex")
    await repo.finish_action_execution("p", "a1", "alex", status="unknown", error="timeout")
    assert not await repo.update_plan_status("p", "alex", "done")
    assert not await repo.claim_action_execution("p", "a1", "alex")
    receipt = {"link": "https://example.test/calendar/event"}
    await repo.finish_action_execution("p", "a1", "alex", status="succeeded", result=receipt)
    completed = (await repo.get_action_execution("p", "a1", "alex"))["completed_at"]
    await repo.finish_action_execution("p", "a1", "alex", status="succeeded", result=receipt)
    assert (await repo.get_action_execution("p", "a1", "alex"))["completed_at"] == completed
    with pytest.raises(ValueError):
        await repo.finish_action_execution("p", "a1", "alex", status="failed")
    assert await repo.update_plan_status("p", "alex", "done")
    assert not await repo.update_plan_status("p", "alex", "done")
    assert "results" not in await repo.get_plan("p", "alex")


async def test_action_ids_can_repeat_in_different_plans():
    for plan in ("p1", "p2"):
        await repo.insert_plan(plan, "alex", actions())
        await repo.approve_plan(plan, "alex")
        await repo.claim_action_execution(plan, "a1", "alex")
    await repo.finish_action_execution("p1", "a1", "alex", status="failed", error="rejected")
    assert await repo.update_plan_status("p1", "alex", "done")
    assert (await repo.get_action_execution("p2", "a1", "alex"))["status"] == "running"


async def test_brief_ownership_and_today_in_user_timezone():
    await repo.upsert_items([event(), event("other-event", "other")])
    with pytest.raises(ValueError):
        await repo.insert_brief("bad", "alex", {"A": "other-event"})
    await repo.insert_brief("b1", "alex", {"A": "event"})
    await repo.insert_brief("b1", "alex", {"A": "event"})
    assert (await repo.get_today_brief("alex"))["letters"] == {"A": "event"}
    async with (await repo.get_pool()).connection() as conn:
        await conn.execute("UPDATE public.briefs SET sent_at = now() - interval '2 days'")
    assert await repo.get_today_brief("alex") is None
    assert await repo.get_today_brief("other") is None


async def test_concurrent_nudge_deduplication_and_sent_state():
    ids = await asyncio.gather(*(
        repo.insert_nudge(f"n{i}", "alex", None, ["yes", "no"], dedupe_key="overspend:week")
        for i in range(8)
    ))
    assert len(set(ids)) == 1
    nudge_id = ids[0]
    now = datetime.now(timezone.utc)
    assert [r["nudge_id"] for r in await repo.due_nudges(now)] == [nudge_id]
    assert await repo.get_pending_nudge("alex") is None
    await repo.mark_nudge_sent(nudge_id, "alex", now)
    assert (await repo.get_pending_nudge("alex"))["nudge_id"] == nudge_id
    assert await repo.insert_nudge("new-id", "alex", None, [], dedupe_key="overspend:week") == nudge_id
    assert await repo.due_nudges(now) == []
    await repo.update_nudge(nudge_id, "alex", "yes", "recorded")
    await repo.update_nudge(nudge_id, "alex", "yes", "recorded")
    with pytest.raises(ValueError):
        await repo.update_nudge(nudge_id, "alex", "no", "changed")
    assert await repo.get_pending_nudge("alex") is None
    assert await repo.insert_nudge("other", "other", None, [], dedupe_key="overspend:week") == "other"


async def test_nudge_due_time_nullable_references_and_user_scope():
    now = datetime.now(timezone.utc)
    await repo.upsert_items([event()])
    await repo.insert_plan("p", "alex", actions())
    await repo.insert_nudge("future", "alex", "event", ["yes"], dedupe_key="future", plan_id="p", send_after=now + timedelta(days=1))
    await repo.insert_nudge("plain", "alex", None, [], dedupe_key="plain")
    assert [r["nudge_id"] for r in await repo.due_nudges(now)] == ["plain"]
    with pytest.raises(LookupError):
        await repo.mark_nudge_sent("future", "alex", now)
    with pytest.raises(LookupError):
        await repo.mark_nudge_sent("plain", "other", now)
    await repo.mark_nudge_sent("plain", "alex", now)
    await repo.mark_nudge_sent("plain", "alex", now + timedelta(seconds=1))
    assert await repo.get_pending_nudge("alex") is None
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await repo.insert_nudge("cross-user", "other", "event", [], dedupe_key="bad", plan_id="p")


async def test_budget_reads_posted_flexible_spend_and_bill_clamping():
    assert await repo.safe_to_spend("alex") == 0
    await repo.save_budget("alex", 1000, 100, None)
    await repo.save_bill("alex", "Monthly bill", 200, 31)
    await repo.save_bill("alex", "Monthly bill", 100, 31)
    assert len(await repo.get_bills("alex")) == 1
    async with (await repo.get_pool()).connection() as conn:
        for txn_id, amount, category, pending, removed in [
            ("food", 10, "FOOD_AND_DRINK", False, False),
            ("unknown-category", 5, None, False, False),
            ("refund", -10, None, False, False),
            ("pending", 100, None, True, False),
            ("removed", 100, None, False, True),
            ("rent", 100, "RENT_AND_UTILITIES", False, False),
            ("transfer", 100, "TRANSFER_OUT", False, False),
        ]:
            await conn.execute(
                """INSERT INTO public.transactions
                    (user_id, ts, txn_id, amount, currency, category, pending, is_removed)
                    VALUES ('alex', now(), %s, %s, 'USD', %s, %s, %s)""",
                (txn_id, amount, category, pending, removed),
            )
        row = await (await conn.execute(
            "SELECT public.flexible_spent_this_month('alex') AS spent, (now() AT TIME ZONE 'America/Phoenix')::date AS today"
        )).fetchone()
    assert row["spent"] == 15
    expected = repo._safe_to_spend(await repo.get_budget("alex"), await repo.get_bills("alex"), row["spent"], row["today"])
    assert await repo.safe_to_spend("alex") == expected
    await repo.save_budget("alex", 1000, 100, 0)
    assert await repo.safe_to_spend("alex") == 0
    assert await repo.get_budget("other") is None


def plaid_transaction(txn_id="txn-food", **changes):
    return dict({
        "txn_id": txn_id, "ts": datetime.now(timezone.utc), "amount": Decimal("12.50"),
        "currency": "USD", "merchant": "Demo Cafe", "category": "FOOD_AND_DRINK", "pending": False,
    }, **changes)


async def plaid_secret(user_id="alex", **changes):
    result = await plaid_store.initialize_connection(
        user_id, **dict({"item_id": f"item-{user_id}", "access_token": f"sandbox-token-{user_id}"}, **changes),
    )
    return result["secret"]


async def apply_plaid(secret, transactions=(), removed=(), *, user_id="alex", cursor="cursor-1", **changes):
    return await plaid_store.apply_sync(
        user_id, expected_secret=secret, transactions=list(transactions), removed_ids=list(removed),
        next_cursor=cursor, synced_at=datetime.now(timezone.utc), **changes,
    )


async def plaid_rows(user_id="alex"):
    async with (await repo.get_pool()).connection() as conn:
        return await (await conn.execute(
            "SELECT * FROM public.transactions WHERE user_id = %s ORDER BY txn_id", (user_id,),
        )).fetchall()


async def test_plaid_initialization_encrypts_and_preserves_existing_connection():
    secret = await plaid_secret()
    assert secret == {
        "item_id": "item-alex", "access_token": "sandbox-token-alex", "environment": "sandbox",
        "cursor": None, "last_synced_at": None,
    }
    assert await apply_plaid(secret, transactions_update_status="HISTORICAL_UPDATE_COMPLETE")
    saved = await repo.get_connection("alex", "plaid")
    existing = await plaid_store.initialize_connection("alex", item_id="unwanted-item", access_token="unwanted-token")
    assert existing == saved
    async with (await repo.get_pool()).connection() as conn:
        raw = await (await conn.execute(
            "SELECT secret_enc FROM public.connections WHERE user_id = 'alex' AND provider = 'plaid'"
        )).fetchone()
    assert b"sandbox-token-alex" not in bytes(raw["secret_enc"])
    assert b"cursor-1" not in bytes(raw["secret_enc"])
    assert existing["secret"]["transactions_update_status"] == "HISTORICAL_UPDATE_COMPLETE"
    with pytest.raises(LookupError):
        await plaid_secret("missing-user")
    assert await repo.get_user("missing-user") is None
    with pytest.raises(ValueError, match="sandbox"):
        await plaid_secret(environment="production")


async def test_plaid_concurrent_initialization_keeps_one_item():
    results = await asyncio.gather(*(
        plaid_store.initialize_connection("alex", item_id=f"item-{i}", access_token=f"sandbox-token-{i}")
        for i in range(8)
    ))
    assert len({row["secret"]["item_id"] for row in results}) == 1
    assert len({row["secret"]["access_token"] for row in results}) == 1


async def test_plaid_idempotency_modifications_changed_timestamp_and_reintroduced_records():
    secret = await plaid_secret()
    original = plaid_transaction(ts=datetime(2026, 10, 18, 19, tzinfo=timezone.utc))
    assert await apply_plaid(secret, [original])
    assert not await apply_plaid(secret, [original])  # A stale page cannot move the cursor.
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, [original], cursor="cursor-2")
    assert len(await plaid_rows()) == 1
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    corrected = dict(original, ts=original["ts"] + timedelta(days=1), amount=Decimal("15.25"), merchant="New name")
    assert await apply_plaid(current, [corrected], cursor="cursor-3")
    rows = await plaid_rows()
    assert len(rows) == 1 and rows[0]["ts"] == corrected["ts"] and rows[0]["amount"] == Decimal("15.25")
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, removed=["txn-food", "nonexistent"], cursor="cursor-4")
    assert (await plaid_rows())[0]["is_removed"]
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, [corrected], cursor="cursor-5")
    assert len(await plaid_rows()) == 1 and not (await plaid_rows())[0]["is_removed"]


async def test_plaid_pending_to_posted_and_removal_only_sync():
    secret = await plaid_secret()
    assert await apply_plaid(secret, [plaid_transaction("pending", pending=True)])
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, [plaid_transaction("posted")], ["pending"], cursor="cursor-2")
    rows = {row["txn_id"]: row for row in await plaid_rows()}
    assert rows["pending"]["is_removed"] and rows["pending"]["pending"]
    assert not rows["posted"]["pending"] and not rows["posted"]["is_removed"]
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, removed=["posted"], cursor="cursor-3")
    assert all(row["is_removed"] for row in await plaid_rows())


@pytest.mark.parametrize("change", [
    {"currency": "EUR"}, {"amount": Decimal("NaN")}, {"amount": float("inf")},
    {"pending": "false"}, {"ts": datetime(2026, 10, 18)}, {"txn_id": ""},
])
async def test_plaid_invalid_batch_leaves_cursor_and_transactions_unchanged(change):
    secret = await plaid_secret()
    with pytest.raises(ValueError):
        await apply_plaid(secret, [plaid_transaction("valid"), dict(plaid_transaction("invalid"), **change)])
    assert await plaid_rows() == []
    assert (await repo.get_connection("alex", "plaid"))["secret"] == secret


async def test_plaid_sql_failure_rolls_back_rows_removals_and_cursor():
    secret = await plaid_secret()
    assert await apply_plaid(secret, [plaid_transaction("original")])
    before = await plaid_rows()
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        await apply_plaid(current, [
            plaid_transaction("original", amount=Decimal("99.00")),
            plaid_transaction("overflow", amount=Decimal("10000000000.00")),
        ], removed=["original"], cursor="should-not-save")
    assert await plaid_rows() == before
    assert (await repo.get_connection("alex", "plaid"))["secret"] == current


async def test_plaid_competing_syncs_have_one_winner_and_cannot_revoke_newer_snapshot():
    secret = await plaid_secret()
    results = await asyncio.gather(*(
        apply_plaid(secret, [plaid_transaction(f"txn-{i}")], cursor=f"cursor-{i}")
        for i in range(8)
    ))
    assert sum(results) == 1 and len(await plaid_rows()) == 1
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert not await plaid_store.mark_relink_required("alex", expected_secret=secret)
    assert (await repo.get_connection("alex", "plaid"))["status"] == "active"
    assert await plaid_store.mark_relink_required("alex", expected_secret=current)
    assert not await apply_plaid(current, [plaid_transaction("late")], cursor="late")
    saved = await repo.get_connection("alex", "plaid")
    assert saved["status"] == "relink_required" and saved["secret"] == current


async def test_plaid_metadata_or_replaced_item_prevents_delayed_sync():
    secret = await plaid_secret()
    changed = dict(secret, transactions_update_status="INITIAL_UPDATE_COMPLETE")
    await repo.save_connection("alex", "plaid", secret=changed)
    assert not await apply_plaid(secret, [plaid_transaction()])
    replaced = dict(secret, item_id="replacement-item", access_token="replacement-token")
    await repo.save_connection("alex", "plaid", secret=replaced)
    assert not await apply_plaid(secret, [plaid_transaction()])
    assert not await plaid_store.mark_relink_required("alex", expected_secret=secret)
    assert await plaid_rows() == []
    assert (await repo.get_connection("alex", "plaid"))["secret"] == replaced


async def test_plaid_not_ready_empty_cursor_and_unchanged_cursor_still_guard_syncs():
    secret = await plaid_secret()
    assert await apply_plaid(secret, cursor="", transactions_update_status="NOT_READY")
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert current["cursor"] == "" and current["transactions_update_status"] == "NOT_READY"
    results = await asyncio.gather(*(
        apply_plaid(current, cursor="", transactions_update_status="NOT_READY") for _ in range(4)
    ))
    assert sum(results) == 1
    assert await plaid_rows() == []


async def test_plaid_sync_isolates_same_transaction_id_between_users():
    alex = await plaid_secret()
    other = await plaid_secret("other")
    assert await apply_plaid(alex, [plaid_transaction("shared-id", amount=10)])
    assert await apply_plaid(other, [plaid_transaction("shared-id", amount=20)], user_id="other")
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert not await apply_plaid(current, [plaid_transaction("shared-id", amount=99)], user_id="other")
    assert await apply_plaid(current, removed=["shared-id"], cursor="cursor-2")
    assert (await plaid_rows())[0]["is_removed"]
    other_row = (await plaid_rows("other"))[0]
    assert other_row["amount"] == 20 and not other_row["is_removed"]


async def test_plaid_imported_transactions_drive_existing_safe_to_spend():
    secret = await plaid_secret()
    await repo.save_budget("alex", 2000, 100, None)
    await repo.save_bill("alex", "Monthly bill", 200, 31)
    original_safe = await repo.safe_to_spend("alex")
    rows = [
        plaid_transaction("food", amount=25),
        plaid_transaction("uncategorized", amount=5, category=None),
        plaid_transaction("pending", amount=100, pending=True),
        plaid_transaction("removed", amount=100),
        plaid_transaction("refund", amount=-5),
        plaid_transaction("transfer", amount=100, category="TRANSFER_OUT"),
        plaid_transaction("rent", amount=100, category="RENT_AND_UTILITIES"),
    ]
    assert await apply_plaid(secret, rows, ["removed"], transactions_update_status="HISTORICAL_UPDATE_COMPLETE")
    async with (await repo.get_pool()).connection() as conn:
        summary = await (await conn.execute(
            "SELECT public.flexible_spent_this_month('alex') AS spent, (now() AT TIME ZONE 'America/Phoenix')::date AS today"
        )).fetchone()
    assert summary["spent"] == Decimal("30.00")
    expected = repo._safe_to_spend(await repo.get_budget("alex"), await repo.get_bills("alex"), 30, summary["today"])
    assert await repo.safe_to_spend("alex") == expected < original_safe
    current = (await repo.get_connection("alex", "plaid"))["secret"]
    assert await apply_plaid(current, rows, ["removed"], cursor="cursor-2")
    assert await repo.safe_to_spend("alex") == expected


async def test_plaid_adapter_pages_persist_once_then_replay_modify_remove_and_update_budget(monkeypatch):
    """Only Plaid HTTP is replaced: normalization, cursor storage and budgets are real."""
    from datetime import time
    from integrations import plaid_sandbox as adapter

    monkeypatch.setenv("PLAID_ENV", "sandbox")
    await plaid_secret()
    await repo.save_budget("alex", 2000, 100, None)
    today = datetime.now(adapter.PHOENIX).date()
    changed_time = datetime.combine(today, time(13), adapter.PHOENIX)

    def raw(txn_id, amount, **changes):
        return dict({
            "transaction_id": txn_id, "amount": amount, "iso_currency_code": "USD",
            "date": today.isoformat(), "pending": False, "merchant_name": "Demo Cafe",
            "personal_finance_category": {"primary": "FOOD_AND_DRINK"},
        }, **changes)

    def page(cursor, *, added=(), modified=(), removed=(), more=False):
        return dict(
            added=list(added), modified=list(modified), removed=list(removed), next_cursor=cursor,
            has_more=more, transactions_update_status="HISTORICAL_UPDATE_COMPLETE", request_id=f"request-{cursor}",
        )

    responses = [
        page("c1", added=[raw("lunch", 25)], more=True),
        page("c2", added=[raw("coffee", 5)]),
        page("c2"),
        page("c3", modified=[raw("lunch", 40, datetime=changed_time.isoformat())],
             removed=[{"transaction_id": "coffee"}]),
    ]
    cursors = []

    async def provider(operation, **kwargs):
        assert operation == "sync" and kwargs["access_token"] == "sandbox-token-alex"
        index = len(cursors)
        cursors.append(kwargs["cursor"])
        # The first page's changes/cursor stay invisible until the second page completes.
        if index < 2:
            assert await plaid_rows() == []
            assert (await repo.get_connection("alex", "plaid"))["secret"]["cursor"] is None
        else:
            assert len(await plaid_rows()) == 2
            assert (await repo.get_connection("alex", "plaid"))["secret"]["cursor"] == "c2"
        return responses[index]

    monkeypatch.setattr(adapter, "_provider_call", provider)
    first = await adapter.sync_transactions("alex")
    assert first.ready and first.pages == 2 and first.added == 2
    assert first.request_ids == ("request-c1", "request-c2")
    original = await plaid_rows()
    assert len(original) == 2 and sum(row["amount"] for row in original) == 30
    assert all(row["ts"] == datetime.combine(today, time(12), adapter.PHOENIX) for row in original)
    saved = (await repo.get_connection("alex", "plaid"))["secret"]
    assert saved["cursor"] == "c2" and saved["transactions_update_status"] == "HISTORICAL_UPDATE_COMPLETE"
    assert saved["last_synced_at"] == repo._iso(first.last_synced_at)
    expected = repo._safe_to_spend(await repo.get_budget("alex"), [], 30, today)
    assert await repo.safe_to_spend("alex") == expected

    second = await adapter.sync_transactions("alex")
    assert second.ready and (second.added, second.modified, second.removed) == (0, 0, 0)
    assert await plaid_rows() == original and await repo.safe_to_spend("alex") == expected

    third = await adapter.sync_transactions("alex")
    assert third.ready and third.modified == 1 and third.removed == 1
    final = {row["txn_id"]: row for row in await plaid_rows()}
    assert len(final) == 2 and final["lunch"]["amount"] == 40
    assert final["lunch"]["ts"] == changed_time and final["coffee"]["is_removed"]
    assert (await repo.get_connection("alex", "plaid"))["secret"]["cursor"] == "c3"
    revised = repo._safe_to_spend(await repo.get_budget("alex"), [], 40, today)
    assert await repo.safe_to_spend("alex") == revised < expected
    assert cursors == [None, "c1", "c2", "c2"]


def onboarding_form(**changes):
    return dict({"action": "connect", "phone": "+14805551234", "monthly_income": "1800",
                 "savings_goal": "100", "weekly_cap": "125", "bills": "Rent | 850 | 1; Utilities | 60 | 31"}, **changes)


async def test_connect_graph_creates_user_budget_bills_and_plaid_eligible_identity(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core import graph, onboarding
    from uagents_core.contrib.protocols.chat import TextContent

    monkeypatch.setattr(graph, "fast", AsyncMock(side_effect=AssertionError("No model for onboarding")))
    ctx = SimpleNamespace(send=AsyncMock())
    assert await repo.get_user("new-asi-sender") is None
    await graph.handle(ctx, "new-asi-sender", "", "asi1",
                       onboarding_form(user_id="other", plan="pro", phone_verified=True), "new-asi-sender")
    user = await repo.get_user("new-asi-sender")
    assert user["phone"] == "+14805551234" and not user["phone_verified"]
    assert user["plan"] == "free" and user["text_opted_in_at"] is None
    assert (await repo.get_budget("new-asi-sender"))["monthly_income"] == 1800
    assert len(await repo.get_bills("new-asi-sender")) == 2
    assert await repo.get_budget("other") is None
    text = " ".join(c.text for call in ctx.send.await_args_list for c in call.args[1].content if isinstance(c, TextContent))
    code = text.split("LINK ")[1].split()[0]
    assert user["phone_code_hash"] == onboarding.link_code_hash(code)
    assert code not in str(user) and user["phone_code_expires_at"] > datetime.now(timezone.utc)
    # This identity now satisfies Plaid's existing-user requirement without any provider call.
    saved = await plaid_store.initialize_connection("new-asi-sender", item_id="test-item", access_token="test-token")
    assert saved["user_id"] == "new-asi-sender"


async def test_connect_upserts_bills_and_preserves_verified_phone_entitlements_and_consent():
    from core import onboarding
    first = await onboarding.save("alex", onboarding_form())
    assert await repo.verify_phone_link("+14805551234", onboarding.link_code_hash(first.link_code)) == "alex"
    opted_in = datetime.now(timezone.utc)
    await repo.upsert_user("alex", plan="pro", tz="America/New_York", text_opted_in_at=opted_in)
    second = await onboarding.save("alex", onboarding_form(monthly_income="2200", bills="Rent | 900 | 2"))
    assert second.phone_verified and second.link_code is None
    user = await repo.get_user("alex")
    assert user["phone_verified"] and user["phone_code_hash"] is None
    assert user["plan"] == "pro" and user["tz"] == "America/New_York" and user["text_opted_in_at"] == opted_in
    bills = {b["name"]: b for b in await repo.get_bills("alex")}
    assert len(bills) == 2 and bills["Rent"]["amount"] == 900 and bills["Utilities"]["amount"] == 60
    assert (await repo.get_budget("alex"))["monthly_income"] == 2200


async def test_connect_rolls_back_user_budget_and_all_bills_on_late_failure():
    with pytest.raises(ValueError):
        await repo.save_onboarding("rollback-new", phone="+14805551234", monthly_income=Decimal(1000),
            savings_goal=Decimal(0), weekly_cap=None,
            bills=[{"name": "Rent", "amount": 300, "due_day": 1}, {"name": "Bad", "amount": 10, "due_day": 32}],
            code_hash="a" * 64, expires=datetime.now(timezone.utc) + timedelta(minutes=10))
    assert await repo.get_user("rollback-new") is None
    assert await repo.get_budget("rollback-new") is None and await repo.get_bills("rollback-new") == []


async def test_connect_new_phone_revokes_old_link_and_stale_challenge():
    from core import onboarding
    first = await onboarding.save("alex", onboarding_form())
    second = await onboarding.save("alex", onboarding_form(phone="+14805551235"))
    assert await repo.verify_phone_link("+14805551234", onboarding.link_code_hash(first.link_code)) is None
    assert await repo.verify_phone_link("+14805551234", onboarding.link_code_hash(second.link_code)) is None
    assert await repo.verify_phone_link("+14805551235", onboarding.link_code_hash(second.link_code)) == "alex"
    await repo.upsert_user("alex", text_opted_in_at=datetime.now(timezone.utc))
    await onboarding.save("alex", onboarding_form(phone="+14805551236"))
    assert await repo.get_user_by_phone("+14805551235") is None
    user = await repo.get_user("alex")
    assert not user["phone_verified"] and user["text_opted_in_at"] is None


async def test_phone_link_expires_and_is_consumed_once_under_concurrency():
    from core import onboarding
    first = await onboarding.save("alex", onboarding_form())
    digest = onboarding.link_code_hash(first.link_code)
    await repo.set_phone("alex", "+14805551234", digest, datetime.now(timezone.utc) - timedelta(seconds=1))
    assert await repo.verify_phone_link("+14805551234", digest) is None
    second = await onboarding.save("alex", onboarding_form())
    digest = onboarding.link_code_hash(second.link_code)
    assert await repo.verify_phone_link("+14805551234", "0" * 64) is None
    results = await asyncio.gather(*(repo.verify_phone_link("+14805551234", digest) for _ in range(4)))
    assert results.count("alex") == 1
    user = await repo.get_user("alex")
    assert user["phone_code_hash"] is None and user["phone_code_expires_at"] is None
    assert user["text_opted_in_at"] is None
    assert await repo.verify_phone_link("+14805551234", digest) is None


async def test_phone_link_cannot_steal_number_from_another_verified_user():
    from core import onboarding
    first = await onboarding.save("alex", onboarding_form())
    second = await onboarding.save("other", onboarding_form())
    assert await repo.verify_phone_link("+14805551234", onboarding.link_code_hash(first.link_code)) == "alex"
    assert await repo.verify_phone_link("+14805551234", onboarding.link_code_hash(second.link_code)) is None
    assert (await repo.get_user_by_phone("+14805551234"))["user_id"] == "alex"
    assert not (await repo.get_user("other"))["phone_verified"]


async def test_onboarded_phone_is_blocked_until_link_then_routes_to_same_asi_identity(monkeypatch):
    from unittest.mock import AsyncMock
    from core import graph, onboarding
    from channels import photon

    result = await onboarding.save("alex", onboarding_form())
    dispatch, send = AsyncMock(), AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(graph, "handle", dispatch)
    monkeypatch.setattr(photon, "send_message", send)
    await photon.handle_inbound("+14805551234", "hi", "msg-before", "space-1", "agent-1", verified_webhook=True)
    dispatch.assert_not_awaited()
    await photon.handle_inbound("+14805551234", f"LINK {result.link_code}", "msg-link", "space-1", "agent-1", verified_webhook=True)
    assert "phone is linked" in send.await_args.args[2]
    await photon.handle_inbound("+14805551234", "hi", "msg-after", "space-1", "agent-1", verified_webhook=True)
    assert dispatch.await_args.kwargs["user_id"] == "alex"
    assert dispatch.await_args.kwargs["channel"] == "photon"
