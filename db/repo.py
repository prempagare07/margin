"""Async storage for Margin. Load .env in the entrypoint, before using this module.

Each event loop owns its pool (the agent launcher uses multiple threads/loops).
Call close_pool() on that loop at shutdown. Writes commit on success and roll
back on failure; provider calls must happen outside these transactions.
"""

import asyncio
import calendar
import json
import os
import re
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from shared.protocol import ActionRequest, GatherResult, Item

_pools: dict[asyncio.AbstractEventLoop, AsyncConnectionPool] = {}
_PROVIDERS = {"canvas", "gmail", "gcal", "slack", "plaid"}
_USER_FIELDS = {
    "tz", "plan", "pro_until", "photon_user_id", "photon_space_id", "text_opted_in_at"
}
_ITEM_FIELDS = tuple(Item.__fields__)
_ITEM_TIMES = ("due_at", "starts_at", "ends_at")


async def get_pool() -> AsyncConnectionPool:
    """Lazily open a small pool on the calling event loop, never at import time."""
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None or pool.closed:
        dsn = os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL is required")
        pool = AsyncConnectionPool(
            dsn, open=False, min_size=1, max_size=4, timeout=8,
            reconnect_timeout=8,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": "-c timezone=UTC",
            },
        )
        _pools[loop] = pool
    # open() is concurrency-safe; wait() allows only one waiter. Connections
    # acquired below already wait (up to the pool timeout) for readiness.
    await pool.open()
    return pool


async def close_pool() -> None:
    """Close only the calling loop's pool, before shutting down that loop."""
    pool = _pools.pop(asyncio.get_running_loop(), None)
    if pool is not None:
        await pool.close()


def _datetime(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("Timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return _datetime(value).isoformat().replace("+00:00", "Z")


def _json(value: object) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _jsonb(value: object) -> Jsonb:
    _json(value)  # Validate before starting a transaction.
    return Jsonb(value, dumps=_json)


def _money(value: float | Decimal) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite() or amount < 0:
        raise ValueError("Amounts must be finite and nonnegative")
    return amount


def _phone(value: str) -> str:
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value):
        raise ValueError("Phone must be normalized E.164, including country code")
    return value


async def _one(query: str, params: tuple = (), *, conn=None) -> dict | None:
    if conn is not None:
        return await (await conn.execute(query, params)).fetchone()
    async with (await get_pool()).connection() as conn:
        return await (await conn.execute(query, params)).fetchone()


async def _all(query: str, params: tuple = ()) -> list[dict]:
    async with (await get_pool()).connection() as conn:
        return await (await conn.execute(query, params)).fetchall()


async def upsert_user(user_id: str, *, conn=None, **kwargs) -> None:
    """Update explicit profile fields; phone verification uses separate helpers."""
    if not user_id or kwargs.keys() - _USER_FIELDS:
        raise ValueError("Invalid user ID or unsupported profile fields")
    if "tz" in kwargs:
        ZoneInfo(kwargs["tz"])
    for field in ("pro_until", "text_opted_in_at"):
        if kwargs.get(field) is not None:
            kwargs[field] = _datetime(kwargs[field])
    async def write(connection):
        # Insert only the identity before applying profile fields. PostgreSQL
        # checks a proposed INSERT before ON CONFLICT, so inserting an opt-in
        # timestamp without the existing phone would otherwise fail its CHECK.
        await connection.execute(
            "INSERT INTO public.users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        if kwargs:
            query = sql.SQL("UPDATE public.users SET {} WHERE user_id = %s").format(
                sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(key)) for key in kwargs)
            )
            await connection.execute(query, (*kwargs.values(), user_id))

    if conn is not None:
        await write(conn)
    else:
        async with (await get_pool()).connection() as connection:
            await write(connection)


async def get_user(user_id: str) -> dict | None:
    return await _one("SELECT * FROM public.users WHERE user_id = %s", (user_id,))


async def get_user_by_phone(phone: str) -> dict | None:
    return await _one(
        "SELECT * FROM public.users WHERE phone = %s AND phone_verified", (_phone(phone),)
    )


