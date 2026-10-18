"""uAgents payment protocol — seller role. Mirrors the horoscope-agent pattern."""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from uagents import Context, Protocol
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    RejectPayment,
    payment_protocol_spec,
)

logger = logging.getLogger(__name__)

CommitHandler = Callable[[Context, str, CommitPayment], Awaitable[None]]
RejectHandler = Callable[[Context, str, RejectPayment], Awaitable[None]]


def build_payment_proto(on_commit: CommitHandler, on_reject: RejectHandler) -> Protocol:
    """Return a payment Protocol with seller role, ready to agent.include()."""
    proto = Protocol(spec=payment_protocol_spec, role="seller")

    @proto.on_message(CommitPayment)
    async def _on_commit(ctx: Context, sender: str, msg: CommitPayment):
        logger.info("payment: CommitPayment from %s transaction_id=%s", sender, msg.transaction_id)
        await on_commit(ctx, sender, msg)

    @proto.on_message(RejectPayment)
    async def _on_reject(ctx: Context, sender: str, msg: RejectPayment):
        logger.info("payment: RejectPayment from %s reason=%s", sender, msg.reason)
        await on_reject(ctx, sender, msg)

    return proto
