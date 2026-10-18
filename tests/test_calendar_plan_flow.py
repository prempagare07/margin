"""Focused contracts for the real calendar planning and execution path."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from uagents_core.contrib.protocols.chat import MetadataContent

from agents import planner
from core import approval, graph
from integrations.composio import calendar, connections
from integrations.composio.actions import _event_params
from integrations.composio.models import CalendarOption, CalendarRecord, ConnectionState, ReadBatch
from shared.protocol import ActionResult, GatherRequest, Item


def _ctx():
    return SimpleNamespace(send=AsyncMock(), _agent=True)


def _payload(message):
    metadata = next(content for content in message.content if isinstance(content, MetadataContent))
    return json.loads(metadata.metadata["card_payload"])


def _event() -> CalendarRecord:
    return CalendarRecord(
        item=Item(
            item_id="event-1", user_id="student", kind="event", title="Class",
            starts_at="2026-10-19T09:00:00Z", ends_at="2026-10-19T10:00:00Z",
            source="gcal", source_id="source-1",
        ),
        calendar_id="native-calendar",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("records", [[], [_event()]])
async def test_planner_caches_with_composio_account_and_returns_complete_slots(monkeypatch, records):
    monkeypatch.setattr(planner, "fetch_calendar_events", AsyncMock(return_value=ReadBatch(records=records)))
    monkeypatch.setattr(
        planner.repo, "get_connection",
        AsyncMock(return_value={"status": "active", "composio_account_id": "account-123"}),
    )
    cache = AsyncMock(return_value=[record.item for record in records])
    monkeypatch.setattr(planner, "cache_items", cache)
    ctx = _ctx()
    request = GatherRequest(
        request_id="request-1", user_id="student",
        window_start="2026-10-19T08:00:00Z", window_end="2026-10-19T22:00:00Z",
    )

    await planner._on_gather(ctx, "margin", request)

    cache.assert_awaited_once()
    assert cache.await_args.args[:3] == ("student", "gcal", "account-123")
    response = ctx.send.await_args.args[1]
    assert response.error is None and response.free_slots


@pytest.mark.asyncio
async def test_auto_selects_writable_primary_and_reads_all_writable(monkeypatch):
    options = [
        CalendarOption("primary", "Primary", "account", writable=True, primary=True),
        CalendarOption("classes", "Classes", "account", writable=True),
        CalendarOption("holidays", "Holidays", "account", readable=True, writable=False),
    ]
    monkeypatch.setattr(calendar, "list_calendars", AsyncMock(return_value=ReadBatch(records=options)))
    save = AsyncMock()
    monkeypatch.setattr(connections, "save_connection_settings", save)

    settings = await calendar.auto_select_calendars("student")

    assert settings == {"calendar_ids": ["primary", "classes"], "write_calendar_id": "primary"}
    save.assert_awaited_once_with("student", "gcal", settings)


@pytest.mark.asyncio
async def test_auto_select_refuses_multiple_writable_without_primary(monkeypatch):
    monkeypatch.setattr(calendar, "list_calendars", AsyncMock(return_value=ReadBatch(records=[
        CalendarOption("one", "One", "account", writable=True),
        CalendarOption("two", "Two", "account", writable=True),
    ])))
    save = AsyncMock()
    monkeypatch.setattr(connections, "save_connection_settings", save)

    with pytest.raises(Exception) as error:
        await calendar.auto_select_calendars("student")

    assert getattr(error.value, "code", None) == "CALENDAR_SELECTION_REQUIRED"
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_links_auto_selects_unconfigured_active_calendar(monkeypatch):
    from integrations import composio_tools

    async def status(_user_id, provider):
        return ConnectionState(provider, "active", f"{provider}-account", {})

    monkeypatch.setattr(composio_tools, "connection_status", AsyncMock(side_effect=status))
    select = AsyncMock(return_value={"calendar_ids": ["primary"], "write_calendar_id": "primary"})
    monkeypatch.setattr(composio_tools, "auto_select_calendars", select)
    ctx = _ctx()

    await graph.handle(ctx, "student", "", "asi1", {"action": "check_links"}, "student")

    select.assert_awaited_once_with("student")
    sent = ctx.send.await_args.args[1]
    assert "Google Calendar: connected" in sent.content[0].text


def test_stored_calendar_actions_match_adapter_and_drop_invalid_blocks():
    slots = [["2026-10-19T16:00:00Z", "2026-10-19T18:00:00Z"]]
    proposed = [
        {"action_id": "inside", "type": "create_event", "params": {
            "title": "Study", "item_id": "item-1",
            "start_iso": "2026-10-19T16:00:00Z", "end_iso": "2026-10-19T17:00:00Z",
        }},
        {"action_id": "outside", "type": "create_event", "params": {
            "title": "Invented", "item_id": "item-2",
            "start_iso": "2026-10-19T19:00:00Z", "end_iso": "2026-10-19T20:00:00Z",
        }},
        {"action_id": "short", "type": "create_event", "params": {
            "title": "Too short", "item_id": "item-3",
            "start_iso": "2026-10-19T16:00:00Z", "end_iso": "2026-10-19T16:15:00Z",
        }},
    ]

    actions = graph._stored_calendar_actions(
        proposed, slots, account_id="account-123", calendar_id="primary",
    )

    assert [action["action_id"] for action in actions] == ["inside"]
    for action in actions:
        parsed = _event_params(action["params"])
        assert parsed.account_id == "account-123" and parsed.calendar_id == "primary"


@pytest.mark.asyncio
async def test_store_plan_persists_only_adapter_valid_actions(monkeypatch):
    monkeypatch.setenv("FAKE_SPECIALISTS", "1")
    create = AsyncMock(return_value="plan-1")
    monkeypatch.setattr(approval, "create_plan", create)
    ctx = _ctx()
    state = {
        "ctx": ctx, "sender": "student", "channel": "asi1", "user_id": "student",
        "items": [{"item_id": "item-1"}], "gather_error": None,
        "free_slots": [["2026-10-19T16:00:00Z", "2026-10-19T18:00:00Z"]],
        "actions": [{"action_id": "a1", "type": "create_event", "params": {
            "title": "Study", "item_id": "item-1",
            "start_iso": "2026-10-19T16:00:00Z", "end_iso": "2026-10-19T17:00:00Z",
            "summary_row": "Study",
        }}],
    }

    await graph.store_plan_node(state)

    stored = create.await_args.args[1]
    assert len(stored) == 1
    assert set(stored[0]["params"]) == {
        "account_id", "calendar_id", "title", "starts_at", "ends_at", "description", "purpose",
    }
    _event_params(stored[0]["params"])


@pytest.mark.asyncio
async def test_execute_node_leaves_claim_and_completion_to_adapter(monkeypatch):
    action = {"action_id": "a1", "type": "create_event", "params": {
        "account_id": "account", "calendar_id": "primary", "title": "Study",
        "starts_at": "2026-10-19T16:00:00Z", "ends_at": "2026-10-19T17:00:00Z",
        "description": "Scheduled", "purpose": "study_block",
    }}
    monkeypatch.setattr(approval, "approve", AsyncMock(return_value=True))
    monkeypatch.setattr(approval, "get_plan", AsyncMock(return_value={"actions": [action]}))
    monkeypatch.setattr(approval, "claim_action", AsyncMock(side_effect=AssertionError("must not claim")))
    monkeypatch.setattr(approval, "complete_action", AsyncMock(side_effect=AssertionError("must not complete")))
    monkeypatch.setattr(approval, "mark_done", AsyncMock())
    monkeypatch.setattr(
        graph, "_execute_action",
        AsyncMock(return_value=ActionResult(action_id="a1", ok=True, link="https://calendar.google.com/event")),
    )
    ctx = _ctx()

    await graph.execute_node({
        "ctx": ctx, "sender": "student", "channel": "asi1", "user_id": "student",
        "selection": {"plan_id": "plan-1"},
    })

    approval.claim_action.assert_not_awaited()
    approval.complete_action.assert_not_awaited()
    approval.mark_done.assert_awaited_once_with("plan-1", "student")


@pytest.mark.asyncio
async def test_connect_card_uses_canvas_token_and_gcal_provider(monkeypatch):
    from integrations import composio_tools
    from web import connect_state

    monkeypatch.setattr(connect_state, "make_connect_url", lambda user_id: "https://margin.test/connect/canvas?token=signed")
    links = AsyncMock(side_effect=lambda user_id, provider: f"https://connect.test/{provider}")
    monkeypatch.setattr(composio_tools, "connect_link", links)
    ctx = _ctx()

    await graph.connect_node({
        "ctx": ctx, "sender": "student", "channel": "asi1", "user_id": "student",
        "selection": {"action": "connect"},
    })

    payload = _payload(ctx.send.await_args.args[1])
    redirects = [child.get("action", {}).get("redirect") for child in payload["root"]["children"]]
    assert "https://margin.test/connect/canvas?token=signed" in redirects
    assert [call.args[1] for call in links.await_args_list] == ["gmail", "gcal", "slack"]


def test_margin_agent_handles_messages_concurrently():
    import ast
    from pathlib import Path

    tree = ast.parse(Path("agents/margin_agent.py").read_text())
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Name) and node.func.id == "Agent"]
    assert any(any(keyword.arg == "handle_messages_concurrently"
                   and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                   for keyword in call.keywords) for call in calls)
