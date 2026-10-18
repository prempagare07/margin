"""Read Canvas calendar feeds without an API token. No submission status is inferred.

Feed contents are untrusted source data. The private subscription URL is never
returned in Items, errors, logs, or telemetry. Fetches use the transport directly
so HTTPX/Sentry client instrumentation cannot record the credential-bearing URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from urllib.parse import urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import httpx
import sentry_sdk
from icalendar import Calendar

from db import repo
from shared.protocol import GatherRequest, GatherResult, Item

logger = logging.getLogger(__name__)
MAX_FEED_BYTES = 2 * 1024 * 1024
MAX_EVENTS = 5000


class CanvasFeedError(ValueError):
    """A public-safe error: never include a feed URL, HTTP response, or credentials."""


class CanvasNotConnected(CanvasFeedError):
    pass


def normalize_feed_url(value: str) -> str:
    """Accept Canvas subscription endpoints only, never arbitrary fetch URLs."""
    try:
        value = value.strip()
        if value.startswith("webcal://"):
            value = "https://" + value[9:]
        u = urlsplit(value)
        configured_hosts = {
            urlsplit(os.environ.get(name, "")).hostname
            for name in ("CANVAS_BASE_URL", "CANVAS_ICAL_URL")
        } - {None, "localhost", "127.0.0.1", "::1"}
        host = u.hostname or ""
        if (len(value) > 2048 or u.scheme != "https" or u.username is not None
                or u.password is not None or u.port not in (None, 443) or u.query or u.fragment
                or not (host.endswith(".instructure.com") or host in configured_hosts)
                or not re.fullmatch(r"/feeds/calendars/[A-Za-z0-9_~.-]+\.ics", u.path)):
            raise ValueError
        return urlunsplit(("https", host, u.path, "", ""))
    except (ValueError, TypeError, AttributeError):
        raise CanvasFeedError("Paste the complete Calendar Feed link from your Canvas calendar.") from None


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, limit: int) -> str:
    text = unescape(re.sub(r"<[^>]*>", " ", str(value or "")))
    text = re.sub(r"(?:https|webcal)://\S*/feeds/calendars/\S+", "[private feed]", text)
    return " ".join(text.split())[:limit]


def _time(value: date | datetime, zone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=zone) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime.combine(value, time.min, zone)
    raise CanvasFeedError("The Canvas feed contains an invalid event date.")


def _source_url(value: object, origin: str) -> str | None:
    """Allow source links back to this Canvas instance, excluding subscription URLs."""
    try:
        u = urlsplit(str(value or ""))
        if (u.scheme == "https" and u.netloc == urlsplit(origin).netloc
                and not u.username and not u.fragment):
            if re.fullmatch(r"/courses/\d+/assignments/\d+", u.path):
                return urlunsplit((u.scheme, u.netloc, u.path, "", ""))
            if u.path == "/calendar" and re.fullmatch(r"event_id=[A-Za-z0-9_-]+", u.query):
                return urlunsplit((u.scheme, u.netloc, u.path, u.query, ""))
    except ValueError:
        pass
    return None


def parse_feed(content: bytes, user_id: str, *, origin: str, tz: str = "America/Phoenix") -> list[Item]:
    """Normalize the published VEVENTs. Date-only assignments keep their time unknown.

