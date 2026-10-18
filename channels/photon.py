"""Photon iMessage channel adapter. Owner: Prem."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from integrations.photon_client import send_message

logger = logging.getLogger(__name__)


def render_text(msg: ChatMessage) -> str:
    parts: list[str] = []
    for content in msg.content:
        if isinstance(content, TextContent) and content.text:
            parts.append(content.text.strip())
        elif isinstance(content, MetadataContent):
            parts.extend(_render_card(content.metadata))
    return "\n".join(part for part in parts if part)


def _render_card(meta: dict) -> list[str]:
    kind = meta.get("card_kind", "")
    try:
        payload = json.loads(meta.get("card_payload", "{}"))
    except Exception:
        return []
    if kind == "form":
        return [payload.get("title", ""), "Reply with: " + ", ".join(
            field["label"] for field in payload.get("fields", []) if "label" in field
        )]
    if kind == "carousel":
        lines = [payload.get("title", "")]
        for index, item in enumerate(payload.get("items", [])[:5]):
            lines.append(f"{chr(65 + index)}. {item.get('title', '')} — {item.get('subtitle', '')}")
        return lines
    if kind == "review":
        lines = [payload.get("title", "Review plan")]
        lines.extend(f"• {row.get('label', '')}: {row.get('value', '')}" for row in payload.get("summary_rows", []))
        lines.append("Reply YES to approve or NO to cancel.")
        return lines
    if kind == "detail":
        lines = [payload.get("title", "")]
        lines.extend(f"{row.get('label', '')}: {row.get('value', '')}" for row in payload.get("summary_rows", []))
        if payload.get("ctas"):
            lines.append("Reply: " + " / ".join(item.get("label", "") for item in payload["ctas"][:3]))
        return lines
    if kind == "custom":
        return _render_section(payload.get("root", {}))
    return []


def _render_section(node: dict) -> list[str]:
    lines: list[str] = []
    kind = node.get("type", "")
    if kind == "section":
        if node.get("title"):
            lines.append(node["title"])
        for child in node.get("children", []):
            lines.extend(_render_section(child))
    elif kind in ("text", "heading"):
        value = node.get("value") or node.get("text", "")
        if value:
            lines.append(value)
    elif kind == "button":
        lines.append(f"[{node.get('label', '')}]")
    return lines


def _reply_key(message_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{message_id}:reply:{sequence}".encode()).hexdigest()
    return f"margin-reply-{digest}"


class PhotonCtx:
    """Minimal graph context that replies in one Photon conversation."""

    def __init__(self, space_id: str, sender_id: str, message_id: str) -> None:
        self._space_id = space_id
        self._sender_id = sender_id
        self._message_id = message_id
        self._sent: list[str] = []

    async def send(self, _to: str, message: Any) -> None:
        text = render_text(message) if isinstance(message, ChatMessage) else str(message)
        if not text.strip():
            return
        sequence = len(self._sent)
        self._sent.append(text)
        await send_message(
            self._space_id,
            self._sender_id,
            text,
            idempotency_key=_reply_key(self._message_id, sequence),
            parent_message_id=self._message_id,
        )


async def send_text(user_id: str, body: str, *, idempotency_key: str | None = None) -> dict | None:
    """Send a scheduled text only after the user has initiated a conversation."""
    from db import repo

    row = await repo.get_user(user_id)
    if not row or not row.get("photon_space_id") or not row.get("photon_user_id"):
        logger.info("photon scheduled send skipped: no saved conversation")
        return None
    key = idempotency_key or "margin-send-" + hashlib.sha256(
        f"{user_id}:{body}".encode()
    ).hexdigest()
    return await send_message(
        row["photon_space_id"], row["photon_user_id"], body, idempotency_key=key
    )


async def handle_inbound(
    sender_handle: str,
    text: str,
    message_id: str,
    conversation_id: str,
    agent_user_id: str,
    *,
    verified_webhook: bool = False,
) -> None:
    """Verify LINK commands or dispatch a linked conversation to the graph."""
    from core import graph
    from core.chat import parse_selection
    from core.onboarding import InvalidSubmission, link_code_hash, phone_number
    from db import repo

    if not verified_webhook or not all(
        isinstance(value, str) and value for value in (text, message_id, conversation_id, agent_user_id)
    ):
        return
    try:
        phone = phone_number(sender_handle)
    except InvalidSubmission:
        return

    link_prefix = re.match(r"^\s*LINK(?:\s|$)", text, re.I)
    if link_prefix:
        match = re.fullmatch(r"\s*LINK\s+([A-Z0-9]{6})\s*", text, re.I)
        try:
            linked = await repo.verify_phone_link(
                phone, link_code_hash(match.group(1).upper())
            ) if match else None
            if linked:
                await repo.upsert_user(
                    linked,
                    photon_space_id=conversation_id,
                    photon_user_id=agent_user_id,
                    text_opted_in_at=datetime.now(timezone.utc),
                )
        except Exception:
            logger.warning("photon link storage unavailable")
            await send_message(
                conversation_id, agent_user_id,
                "Phone linking is temporarily unavailable. Please try again shortly.",
                idempotency_key=_reply_key(message_id, 0), parent_message_id=message_id,
            )
            return
        reply = (
            "Your phone is linked to Margin. Daily briefs are now on."
            if linked else
            "That link code is invalid, expired, or for a different number. Submit the Connect form for a new code."
        )
        await send_message(
            conversation_id, agent_user_id, reply,
            idempotency_key=_reply_key(message_id, 0), parent_message_id=message_id,
        )
        return

    try:
        row = await repo.get_user_by_phone(phone)
    except Exception:
        row = None
    if not row or not row.get("photon_space_id") or not row.get("photon_user_id"):
        logger.info("photon inbound skipped: sender or conversation is not linked")
        return
    selection = await parse_selection(text)
    ctx = PhotonCtx(row["photon_space_id"], row["photon_user_id"], message_id)
    await graph.handle(
        ctx=ctx,
        user_id=row["user_id"],
        text=text,
        channel="photon",
        selection=selection,
        sender=row["user_id"],
    )
