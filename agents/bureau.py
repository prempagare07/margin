"""Starts all five Margin agents. Owner: Prem."""
import json
import os
import threading
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

# Each agent gets its own port so the local inspector can reach it.
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


def _make_agent(name: str, seed_env: str, port: int) -> Agent:
    seed = os.environ[seed_env]
    agent = Agent(name=name, seed=seed, port=port, mailbox=True)
    agent.include(_make_stub_proto(name), publish_manifest=True)
    return agent


AGENTS = [_make_agent(name, seed_env, port) for name, seed_env, port in AGENT_CONFIGS]


def _print_addresses() -> None:
    addresses = {}
    print("\n=== Margin Agents ===")
    for agent, (_, _, port) in zip(AGENTS, AGENT_CONFIGS):
        inspector = AGENTVERSE_BASE.format(port=port, address=agent.address)
        print(f"  {agent.name:<20} {agent.address}  port={port}")
        print(f"  {'':20} inspector: {inspector}")
        addresses[agent.name] = agent.address
    print()
    os.makedirs("agents", exist_ok=True)
    with open("agents/addresses.json", "w") as f:
        json.dump(addresses, f, indent=2)
    print("  Addresses written to agents/addresses.json")
    print("=====================\n")


if __name__ == "__main__":
    _print_addresses()
    threads = [
        threading.Thread(target=agent.run, daemon=True)
        for agent in AGENTS[1:]  # specialists in background threads
    ]
    for t in threads:
        t.start()
    AGENTS[0].run()  # margin (orchestrator) runs on main thread
