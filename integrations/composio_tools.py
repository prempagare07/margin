from __future__ import annotations

# Composio account connections and tool loading. Owner: Alex.

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from shared.protocol import Item

log = logging.getLogger(__name__)

now = datetime.now(timezone.utc)


def _d(days: int, hours: int = 0) -> str:
    return (now + timedelta(days=days, hours=hours)).isoformat()


def _fake_emails(user_id: str) -> list[Item]:
    return [
        Item(item_id=str(uuid4()), user_id=user_id, kind="email_action",
             title="Midterm review session – BYAC 110",
             quote="The TA confirmed the review will be held in BYAC 110 on Thursday at 6pm.",
             source="gmail", source_id="gmail-fake-1", status="open"),
    ]


def _fake_gcal(user_id: str) -> tuple[list[Item], list[list[str]]]:
    # 5 free slots: 9am–12pm each weekday this week
    monday = now - timedelta(days=now.weekday())
    slots = []
    for offset in range(5):
        day = monday + timedelta(days=offset)
        start = day.replace(hour=9, minute=0, second=0, microsecond=0)
        end = day.replace(hour=12, minute=0, second=0, microsecond=0)
        slots.append([start.isoformat(), end.isoformat()])
    return [], slots


async def fetch_emails(user_id: str) -> list[Item]:
    key = os.getenv("COMPOSIO_API_KEY", "")
    if not key:
        return _fake_emails(user_id)

    try:
        from composio.sdk import Composio  # type: ignore

        def _sync() -> list[Item]:
            client = Composio(api_key=key)
            result = client.tools.execute(
                "GMAIL_FETCH_EMAILS",
                connected_account_id=user_id,
                params={"max_results": 10},
            )
            emails = result.get("data", {}).get("messages", []) if isinstance(result, dict) else []
            items: list[Item] = []
            for msg in emails:
                subject = msg.get("subject") or msg.get("snippet", "Email")
                body = msg.get("body") or msg.get("snippet", "")
                items.append(Item(
                    item_id=str(uuid4()),
                    user_id=user_id,
                    kind="email_action",
                    title=subject,
                    quote=body[:200] if body else None,
                    source="gmail",
                    source_id=msg.get("id", str(uuid4())),
                    status="open",
                ))
            return items

        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception as exc:
        log.warning("composio fetch_emails failed: %s", exc)
        return []


async def fetch_gcal_events(
    user_id: str, window_start: str, window_end: str
) -> tuple[list[Item], list[list[str]]]:
    key = os.getenv("COMPOSIO_API_KEY", "")
    if not key:
        return _fake_gcal(user_id)

    try:
        from composio.sdk import Composio  # type: ignore

        def _sync() -> tuple[list[Item], list[list[str]]]:
            client = Composio(api_key=key)
            result = client.tools.execute(
                "GOOGLECALENDAR_LIST_EVENTS",
                connected_account_id=user_id,
                params={"time_min": window_start, "time_max": window_end},
            )
            events_raw = result.get("data", {}).get("items", []) if isinstance(result, dict) else []
            items: list[Item] = []
            for ev in events_raw:
                start = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date", "")
                end = (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date", "")
                items.append(Item(
                    item_id=str(uuid4()),
                    user_id=user_id,
                    kind="event",
                    title=ev.get("summary", "Event"),
                    starts_at=start,
                    ends_at=end,
                    location=ev.get("location"),
                    source="gcal",
                    source_id=ev.get("id", str(uuid4())),
                    status="open",
                ))
            # Compute free slots: gaps >= 1h, 9am–9pm, weekdays
            free_slots: list[list[str]] = []
            try:
                sorted_events = sorted(items, key=lambda x: x.starts_at or "")
                ws = datetime.fromisoformat(window_start)
                we = datetime.fromisoformat(window_end)
                cursor = ws
                while cursor < we:
                    if cursor.weekday() < 5:
                        day_start = cursor.replace(hour=9, minute=0, second=0, microsecond=0)
                        day_end = cursor.replace(hour=21, minute=0, second=0, microsecond=0)
                        slot_start = day_start
                        for item in sorted_events:
                            if not item.starts_at or not item.ends_at:
                                continue
                            ev_start = datetime.fromisoformat(item.starts_at)
                            ev_end = datetime.fromisoformat(item.ends_at)
                            if ev_end <= slot_start or ev_start >= day_end:
                                continue
                            if ev_start > slot_start and (ev_start - slot_start).total_seconds() >= 3600:
                                free_slots.append([slot_start.isoformat(), ev_start.isoformat()])
                            slot_start = max(slot_start, ev_end)
                        if slot_start < day_end and (day_end - slot_start).total_seconds() >= 3600:
                            free_slots.append([slot_start.isoformat(), day_end.isoformat()])
                    cursor += timedelta(days=1)
            except Exception as slot_exc:
                log.warning("free slot computation failed: %s", slot_exc)
            return items, free_slots

        return await asyncio.get_event_loop().run_in_executor(None, _sync)
    except Exception as exc:
        log.warning("composio fetch_gcal_events failed: %s", exc)
        return [], []


async def create_calendar_event(
    user_id: str, title: str, start_iso: str, end_iso: str, description: str = ""
) -> dict:
    key = os.getenv("COMPOSIO_API_KEY", "")
    if not key:
        return {"ok": False, "error": "no composio key"}

    try:
        from composio.sdk import Composio  # type: ignore

        def _sync() -> dict:
            client = Composio(api_key=key)
            return client.tools.execute(
                "GOOGLECALENDAR_CREATE_EVENT",
                connected_account_id=user_id,
                params={
                    "summary": title,
                    "start": {"dateTime": start_iso},
                    "end": {"dateTime": end_iso},
                    "description": description,
                },
            )

        return await asyncio.get_event_loop().run_in_executor(None, _sync) or {}
    except Exception as exc:
        log.warning("composio create_calendar_event failed: %s", exc)
        return {"ok": False, "error": str(exc)}


async def create_email_draft(user_id: str, to: str, subject: str, body: str) -> dict:
    key = os.getenv("COMPOSIO_API_KEY", "")
    if not key:
        return {"ok": False, "error": "no composio key"}

    try:
        from composio.sdk import Composio  # type: ignore

        def _sync() -> dict:
            client = Composio(api_key=key)
            return client.tools.execute(
                "GMAIL_CREATE_EMAIL_DRAFT",
                connected_account_id=user_id,
                params={"to": to, "subject": subject, "body": body},
            )

        return await asyncio.get_event_loop().run_in_executor(None, _sync) or {}
    except Exception as exc:
        log.warning("composio create_email_draft failed: %s", exc)
        return {"ok": False, "error": str(exc)}