Canvas normally emits individual occurrences. Unexpanded recurrence rules are
rejected explicitly instead of silently omitting future occurrences.
"""
    if not user_id or len(content) > MAX_FEED_BYTES:
        raise CanvasFeedError("The Canvas feed is too large or its user is missing.")
    try:
        calendar = Calendar.from_ical(content)
        if calendar.name != "VCALENDAR" or calendar.errors:
            raise ValueError
        zone = ZoneInfo(str(calendar.get("X-WR-TIMEZONE") or tz))
        events = calendar.walk("VEVENT")
        if len(events) > MAX_EVENTS:
            raise ValueError
        # Prefer an assignment override to the base due date, then newest sequence.
        selected: dict[str, tuple[tuple, Item]] = {}
        for event in events:
            if event.errors:
                raise ValueError
            if str(event.get("STATUS", "")).upper() == "CANCELLED":
                continue
            if any(event.get(k) is not None for k in ("RRULE", "RDATE", "EXDATE")):
                raise CanvasFeedError("This feed contains an unsupported recurring event. Use the Canvas calendar feed directly.")
            uid = str(event.get("UID", ""))
            if not uid or not event.get("SUMMARY"):
                raise ValueError
            source_url = _source_url(event.get("URL"), origin)
            assignment = bool(re.search(r"(?:^|[-_])assignment(?:[-_]|$)", uid, re.I)
                              or (source_url and "/assignments/" in source_url))
            raw_start = event.decoded("DUE") if assignment and event.get("DUE") else event.decoded("DTSTART", None)
            if raw_start is None:
                # An undated entry cannot establish a deadline or busy interval.
                continue
            if isinstance(raw_start, datetime) and raw_start.tzinfo is None:
                prop = event.get("DUE") or event.get("DTSTART")
                if prop.params.get("TZID"):
                    zone_for_event = ZoneInfo(str(prop.params["TZID"]))
                else:
                    zone_for_event = zone
            else:
                zone_for_event = zone
            start = _time(raw_start, zone_for_event)
            all_day = not isinstance(raw_start, datetime)
            raw_end = event.decoded("DTEND", None)
            if raw_end is not None:
                end = _time(raw_end, zone_for_event)
            elif event.get("DURATION"):
                end = start + event.decoded("DURATION")
            else:
                end = start + timedelta(days=1) if all_day else start
            if end < start:
                raise ValueError
            title = _text(event.get("SUMMARY"), 500)
            course_match = re.search(r"\s+\[([^\[\]]+)\]$", title)
            course = course_match.group(1) if course_match else None
            if course_match:
                title = title[:course_match.start()]
            quote = _text(event.get("DESCRIPTION"), 280)
            if assignment and all_day:
                quote = f"Due date: {raw_start.isoformat()}; time not provided by Canvas. {quote}".strip()[:350]
            # A hostile UID could itself contain a private URL; hash opaque UIDs.
            source_id = ("assignment:" + source_url if assignment and source_url
                         else "ical:" + hashlib.sha256(uid.encode()).hexdigest())
            item_id = str(uuid5(NAMESPACE_URL, f"{user_id}\ncanvas_ical\n{origin}\n{source_id}"))
            item = Item(
                item_id=item_id, user_id=user_id, kind="assignment" if assignment else "event",
                title=title, course=course, source="canvas_ical", source_id=source_id,
                source_url=source_url, quote=quote or None, location=_text(event.get("LOCATION"), 250) or None,
                due_at=_iso(start) if assignment and not all_day else None,
                starts_at=_iso(start) if not assignment or all_day else None,
                ends_at=_iso(end) if not assignment or all_day else None,
                # open means actionable, not a claim about Canvas submission status.
                status="open",
            )
            stamp = event.decoded("DTSTAMP", None)
            rank = ("override" in uid.lower(), int(event.get("SEQUENCE", 0)),
                    _time(stamp, zone).timestamp() if stamp is not None else 0)
            if source_id not in selected or rank > selected[source_id][0]:
                selected[source_id] = rank, item
        return sorted((pair[1] for pair in selected.values()), key=lambda x: (x.due_at or x.starts_at or "", x.item_id))
    except CanvasFeedError:
        raise
    except Exception:
        raise CanvasFeedError("Canvas did not return a readable calendar feed. Copy a fresh Calendar Feed link and try again.") from None


async def fetch_feed(
    ical_url: str, user_id: str, *, tz: str = "America/Phoenix",
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Item]:
    """Validate and fetch a bounded feed, with two retries for transient failures."""
    url = normalize_feed_url(ical_url)
    origin = "https://" + (urlsplit(url).hostname or "")
    failure = "Canvas is temporarily unavailable. Please try again."
    # Transport avoids the HTTPX client access log and automatic URL-bearing spans.
    async with (transport or httpx.AsyncHTTPTransport(retries=0, trust_env=False)) as http:
        for attempt in range(3):
            try:
                with sentry_sdk.start_span(op="tool.canvas", name="Read Canvas calendar feed") as span:
                    async with asyncio.timeout(15):
                        response = await http.handle_async_request(httpx.Request(
                            "GET", url, headers={"Accept": "text/calendar", "User-Agent": "Margin/1.0"},
                            extensions={"timeout": httpx.Timeout(10).as_dict()},
                        ))
                        try:
                            span.set_data("http.status_code", response.status_code)
                            if response.status_code in (429, 500, 502, 503, 504):
                                raise httpx.TransportError("Canvas transient failure")
                            if response.status_code != 200:
                                # No redirects: do not forward a private feed to another endpoint.
                                raise CanvasFeedError("Canvas could not open that feed. Copy a fresh Calendar Feed link and try again.")
                            content = bytearray()
                            async for chunk in response.aiter_bytes():
                                content.extend(chunk)
                                if len(content) > MAX_FEED_BYTES:
                                    raise CanvasFeedError("The Canvas calendar feed is too large.")
                        finally:
                            await response.aclose()
                return parse_feed(bytes(content), user_id, origin=origin, tz=tz)
            except (httpx.TransportError, TimeoutError):
                if attempt < 2:
                    await asyncio.sleep(0.25 * 2 ** attempt)
    raise CanvasFeedError(failure) from None


def _in_window(item: Item, start: str, end: str) -> bool:
    lower, upper = repo._datetime(start), repo._datetime(end)
    due = repo._datetime(item.due_at) if item.due_at else None
    begins = repo._datetime(item.starts_at) if item.starts_at else None
    finishes = repo._datetime(item.ends_at) if item.ends_at else begins
    return bool((due is not None and lower <= due < upper)
                or (begins is not None and (lower <= begins < upper or begins < lower < finishes)))


async def get_items(user_id: str) -> list[Item]:
    """Read only this user's saved active feed. Never fall back to a global demo URL."""
    connection = await repo.get_connection(user_id, "canvas")
    if not connection or connection["status"] != "active":
        raise CanvasNotConnected("Connect your Canvas calendar feed in Margin first.")
    url = (connection.get("secret") or {}).get("ical_url")
    if not url:
        raise CanvasNotConnected("Reconnect Canvas using its Calendar Feed link.")
    user = await repo.get_user(user_id)
    return await fetch_feed(url, user_id, tz=(user or {}).get("tz", "America/Phoenix"))


