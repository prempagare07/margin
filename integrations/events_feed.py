from __future__ import annotations

# SunDevilSync events feed client. Owners: Devin / Alex.

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from shared.protocol import Item

log = logging.getLogger(__name__)

now = datetime.now(timezone.utc)


def _d(days: int, hours: int = 0) -> str:
    return (now + timedelta(days=days, hours=hours)).isoformat()


def _fake_events(user_id: str) -> list[Item]:
    starts = now + timedelta(days=2, hours=5)
    ends = starts + timedelta(hours=2)
    return [
        Item(
            item_id=str(uuid4()),
            user_id=user_id,
            kind="event",
            title="ASU Club Dinner",
            starts_at=starts.isoformat(),
            ends_at=ends.isoformat(),
            due_at=starts.isoformat(),
            location="MU 202",
            source="sundevilsync",
            source_id="sds-dinner-fixture",
            cost_usd=15.0,
            status="open",
        )
    ]


async def fetch_events(user_id: str, window_end: str) -> list[Item]:
    feed_url = os.getenv("EVENTS_FEED_URL", "")
    if not feed_url:
        return _fake_events(user_id)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(feed_url)
            r.raise_for_status()
            content = r.content

        try:
            from icalendar import Calendar  # type: ignore
        except ImportError:
            log.warning("icalendar package not installed; returning fixture events")
            return _fake_events(user_id)

        cal = Calendar.from_ical(content)
        items: list[Item] = []

        for component in cal.walk():
            if component.name != "VEVENT":
                continue

            dtstart = component.get("DTSTART")
            dtend = component.get("DTEND")
            uid = component.get("UID", str(uuid4()))
            summary = str(component.get("SUMMARY", "Event"))
            location = component.get("LOCATION")

            if dtstart is None:
                continue

            start_dt = dtstart.dt
            if hasattr(start_dt, "date") and not hasattr(start_dt, "hour"):
                # date-only; convert to UTC datetime
                start_dt = datetime(start_dt.year, start_dt.month, start_dt.day, tzinfo=timezone.utc)
            elif start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            else:
                start_dt = start_dt.astimezone(timezone.utc)

            start_iso = start_dt.isoformat()
            if start_iso > window_end:
                continue

            end_iso: str | None = None
            if dtend is not None:
                end_dt = dtend.dt
                if hasattr(end_dt, "date") and not hasattr(end_dt, "hour"):
                    end_dt = datetime(end_dt.year, end_dt.month, end_dt.day, tzinfo=timezone.utc)
                elif end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                else:
                    end_dt = end_dt.astimezone(timezone.utc)
                end_iso = end_dt.isoformat()

            items.append(Item(
                item_id=f"sds-{uid}",
                user_id=user_id,
                kind="event",
                title=summary,
                starts_at=start_iso,
                ends_at=end_iso,
                due_at=start_iso,
                location=str(location) if location else None,
                source="sundevilsync",
                source_id=str(uid),
                status="open",
            ))

            if len(items) >= 10:
                break

        return items

    except Exception as exc:
        log.warning("events_feed fetch_events failed: %s", exc)
        return []
