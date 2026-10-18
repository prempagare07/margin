"""Starts all five Margin agents via uagents Bureau. Owner: Prem."""
import json
import os

from dotenv import load_dotenv

load_dotenv()

from core.observability import init as _sentry_init
_sentry_init()

from uagents import Bureau

from agents.academic import agent as _academic
from agents.inbox import agent as _inbox
from agents.life import agent as _life
from agents.margin_agent import agent as _margin
from agents.planner import agent as _planner

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


def _print_addresses() -> None:
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

    bureau = Bureau()
    bureau.add(_margin)
    bureau.add(_academic)
    bureau.add(_inbox)
    bureau.add(_planner)
    bureau.add(_life)
    bureau.run()