async def list_active_text_users() -> list[dict]:
    """Return verified, opted-in users with optional budget settings."""
    return await _all(
        """SELECT u.user_id, u.phone, u.tz, b.monthly_income,
                  b.weekly_cap, b.savings_goal
           FROM public.users u
           LEFT JOIN public.budgets b USING (user_id)
           WHERE u.phone_verified AND u.text_opted_in_at IS NOT NULL
           ORDER BY u.user_id"""
    )


async def set_phone(user_id: str, phone: str, code_hash: str, expires: datetime, *, conn=None) -> None:
    if not code_hash:
        raise ValueError("A verification code hash is required")
    row = await _one(
        """UPDATE public.users SET phone = %s, phone_verified = false,
               text_opted_in_at = NULL, phone_code_hash = %s, phone_code_expires_at = %s
           WHERE user_id = %s RETURNING user_id""",
        (_phone(phone), code_hash, _datetime(expires), user_id), conn=conn,
    )
    if row is None:
        raise LookupError("User not found")


async def verify_phone(user_id: str, code_hash: str) -> bool:
    """Compare the caller's code hash and expiry atomically before verifying."""
    async with (await get_pool()).connection() as conn:
        await conn.execute(
            """UPDATE public.users SET phone_code_hash = NULL, phone_code_expires_at = NULL
               WHERE user_id = %s AND phone_code_expires_at <= now()""", (user_id,),
        )
        cur = await conn.execute(
            """UPDATE public.users SET phone_verified = true,
                   phone_code_hash = NULL, phone_code_expires_at = NULL
               WHERE user_id = %s AND phone_code_hash = %s
                 AND phone_code_expires_at > now() RETURNING user_id""", (user_id, code_hash),
        )
        return await cur.fetchone() is not None


async def save_onboarding(
    user_id: str, *, phone: str, monthly_income: Decimal, savings_goal: Decimal,
    weekly_cap: Decimal | None, bills: list[dict], code_hash: str, expires: datetime,
) -> bool:
    """Save one validated Connect submission atomically; return phone_verified.

    Bills are upserted by name; omitted bills are preserved. A repeated submit
    never revokes an already verified, unchanged number or modifies entitlements.
    """
    _phone(phone)
    async with (await get_pool()).connection() as conn:
        await upsert_user(user_id, conn=conn)
        user = await _one("SELECT * FROM public.users WHERE user_id = %s FOR UPDATE", (user_id,), conn=conn)
        await save_budget(user_id, monthly_income, savings_goal, weekly_cap, conn=conn)
        for bill in bills:
            await save_bill(user_id, bill["name"], bill["amount"], bill["due_day"], conn=conn)
        verified = user["phone"] == phone and user["phone_verified"]
        if not verified:
            await set_phone(user_id, phone, code_hash, expires, conn=conn)
        return bool(verified)


async def verify_phone_link(phone: str, code_hash: str) -> str | None:
    """Consume a possession challenge from a signed inbound Photon message.

    The sender phone must equal the submitted number. No caller-supplied user ID
    participates in this lookup. Verifying does not opt the user into reminders.
    """
    from psycopg.errors import UniqueViolation

    _phone(phone)
    if not re.fullmatch(r"[a-f0-9]{64}", code_hash):
        return None
    try:
        async with (await get_pool()).connection() as conn:
            rows = await (await conn.execute(
                """SELECT user_id FROM public.users
                   WHERE phone = %s AND phone_code_hash = %s AND NOT phone_verified
                     AND phone_code_expires_at > now() FOR UPDATE""", (phone, code_hash),
            )).fetchall()
            if len(rows) != 1:
                return None
            user_id = rows[0]["user_id"]
            await conn.execute(
                """UPDATE public.users SET phone_verified = true,
                   phone_code_hash = NULL, phone_code_expires_at = NULL
                   WHERE user_id = %s""", (user_id,),
            )
            return user_id
    except UniqueViolation:
        # Another user already owns this verified number. Never steal a link.
        return None


def _cipher() -> Fernet:
    key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is required for connection secrets")
    return Fernet(key.encode())


