"""7 am morning brief and 9 pm nudge sweep. Owner: Prem."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core import approval
from core.graph import _fake_items, _fake_slots
from integrations.photon_client import send_message

logger = logging.getLogger(__name__)

FAKE = os.environ.get("FAKE_SPECIALISTS", "0") == "1"

# ---------------------------------------------------------------------------
# DB helpers (best-effort; fall back to in-memory maps)
# ---------------------------------------------------------------------------

# In-memory user registry for when DB is unavailable.
# Populated by register_user() called during onboarding.
_users: dict[str, dict] = {}   # user_id → {phone, tz, monthly_income, weekly_cap, savings_goal}
_briefs: dict[str, dict] = {}  # brief_id → {user_id, letters, sent_at}
_nudges: dict[str, dict] = {}  # dedupe_key → nudge dict


def register_user(
    user_id: str,
    phone: str,
    tz: str = "America/Phoenix",
    monthly_income: float = 0.0,
    weekly_cap: float | None = None,
    savings_goal: float = 0.0,
) -> None:
    """Register or update a user in the in-memory store (called on connect_form submit)."""
    _users[user_id] = {
        "user_id": user_id,
        "phone": phone,
        "tz": tz,
        "monthly_income": monthly_income,
        "weekly_cap": weekly_cap,
        "savings_goal": savings_goal,
    }
    logger.info("scheduler: registered user %s phone=%s", user_id, phone)


async def _all_active_users() -> list[dict]:
    """Return all users who have phone + opted in.

    Tries DB first; falls back to in-memory registry.
    """
    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT u.user_id, u.phone, u.tz, b.monthly_income,"
                    " b.weekly_cap, b.savings_goal"
                    " FROM users u LEFT JOIN budgets b USING (user_id)"
                    " WHERE u.phone_verified AND u.text_opted_in_at IS NOT NULL"
                )
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning("_all_active_users DB error: %s", exc)

    return list(_users.values())


async def _brief_sent_today(user_id: str, tz: str) -> bool:
    """Return True if a brief was already sent to this user today (local time)."""
    local_today = datetime.now(ZoneInfo(tz)).date()
    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM briefs"
                    " WHERE user_id = %s"
                    " AND sent_at AT TIME ZONE %s >= %s::date"
                    " LIMIT 1",
                    (user_id, tz, str(local_today)),
                )
                return (await cur.fetchone()) is not None
        except Exception as exc:
            logger.warning("_brief_sent_today DB error: %s", exc)

    for b in _briefs.values():
        if b["user_id"] == user_id:
            sent_local = datetime.fromisoformat(b["sent_at"]).astimezone(ZoneInfo(tz)).date()
            if sent_local == local_today:
                return True
    return False


async def _record_brief(user_id: str, letters: dict[str, str]) -> str:
    brief_id = str(uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO briefs (brief_id, user_id, letters) VALUES (%s, %s, %s)",
                    (brief_id, user_id, json.dumps(letters)),
                )
            return brief_id
        except Exception as exc:
            logger.warning("_record_brief DB error: %s", exc)

    _briefs[brief_id] = {"brief_id": brief_id, "user_id": user_id,
                         "letters": letters, "sent_at": now_iso}
    return brief_id


async def _nudge_already_sent(user_id: str, dedupe_key: str) -> bool:
    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT 1 FROM nudges"
                    " WHERE user_id = %s AND dedupe_key = %s AND sent_at IS NOT NULL"
                    " LIMIT 1",
                    (user_id, dedupe_key),
                )
                return (await cur.fetchone()) is not None
        except Exception as exc:
            logger.warning("_nudge_already_sent DB error: %s", exc)

    n = _nudges.get(dedupe_key)
    return n is not None and n.get("sent_at") is not None


async def _record_nudge(user_id: str, item_id: str | None, dedupe_key: str) -> None:
    nudge_id = str(uuid4())
    now_iso = datetime.now(timezone.utc).isoformat()
    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                await conn.execute(
                    "INSERT INTO nudges (nudge_id, user_id, item_id, dedupe_key,"
                    " sent_at, options) VALUES (%s, %s, %s, %s, now(), %s)"
                    " ON CONFLICT (user_id, dedupe_key) DO UPDATE SET sent_at = now()",
                    (nudge_id, user_id, item_id, dedupe_key, json.dumps([])),
                )
            return
        except Exception as exc:
            logger.warning("_record_nudge DB error: %s", exc)

    _nudges[dedupe_key] = {"nudge_id": nudge_id, "user_id": user_id,
                           "item_id": item_id, "dedupe_key": dedupe_key,
                           "sent_at": now_iso}


# ---------------------------------------------------------------------------
# Safe-to-spend calculation
# ---------------------------------------------------------------------------

async def _safe_to_spend(user: dict) -> float:
    """
    safe = (monthly_income - bills_remaining - savings_goal - flexible_spent) / weeks_left
    Capped by weekly_cap if set. Falls back to monthly_income / 4 if no budget row.
    """
    monthly_income = float(user.get("monthly_income") or 0)
    savings_goal = float(user.get("savings_goal") or 0)
    weekly_cap = user.get("weekly_cap")
    if weekly_cap is not None:
        weekly_cap = float(weekly_cap)

    flexible_spent = 0.0
    bills_remaining = 0.0

    pool = await approval._get_pool()
    if pool is not None and pool is not approval._UNAVAILABLE:
        try:
            async with pool.connection() as conn:
                # Flexible spend this month
                cur = await conn.execute(
                    "SELECT public.flexible_spent_this_month(%s)", (user["user_id"],)
                )
                row = await cur.fetchone()
                if row:
                    flexible_spent = float(list(row.values())[0] or 0)

                # Bills still due this month
                now_local = datetime.now(ZoneInfo(user.get("tz", "America/Phoenix")))
                cur2 = await conn.execute(
                    "SELECT COALESCE(SUM(amount), 0) AS total FROM bills"
                    " WHERE user_id = %s AND due_day >= %s",
                    (user["user_id"], now_local.day),
                )
                row2 = await cur2.fetchone()
                if row2:
                    bills_remaining = float(list(row2.values())[0] or 0)
        except Exception as exc:
            logger.warning("_safe_to_spend DB error: %s", exc)

    now = datetime.now(ZoneInfo(user.get("tz", "America/Phoenix")))
    # Weeks left = roughly (days until end of month) / 7, min 1
    import calendar
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_left = max(days_in_month - now.day + 1, 1)
    weeks_left = max(days_left / 7, 1)

    available = monthly_income - bills_remaining - savings_goal - flexible_spent
    safe = max(available / weeks_left, 0)

    if weekly_cap is not None:
        safe = min(safe, weekly_cap)

    return round(safe, 2)


# ---------------------------------------------------------------------------
# Morning brief (7 am local)
# ---------------------------------------------------------------------------

async def send_morning_brief(user: dict) -> None:
    """Build and send the 7 am brief for one user."""
    user_id = user["user_id"]
    phone = user.get("phone", "")
    tz = user.get("tz", "America/Phoenix")

    if not phone:
        return

    if await _brief_sent_today(user_id, tz):
        logger.debug("brief already sent today for %s", user_id)
        return

    # Gather items
    items = _fake_items() if FAKE else _fake_items()   # real path wired in P7+

    if not items:
        return

    # Assign letters A-E
    top = items[:5]
    letters: dict[str, str] = {}
    lines = ["Good morning! Here's your day:\n"]
    for i, item in enumerate(top):
        label = chr(65 + i)           # A, B, C…
        letters[label] = item["item_id"]
        due = ""
        if item.get("due_at"):
            try:
                dt = datetime.fromisoformat(item["due_at"].replace("Z", "+00:00"))
                due = " (due " + dt.astimezone(ZoneInfo(tz)).strftime("%-m/%-d") + ")"
            except Exception:
                pass
        lines.append(f"{label}. {item['title']}{due}")

    safe = await _safe_to_spend(user)
    lines.append(f"\n💰 Safe to spend this week: ${safe:.0f}")
    lines.append("\nReply with a letter to get details, or 'plan my week' to schedule.")

    text = "\n".join(lines)
    await send_message(phone, text, idempotency_key=f"brief:{user_id}:{datetime.now(ZoneInfo(tz)).date()}")
    await _record_brief(user_id, letters)
    logger.info("morning brief sent to %s (%s items)", user_id, len(top))


async def run_morning_briefs() -> None:
    """Called at 7 am America/Phoenix. Sends brief to all active users."""
    from core.observability import monitor as obs_monitor
    @obs_monitor("margin-morning-brief")
    async def _inner():
        logger.info("morning brief sweep starting")
        users = await _all_active_users()
        logger.info("morning brief: %d active users", len(users))
        for user in users:
            try:
                await send_morning_brief(user)
            except Exception as exc:
                logger.exception("morning brief error for %s: %s", user.get("user_id"), exc)
    await _inner()


# ---------------------------------------------------------------------------
# Evening nudge sweep (9 pm local)
# ---------------------------------------------------------------------------

async def send_evening_nudge(user: dict) -> None:
    """Send up to 3 nudge items for one user."""
    user_id = user["user_id"]
    phone = user.get("phone", "")
    tz = user.get("tz", "America/Phoenix")

    if not phone:
        return

    items = _fake_items() if FAKE else _fake_items()
    # Only open items with upcoming deadlines
    open_items = [it for it in items if it.get("status") == "open"][:3]

    if not open_items:
        return

    today = datetime.now(ZoneInfo(tz)).date()
    dedupe_key = f"nudge:{user_id}:{today}"
    if await _nudge_already_sent(user_id, dedupe_key):
        return

    lines = ["Evening check-in 🌙\n"]
    for i, item in enumerate(open_items, 1):
        due = ""
        if item.get("due_at"):
            try:
                dt = datetime.fromisoformat(item["due_at"].replace("Z", "+00:00"))
                days_left = (dt.astimezone(ZoneInfo(tz)).date() - today).days
                if days_left == 0:
                    due = " — due TODAY"
                elif days_left == 1:
                    due = " — due tomorrow"
                else:
                    due = f" — due in {days_left}d"
            except Exception:
                pass
        lines.append(f"{i}. {item['title']}{due}")

    safe = await _safe_to_spend(user)
    lines.append(f"\n💰 ${safe:.0f} left to spend this week.")
    lines.append("\nReply 1/2/3 for details or 'plan my week' to schedule.")

    text = "\n".join(lines)
    await send_message(phone, text, idempotency_key=f"nudge:{user_id}:{today}")
    await _record_nudge(user_id, None, dedupe_key)
    logger.info("evening nudge sent to %s", user_id)


async def run_evening_nudges() -> None:
    """Called at 9 pm America/Phoenix. Sends nudge to all active users."""
    from core.observability import monitor as obs_monitor
    @obs_monitor("margin-evening-nudge")
    async def _inner():
        logger.info("evening nudge sweep starting")
        users = await _all_active_users()
        for user in users:
            try:
                await send_evening_nudge(user)
            except Exception as exc:
                logger.exception("evening nudge error for %s: %s", user.get("user_id"), exc)
    await _inner()


# ---------------------------------------------------------------------------
# Scheduler lifecycle
# ---------------------------------------------------------------------------

_scheduler: AsyncIOScheduler | None = None


def start_scheduler() -> AsyncIOScheduler:
    """Create and start the APScheduler instance.

    Must be called from within a running asyncio event loop (e.g. agent startup).
    All times are America/Phoenix so the cron fires correctly regardless of
    server timezone.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = AsyncIOScheduler(timezone="America/Phoenix")

    _scheduler.add_job(
        run_morning_briefs,
        CronTrigger(hour=7, minute=0, timezone="America/Phoenix"),
        id="morning_brief",
        replace_existing=True,
        misfire_grace_time=300,   # fire up to 5 min late if process was down
    )
    _scheduler.add_job(
        run_evening_nudges,
        CronTrigger(hour=21, minute=0, timezone="America/Phoenix"),
        id="evening_nudge",
        replace_existing=True,
        misfire_grace_time=300,
    )

    _scheduler.start()
    logger.info("scheduler started: morning brief at 07:00, nudge at 21:00 America/Phoenix")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("scheduler stopped")
    _scheduler = None
