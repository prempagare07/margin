"""Shared futures dict for orchestrator ↔ specialist request-response. Owner: Prem."""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# All futures live here. Keys are request_ids set by the orchestrator.
# Both the orchestrator's gather_node (which creates them) and its
# on_message(GatherResult) handler (which resolves them) run in the same
# event loop, so plain asyncio.Future works safely.
_futures: dict[str, asyncio.Future] = {}


def register(request_id: str) -> asyncio.Future:
    """Create and store a Future keyed by request_id. Returns it to await on."""
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    _futures[request_id] = fut
    return fut


def resolve(request_id: str, result) -> None:
    """Resolve the Future for request_id. No-op if already resolved or not found."""
    fut = _futures.pop(request_id, None)
    if fut is None:
        logger.debug("gather.resolve: unknown request_id %s", request_id)
        return
    if not fut.done():
        fut.set_result(result)
