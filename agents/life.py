"""Life specialist agent — campus events + budget (margin-life). Owner: Prem."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core.chat import make_chat_protocol, send_text
from integrations.events_feed import fetch_events
from integrations.plaid_sandbox import PlaidError, budget_snapshot, fetch_bills, sync_transactions
from shared.protocol import GatherRequest, GatherResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="margin-life",
    seed=os.environ["LIFE_SEED"],
    port=8004,
    mailbox=True,
)

# ---------------------------------------------------------------------------
# Gather handler — returns campus events + bills
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("life: gather request from %s for user %s", sender, msg.user_id)
    items = []
    errors = []
    try:
        items.extend(await fetch_events(msg.user_id, msg.window_end))
    except Exception:
        logger.warning("life: campus event retrieval failed")
        errors.append("Campus events are unavailable.")
    try:
        items.extend(await fetch_bills(msg.user_id))
    except Exception:
        logger.warning("life: configured bill retrieval failed")
        errors.append("Configured bills are unavailable.")
    try:
        synced = await sync_transactions(msg.user_id)
        if not synced.ready:
            errors.append("Bank transaction history is still updating; spending is unavailable.")
    except PlaidError as exc:
        errors.append(str(exc))
    except Exception:
        logger.warning("life: transaction sync failed")
        errors.append("Bank transactions are unavailable.")
    result = GatherResult(
        request_id=msg.request_id,
        agent="life",
        items=items,
        error=" ".join(errors) or None,
    )
    await ctx.send(sender, result)


# ---------------------------------------------------------------------------
# Direct chat
# ---------------------------------------------------------------------------

async def _on_text(ctx: Context, sender: str, text: str, selection: dict | None) -> None:
    lower = text.lower()
    now = datetime.now(timezone.utc)
    window_end = (now + timedelta(days=7)).isoformat()

    if any(k in lower for k in ("event", "dinner", "club", "campus", "activity")):
        events = await fetch_events(sender, window_end)
        if events:
            try:
                snapshot = await budget_snapshot(sender)
            except PlaidError as exc:
                lines = [f"• {event.title} — " + (
                    f"${event.cost_usd:.2f}" if event.cost_usd is not None else "price not provided"
                ) for event in events[:5]]
                await send_text(ctx, sender, f"Budget unavailable: {exc}\nUpcoming events:\n" + "\n".join(lines))
                return
            from core.cards import events_carousel
            await ctx.send(sender, events_carousel(
                [event.model_dump() for event in events[:5]],
                safe_to_spend=snapshot.safe_to_spend,
            ))
        else:
            await send_text(ctx, sender, "No upcoming campus events found.")
    elif any(k in lower for k in ("budget", "afford", "spend")):
        try:
            snapshot = await budget_snapshot(sender)
        except PlaidError as exc:
            await send_text(ctx, sender, f"Budget unavailable: {exc}")
            return
        if snapshot.safe_to_spend < 0:
            message = f"Your budget has a ${abs(snapshot.safe_to_spend):.2f} weekly shortfall."
        else:
            message = f"Safe to spend this week: ${snapshot.safe_to_spend:.2f}."
        await send_text(ctx, sender, message)
    elif any(k in lower for k in ("bill", "payment", "rent")):
        try:
            bills = await fetch_bills(sender)
        except Exception:
            logger.warning("life: configured bill retrieval failed")
            await send_text(ctx, sender, "Configured bills are unavailable. Please try again.")
            return
        if bills:
            lines = [f"• {b.title} — due {(b.due_at or '')[:10]}" for b in bills[:5]]
            await send_text(ctx, sender, "Upcoming bills:\n" + "\n".join(lines))
        else:
            await send_text(ctx, sender, "No upcoming bills found.")
    else:
        await send_text(ctx, sender, "I handle campus events and budget. Try 'show events', 'show bills', or 'how much can I spend?'.")


_proto = make_chat_protocol(
    agent_name="margin-life",
    on_text=_on_text,
    greeting="margin-life online — I handle campus events and budget.",
)
agent.include(_proto, publish_manifest=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
