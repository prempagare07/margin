"""Starts all five Margin agents, each on its own port and event loop. Owner: Prem."""
import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from core.observability import init as _sentry_init
_sentry_init()

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

AGENT_CONFIGS = [
    ("margin",          "MARGIN_SEED",   8000),
    ("margin-academic", "ACADEMIC_SEED", 8001),
    ("margin-inbox",    "INBOX_SEED",    8002),
    ("margin-planner",  "PLANNER_SEED",  8003),
    ("margin-life",     "LIFE_SEED",     8004),
]
AGENTVERSE_BASE = (
    "https://agentverse.ai/inspect/"
    "?uri=http%3A//127.0.0.1%3A{port}&address={address}"
)


def _make_stub_proto(name: str) -> Protocol:
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def _handle(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=datetime.now(timezone.utc),
                acknowledged_msg_id=msg.msg_id,
            ),
        )
        if any(isinstance(c, StartSessionContent) for c in msg.content):
            await ctx.send(
                sender,
                ChatMessage(
                    timestamp=datetime.now(timezone.utc),
                    msg_id=uuid4(),
                    content=[TextContent(type="text", text=f"{name} online")],
                ),
            )
            return
        for item in msg.content:
            if isinstance(item, TextContent) and item.text.strip():
                await ctx.send(
                    sender,
                    ChatMessage(
                        timestamp=datetime.now(timezone.utc),
                        msg_id=uuid4(),
                        content=[TextContent(type="text", text=f"{name} online")],
                    ),
                )
                return

    @proto.on_message(ChatAcknowledgement)
    async def _handle_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    return proto


def _run_agent(name: str, seed_env: str, port: int) -> None:
    """Create and run one agent in its own event loop (called inside a thread)."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    seed = os.environ[seed_env]
    agent = Agent(name=name, seed=seed, port=port, mailbox=True)
    agent.include(_make_stub_proto(name), publish_manifest=True)
    try:
        loop.run_until_complete(agent.run_async())
    finally:
        loop.close()


def _print_addresses() -> None:
    """Derive addresses from seeds and print inspector links."""
    from uagents_core.identity import Identity

    addresses: dict[str, str] = {}
    print("\n=== Margin Agents ===")
    for name, seed_env, port in AGENT_CONFIGS:
        seed = os.environ[seed_env]
        address = Identity.from_seed(seed=seed, index=0).address
        inspector = AGENTVERSE_BASE.format(port=port, address=address)
        print(f"  {name:<20} {address}  port={port}")
        print(f"  {'':20} inspector: {inspector}")
        addresses[name] = address
    print()
    os.makedirs("agents", exist_ok=True)
    with open("agents/addresses.json", "w") as f:
        json.dump(addresses, f, indent=2)
    print("  Addresses written to agents/addresses.json")
    print("=====================\n")


if __name__ == "__main__":
    _print_addresses()

    threads = []
    for cfg in AGENT_CONFIGS[1:]:
        t = threading.Thread(target=_run_agent, args=cfg, daemon=True)
        t.start()
        threads.append(t)

    # Run margin (orchestrator) on the main thread
    _run_agent(*AGENT_CONFIGS[0])
