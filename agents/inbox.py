"""Inbox specialist agent — Gmail + Slack (margin-inbox). Owner: Prem."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core.chat import make_chat_protocol, send_text
from integrations.composio_tools import create_email_draft, fetch_emails
from shared.protocol import ActionRequest, ActionResult, GatherRequest, GatherResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="margin-inbox",
    seed=os.environ["INBOX_SEED"],
    port=8002,
    mailbox=True,
)

# ---------------------------------------------------------------------------
# Gather handler
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("inbox: gather request from %s for user %s", sender, msg.user_id)
    try:
        items = await fetch_emails(msg.user_id)
        result = GatherResult(
            request_id=msg.request_id,
            agent="inbox",
            items=items,
        )
    except Exception as exc:
        logger.exception("inbox gather failed: %s", exc)
        result = GatherResult(
            request_id=msg.request_id,
            agent="inbox",
            error=str(exc),
        )
    await ctx.send(sender, result)


# ---------------------------------------------------------------------------
# Action handler — create_draft
# ---------------------------------------------------------------------------

@agent.on_message(ActionRequest)
async def _on_action(ctx: Context, sender: str, msg: ActionRequest) -> None:
    if msg.type != "create_draft":
        await ctx.send(sender, ActionResult(
            action_id=msg.action_id, ok=False,
            error=f"inbox agent does not handle action type '{msg.type}'"
        ))
        return

    p = msg.params
    result = await create_email_draft(
        user_id=msg.user_id,
        to=p.get("to", ""),
        subject=p.get("subject", ""),
        body=p.get("body", ""),
    )
    ok = isinstance(result, dict) and result.get("ok") is not False
    await ctx.send(sender, ActionResult(
        action_id=msg.action_id,
        ok=ok,
        link=result.get("link") if isinstance(result, dict) else None,
        error=result.get("error") if not ok and isinstance(result, dict) else None,
    ))


# ---------------------------------------------------------------------------
# Direct chat
# ---------------------------------------------------------------------------

async def _on_text(ctx: Context, sender: str, text: str, selection: dict | None) -> None:
    items = await fetch_emails(sender)
    if items:
        from core.cards import priorities_carousel
        await ctx.send(sender, priorities_carousel(items[:5]))
    else:
        await send_text(ctx, sender, "No action items in your inbox right now.")


_proto = make_chat_protocol(
    agent_name="margin-inbox",
    on_text=_on_text,
    greeting="margin-inbox online — I handle Gmail and Slack.",
)
agent.include(_proto, publish_manifest=True)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
