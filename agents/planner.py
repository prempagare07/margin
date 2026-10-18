"""Calendar specialist agent — Google Calendar + free slots (margin-planner). Owner: Prem."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core.chat import make_chat_protocol, send_text
from integrations.composio_tools import (
    IntegrationError,
    cached_gather_result,
    cache_items,
    compute_free_slots,
    execute_approved_action,
    fetch_calendar_events,
    filter_items,
)
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
# Gather handler — returns calendar events + free slots
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("planner: gather for user %s", msg.user_id)
    try:
        batch = await fetch_calendar_events(
            msg.user_id,
            start=msg.window_start,
            end=msg.window_end,
        )
        account_id = batch.records[0].calendar_id if batch.records else ""
        items_raw = [r.item for r in batch.records]
        items = await cache_items(msg.user_id, "gcal", account_id, items_raw)
        items = filter_items(items, start=msg.window_start, end=msg.window_end)

        # free slots only from a complete batch — partial = stale scheduling risk
        if batch.complete:
            free_slots = compute_free_slots(
                batch.records,
                start=msg.window_start,
                end=msg.window_end,
                tz="America/Phoenix",
            )
        else:
            free_slots = []

        await ctx.send(sender, GatherResult(
            request_id=msg.request_id,
            agent="planner",
            items=items,
            free_slots=free_slots,
        ))
    except IntegrationError as exc:
        logger.warning("planner: gather error %s", exc.code)
        result = await cached_gather_result(msg, "gcal", error=exc)
        await ctx.send(sender, result)
    except Exception as exc:
        logger.exception("planner gather failed: %s", exc)
        await ctx.send(sender, GatherResult(
            request_id=msg.request_id,
            agent="planner",
            error=str(exc),
        ))


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

    try:
        result = await execute_approved_action(msg)
    except IntegrationError as exc:
        result = ActionResult(action_id=msg.action_id, ok=False, error=exc.message)
    except Exception as exc:
        result = ActionResult(action_id=msg.action_id, ok=False, error=str(exc))

    await ctx.send(sender, result)


# ---------------------------------------------------------------------------
# Direct chat
# ---------------------------------------------------------------------------

async def _on_text(ctx: Context, sender: str, text: str, selection: dict | None) -> None:
    now = datetime.now(timezone.utc)
    try:
        batch = await fetch_calendar_events(
            sender,
            start=now.isoformat(),
            end=(now + timedelta(days=7)).isoformat(),
        )
        slots = compute_free_slots(
            batch.records,
            start=now.isoformat(),
            end=(now + timedelta(days=7)).isoformat(),
            tz="America/Phoenix",
        ) if batch.complete else []
    except Exception:
        slots = []

    if slots:
        lines = [f"• {s[0][:16].replace('T', ' ')} → {s[1][:16].replace('T', ' ')}" for s in slots[:5]]
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
