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
from integrations.plaid_sandbox import fetch_bills
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
    try:
        events = await fetch_events(msg.user_id, msg.window_end)
        bills = await fetch_bills(msg.user_id)
        result = GatherResult(
            request_id=msg.request_id,
            agent="life",
            items=events + bills,
        )
    except Exception as exc:
        logger.exception("life gather failed: %s", exc)
        result = GatherResult(
            request_id=msg.request_id,
            agent="life",
            error=str(exc),
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
            from core.cards import events_carousel
            await ctx.send(sender, events_carousel(events[:5], safe_to_spend=50.0))
        else:
            await send_text(ctx, sender, "No upcoming campus events found.")
    elif any(k in lower for k in ("bill", "payment", "rent", "budget")):
        bills = await fetch_bills(sender)
        if bills:
            lines = [f"• {b.title} — due {(b.due_at or '')[:10]}" for b in bills[:5]]
            await send_text(ctx, sender, "Upcoming bills:\n" + "\n".join(lines))
        else:
            await send_text(ctx, sender, "No upcoming bills found.")
    else:
        await send_text(ctx, sender, "I handle campus events and budget. Try 'show events' or 'show bills'.")


_proto = make_chat_protocol(
    agent_name="margin-life",
    on_text=_on_text,
    greeting="margin-life online — I handle campus events and budget.",
)
agent.include(_proto, publish_manifest=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