async def save_connection(
    user_id: str, provider: str, *, composio_account_id: str | None = None,
    secret: dict | None = None, status: str = "active",
) -> None:
    """Replace a connection snapshot. None explicitly clears that optional field."""
    if provider not in _PROVIDERS or not status:
        raise ValueError("Invalid connection provider or status")
    if secret is not None and not isinstance(secret, dict):
        raise ValueError("Connection secret must be a JSON object")
    encrypted = _cipher().encrypt(_json(secret).encode()) if secret is not None else None
    await _one(
        """INSERT INTO public.connections
               (user_id, provider, composio_account_id, secret_enc, status)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (user_id, provider) DO UPDATE SET
               composio_account_id = EXCLUDED.composio_account_id,
               secret_enc = EXCLUDED.secret_enc, status = EXCLUDED.status, updated_at = now()
           RETURNING user_id""", (user_id, provider, composio_account_id, encrypted, status),
    )


async def get_connection(user_id: str, provider: str) -> dict | None:
    if provider not in _PROVIDERS:
        raise ValueError("Invalid connection provider")
    row = await _one(
        "SELECT * FROM public.connections WHERE user_id = %s AND provider = %s",
        (user_id, provider),
    )
    if row is not None:
        encrypted = row.pop("secret_enc")
        row["secret"] = json.loads(_cipher().decrypt(bytes(encrypted))) if encrypted is not None else None
    return row


async def set_connection_status(user_id: str, provider: str, account_id: str | None, status: str) -> None:
    """Update status without overwriting concurrent settings or a newly selected account."""
    if provider not in _PROVIDERS or status not in {"active", "relink_required", "pending"}:
        raise ValueError("Invalid connection status")
    await _one("""UPDATE public.connections SET status = %s, updated_at = now()
                  WHERE user_id = %s AND provider = %s AND composio_account_id IS NOT DISTINCT FROM %s
                  RETURNING user_id""", (status, user_id, provider, account_id))


def _prepare_item(raw: dict) -> dict:
    data = dict(raw)
    for field in _ITEM_TIMES:
        if data.get(field) is not None:
            data[field] = _iso(_datetime(data[field]))
    item = Item.model_validate(data).model_dump()
    for field in _ITEM_TIMES:
        if item[field] is not None:
            item[field] = _datetime(item[field])
    if item["ends_at"] is not None and (
        item["starts_at"] is None or item["ends_at"] < item["starts_at"]
    ):
        raise ValueError("An event end requires a start at or before it")
    for field in ("cost_usd", "est_hours"):
        if item[field] is not None:
            _money(item[field])
    return item


def _item_row(row: dict) -> dict:
    row = dict(row)
    for field in (*_ITEM_TIMES, "fetched_at"):
        if row.get(field) is not None:
            row[field] = _iso(row[field])
    if row.get("cost_usd") is not None:
        row["cost_usd"] = float(row["cost_usd"])
    return row


async def upsert_items(items: list[dict]) -> None:
    """Refresh source snapshots atomically, retaining stable IDs and local state."""
    values = [_prepare_item(item) for item in items]
    if not values:
        return
    updates = [field for field in _ITEM_FIELDS if field not in ("item_id", "user_id", "source", "source_id", "status")]
    query = sql.SQL("""INSERT INTO public.items ({}) VALUES ({})
        ON CONFLICT (user_id, source, source_id) DO UPDATE SET {},
        status = CASE WHEN items.status IN ('done', 'snoozed') THEN items.status
                      ELSE EXCLUDED.status END, fetched_at = now()""").format(
        sql.SQL(", ").join(map(sql.Identifier, _ITEM_FIELDS)),
        sql.SQL(", ").join(sql.Placeholder() for _ in _ITEM_FIELDS),
        sql.SQL(", ").join(sql.SQL("{0} = EXCLUDED.{0}").format(sql.Identifier(f)) for f in updates),
    )
    async with (await get_pool()).connection() as conn:
        async with conn.cursor() as cursor:
            await cursor.executemany(query, [tuple(item[f] for f in _ITEM_FIELDS) for item in values])


async def last_items_by_source(user_id: str, source: str) -> list[dict]:
    return [_item_row(row) for row in await _all(
        """SELECT * FROM public.items WHERE user_id = %s AND source = %s
           ORDER BY fetched_at DESC, item_id""", (user_id, source),
    )]


