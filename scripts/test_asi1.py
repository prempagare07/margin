"""Quick live test: spin up a one-shot uAgent and exchange messages with margin.

Usage:
    python scripts/test_asi1.py
    python scripts/test_asi1.py "plan my week"
"""
from __future__ import annotations
import asyncio, json, sys, uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, StartSessionContent,
    TextContent, MetadataContent, chat_protocol_spec,
)

MARGIN = "agent1qdfscf9rpmmpq40uyd694uz6dve2e0jza02s5dez07lk5wnf3dnn6cnndt4"
MESSAGES = [
    "hi",
    sys.argv[1] if len(sys.argv) > 1 else "what is my budget?",
    "what happened today",
]

received: list[str] = []
_done = asyncio.Event()


def _strip_metadata(msg: ChatMessage) -> str:
    """Extract plain text or card JSON from a ChatMessage."""
    parts = []
    for c in msg.content:
        if isinstance(c, TextContent):
            parts.append(c.text)
        elif isinstance(c, MetadataContent):
            kind = (c.metadata or {}).get("card_kind")
            payload_raw = (c.metadata or {}).get("card_payload", "")
            if kind:
                try:
                    payload = json.loads(payload_raw)
                    parts.append(f"[CARD:{kind}] {json.dumps(payload, indent=2)[:400]}")
                except Exception:
                    parts.append(f"[CARD:{kind}]")
    return "\n".join(parts) or "(empty)"


tester = Agent(
    name="margin-tester",
    seed="margin-tester-seed-2026-lahacks",
    port=8097,
    mailbox=True,
)

proto = Protocol(spec=chat_protocol_spec, role="user")
_session = str(uuid.uuid4())
_msg_queue: asyncio.Queue = asyncio.Queue()


@proto.on_message(ChatMessage)
async def _on_reply(ctx: Context, sender: str, msg: ChatMessage) -> None:
    await ctx.send(sender, ChatAcknowledgement(
        timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id
    ))
    text = _strip_metadata(msg)
    print(f"\n  ← margin: {text}\n")
    received.append(text)
    await _msg_queue.put("reply")


@proto.on_message(ChatAcknowledgement)
async def _on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement) -> None:
    pass


tester.include(proto, publish_manifest=True)


@tester.on_event("startup")
async def _run(ctx: Context) -> None:
    # Start session
    # Wait for mailbox registration before sending — registration takes ~10-15s
    await asyncio.sleep(15)
    await ctx.send(MARGIN, ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid.uuid4(),
        content=[StartSessionContent(type="start-session")],
    ))
    await asyncio.sleep(5)

    for i, text in enumerate(MESSAGES):
        print(f"\n  → [{i+1}/{len(MESSAGES)}] sending: {text!r}")
        await ctx.send(MARGIN, ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid.uuid4(),
            content=[TextContent(type="text", text=text)],
        ))
        try:
            await asyncio.wait_for(_msg_queue.get(), timeout=30)
        except asyncio.TimeoutError:
            print("  ✗ TIMEOUT — no reply in 30s")

    print("\n--- Test complete ---")
    print(f"Sent: {len(MESSAGES)}  Received: {len(received)}")
    _done.set()


@tester.on_event("shutdown")
async def _shutdown(ctx: Context) -> None:
    pass


if __name__ == "__main__":
    print(f"Starting tester agent → margin @ {MARGIN}")
    print(f"Messages to send: {MESSAGES}\n")
    tester.run()
