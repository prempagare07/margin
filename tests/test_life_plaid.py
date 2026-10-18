"""Life's finance paths use persisted Plaid spending without live agents or calls."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.protocol import GatherRequest, Item


@pytest.fixture
def life(monkeypatch):
    import dotenv
    import uagents

    class LocalAgent:
        def __init__(self, **kwargs):
            pass

        def on_message(self, model):
            return lambda handler: handler

        def include(self, *args, **kwargs):
            pass

    # Do not load developer credentials or initialize a registered agent.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.setattr(uagents, "Agent", LocalAgent)
    monkeypatch.setenv("LIFE_SEED", "local-test-only")
    spec = importlib.util.spec_from_file_location(
        "_margin_life_test", Path(__file__).parents[1] / "agents/life.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "fetch_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "fetch_bills", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "sync_transactions", AsyncMock(return_value=SimpleNamespace(ready=True)))
    monkeypatch.setattr(module, "budget_snapshot", AsyncMock(return_value=SimpleNamespace(safe_to_spend=27.5)))
    return module


def item(kind="event", cost=15.0):
    return Item(
        item_id=kind, user_id="alex", kind=kind,
        title="Club dinner" if kind == "event" else "Rent",
        starts_at="2026-10-20T17:00:00Z" if kind == "event" else None,
        due_at="2026-10-31T19:00:00Z" if kind == "bill" else None,
        source="sundevilsync" if kind == "event" else "plaid",
        source_id=kind, cost_usd=cost,
    )


def request():
    return GatherRequest(
        request_id="req", user_id="alex", window_start="2026-10-18T00:00:00Z",
        window_end="2026-10-25T00:00:00Z",
    )


def text_sent(ctx):
    return " ".join(content.text for content in ctx.send.await_args.args[1].content
                    if hasattr(content, "text"))


@pytest.mark.asyncio
async def test_gather_syncs_for_owner_and_returns_configured_bills(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_events.return_value = [item()]
    life.fetch_bills.return_value = [item("bill", 850)]
    await life._on_gather(ctx, "orchestrator", request())
    life.sync_transactions.assert_awaited_once_with("alex")
    result = ctx.send.await_args.args[1]
    assert result.error is None
    assert [value.kind for value in result.items] == ["event", "bill"]


@pytest.mark.asyncio
async def test_gather_preserves_independent_items_on_bank_failure(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_events.return_value = [item()]
    life.fetch_bills.return_value = [item("bill")]
    life.sync_transactions.side_effect = life.PlaidError("NOT_CONNECTED", "Connect the sandbox bank first.")
    await life._on_gather(ctx, "orchestrator", request())
    result = ctx.send.await_args.args[1]
    assert len(result.items) == 2
    assert "Connect the sandbox bank first." in result.error


@pytest.mark.asyncio
async def test_gather_reports_incomplete_history(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.sync_transactions.return_value = SimpleNamespace(ready=False)
    await life._on_gather(ctx, "orchestrator", request())
    assert "still updating" in ctx.send.await_args.args[1].error


@pytest.mark.asyncio
async def test_event_failure_does_not_skip_sync_or_expose_raw_exception(life, caplog):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_events.side_effect = RuntimeError("private-provider-response")
    life.fetch_bills.return_value = [item("bill")]
    await life._on_gather(ctx, "orchestrator", request())
    result = ctx.send.await_args.args[1]
    assert result.items[0].kind == "bill"
    assert result.error == "Campus events are unavailable."
    assert "private-provider-response" not in caplog.text
    life.sync_transactions.assert_awaited_once_with("alex")


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["show my budget", "what can I afford?", "how much can I spend?"])
async def test_budget_chat_uses_synced_snapshot(life, prompt):
    ctx = SimpleNamespace(send=AsyncMock())
    await life._on_text(ctx, "alex", prompt, None)
    life.budget_snapshot.assert_awaited_once_with("alex")
    assert "Safe to spend this week: $27.50" in text_sent(ctx)
    life.fetch_bills.assert_not_awaited()


@pytest.mark.asyncio
async def test_budget_deficit_is_not_presented_as_available_spending(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.budget_snapshot.return_value = SimpleNamespace(safe_to_spend=-17.25)
    await life._on_text(ctx, "alex", "show my budget", None)
    assert "$17.25 weekly shortfall" in text_sent(ctx)
    assert "Safe to spend" not in text_sent(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["NOT_CONNECTED", "BUDGET_NOT_CONFIGURED", "DATA_NOT_READY"])
async def test_budget_failure_never_reports_a_fabricated_amount(life, code):
    ctx = SimpleNamespace(send=AsyncMock())
    life.budget_snapshot.side_effect = life.PlaidError(code, "Budget inputs are unavailable.")
    await life._on_text(ctx, "alex", "show my budget", None)
    text = text_sent(ctx)
    assert "Budget unavailable" in text and "Budget inputs are unavailable." in text
    assert "Safe to spend" not in text and "$" not in text


@pytest.mark.asyncio
async def test_events_card_budget_badge_uses_real_snapshot(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_events.return_value = [item()]
    await life._on_text(ctx, "alex", "show events", None)
    life.budget_snapshot.assert_awaited_once_with("alex")
    metadata = next(part.metadata for part in ctx.send.await_args.args[1].content
                    if hasattr(part, "metadata"))
    assert json.loads(metadata["card_payload"])["items"][0]["badges"] == ["$15", "leaves $12 this week"]


@pytest.mark.asyncio
async def test_events_remain_visible_without_inventing_a_budget(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_events.return_value = [item(cost=None)]
    life.budget_snapshot.side_effect = life.PlaidError("NOT_CONNECTED", "Connect the sandbox bank first.")
    await life._on_text(ctx, "alex", "show events", None)
    text = text_sent(ctx)
    assert "Budget unavailable" in text and "Club dinner" in text
    assert "price not provided" in text and "leaves $" not in text


@pytest.mark.asyncio
async def test_configured_bills_do_not_depend_on_bank_connection(life):
    ctx = SimpleNamespace(send=AsyncMock())
    life.fetch_bills.return_value = [item("bill", 850)]
    await life._on_text(ctx, "alex", "show bills", None)
    assert "Rent" in text_sent(ctx)
    life.sync_transactions.assert_not_awaited()
    life.budget_snapshot.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_configured_bills_stay_empty(life):
    ctx = SimpleNamespace(send=AsyncMock())
    await life._on_text(ctx, "alex", "show bills", None)
    assert text_sent(ctx) == "No upcoming bills found."

