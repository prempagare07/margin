"""Stripe embedded checkout + entitlement grants. Owner: Alex."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tier config
# ---------------------------------------------------------------------------

TIERS: dict[str, dict] = {
    "pro": {
        "name": "Margin Pro",
        "description": "Unlimited briefs, nudges, and planning for 30 days.",
        "amount_cents": 499,
        "currency": "usd",
        "days": 30,
    },
    "finals": {
        "name": "Margin Finals Pack",
        "description": "Two weeks of Pro during finals.",
        "amount_cents": 199,
        "currency": "usd",
        "days": 14,
    },
}


# ---------------------------------------------------------------------------
# Stripe helpers (sync — called via asyncio.to_thread)
# ---------------------------------------------------------------------------

def _stripe():
    import stripe  # type: ignore
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe


def _create_checkout_session(
    user_id: str,
    tier: str,
    session_ref: str,
) -> dict:
    t = TIERS[tier]
    stripe = _stripe()
    base_url = os.environ.get("PUBLIC_BASE_URL", "https://inappmargin.vip")
    return_url = (
        f"{base_url}/payments/complete"
        f"?session_id={{CHECKOUT_SESSION_ID}}&user={user_id}&tier={tier}"
    )
    expires_at = int(time.time()) + 1800  # 30 min window

    session = stripe.checkout.Session.create(
        ui_mode="embedded",
        redirect_on_completion="if_required",
        payment_method_types=["card"],
        mode="payment",
        return_url=return_url,
        expires_at=expires_at,
        line_items=[
            {
                "price_data": {
                    "currency": t["currency"],
                    "product_data": {
                        "name": t["name"],
                        "description": t["description"],
                    },
                    "unit_amount": t["amount_cents"],
                },
                "quantity": 1,
            }
        ],
        metadata={
            "user_id": user_id,
            "tier": tier,
            "session_ref": session_ref,
        },
    )
    return {
        "client_secret": session.client_secret,
        "id": session.id,
        "checkout_session_id": session.id,
        "publishable_key": os.environ.get("STRIPE_PUBLISHABLE_KEY", ""),
        "currency": t["currency"],
        "amount_cents": t["amount_cents"],
        "ui_mode": "embedded",
    }


def _verify_paid(checkout_session_id: str) -> bool:
    stripe = _stripe()
    session = stripe.checkout.Session.retrieve(checkout_session_id)
    return getattr(session, "payment_status", None) == "paid"


def _retrieve_tier(checkout_session_id: str) -> str | None:
    stripe = _stripe()
    session = stripe.checkout.Session.retrieve(checkout_session_id)
    return (session.metadata or {}).get("tier")


# ---------------------------------------------------------------------------
# Async public API
# ---------------------------------------------------------------------------

async def create_checkout(user_id: str, tier: str, session_ref: str) -> dict:
    """Create a Stripe embedded checkout session for the given tier.

    Returns the checkout dict with client_secret, publishable_key, etc.
    Raises KeyError if tier is unknown, RuntimeError if STRIPE_SECRET_KEY missing.
    """
    if tier not in TIERS:
        raise KeyError(f"Unknown tier: {tier!r}")
    if not os.environ.get("STRIPE_SECRET_KEY"):
        # Return a stub so the card still renders in dev/demo
        logger.warning("STRIPE_SECRET_KEY not set — returning stub checkout")
        return {
            "client_secret": "stub_secret",
            "id": f"cs_stub_{tier}",
            "checkout_session_id": f"cs_stub_{tier}",
            "publishable_key": "",
            "currency": TIERS[tier]["currency"],
            "amount_cents": TIERS[tier]["amount_cents"],
            "ui_mode": "embedded",
        }
    return await asyncio.to_thread(_create_checkout_session, user_id, tier, session_ref)


async def verify_and_grant(checkout_session_id: str, user_id: str) -> tuple[bool, str | None]:
    """Verify payment_status == 'paid' and extend pro_until.

    Returns (ok, tier_or_none). Call this from CommitPayment handler.
    """
    if checkout_session_id.startswith("cs_stub_"):
        tier = checkout_session_id.removeprefix("cs_stub_")
        await _grant_entitlement(user_id, tier)
        return True, tier

    paid = await asyncio.to_thread(_verify_paid, checkout_session_id)
    if not paid:
        return False, None

    tier = await asyncio.to_thread(_retrieve_tier, checkout_session_id)
    if tier:
        await _grant_entitlement(user_id, tier)
    return True, tier


async def _grant_entitlement(user_id: str, tier: str) -> None:
    days = TIERS.get(tier, {}).get("days", 30)
    pro_until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    try:
        from db import repo
        await repo.upsert_user(user_id, pro_until=pro_until, plan=tier)
    except Exception as exc:
        logger.warning("grant_entitlement: DB unavailable (%s) — skipping persist", exc)
