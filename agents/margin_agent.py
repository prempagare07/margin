"""Margin orchestrator agent. Owner: Prem."""
from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from uagents import Agent, Context

from channels.scheduler import start_scheduler, stop_scheduler
from core import gather as gather_bus
from core import graph
from core.cards import connect_form
from core.chat import make_chat_protocol
from shared.protocol import ActionRequest, ActionResult, GatherResult

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
    start_scheduler()


@agent.on_event("shutdown")
async def _shutdown(ctx: Context) -> None:
    stop_scheduler()


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
# GatherResult handler — resolves futures created in graph.gather_node
# ---------------------------------------------------------------------------

@agent.on_message(GatherResult)
async def _on_gather_result(ctx: Context, sender: str, msg: GatherResult) -> None:
    gather_bus.resolve(msg.request_id, msg)


# ---------------------------------------------------------------------------
# ActionResult handler — resolves futures created in graph.execute_node
# ---------------------------------------------------------------------------

@agent.on_message(ActionResult)
async def _on_action_result(ctx: Context, sender: str, msg: ActionResult) -> None:
    gather_bus.resolve(msg.action_id, msg)


# ---------------------------------------------------------------------------
# Standalone run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    agent.run()
