"""Stored plans, review and execute-once approval. Owner: Prem."""
from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# DB pool (lazy, optional)
# ---------------------------------------------------------------------------
_pool = None
_UNAVAILABLE = object()  # sentinel: tried once and failed


async def _get_pool():
    global _pool
    if _pool is _UNAVAILABLE:
        return None
    if _pool is not None:
        return _pool
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        _pool = _UNAVAILABLE
        return None
    try:
        from psycopg_pool import AsyncConnectionPool
        from psycopg.rows import dict_row
        candidate = AsyncConnectionPool(
            url, min_size=1, max_size=4,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        await candidate.open(wait=True, timeout=5)
        _pool = candidate
        logger.info("approval: DB pool connected")
        return _pool
    except Exception as exc:
        logger.warning("approval: DB pool failed (%s) — using in-memory store", exc)
        _pool = _UNAVAILABLE
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def create_plan(user_id: str, actions: list[dict]) -> str:
    """Persist a pending plan; return plan_id."""
    plan_id = str(uuid4())
    pool = await _get_pool()
    if pool is None:
        _mem_upsert_plan(plan_id, user_id, actions)
        return plan_id
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO plans (plan_id, user_id, actions) VALUES (%s, %s, %s)",
                (plan_id, user_id, json.dumps(actions)),
            )
            for a in actions:
                await conn.execute(
                    "INSERT INTO action_executions (plan_id, action_id, user_id)"
                    " VALUES (%s, %s, %s)",
                    (plan_id, a["action_id"], user_id),
                )
        return plan_id
    except Exception as exc:
        logger.warning("create_plan DB error: %s — using memory", exc)
        _mem_upsert_plan(plan_id, user_id, actions)
        return plan_id


async def get_plan(plan_id: str, user_id: str) -> dict | None:
    pool = await _get_pool()
    if pool is None:
        return _mem_plans.get(plan_id)
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM plans WHERE plan_id = %s AND user_id = %s",
                (plan_id, user_id),
            )
            return await cur.fetchone()
    except Exception as exc:
        logger.warning("get_plan DB error: %s", exc)
        return _mem_plans.get(plan_id)


async def pending_plan_for(user_id: str) -> dict | None:
    """Most recent pending plan for a user, or None."""
    pool = await _get_pool()
    if pool is None:
        for p in reversed(list(_mem_plans.values())):
            if p["user_id"] == user_id and p["status"] == "pending":
                return p
        return None
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM plans WHERE user_id = %s AND status = 'pending'"
                " ORDER BY created_at DESC LIMIT 1",
                (user_id,),
            )
            return await cur.fetchone()
    except Exception as exc:
        logger.warning("pending_plan_for DB error: %s", exc)
        for p in reversed(list(_mem_plans.values())):
            if p["user_id"] == user_id and p["status"] == "pending":
                return p
        return None


async def _set_status(
    plan_id: str, user_id: str,
    from_status: str, to_status: str,
    ts_col: str | None = None,
) -> bool:
    pool = await _get_pool()
    if pool is None:
        p = _mem_plans.get(plan_id)
        if p and p["user_id"] == user_id and p["status"] == from_status:
            p["status"] = to_status
            if ts_col:
                p[ts_col] = datetime.now(timezone.utc).isoformat()
            return True
        return False
    try:
        col_part = f", {ts_col} = now()" if ts_col else ""
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE plans SET status = %s{col_part}"
                " WHERE plan_id = %s AND user_id = %s AND status = %s",
                (to_status, plan_id, user_id, from_status),
            )
            return (cur.rowcount or 0) == 1
    except Exception as exc:
        logger.warning("_set_status DB error: %s", exc)
        return False


async def approve(plan_id: str, user_id: str) -> bool:
    return await _set_status(plan_id, user_id, "pending", "approved", "approved_at")


async def cancel(plan_id: str, user_id: str) -> bool:
    return await _set_status(plan_id, user_id, "pending", "cancelled")


async def mark_done(plan_id: str, user_id: str) -> None:
    await _set_status(plan_id, user_id, "approved", "done", "executed_at")


async def claim_action(plan_id: str, action_id: str, user_id: str) -> bool:
    """Atomically set action pending→running. Returns True if claimed."""
    pool = await _get_pool()
    if pool is None:
        key = (plan_id, action_id)
        ex = _mem_execs.get(key)
        if ex and ex["status"] == "pending":
            ex["status"] = "running"
            return True
        return False
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE action_executions"
                " SET status = 'running', started_at = now()"
                " WHERE plan_id = %s AND action_id = %s"
                " AND user_id = %s AND status = 'pending'",
                (plan_id, action_id, user_id),
            )
            return (cur.rowcount or 0) == 1
    except Exception as exc:
        logger.warning("claim_action DB error: %s", exc)
        return False


async def complete_action(
    plan_id: str, action_id: str, user_id: str,
    result: dict,
    error: str | None = None,
) -> None:
    status = "failed" if error else "succeeded"
    pool = await _get_pool()
    if pool is None:
        key = (plan_id, action_id)
        if key in _mem_execs:
            _mem_execs[key].update({"status": status, "result": result, "error": error})
        return
    try:
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE action_executions"
                " SET status = %s, result = %s, error = %s, completed_at = now()"
                " WHERE plan_id = %s AND action_id = %s AND user_id = %s",
                (status, json.dumps(result), error, plan_id, action_id, user_id),
            )
    except Exception as exc:
        logger.warning("complete_action DB error: %s", exc)
