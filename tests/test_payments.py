"""Payment flow tests (FAKE_SPECIALISTS=1, no real Stripe). Owner: Prem."""
from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["FAKE_SPECIALISTS"] = "1"


# ---------------------------------------------------------------------------
# stripe_flow unit tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_checkout_stub_when_no_key(monkeypatch):
    """Returns stub dict when STRIPE_SECRET_KEY is absent."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    from payments.stripe_flow import create_checkout
    result = await create_checkout("u1", "pro", "sess1")
    assert result["checkout_session_id"].startswith("cs_stub_")
    assert result["amount_cents"] == 499


@pytest.mark.asyncio
async def test_create_checkout_unknown_tier():
    from payments.stripe_flow import create_checkout
    with pytest.raises(KeyError):
        await create_checkout("u1", "gold", "sess1")


@pytest.mark.asyncio
async def test_verify_and_grant_stub_session():
    """Stub session IDs are always considered paid."""
    from payments.stripe_flow import verify_and_grant
    with patch("payments.stripe_flow._grant_entitlement", new=AsyncMock()) as mock_grant:
        ok, tier = await verify_and_grant("cs_stub_finals", "u1")
    assert ok is True
    assert tier == "finals"
    mock_grant.assert_awaited_once_with("u1", "finals")


@pytest.mark.asyncio
async def test_verify_and_grant_real_stripe_paid():
    """Real session ID verified as paid → grant entitlement."""
    from payments.stripe_flow import verify_and_grant
    with patch("payments.stripe_flow.asyncio.to_thread") as mock_thread, \
         patch("payments.stripe_flow._grant_entitlement", new=AsyncMock()) as mock_grant:
        # First to_thread call: _verify_paid → True
        # Second to_thread call: _retrieve_tier → "pro"
        mock_thread.side_effect = [True, "pro"]
        ok, tier = await verify_and_grant("cs_real_123", "u1")
    assert ok is True
    assert tier == "pro"


@pytest.mark.asyncio
async def test_verify_and_grant_real_stripe_unpaid():
    from payments.stripe_flow import verify_and_grant
    with patch("payments.stripe_flow.asyncio.to_thread", return_value=False):
        ok, tier = await verify_and_grant("cs_real_456", "u1")
    assert ok is False
    assert tier is None


# ---------------------------------------------------------------------------
# upgrade_node integration tests
# ---------------------------------------------------------------------------

from tests.test_e2e_flow import MockCtx


@pytest.mark.asyncio
async def test_upgrade_node_no_tier_shows_card():
    """upgrade action without tier → upgrade_card sent."""
    from core.graph import handle
    ctx = MockCtx()
    await handle(ctx, user_id="u-pay-1", text="I want to upgrade",
                 channel="asi1", sender="u-pay-1")
    from uagents_core.contrib.protocols.chat import MetadataContent
    meta = None
    for _, msg in reversed(ctx.sent):
        if hasattr(msg, "content"):
            for c in msg.content:
                if isinstance(c, MetadataContent):
                    meta = c
                    break
    assert meta is not None
    assert meta.metadata["card_kind"] == "custom"
    assert "Upgrade" in meta.metadata["card_payload"]


@pytest.mark.asyncio
async def test_upgrade_node_with_tier_sends_request_payment(monkeypatch):
    """upgrade action with tier → RequestPayment sent."""
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    from uagents_core.contrib.protocols.payment import RequestPayment
    from core.graph import handle

    ctx = MockCtx()
    await handle(
        ctx,
        user_id="u-pay-2",
        text="",
        channel="asi1",
        selection={"action": "upgrade", "tier": "pro"},
        sender="u-pay-2",
    )
    payment_msgs = [msg for _, msg in ctx.sent if isinstance(msg, RequestPayment)]
    assert payment_msgs, "expected RequestPayment to be sent"
    req = payment_msgs[0]
    assert req.accepted_funds[0].payment_method == "stripe"
    assert req.accepted_funds[0].amount == "4.99"


@pytest.mark.asyncio
async def test_upgrade_node_finals_tier(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)

    from uagents_core.contrib.protocols.payment import RequestPayment
    from core.graph import handle

    ctx = MockCtx()
    await handle(
        ctx,
        user_id="u-pay-3",
        text="",
        channel="asi1",
        selection={"action": "upgrade", "tier": "finals"},
        sender="u-pay-3",
    )
    payment_msgs = [msg for _, msg in ctx.sent if isinstance(msg, RequestPayment)]
    assert payment_msgs
    req = payment_msgs[0]
    assert req.accepted_funds[0].amount == "1.99"