async def items_in_window(user_id: str, start: str, end: str) -> list[dict]:
    """Return deadlines in [start, end) and events overlapping it, including points."""
    lower, upper = _datetime(start), _datetime(end)
    if upper <= lower:
        raise ValueError("Window end must follow its start")
    rows = await _all(
        """SELECT * FROM public.items WHERE user_id = %s AND (
              (due_at >= %s AND due_at < %s)
           OR (starts_at >= %s AND starts_at < %s)
           OR (starts_at < %s AND ends_at > %s))
           ORDER BY COALESCE(starts_at, due_at), item_id""",
        (user_id, lower, upper, lower, upper, lower, lower),
    )
    return [_item_row(row) for row in rows]


async def get_item(user_id: str, item_id: str) -> dict | None:
    """Resolve a source only through an item owned by the authenticated user."""
    row = await _one("SELECT * FROM public.items WHERE user_id = %s AND item_id = %s", (user_id, item_id))
    return _item_row(row) if row is not None else None


async def cached_gather_result(
    request_id: str, agent: str, user_id: str, sources: list[str],
    window_start: str, window_end: str, *, error: str,
) -> GatherResult:
    """Build an explicitly stale fallback. Free slots require Planner recomputation."""
    rows = [row for row in await items_in_window(user_id, window_start, window_end)
            if row["source"] in sources]
    return GatherResult(
        request_id=request_id, agent=agent, items=[Item.model_validate(row) for row in rows],
        stale_since=_iso(min(_datetime(row["fetched_at"]) for row in rows)) if rows else None,
        error=error,
    )


def _actions(plan_id: str, user_id: str, actions: list[dict]) -> list[dict]:
    if not isinstance(actions, list):
        raise ValueError("Plan actions must be an array")
    result, ids = [], set()
    for action in actions:
        if not isinstance(action, dict) or set(action) != {"action_id", "type", "params"}:
            raise ValueError("Each action requires action_id, type and params only")
        action_id = action["action_id"]
        if not isinstance(action_id, str) or not action_id.strip() or action_id in ids:
            raise ValueError("Action IDs must be nonempty and unique within the plan")
        if not isinstance(action["params"], dict):
            raise ValueError("Action params must be a JSON object")
        ActionRequest(plan_id=plan_id, user_id=user_id, **action)
        ids.add(action_id)
        result.append(dict(action))
    _json(result)
    return result


async def insert_plan(plan_id: str, user_id: str, actions: list[dict]) -> None:
    actions = _actions(plan_id, user_id, actions)
    async with (await get_pool()).connection() as conn:
        cur = await conn.execute(
            """INSERT INTO public.plans (plan_id, user_id, actions) VALUES (%s, %s, %s)
               ON CONFLICT (plan_id) DO NOTHING RETURNING plan_id""",
            (plan_id, user_id, _jsonb(actions)),
        )
        if await cur.fetchone() is None:
            row = await (await conn.execute(
                "SELECT actions FROM public.plans WHERE plan_id = %s AND user_id = %s",
                (plan_id, user_id),
            )).fetchone()
            if row is None or row["actions"] != actions:
                raise ValueError("Plan ID is already in use; stored actions are immutable")


async def get_plan(plan_id: str, user_id: str) -> dict | None:
    return await _one("SELECT * FROM public.plans WHERE plan_id = %s AND user_id = %s", (plan_id, user_id))


async def pending_plan_for(user_id: str) -> dict | None:
    return await _one(
        """SELECT * FROM public.plans WHERE user_id = %s AND status = 'pending'
           ORDER BY created_at DESC, plan_id DESC LIMIT 1""", (user_id,),
    )


async def approve_plan(plan_id: str, user_id: str) -> bool:
    async with (await get_pool()).connection() as conn:
        row = await (await conn.execute(
            """UPDATE public.plans SET status = 'approved', approved_at = now()
               WHERE plan_id = %s AND user_id = %s AND status = 'pending' RETURNING actions""",
            (plan_id, user_id),
        )).fetchone()
        if row is None:
            return False
        for action in _actions(plan_id, user_id, row["actions"]):
            await conn.execute(
                """INSERT INTO public.action_executions (plan_id, action_id, user_id)
                   VALUES (%s, %s, %s)""", (plan_id, action["action_id"], user_id),
            )
        return True


