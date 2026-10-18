"""Stored plans, review and execute-once approval. Owner: Prem."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory fallback (no DB or DB down)
# ---------------------------------------------------------------------------
_mem_plans: dict[str, dict] = {}
_mem_execs: dict[tuple, dict] = {}


def _mem_upsert_plan(plan_id: str, user_id: str, actions: list[dict]) -> None:
    _mem_plans[plan_id] = {
        "plan_id": plan_id, "user_id": user_id,
        "actions": actions, "status": "pending",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "approved_at": None, "executed_at": None,
    }
    for a in actions:
        _mem_execs[(plan_id, a["action_id"])] = {
            "plan_id": plan_id, "action_id": a["action_id"],
            "user_id": user_id, "status": "pending",
            "result": None, "error": None,
        }


def _db_available() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


async def _repo():
    """Return db.repo, using memory only when no database is configured."""
    if not _db_available():
        return None
    from db import repo
    await repo.get_pool()  # configured database failures must remain visible
    return repo


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_plan(user_id: str, actions: list[dict]) -> str:
    plan_id = str(uuid4())
    r = await _repo()
    if r is None:
        _mem_upsert_plan(plan_id, user_id, actions)
        return plan_id
    try:
        await r.insert_plan(plan_id, user_id, actions)
        return plan_id
    except Exception:
        logger.warning("create_plan: configured database write failed")
        raise


async def get_plan(plan_id: str, user_id: str) -> dict | None:
    r = await _repo()
    if r is None:
        return _mem_plans.get(plan_id)
    try:
        row = await r.get_plan(plan_id, user_id)
        if row is not None:
            return dict(row)
        return None
    except Exception as exc:
        logger.warning("get_plan DB error: %s", exc)
        return _mem_plans.get(plan_id)


async def pending_plan_for(user_id: str) -> dict | None:
    r = await _repo()
    if r is None:
        for p in reversed(list(_mem_plans.values())):
            if p["user_id"] == user_id and p["status"] == "pending":
                return p
        return None
    try:
        row = await r.pending_plan_for(user_id)
        return dict(row) if row is not None else None
    except Exception as exc:
        logger.warning("pending_plan_for DB error: %s", exc)
        for p in reversed(list(_mem_plans.values())):
            if p["user_id"] == user_id and p["status"] == "pending":
                return p
        return None


async def approve(plan_id: str, user_id: str) -> bool:
    r = await _repo()
    if r is None:
        p = _mem_plans.get(plan_id)
        if p and p["user_id"] == user_id and p["status"] == "pending":
            p["status"] = "approved"
            p["approved_at"] = datetime.now(timezone.utc).isoformat()
            return True
        return False
    try:
        return await r.approve_plan(plan_id, user_id)
    except Exception as exc:
        logger.warning("approve DB error: %s", exc)
        return False


async def cancel(plan_id: str, user_id: str) -> bool:
    r = await _repo()
    if r is None:
        p = _mem_plans.get(plan_id)
        if p and p["user_id"] == user_id and p["status"] == "pending":
            p["status"] = "cancelled"
            return True
        return False
    try:
        return await r.update_plan_status(plan_id, user_id, "cancelled")
    except Exception as exc:
        logger.warning("cancel DB error: %s", exc)
        return False


async def mark_done(plan_id: str, user_id: str) -> None:
    r = await _repo()
    if r is None:
        p = _mem_plans.get(plan_id)
        if p and p["user_id"] == user_id:
            p["status"] = "done"
            p["executed_at"] = datetime.now(timezone.utc).isoformat()
        return
    try:
        await r.update_plan_status(plan_id, user_id, "done")
    except Exception as exc:
        logger.warning("mark_done DB error: %s", exc)


async def claim_action(plan_id: str, action_id: str, user_id: str) -> bool:
    r = await _repo()
    if r is None:
        key = (plan_id, action_id)
        ex = _mem_execs.get(key)
        if ex and ex["status"] == "pending":
            ex["status"] = "running"
            return True
        return False
    try:
        return await r.claim_action_execution(plan_id, action_id, user_id)
    except Exception as exc:
        logger.warning("claim_action DB error: %s", exc)
        return False


async def complete_action(
    plan_id: str, action_id: str, user_id: str,
    result: dict,
    error: str | None = None,
) -> None:
    r = await _repo()
    if r is None:
        key = (plan_id, action_id)
        if key in _mem_execs:
            status = "failed" if error else "succeeded"
            _mem_execs[key].update({"status": status, "result": result, "error": error})
        return
    try:
        status = "failed" if error else "succeeded"
        await r.finish_action_execution(plan_id, action_id, user_id, status=status, result=result, error=error)
    except Exception as exc:
        logger.warning("complete_action DB error: %s", exc)