async def get_assignments(user_id: str) -> list[Item]:
    """Compatibility helper for Academic: all assignments published in the feed."""
    return [item for item in await get_items(user_id) if item.kind == "assignment"]


async def gather(request: GatherRequest) -> GatherResult:
    """Academic can return this result directly; no agent registration occurs here."""
    try:
        if repo._datetime(request.window_end) <= repo._datetime(request.window_start):
            raise ValueError
    except (TypeError, ValueError):
        return GatherResult(request_id=request.request_id, agent="margin-academic", error="Invalid time window.")
    try:
        items = await get_items(request.user_id)
    except CanvasNotConnected as exc:
        return GatherResult(request_id=request.request_id, agent="margin-academic", error=str(exc))
    except Exception:
        logger.warning("Canvas read failed; trying cached items")
        try:
            return await repo.cached_gather_result(
                request.request_id, "margin-academic", request.user_id, ["canvas_ical"],
                request.window_start, request.window_end, error="Canvas is unavailable; showing cached data if available.",
            )
        except Exception:
            return GatherResult(request_id=request.request_id, agent="margin-academic", error="Canvas and its cached data are unavailable.")
    error = None
    try:
        await repo.upsert_items([item.model_dump() for item in items])
        saved = {row["source_id"]: row for row in await repo.last_items_by_source(request.user_id, "canvas_ical")}
        for item in items:
            if item.source_id in saved:
                # Preserve the local completion state and IDs referenced by nudges.
                item.item_id = saved[item.source_id]["item_id"]
                item.status = saved[item.source_id]["status"]
    except Exception:
        logger.warning("Canvas read succeeded, but the item cache could not be updated")
        error = "Canvas data is current, but could not be cached."
    return GatherResult(
        request_id=request.request_id, agent="margin-academic",
        items=[item for item in items if _in_window(item, request.window_start, request.window_end)], error=error,
    )
