"""Photon channel adapter — render ChatMessages as SMS text. Owner: Prem."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from integrations.photon_client import send_message

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Render a ChatMessage to plain SMS text
# ---------------------------------------------------------------------------

def render_text(msg: ChatMessage) -> str:
    """Convert a ChatMessage (possibly a card) to a short SMS string."""
    parts: list[str] = []

    for c in msg.content:
        if isinstance(c, TextContent) and c.text:
            parts.append(c.text.strip())
        elif isinstance(c, MetadataContent):
            parts.extend(_render_card(c.metadata))

    return "\n".join(p for p in parts if p)


def _render_card(meta: dict) -> list[str]:
    kind = meta.get("card_kind", "")
    raw = meta.get("card_payload", "{}")
    try:
        payload = json.loads(raw)
    except Exception:
        return []

    if kind == "form":
        return [
            payload.get("title", ""),
            "Reply with: " + ", ".join(f["label"] for f in payload.get("fields", [])),
        ]

    if kind == "carousel":
        lines = [payload.get("title", "")]
        for i, item in enumerate(payload.get("items", [])[:5]):
            label = chr(65 + i)
            lines.append(f"{label}. {item.get('title', '')} — {item.get('subtitle', '')}")
        return lines

    if kind == "review":
        lines = [payload.get("title", "Review plan")]
        for row in payload.get("summary_rows", []):
            lines.append(f"• {row.get('label', '')}: {row.get('value', '')}")
        lines.append("Reply YES to approve or NO to cancel.")
        return lines

    if kind == "detail":
        lines = [payload.get("title", "")]
        for row in payload.get("summary_rows", []):
            lines.append(f"{row.get('label', '')}: {row.get('value', '')}")
        ctas = payload.get("ctas", [])
        if ctas:
            options = " / ".join(c.get("label", "") for c in ctas[:3])
            lines.append(f"Reply: {options}")
        return lines

    if kind == "custom":
        return _render_section(payload.get("root", {}))

    return []


def _render_section(node: dict) -> list[str]:
    lines: list[str] = []
    t = node.get("type", "")
    if t == "section":
        if node.get("title"):
            lines.append(node["title"])
        for child in node.get("children", []):
            lines.extend(_render_section(child))
    elif t in ("text", "heading"):
        v = node.get("value") or node.get("text", "")
        if v:
            lines.append(v)
    elif t == "button":
        lines.append(f"[{node.get('label', '')}]")
    return lines


# ---------------------------------------------------------------------------
# PhotonCtx — a minimal stand-in for uAgent Context for the Photon channel
# ---------------------------------------------------------------------------

class PhotonCtx:
    """Wraps a phone number so graph.handle() can call ctx.send()."""

    def __init__(self, phone: str) -> None:
        self._phone = phone
        self._sent: list[str] = []   # for testing

    async def send(self, _to: str, message: Any) -> None:
        """Called by graph nodes. Extracts text and sends via Photon."""
        if isinstance(message, ChatMessage):
            text = render_text(message)
        else:
            text = str(message)

        if not text.strip():
            return

        self._sent.append(text)
        await send_message(self._phone, text)


# ---------------------------------------------------------------------------
# Inbound dispatcher (called by web/server.py)
# ---------------------------------------------------------------------------

async def handle_inbound(
    phone: str, text: str, user_id: str | None = None, *, verified_webhook: bool = False,
) -> None:
    """Dispatch an inbound Photon message through the graph.

    Requires verified webhook provenance, then resolves the verified phone owner.
    A LINK challenge is the only command accepted before ownership is verified.
    The legacy user_id argument is ignored; it cannot override DB ownership.
    """
    from core import graph
    from core.chat import parse_selection
    from core.onboarding import InvalidSubmission, link_code_hash, phone_number
    from db import repo

    if not verified_webhook:
        return
    try:
        phone = phone_number(phone)
    except InvalidSubmission:
        return
    if not isinstance(text, str):
        return
    # Possession comes from the signed provider event's sender, not from JSON.
    if re.match(r"^\s*LINK(?:\s|$)", text, re.I):
        match = re.fullmatch(r"\s*LINK\s+([A-Za-z0-9_-]{32})\s*", text, re.I)
        try:
            linked = await repo.verify_phone_link(phone, link_code_hash(match[1])) if match else None
        except Exception:
            logger.warning("photon: phone verification storage unavailable")
            await send_message(phone, "Phone linking is temporarily unavailable. Please try again shortly.")
            return
        await send_message(phone, "Your phone is linked to Margin. You can now ask me questions here." if linked else
                           "That link code is invalid, expired, or unavailable for this number. Submit the Connect form in ASI:One for a new code.")
        return
    try:
        row = await repo.get_user_by_phone(phone)
        uid = row["user_id"] if row else None
    except Exception:
        uid = None
    if not uid:
        logger.warning("photon: unlinked sender — dropping message")
        return
    selection = await parse_selection(text)

    ctx = PhotonCtx(phone)
    await graph.handle(
        ctx=ctx,
        user_id=uid,
        text=text,
        channel="photon",
        selection=selection,
        sender=phone,
    )
