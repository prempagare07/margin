"""Canvas specialist agent (margin-academic). Owner: Prem."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core.chat import make_chat_protocol, send_text
from integrations import canvas_client
from shared.protocol import ActionRequest, ActionResult, GatherRequest, GatherResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="margin-academic",
    seed=os.environ["ACADEMIC_SEED"],
    port=8001,
    mailbox=True,
)

# ---------------------------------------------------------------------------
# Gather handler (called by orchestrator)
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("academic: gather request from %s for user %s", sender, msg.user_id)
    result = await canvas_client.gather(msg)
    await ctx.send(sender, result)


# ---------------------------------------------------------------------------
# Direct chat (ASI:One users talking to this specialist)
# ---------------------------------------------------------------------------

async def _on_text(ctx: Context, sender: str, text: str, selection: dict | None) -> None:
    lower = text.lower()
    if any(k in lower for k in ("assignment", "canvas", "due", "course", "grade")):
        now = datetime.now(timezone.utc)
        from shared.protocol import GatherRequest
        from uuid import uuid4
        req = GatherRequest(
            request_id=str(uuid4()),
            user_id=sender,
            window_start=now.isoformat(),
            window_end=(now + timedelta(days=7)).isoformat(),
        )
        result = await canvas_client.gather(req)
        if result.items:
            from core.cards import priorities_carousel
            await ctx.send(sender, priorities_carousel([i.model_dump() for i in result.items[:5]]))
        else:
            await send_text(ctx, sender, "No upcoming Canvas assignments found.")
    else:
        await send_text(ctx, sender, "I handle Canvas assignments and announcements. Try 'show my assignments'.")


_proto = make_chat_protocol(
    agent_name="margin-academic",
    on_text=_on_text,
    greeting="margin-academic online — I handle Canvas assignments.",
)
agent.include(_proto, publish_manifest=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