async def update_plan_status(
    plan_id: str, user_id: str, status: Literal["cancelled", "done"],
) -> bool:
    if status == "cancelled":
        row = await _one(
            """UPDATE public.plans SET status = 'cancelled'
               WHERE plan_id = %s AND user_id = %s AND status = 'pending' RETURNING plan_id""",
            (plan_id, user_id),
        )
    elif status == "done":
        row = await _one(
            """UPDATE public.plans p SET status = 'done', executed_at = now()
               WHERE p.plan_id = %s AND p.user_id = %s AND p.status = 'approved'
                 AND NOT EXISTS (
                   SELECT 1 FROM jsonb_array_elements(p.actions) a WHERE NOT EXISTS (
                     SELECT 1 FROM public.action_executions e
                     WHERE e.plan_id = p.plan_id AND e.user_id = p.user_id
                       AND e.action_id = a->>'action_id' AND e.status IN ('succeeded', 'failed')))
                 AND NOT EXISTS (
                   SELECT 1 FROM public.action_executions e WHERE e.plan_id = p.plan_id
                     AND e.status NOT IN ('succeeded', 'failed'))
               RETURNING p.plan_id""", (plan_id, user_id),
        )
    else:
        raise ValueError("Use approve_plan for approval; other target statuses are cancelled or done")
    return row is not None


async def get_action_execution(plan_id: str, action_id: str, user_id: str) -> dict | None:
    return await _one(
        "SELECT * FROM public.action_executions WHERE plan_id = %s AND action_id = %s AND user_id = %s",
        (plan_id, action_id, user_id),
    )


async def list_action_executions(plan_id: str, user_id: str) -> list[dict]:
    return await _all(
        "SELECT * FROM public.action_executions WHERE plan_id = %s AND user_id = %s ORDER BY action_id",
        (plan_id, user_id),
    )


async def claim_action_execution(plan_id: str, action_id: str, user_id: str) -> bool:
    row = await _one(
        """UPDATE public.action_executions e SET status = 'running', started_at = now()
           WHERE e.plan_id = %s AND e.action_id = %s AND e.user_id = %s AND e.status = 'pending'
             AND EXISTS (SELECT 1 FROM public.plans p
               WHERE p.plan_id = e.plan_id AND p.user_id = e.user_id AND p.status = 'approved'
                 AND p.actions @> jsonb_build_array(jsonb_build_object('action_id', e.action_id)))
           RETURNING e.action_id""", (plan_id, action_id, user_id),
    )
    return row is not None


async def finish_action_execution(
    plan_id: str, action_id: str, user_id: str, *,
    status: Literal["succeeded", "failed", "unknown"], result: dict | None = None,
    error: str | None = None,
) -> None:
    if status not in {"succeeded", "failed", "unknown"} or (result is not None and not isinstance(result, dict)):
        raise ValueError("Invalid execution outcome or receipt")
    receipt = _jsonb(result) if result is not None else None
    async with (await get_pool()).connection() as conn:
        row = await (await conn.execute(
            """SELECT * FROM public.action_executions
               WHERE plan_id = %s AND action_id = %s AND user_id = %s FOR UPDATE""",
            (plan_id, action_id, user_id),
        )).fetchone()
        if row is None:
            raise LookupError("Action execution not found")
        if row["status"] == status and row["result"] == result and row["error"] == error:
            return
        if row["status"] not in {"running", "unknown"} or (row["status"] == "unknown" and status == "unknown"):
            raise ValueError("Execution must be claimed; terminal outcomes cannot be overwritten")
        await conn.execute(
            """UPDATE public.action_executions SET status = %s, result = %s, error = %s,
                   completed_at = CASE WHEN %s = 'unknown' THEN NULL ELSE now() END
               WHERE plan_id = %s AND action_id = %s AND user_id = %s""",
            (status, receipt, error, status, plan_id, action_id, user_id),
        )


async def insert_brief(brief_id: str, user_id: str, letters: dict[str, str]) -> None:
    if not isinstance(letters, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in letters.items()):
        raise ValueError("Brief letters must map strings to item IDs")
    async with (await get_pool()).connection() as conn:
        owned = await (await conn.execute(
            "SELECT item_id FROM public.items WHERE user_id = %s AND item_id = ANY(%s)",
            (user_id, list(letters.values())),
        )).fetchall()
        if {r["item_id"] for r in owned} != set(letters.values()):
            raise ValueError("Brief items must belong to this user")
        cur = await conn.execute(
            """INSERT INTO public.briefs (brief_id, user_id, letters) VALUES (%s, %s, %s)
               ON CONFLICT (brief_id) DO NOTHING RETURNING brief_id""", (brief_id, user_id, _jsonb(letters)),
        )
        if await cur.fetchone() is None:
            row = await (await conn.execute(
                "SELECT letters FROM public.briefs WHERE brief_id = %s AND user_id = %s", (brief_id, user_id),
            )).fetchone()
            if row is None or row["letters"] != letters:
                raise ValueError("Brief ID is already in use")


