"""Starts all five Margin agents in one Bureau. Owner: Prem."""
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Bureau, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

PORT = 8000
AGENTVERSE_BASE = "https://agentverse.ai/inspect/?uri=http%3A//127.0.0.1%3A{port}&address={address}"


def _make_stub_proto(name: str) -> Protocol:
    """Stub chat protocol: ack every message, reply '<name> online' to text."""
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
        pass  # never reply to acks

    return proto


def _make_agent(name: str, seed_env: str) -> Agent:
    seed = os.environ[seed_env]
    agent = Agent(name=name, seed=seed, port=PORT, mailbox=True)
    proto = _make_stub_proto(name)
    agent.include(proto, publish_manifest=True)
    return agent


margin = _make_agent("margin", "MARGIN_SEED")
academic = _make_agent("margin-academic", "ACADEMIC_SEED")
inbox = _make_agent("margin-inbox", "INBOX_SEED")
planner = _make_agent("margin-planner", "PLANNER_SEED")
life = _make_agent("margin-life", "LIFE_SEED")

AGENTS = [margin, academic, inbox, planner, life]

bureau = Bureau(port=PORT)
for a in AGENTS:
    bureau.add(a)


def _print_addresses() -> None:
    addresses = {}
    print("\n=== Margin Agents ===")
    for a in AGENTS:
        inspector = AGENTVERSE_BASE.format(port=PORT, address=a.address)
        print(f"  {a.name:<20} {a.address}")
        print(f"  {'':20} inspector: {inspector}")
        addresses[a.name] = a.address
    print()
    os.makedirs("agents", exist_ok=True)
    with open("agents/addresses.json", "w") as f:
        json.dump(addresses, f, indent=2)
    print("  Addresses written to agents/addresses.json")
    print("=====================\n")


if __name__ == "__main__":
    _print_addresses()
    bureau.run()
