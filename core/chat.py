"""Chat protocol factory, send helpers and selection parser. Owner: Prem."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    MetadataContent,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from core.llm import fast
from core.observability import set_user as _set_user

logger = logging.getLogger(__name__)

OnText = Callable[[Context, str, str, dict | None], Awaitable[None]]


def make_chat_protocol(
    agent_name: str,
    on_text: OnText,
    greeting: str | ChatMessage,
) -> Protocol:
    """Build a chat Protocol for any Margin agent.

    Acks every ChatMessage; never replies to acks; sends greeting on
    StartSessionContent; calls on_text(ctx, sender, text, selection) for
    TextContent where selection is the parsed card action dict or None.
    """
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def _handle(ctx: Context, sender: str, msg: ChatMessage) -> None:
        logger.info("[%s] ChatMessage from %s", agent_name, sender[:20])
        _set_user(sender)
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=datetime.now(timezone.utc),
                acknowledged_msg_id=msg.msg_id,
            ),
        )

        if any(isinstance(c, StartSessionContent) for c in msg.content):
            if isinstance(greeting, ChatMessage):
                await ctx.send(sender, greeting)
            else:
                await ctx.send(sender, send_text_msg(greeting))
            return

        # Form clients may send selection/values as metadata instead of text.
        # Never treat an outgoing card_payload as an inbound selection.
        for item in msg.content:
            if isinstance(item, MetadataContent):
                raw = item.metadata.get("selection") or item.metadata.get("card_selection")
                if raw:
                    selection = await parse_selection(raw)
                    if selection:
                        for key in ("values", "form_data", "fields"):
                            if key in item.metadata:
                                try:
                                    selection[key] = json.loads(item.metadata[key])
                                except (TypeError, ValueError):
                                    selection[key] = None  # validation will reject it
                        await on_text(ctx, sender, "", selection)
                        return

        for item in msg.content:
            if isinstance(item, TextContent):
                text = (item.text or "").strip()
                if not text:
                    continue
                selection = await parse_selection(text)
                try:
                    await on_text(ctx, sender, text, selection)
                except Exception as exc:
                    logger.exception("on_text error in %s: %s", agent_name, exc)
                    await ctx.send(sender, send_text_msg(
                        "Sorry, something went wrong. Please try again."
                    ))
                return

    @proto.on_message(ChatAcknowledgement)
    async def _handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
        pass  # never reply to acks

    return proto


async def parse_selection(text: str) -> dict | None:
    """Extract a card-action dict from inbound text.

    JSON first; if it looks like a card action return it directly.
    Otherwise ask fast() to extract the action fields.
    Returns None if text is plain user prose.
    """
    if not text:
        return None
    stripped = re.sub(r"^(?:@\S+\s+)+", "", text.strip())

    # Direct JSON from @mention
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict) and "action" in data:
                return data
            if isinstance(data, dict) and isinstance(data.get("selection"), dict) and "action" in data["selection"]:
                return {**{k: v for k, v in data.items() if k != "selection"}, **data["selection"]}
        except json.JSONDecodeError:
            pass

    # Never send phone/budget fields to a model or infer financial values from prose.
    if stripped.lower() == "connect":
        return {"action": "connect"}
    if re.search(r'\b(monthly_income|weekly_cap|savings_goal)\b|["\']?(phone|bills)["\']?\s*[:=]', stripped, re.I):
        return {"action": "connect", "_invalid": True}

    # Only call fast() if the text looks like a card selection
    card_keywords = (
        "action", "plan_id", "item_id", "event_id", "block",
        "approve", "cancel", "open_task", "plan_task",
        "draft_ext", "snooze", "add_event", "check_links", "linked",
        "upgrade",
    )
    lower = stripped.lower()
    if not any(kw in lower for kw in card_keywords):
        return None

    try:
        result = await fast(
            [
                {
                    "role": "system",
                    "content": (
                        "Extract card action fields from the text. "
                        "Return a JSON object with any of: action, plan_id, item_id, "
                        "event_id, block, app. Return {} if not a card action."
                    ),
                },
                {"role": "user", "content": stripped},
            ],
            timeout=8,
        )
        if isinstance(result, str):
            data = json.loads(result) if result.strip().startswith("{") else {}
        else:
            data = result
        return data if isinstance(data, dict) and data else None
    except Exception:
        return None


def send_text_msg(text: str) -> ChatMessage:
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=[TextContent(type="text", text=text)],
    )


async def send_text(ctx: Context, to: str, text: str) -> None:
    await ctx.send(to, send_text_msg(text))


async def send_card(
    ctx: Context,
    to: str,
    kind: str,
    payload: dict,
    narration: str = "",
) -> None:
    from core.cards import card_message
    await ctx.send(to, card_message(kind, payload, narration))
