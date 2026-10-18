from __future__ import annotations

# Canvas courses, assignments, announcements and iCal fallback. Owners: Devin / Alex.

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


def _fake_items(user_id: str) -> list[Item]:
    return [
        Item(item_id=str(uuid4()), user_id=user_id, kind="assignment",
             title="HW 5 – Linear Algebra", course="MAT 343",
             due_at=_d(2), source="canvas", source_id="canvas-fake-1", status="open"),
        Item(item_id=str(uuid4()), user_id=user_id, kind="assignment",
             title="Project Report Draft", course="CSE 360",
             due_at=_d(4), source="canvas", source_id="canvas-fake-2", status="open"),
        Item(item_id=str(uuid4()), user_id=user_id, kind="assignment",
             title="Reading Response 3", course="ENG 102",
             due_at=_d(5), source="canvas", source_id="canvas-fake-3", status="open"),
        Item(item_id=str(uuid4()), user_id=user_id, kind="announcement",
             title="Midterm review session – BYAC 110", course="CSE 360",
             due_at=_d(1), source="canvas", source_id="canvas-fake-ann-1", status="open"),
    ]


async def fetch_items(user_id: str, window_end: str) -> list[Item]:
    token = os.getenv("CANVAS_TEST_TOKEN", "")
    base = os.getenv("CANVAS_BASE_URL", "https://canvas.asu.edu").rstrip("/")

    if not token:
        return _fake_items(user_id)

    items: list[Item] = []
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{base}/api/v1/courses",
                headers=headers,
                params={"enrollment_state": "active", "per_page": 20},
            )
            r.raise_for_status()
            courses = r.json()

            for course in courses:
                cid = course.get("id")
                cname = course.get("name", "Unknown")

                # Assignments
                try:
                    ar = await client.get(
                        f"{base}/api/v1/courses/{cid}/assignments",
                        headers=headers,
                        params={"include[]": "submission", "bucket": "upcoming", "per_page": 50},
                    )
                    ar.raise_for_status()
                    for a in ar.json():
                        due = a.get("due_at")
                        if not due or due > window_end:
                            continue
                        items.append(Item(
                            item_id=f"canvas-{a['id']}",
                            user_id=user_id,
                            kind="assignment",
                            title=a.get("name", "Assignment"),
                            course=cname,
                            due_at=due,
                            source="canvas",
                            source_id=str(a["id"]),
                            source_url=a.get("html_url"),
                            status="open",
                        ))
                except Exception as exc:
                    log.warning("canvas assignments error course=%s: %s", cid, exc)

                # Announcements
                try:
                    anr = await client.get(
                        f"{base}/api/v1/announcements",
                        headers=headers,
                        params={"context_codes[]": f"course_{cid}", "per_page": 10},
                    )
                    anr.raise_for_status()
                    for ann in anr.json():
                        due = ann.get("posted_at") or ann.get("created_at")
                        if due and due > window_end:
                            continue
                        items.append(Item(
                            item_id=f"canvas-ann-{ann['id']}",
                            user_id=user_id,
                            kind="announcement",
                            title=ann.get("title", "Announcement"),
                            course=cname,
                            due_at=due,
                            source="canvas",
                            source_id=str(ann["id"]),
                            source_url=ann.get("html_url"),
                            status="open",
                        ))
                except Exception as exc:
                    log.warning("canvas announcements error course=%s: %s", cid, exc)

    except Exception as exc:
        log.warning("canvas fetch_items failed: %s", exc)
        return []

    return items
