"""Calendar specialist agent — Google Calendar + free slots (margin-planner). Owner: Prem."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core.chat import make_chat_protocol, send_text
from integrations.composio_tools import create_calendar_event, fetch_gcal_events
from shared.protocol import ActionRequest, ActionResult, GatherRequest, GatherResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="margin-planner",
    seed=os.environ["PLANNER_SEED"],
    port=8003,
    mailbox=True,
)

# ---------------------------------------------------------------------------
# Gather handler — returns events + free slots
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("planner: gather request from %s for user %s", sender, msg.user_id)
    try:
        items, free_slots = await fetch_gcal_events(
            msg.user_id, msg.window_start, msg.window_end
        )
        result = GatherResult(
            request_id=msg.request_id,
            agent="planner",
            items=items,
            free_slots=free_slots,
        )
    except Exception as exc:
        logger.exception("planner gather failed: %s", exc)
        result = GatherResult(
            request_id=msg.request_id,
            agent="planner",
            error=str(exc),
        )
    await ctx.send(sender, result)


# ---------------------------------------------------------------------------
# Action handler — create_event and add_reminder
# ---------------------------------------------------------------------------

@agent.on_message(ActionRequest)
async def _on_action(ctx: Context, sender: str, msg: ActionRequest) -> None:
    if msg.type not in ("create_event", "add_reminder"):
        await ctx.send(sender, ActionResult(
            action_id=msg.action_id, ok=False,
            error=f"planner agent does not handle action type '{msg.type}'"
        ))
        return

    p = msg.params
    result = await create_calendar_event(
        user_id=msg.user_id,
        title=p.get("title", "Margin block"),
        start_iso=p.get("start_iso", ""),
        end_iso=p.get("end_iso", ""),
        description=p.get("description", "Scheduled by Margin"),
    )

    ok = isinstance(result, dict) and result.get("ok") is not False
    link = None
    if isinstance(result, dict):
        link = (result.get("data") or {}).get("htmlLink") or result.get("link")

    await ctx.send(sender, ActionResult(
        action_id=msg.action_id,
        ok=ok,
        link=link or "https://calendar.google.com",
        error=result.get("error") if not ok and isinstance(result, dict) else None,
    ))


# ---------------------------------------------------------------------------
# Direct chat
# ---------------------------------------------------------------------------

async def _on_text(ctx: Context, sender: str, text: str, selection: dict | None) -> None:
    now = datetime.now(timezone.utc)
    _, slots = await fetch_gcal_events(
        sender,
        now.isoformat(),
        (now + timedelta(days=7)).isoformat(),
    )
    if slots:
        lines = [f"• {s[0][:16].replace('T',' ')} → {s[1][:16].replace('T',' ')}" for s in slots[:5]]
        await send_text(ctx, sender, "Your free slots this week:\n" + "\n".join(lines))
    else:
        await send_text(ctx, sender, "No free slots found. Try connecting Google Calendar.")


_proto = make_chat_protocol(
    agent_name="margin-planner",
    on_text=_on_text,
    greeting="margin-planner online — I handle Google Calendar.",
)
agent.include(_proto, publish_manifest=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
