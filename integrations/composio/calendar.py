"""Selected-calendar reads and deterministic availability; never writes events."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.protocol import Item

from .models import (
    CalendarOption,
    CalendarRecord,
    IntegrationError,
    ReadBatch,
    iso,
    item_id,
    parse_time,
    source_key,
    validate_window,
)

MAX_EVENTS = 1000
MAX_PAGES = 10
MIN_SLOT = timedelta(minutes=60)
_READ_ROLES = {"reader", "writerWithoutPrivateAccess", "writer", "owner"}
_WRITE_ROLES = {"writerWithoutPrivateAccess", "writer", "owner"}
_ACCOUNT_ERRORS = {"ACCOUNT_CHANGED", "RELINK_REQUIRED", "NOT_CONNECTED"}


def _error(code: str = "SCHEMA_MISMATCH", *, log_id: str | None = None) -> IntegrationError:
    return IntegrationError(code, "gcal", log_id=log_id)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise _error("INVALID_TIMEZONE") from None


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if tag in {"p", "div", "li"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _text(value: object, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _error()
    parser = _Text()
    parser.feed(value)
    text = " ".join("".join(parser.parts).split())
    return text[:limit] or None


def _link(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "https"
            and parsed.hostname in {"calendar.google.com", "www.google.com"}
            and parsed.path.startswith("/calendar")
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
        ):
            return value
    except ValueError:
        pass
    return None


def _event_time(value: object, calendar_timezone: str) -> datetime:
    if not isinstance(value, dict):
        raise _error()
    if isinstance(value.get("date"), str) and not value.get("dateTime"):
        try:
            day = date.fromisoformat(value["date"])
        except ValueError:
            raise _error() from None
        return datetime.combine(day, time.min, _zone(calendar_timezone)).astimezone(timezone.utc)
    if not isinstance(value.get("dateTime"), str) or value.get("date"):
        raise _error()
    try:
        stamp = datetime.fromisoformat(value["dateTime"].replace("Z", "+00:00"))
    except ValueError:
        raise _error() from None
    if stamp.tzinfo is None:
        # Google permits local dateTime values when a timeZone is supplied.
        zone = _zone(value.get("timeZone") or calendar_timezone)
        first = stamp.replace(tzinfo=zone, fold=0)
        second = stamp.replace(tzinfo=zone, fold=1)
        # An offset-free ambiguous or nonexistent wall-clock time cannot establish
        # availability. Request an explicit provider offset instead of guessing.
        if first.utcoffset() != second.utcoffset() or first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != stamp:
            raise _error()
        stamp = first
    return stamp.astimezone(timezone.utc)


def _normalize_event(
    value: object, *, user_id: str, account_id: str, calendar_id: str, calendar_timezone: str
) -> CalendarRecord | None:
    if not isinstance(value, dict):
        raise _error()
    if value.get("status") == "cancelled":
        return None
    native_id = value.get("id")
    if not isinstance(native_id, str) or not native_id:
        raise _error()
    begins = _event_time(value.get("start"), calendar_timezone)
    finishes = _event_time(value.get("end"), calendar_timezone)
    if finishes < begins or bool(value["start"].get("date")) != bool(value["end"].get("date")):
        raise _error()
    if value.get("recurrence"):
        # With single_events=True only expanded instances are acceptable.
        raise _error()
    attendees = value.get("attendees") or []
    if not isinstance(attendees, list) or any(not isinstance(a, dict) for a in attendees):
        raise _error()
    declined = any(a.get("self") is True and a.get("responseStatus") == "declined" for a in attendees)
    private = value.get("visibility") in {"private", "confidential"}
    key = source_key(account_id, calendar_id, native_id)
    item = Item(
        item_id=item_id(user_id, "gcal", key),
        user_id=user_id,
        kind="event",
        title="Busy" if private else (_text(value.get("summary"), 500) or "Busy"),
        starts_at=iso(begins),
        ends_at=iso(finishes),
        location=None if private else _text(value.get("location"), 500),
        source="gcal",
        source_id=key,
        source_url=_link(value.get("htmlLink")),
        quote=None if private else _text(value.get("description"), 280),
    )
    return CalendarRecord(
        item=item,
        calendar_id=calendar_id,
        blocks_time=finishes > begins and not declined and value.get("transparency") != "transparent",
    )


def _overlaps(record: CalendarRecord, lower: datetime, upper: datetime) -> bool:
    begins = parse_time(record.item.starts_at)
    finishes = parse_time(record.item.ends_at)
    return (begins < upper and finishes > lower) or (begins == finishes and lower <= begins < upper)


def _issue(batch: ReadBatch, error: IntegrationError) -> None:
    batch.complete = False
    batch.issues.append(error)
    if error.log_id and error.log_id not in batch.log_ids:
        batch.log_ids.append(error.log_id)


def _log_id(batch: ReadBatch, value: str | None) -> None:
    if value and value not in batch.log_ids:
        batch.log_ids.append(value)


def _page(data: object, items_key: str, cursor_key: str) -> tuple[list, str | None]:
    if not isinstance(data, dict) or not isinstance(data.get(items_key), list):
        raise _error()
    cursor = data.get(cursor_key)
    if cursor is not None and not isinstance(cursor, str):
        raise _error()
    return data[items_key], cursor or None


async def _list_calendars(user_id: str, account_id: str) -> ReadBatch[CalendarOption]:
    from . import runtime

    batch: ReadBatch[CalendarOption] = ReadBatch()
    calendars: dict[str, CalendarOption] = {}
    cursor: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        arguments = {"max_results": 250, "show_hidden": True, "show_deleted": False}
        if cursor:
            arguments["page_token"] = cursor
        try:
            result = await runtime.execute(user_id, "gcal", "calendar_list", arguments, expected_account_id=account_id)
            _log_id(batch, result.log_id)
            values, next_cursor = _page(result.data, "calendars", "next_page_token")
            for value in values:
                try:
                    if not isinstance(value, dict):
                        raise _error()
                    if value.get("deleted") is True:
                        continue
                    calendar_id, role, zone = value.get("id"), value.get("accessRole"), value.get("timeZone")
                    if not isinstance(calendar_id, str) or not calendar_id or not isinstance(role, str) or role not in _READ_ROLES | {"freeBusyReader"}:
                        raise _error()
                    _zone(zone)
                    calendars[calendar_id] = CalendarOption(
                        id=calendar_id,
                        name=_text(value.get("summaryOverride") or value.get("summary"), 500) or calendar_id,
                        account_id=account_id,
                        readable=role in _READ_ROLES,
                        writable=role in _WRITE_ROLES,
                        primary=value.get("primary") is True,
                        timezone=zone,
                    )
                except (IntegrationError, TypeError, ValueError) as exc:
                    _issue(batch, exc if isinstance(exc, IntegrationError) else _error(log_id=result.log_id))
            if not next_cursor:
                break
            if next_cursor in seen:
                _issue(batch, _error("PARTIAL_RESULT", log_id=result.log_id))
                break
            cursor = next_cursor
            seen.add(cursor)
        except IntegrationError as exc:
            _issue(batch, exc)
            if exc.code in _ACCOUNT_ERRORS:
                calendars.clear()
            break
    else:
        _issue(batch, _error("PARTIAL_RESULT"))
    batch.records = list(calendars.values())
    return batch


async def list_calendars(user_id: str) -> ReadBatch[CalendarOption]:
    """Discover native IDs and access roles only within the verified account."""
    from . import connections, runtime

    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, "gcal")
        return await _list_calendars(user_id, connection.account_id)


async def auto_select_calendars(user_id: str) -> dict:
    """Read every writable calendar and choose the writable primary for writes."""
    from . import connections

    batch = await list_calendars(user_id)
    if not batch.complete or batch.issues:
        raise _error("PARTIAL_RESULT")
    writable = [option for option in batch.records if option.writable]
    primary = [option for option in writable if option.primary]
    if len(primary) == 1:
        target = primary[0]
    elif not primary and len(writable) == 1:
        target = writable[0]
    else:
        raise _error("CALENDAR_SELECTION_REQUIRED")
    settings = {
        "calendar_ids": [option.id for option in writable],
        "write_calendar_id": target.id,
    }
    await connections.save_connection_settings(user_id, "gcal", settings)
    return settings


async def fetch_calendar_events(user_id: str, *, start: str, end: str) -> ReadBatch[CalendarRecord]:
    """Read expanded instances from every selected calendar, disclosing gaps."""
    from . import connections, runtime

    try:
        lower, upper = validate_window(start, end)
    except (TypeError, ValueError):
        raise _error("INVALID_WINDOW") from None
    batch: ReadBatch[CalendarRecord] = ReadBatch()
    records: dict[str, CalendarRecord] = {}
    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, "gcal")
        selected = connection.settings.get("calendar_ids")
        if not isinstance(selected, list) or not selected or any(not isinstance(c, str) or not c for c in selected):
            raise _error("CALENDAR_SELECTION_REQUIRED")
        selected = list(dict.fromkeys(selected))
        discovery = await _list_calendars(user_id, connection.account_id)
        batch.log_ids.extend(discovery.log_ids)
        if not discovery.complete or discovery.issues:
            batch.complete = False
            batch.issues.extend(discovery.issues or [_error("PARTIAL_RESULT")])
            return batch
        options = {c.id: c for c in discovery.records}
        pages = 0
        exhausted = False
        for calendar_id in selected:
            option = options.get(calendar_id)
            if option is None or not option.readable:
                _issue(batch, _error("PERMISSION_DENIED"))
                continue
            if calendar_id.startswith("ca_"):
                # The captured Events List schema documents automatic rewriting
                # of this prefix to primary. Never read a different calendar.
                _issue(batch, _error())
                continue
            cursor: str | None = None
            seen: set[str] = set()
            while True:
                if pages >= MAX_PAGES:
                    _issue(batch, _error("PARTIAL_RESULT"))
                    exhausted = True
                    break
                arguments = {
                    "calendarId": calendar_id,
                    "timeMin": iso(lower),
                    "timeMax": iso(upper),
                    "singleEvents": True,
                    "showDeleted": False,
                    "showHiddenInvitations": True,
                    "orderBy": "startTime",
                    "maxResults": 250,
                }
                if cursor:
                    arguments["pageToken"] = cursor
                pages += 1
                try:
                    result = await runtime.execute(user_id, "gcal", "event_list", arguments, expected_account_id=connection.account_id)
                    _log_id(batch, result.log_id)
                    values, next_cursor = _page(result.data, "items", "nextPageToken")
                    # The event collection can disclose an access change since
                    # discovery; never use a degraded free/busy-only response.
                    role = result.data.get("accessRole", "reader")
                    if not isinstance(role, str) or role not in _READ_ROLES:
                        raise _error("PERMISSION_DENIED", log_id=result.log_id)
                    zone = result.data.get("timeZone") or option.timezone
                    _zone(zone)
                    for value in values:
                        try:
                            record = _normalize_event(value, user_id=user_id, account_id=connection.account_id,
                                                      calendar_id=calendar_id, calendar_timezone=zone)
                            if record is None or not _overlaps(record, lower, upper):
                                continue
                            if record.item.source_id not in records and len(records) >= MAX_EVENTS:
                                _issue(batch, _error("PARTIAL_RESULT", log_id=result.log_id))
                                exhausted = True
                                break
                            records[record.item.source_id] = record
                        except (IntegrationError, ValueError, TypeError) as exc:
                            _issue(batch, exc if isinstance(exc, IntegrationError) else _error(log_id=result.log_id))
                    if exhausted or not next_cursor:
                        break
                    if next_cursor in seen:
                        _issue(batch, _error("PARTIAL_RESULT", log_id=result.log_id))
                        break
                    cursor = next_cursor
                    seen.add(cursor)
                except IntegrationError as exc:
                    _issue(batch, exc)
                    if exc.code in _ACCOUNT_ERRORS:
                        records.clear()
                        exhausted = True
                    break
            if exhausted:
                break
    batch.records = sorted(records.values(), key=lambda r: (r.item.starts_at, r.item.source_id))
    return batch


def compute_free_slots(
    events: list[CalendarRecord], *, start: str, end: str, tz: str
) -> list[list[str]]:
    """Return >=60-minute gaps in local 08:00–22:00 days.

    Call only with records from a complete, successful fresh read of all selected
    calendars. A partial/stale batch cannot establish availability.
    """
    lower, upper = validate_window(start, end)
    zone = _zone(tz)
    busy: list[tuple[datetime, datetime]] = []
    for record in events:
        if not record.blocks_time:
            continue
        begins = parse_time(record.item.starts_at)
        finishes = parse_time(record.item.ends_at)
        if finishes < begins:
            raise _error()
        if finishes > begins and begins < upper and finishes > lower:
            busy.append((max(begins, lower), min(finishes, upper)))
    busy.sort()
    merged: list[tuple[datetime, datetime]] = []
    for begins, finishes in busy:
        if merged and begins <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(finishes, merged[-1][1]))
        else:
            merged.append((begins, finishes))

    result: list[list[str]] = []
    day = lower.astimezone(zone).date()
    last_day = upper.astimezone(zone).date()
    while day <= last_day:
        day_start = max(lower, datetime.combine(day, time(8), zone).astimezone(timezone.utc))
        day_end = min(upper, datetime.combine(day, time(22), zone).astimezone(timezone.utc))
        cursor = day_start
        if day_start < day_end:
            for begins, finishes in merged:
                if finishes <= day_start:
                    continue
                if begins >= day_end:
                    break
                clipped_start, clipped_end = max(begins, day_start), min(finishes, day_end)
                if clipped_start - cursor >= MIN_SLOT:
                    result.append([iso(cursor), iso(clipped_start)])
                cursor = max(cursor, clipped_end)
            if day_end - cursor >= MIN_SLOT:
                result.append([iso(cursor), iso(day_end)])
        day += timedelta(days=1)
    return result
