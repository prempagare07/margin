"""Inbox specialist agent — Gmail + Slack (margin-inbox). Owner: Prem."""
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
    fetch_gmail_messages,
    fetch_slack_messages,
    filter_items,
    item_id as make_item_id,
    execute_approved_action,
)
from shared.protocol import ActionRequest, ActionResult, GatherRequest, GatherResult, Item

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
# Helpers
# ---------------------------------------------------------------------------

def _msg_to_item(msg, user_id: str) -> Item:
    kind = "slack_action" if msg.provider == "slack" else "email_action"
    source = "slack" if msg.provider == "slack" else "gmail"
    return Item(
        item_id=make_item_id(user_id, source, msg.source_id),
        user_id=user_id,
        kind=kind,
        title=msg.subject or "(no subject)",
        source=source,
        source_id=msg.source_id,
        source_url=msg.source_url,
        quote=(msg.body[:200].strip() if msg.body else None),
        status="open",
    )


# ---------------------------------------------------------------------------
# Gather handler
# ---------------------------------------------------------------------------

@agent.on_message(GatherRequest)
async def _on_gather(ctx: Context, sender: str, msg: GatherRequest) -> None:
    logger.info("inbox: gather for user %s", msg.user_id)
    items: list[Item] = []

    for fetch_fn, provider in (
        (fetch_gmail_messages, "gmail"),
        (fetch_slack_messages, "slack"),
    ):
        try:
            batch = await fetch_fn(
                msg.user_id,
                since=msg.window_start,
                until=msg.window_end,
            )
            raw = [_msg_to_item(m, msg.user_id) for m in batch.records]
            cached = await cache_items(msg.user_id, provider, (batch.records[0].account_id if batch.records else ""), raw)
            items.extend(filter_items(cached, start=msg.window_start, end=msg.window_end))
        except IntegrationError as exc:
            logger.warning("inbox: %s gather error %s", provider, exc.code)
            fallback = await cached_gather_result(msg, provider, error=exc)
            items.extend(fallback.items or [])
        except Exception as exc:
            logger.exception("inbox: %s gather failed: %s", provider, exc)

    await ctx.send(sender, GatherResult(
        request_id=msg.request_id,
        agent="inbox",
        items=items,
    ))


# ---------------------------------------------------------------------------
# Action handler — create_draft (and fetch_source)
# ---------------------------------------------------------------------------

@agent.on_message(ActionRequest)
async def _on_action(ctx: Context, sender: str, msg: ActionRequest) -> None:
    if msg.type not in ("create_draft", "fetch_source"):
        await ctx.send(sender, ActionResult(
            action_id=msg.action_id, ok=False,
            error=f"inbox agent does not handle action type '{msg.type}'"
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
        batch = await fetch_gmail_messages(
            sender,
            since=now.isoformat(),
            until=(now + timedelta(days=7)).isoformat(),
        )
        items = [_msg_to_item(m, sender) for m in batch.records[:5]]
    except Exception:
        items = []

    if items:
        from core.cards import priorities_carousel
        await ctx.send(sender, priorities_carousel([i.model_dump() for i in items]))
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