async def get_today_brief(user_id: str) -> dict | None:
    return await _one(
        """SELECT b.* FROM public.briefs b JOIN public.users u USING (user_id)
           WHERE b.user_id = %s
             AND (b.sent_at AT TIME ZONE u.tz)::date = (now() AT TIME ZONE u.tz)::date
           ORDER BY b.sent_at DESC, b.brief_id DESC LIMIT 1""", (user_id,),
    )


async def insert_nudge(
    nudge_id: str, user_id: str, item_id: str | None, options: list | dict, *,
    dedupe_key: str, plan_id: str | None = None, send_after: datetime | None = None,
) -> str:
    if not isinstance(dedupe_key, str) or not dedupe_key.strip() or not isinstance(options, (list, dict)):
        raise ValueError("Nudges require a nonempty dedupe_key and JSON array/object options")
    async with (await get_pool()).connection() as conn:
        row = await (await conn.execute(
            """INSERT INTO public.nudges
                   (nudge_id, user_id, item_id, options, dedupe_key, plan_id, send_after)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, dedupe_key) DO NOTHING RETURNING nudge_id""",
            (nudge_id, user_id, item_id, _jsonb(options), dedupe_key, plan_id,
             _datetime(send_after) if send_after is not None else None),
        )).fetchone()
        if row is None:
            row = await (await conn.execute(
                "SELECT nudge_id FROM public.nudges WHERE user_id = %s AND dedupe_key = %s",
                (user_id, dedupe_key),
            )).fetchone()
        return row["nudge_id"]


async def due_nudges(now: datetime) -> list[dict]:
    """For the trusted scheduler. Dispatch with one worker or provider idempotency."""
    return await _all(
        """SELECT * FROM public.nudges WHERE sent_at IS NULL
             AND (send_after IS NULL OR send_after <= %s)
           ORDER BY COALESCE(send_after, created_at), nudge_id""", (_datetime(now),),
    )


async def mark_nudge_sent(nudge_id: str, user_id: str, sent_at: datetime) -> None:
    sent_at = _datetime(sent_at)
    row = await _one(
        """UPDATE public.nudges SET sent_at = COALESCE(sent_at, %s)
           WHERE nudge_id = %s AND user_id = %s AND (send_after IS NULL OR send_after <= %s)
           RETURNING nudge_id""", (sent_at, nudge_id, user_id, sent_at),
    )
    if row is None:
        raise LookupError("Nudge not found or not due")


async def get_pending_nudge(user_id: str) -> dict | None:
    return await _one(
        """SELECT * FROM public.nudges WHERE user_id = %s AND sent_at IS NOT NULL
             AND reply IS NULL AND options NOT IN ('[]'::jsonb, '{}'::jsonb)
           ORDER BY sent_at DESC, nudge_id DESC LIMIT 1""", (user_id,),
    )


async def nudge_was_sent(user_id: str, dedupe_key: str) -> bool:
    """Return whether this user's idempotent nudge has already been sent."""
    return await _one(
        """SELECT nudge_id FROM public.nudges
           WHERE user_id = %s AND dedupe_key = %s AND sent_at IS NOT NULL
           LIMIT 1""",
        (user_id, dedupe_key),
    ) is not None


async def update_nudge(nudge_id: str, user_id: str, reply: str, action_taken: str) -> None:
    async with (await get_pool()).connection() as conn:
        row = await (await conn.execute(
            """SELECT * FROM public.nudges WHERE nudge_id = %s AND user_id = %s
               AND sent_at IS NOT NULL AND options NOT IN ('[]'::jsonb, '{}'::jsonb) FOR UPDATE""",
            (nudge_id, user_id),
        )).fetchone()
        if row is None:
            raise LookupError("No sent reply-enabled nudge found")
        if row["reply"] is not None:
            if row["reply"] == reply and row["action_taken"] == action_taken:
                return
            raise ValueError("Nudge has already been answered")
        await conn.execute(
            "UPDATE public.nudges SET reply = %s, action_taken = %s WHERE nudge_id = %s AND user_id = %s",
            (reply, action_taken, nudge_id, user_id),
        )


