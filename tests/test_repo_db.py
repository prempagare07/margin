"""Integration tests on a disposable LOCAL database named margin_test.

Set TEST_DATABASE_URL explicitly. This suite never loads .env or falls back to
DATABASE_URL. It truncates its test tables between cases; see docs/HANDOFF.md.
Timescale aggregate creation is excluded; tables and the spending function come
directly from the production schema, including all constraints and indexes.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet, InvalidToken
from psycopg.conninfo import conninfo_to_dict

from db import repo
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
