"""Margin orchestrator agent. Owner: Prem."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from core import graph
from core.cards import connect_form
from core.chat import make_chat_protocol

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

agent = Agent(
    name="margin",
    seed=os.environ["MARGIN_SEED"],
    port=8000,
    mailbox=True,
)


@agent.on_event("startup")
async def _startup(ctx: Context) -> None:
    logger.info("margin agent online: %s", ctx.address)


# ---------------------------------------------------------------------------
# Chat protocol
# ---------------------------------------------------------------------------

async def _on_text(
    ctx: Context,
    sender: str,
    text: str,
    selection: dict | None,
) -> None:
    await graph.handle(
        ctx=ctx,
        user_id=sender,
        text=text,
        channel="asi1",
        selection=selection,
        sender=sender,
    )


_proto = make_chat_protocol(
    agent_name="margin",
    on_text=_on_text,
    greeting=connect_form(),
)

agent.include(_proto, publish_manifest=True)

# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
