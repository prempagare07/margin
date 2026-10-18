"""Margin orchestrator agent. Owner: Prem."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from channels.scheduler import start_scheduler, stop_scheduler
from core import gather as gather_bus
from core import graph
from core.cards import connect_form, welcome_card
from core.chat import make_chat_protocol, send_text, send_text_msg
from shared.protocol import ActionRequest, ActionResult, GatherResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    RejectPayment,
)

agent = Agent(
    name="margin",
    seed=os.environ["MARGIN_SEED"],
    port=8000,
    mailbox=True,
    handle_messages_concurrently=True,
    publish_agent_details=True,
)


@agent.on_event("startup")
async def _startup(ctx: Context) -> None:
    logger.info("margin agent online: %s", agent.address)
    start_scheduler()


@agent.on_event("shutdown")
async def _shutdown(ctx: Context) -> None:
    stop_scheduler()


# ---------------------------------------------------------------------------
# Chat protocol
# ---------------------------------------------------------------------------

async def _on_text(
    ctx: Context,
    sender: str,
    text: str,
    selection: dict | None,
) -> None:
    await graph.handle(
        ctx=ctx,
        user_id=sender,
        text=text,
        channel="asi1",
        selection=selection,
        sender=sender,
    )


_proto = make_chat_protocol(
    agent_name="margin",
    on_text=_on_text,
    greeting=[welcome_card(), connect_form()],
)

agent.include(_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# Payment protocol (seller role) — mirrors stripe-horoscope-agent pattern
# ---------------------------------------------------------------------------

async def _on_commit(ctx: Context, sender: str, msg: CommitPayment) -> None:
    if msg.funds.payment_method != "stripe" or not msg.transaction_id:
        await ctx.send(sender, RejectPayment(reason="Only Stripe payments are accepted."))
        return

    from payments.stripe_flow import TIERS, verify_and_grant
    ok, tier = await verify_and_grant(msg.transaction_id, sender)
    if not ok:
        await ctx.send(sender, RejectPayment(reason="Stripe payment not completed yet. Please finish checkout."))
        return

    await ctx.send(sender, CompletePayment(transaction_id=msg.transaction_id))
    t = TIERS.get(tier or "", {})
    name = t.get("name", "Margin Pro")
    days = t.get("days", 30)
    from core.cards import payment_success_card
    await ctx.send(sender, payment_success_card(name, days))


async def _on_reject(ctx: Context, sender: str, msg: RejectPayment) -> None:
    reason = msg.reason or "Payment was rejected."
    await send_text(ctx, sender, f"Payment cancelled: {reason}. Say 'upgrade' to try again.")


from payments.payment_proto import build_payment_proto
_payment_proto = build_payment_proto(_on_commit, _on_reject)
agent.include(_payment_proto, publish_manifest=True)


# ---------------------------------------------------------------------------
# GatherResult handler — resolves futures created in graph.gather_node
# ---------------------------------------------------------------------------

@agent.on_message(GatherResult)
async def _on_gather_result(ctx: Context, sender: str, msg: GatherResult) -> None:
    gather_bus.resolve(msg.request_id, msg)


# ---------------------------------------------------------------------------
# ActionResult handler — resolves futures created in graph.execute_node
# ---------------------------------------------------------------------------

@agent.on_message(ActionResult)
async def _on_action_result(ctx: Context, sender: str, msg: ActionResult) -> None:
    gather_bus.resolve(msg.action_id, msg)


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