async def get_budget(user_id: str) -> dict | None:
    return await _one("SELECT * FROM public.budgets WHERE user_id = %s", (user_id,))


async def save_budget(user_id: str, monthly_income: float, savings_goal: float, weekly_cap: float | None, *, conn=None) -> None:
    await _one(
        """INSERT INTO public.budgets (user_id, monthly_income, savings_goal, weekly_cap)
           VALUES (%s, %s, %s, %s) ON CONFLICT (user_id) DO UPDATE SET
             monthly_income = EXCLUDED.monthly_income, savings_goal = EXCLUDED.savings_goal,
             weekly_cap = EXCLUDED.weekly_cap RETURNING user_id""",
        (user_id, _money(monthly_income), _money(savings_goal), _money(weekly_cap) if weekly_cap is not None else None), conn=conn,
    )


async def get_bills(user_id: str) -> list[dict]:
    return await _all("SELECT * FROM public.bills WHERE user_id = %s ORDER BY due_day, name", (user_id,))


async def save_bill(user_id: str, name: str, amount: float, due_day: int, *, conn=None) -> None:
    if not name.strip() or isinstance(due_day, bool) or not isinstance(due_day, int) or not 1 <= due_day <= 31:
        raise ValueError("Bills require a name and due_day between 1 and 31")
    await _one(
        """INSERT INTO public.bills (user_id, name, amount, due_day) VALUES (%s, %s, %s, %s)
           ON CONFLICT (user_id, name) DO UPDATE SET amount = EXCLUDED.amount, due_day = EXCLUDED.due_day
           RETURNING user_id""", (user_id, name, _money(amount), due_day), conn=conn,
    )


def _safe_to_spend(budget: dict, bills: list[dict], spent: Decimal, today: date) -> float:
    """fractional-weeks formula; negative amounts indicate a deficit."""
    month_end = calendar.monthrange(today.year, today.month)[1]
    bills_left = sum((Decimal(str(b["amount"])) for b in bills
                      if min(b["due_day"], month_end) >= today.day), Decimal(0))
    weeks_left = max(Decimal(1), Decimal(month_end - today.day + 1) / 7)
    safe = (Decimal(str(budget["monthly_income"])) - bills_left
            - Decimal(str(budget["savings_goal"])) - spent) / weeks_left
    if budget["weekly_cap"] is not None:
        safe = min(safe, Decimal(str(budget["weekly_cap"])))
    return float(safe.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


async def safe_to_spend(user_id: str) -> float:
    row = await _one(
        """SELECT b.*, public.flexible_spent_this_month(b.user_id) AS spent,
               (now() AT TIME ZONE 'America/Phoenix')::date AS today,
               (SELECT COALESCE(jsonb_agg(jsonb_build_object('amount', amount, 'due_day', due_day)), '[]')
                FROM public.bills WHERE user_id = b.user_id) AS bills
           FROM public.budgets b WHERE b.user_id = %s""", (user_id,),
    )
    if row is None:
        return 0.0
    return _safe_to_spend(row, row["bills"], row["spent"], row["today"])


async def delete_user(user_id: str) -> None:
    """Delete all data for a user (connections, items, plans, nudges, briefs, budget, bills, user row).

    Used by reset_node so the student can start fresh. This is intentionally destructive
    and irreversible — the caller must confirm before invoking.
    """
    async with (await get_pool()).connection() as conn:
        # Delete in dependency order to avoid FK violations (nulls cascade where possible,
        # but explicit order avoids surprises with future schema changes).
        for table in (
            "public.action_executions",
            "public.plans",
            "public.nudges",
            "public.briefs",
            "public.items",
            "public.connections",
            "public.bills",
            "public.budgets",
            "public.purchases",
            "public.transactions",
            "public.users",
        ):
            await conn.execute(
                sql.SQL("DELETE FROM {} WHERE user_id = %s").format(sql.Identifier(*table.split("."))),
                (user_id,),
            )
