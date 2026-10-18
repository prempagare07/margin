"""End-to-end flow tests (in-process, FAKE_SPECIALISTS=1). Owner: Prem."""
from __future__ import annotations

import asyncio
import os

import pytest

# Force fake/in-memory mode — these are in-process logic tests, not DB integration tests
os.environ["FAKE_SPECIALISTS"] = "1"
os.environ["DATABASE_URL"] = ""

from unittest.mock import AsyncMock, MagicMock, patch

from core import approval
from core.cards import (
    connect_form,
    done as done_card,
    review as review_card,
)
from core.chat import parse_selection
from core.graph import handle
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent


# ---------------------------------------------------------------------------
# Minimal mock context that records sends
# ---------------------------------------------------------------------------

class MockCtx:
    def __init__(self):
        self.sent = []
        self._agent = True          # makes _is_agent_ctx() return True

    async def send(self, to: str, msg) -> None:
        self.sent.append((to, msg))

    @property
    def address(self):
        return "agent1qtest"


def _last_card(ctx: MockCtx):
    """Return the MetadataContent from the last sent ChatMessage, or None."""
    from uagents_core.contrib.protocols.chat import ChatMessage
    for _, msg in reversed(ctx.sent):
        if isinstance(msg, type(connect_form())):   # ChatMessage
            for c in msg.content:
                if isinstance(c, MetadataContent):
                    return c
    return None


def _last_text(ctx: MockCtx) -> str:
    from uagents_core.contrib.protocols.chat import ChatMessage
    for _, msg in reversed(ctx.sent):
        if hasattr(msg, "content"):
            for c in msg.content:
                if isinstance(c, TextContent):
                    return c.text
    return ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_week_produces_review_card():
    approval._mem_plans.clear()
    ctx = MockCtx()
    await handle(ctx, user_id="u-e2e-1", text="plan my week",
                 channel="asi1", sender="u-e2e-1")
    meta = _last_card(ctx)
    assert meta is not None, "expected a card to be sent"
    assert meta.metadata["card_kind"] == "review"
    assert "plan_id" in meta.metadata["card_payload"]


@pytest.mark.asyncio
async def test_approve_produces_done_card():
    approval._mem_plans.clear()
    approval._mem_execs.clear()
    ctx = MockCtx()

    # Step 1: generate a plan
    await handle(ctx, user_id="u-e2e-2", text="plan my week",
                 channel="asi1", sender="u-e2e-2")

    # Extract plan_id from review card
    import json
    meta = _last_card(ctx)
    assert meta is not None
    payload = json.loads(meta.metadata["card_payload"])
    plan_id = payload["approve_cta"]["selection"]["plan_id"]

    # Step 2: approve it
    ctx2 = MockCtx()
    await handle(ctx2, user_id="u-e2e-2", text="",
                 channel="asi1",
                 selection={"action": "approve", "plan_id": plan_id},
                 sender="u-e2e-2")

    meta2 = _last_card(ctx2)
    assert meta2 is not None, "expected done card after approve"
    assert meta2.metadata["card_kind"] == "custom"
    assert "Done" in meta2.metadata["card_payload"]


@pytest.mark.asyncio
async def test_approve_idempotent():
    approval._mem_plans.clear()
    approval._mem_execs.clear()
    ctx = MockCtx()

    await handle(ctx, user_id="u-e2e-3", text="plan my week",
                 channel="asi1", sender="u-e2e-3")

    import json
    payload = json.loads(_last_card(ctx).metadata["card_payload"])
    plan_id = payload["approve_cta"]["selection"]["plan_id"]

    # Approve once
    ctx2 = MockCtx()
    await handle(ctx2, user_id="u-e2e-3", text="",
                 channel="asi1", selection={"action": "approve", "plan_id": plan_id},
                 sender="u-e2e-3")
    assert _last_card(ctx2).metadata["card_kind"] == "custom"  # done

    # Approve again — should get error text, not another done card
    ctx3 = MockCtx()
    await handle(ctx3, user_id="u-e2e-3", text="",
                 channel="asi1", selection={"action": "approve", "plan_id": plan_id},
                 sender="u-e2e-3")
    text = _last_text(ctx3)
    assert "already done" in text.lower() or "couldn't" in text.lower()


@pytest.mark.asyncio
async def test_cancel_plan():
    approval._mem_plans.clear()
    ctx = MockCtx()

    await handle(ctx, user_id="u-e2e-4", text="plan my week",
                 channel="asi1", sender="u-e2e-4")

    import json
    payload = json.loads(_last_card(ctx).metadata["card_payload"])
    plan_id = payload["reject_cta"]["selection"]["plan_id"]

    ctx2 = MockCtx()
    await handle(ctx2, user_id="u-e2e-4", text="",
                 channel="asi1", selection={"action": "cancel", "plan_id": plan_id},
                 sender="u-e2e-4")

    plan = await approval.get_plan(plan_id, "u-e2e-4")
    assert plan["status"] == "cancelled"
    assert "cancelled" in _last_text(ctx2).lower()


@pytest.mark.asyncio
async def test_upgrade_card():
    ctx = MockCtx()
    await handle(ctx, user_id="u-e2e-5", text="I want to upgrade to pro",
                 channel="asi1", sender="u-e2e-5")
    meta = _last_card(ctx)
    assert meta is not None
    assert meta.metadata["card_kind"] == "custom"
    assert "Upgrade" in meta.metadata["card_payload"]


@pytest.mark.asyncio
async def test_photon_plan_flow():
    """Full plan→approve flow over Photon channel (text only)."""
    approval._mem_plans.clear()
    approval._mem_execs.clear()

    from channels.photon import PhotonCtx

    sent = []
    # patch where PhotonCtx.send() resolves it (channels.photon imports it directly)
    with patch("channels.photon.send_message",
               new=AsyncMock(side_effect=lambda to, text, **kw: sent.append(text) or {"ok": True})):
        ph_ctx = PhotonCtx("+16027513236")
        await handle(ph_ctx, user_id="+16027513236", text="plan my week",
                     channel="photon", sender="+16027513236")

    assert sent, "expected Photon message to be sent"
    assert "YES" in sent[0].upper() or "plan" in sent[0].lower()


@pytest.mark.asyncio
async def test_parse_selection_json():
    sel = await parse_selection('{"action": "approve", "plan_id": "abc"}')
    assert sel == {"action": "approve", "plan_id": "abc"}


@pytest.mark.asyncio
async def test_parse_selection_plain_text_returns_none():
    sel = await parse_selection("what's due tomorrow?")
    assert sel is None
