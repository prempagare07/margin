"""Validated adapter values; provider message content remains untrusted data."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Generic, Literal, TypeVar
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from shared.protocol import Item

Provider = Literal["gmail", "gcal", "slack"]
T = TypeVar("T")
MAX_MESSAGE_CHARS = 12_000
MAX_PAGES = 10


def _identity(*values):
    if any(not isinstance(value, str) or not value.strip() for value in values):
        raise ValueError("Identity components must be nonempty strings")


def parse_time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(parsed, datetime) or parsed.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required")
    return parsed.astimezone(timezone.utc)


def iso(value: str | datetime) -> str:
    return parse_time(value).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return iso(datetime.now(timezone.utc))


def validate_window(start: str, end: str) -> tuple[datetime, datetime]:
    lower, upper = parse_time(start), parse_time(end)
    if lower >= upper:
        raise ValueError("Window end must be after its start")
    return lower, upper


def source_key(*parts: str) -> str:
    if not parts or any(not isinstance(p, str) or not p for p in parts):
        raise ValueError("Source identity components must be nonempty strings")
    return json.dumps(parts, separators=(",", ":"), ensure_ascii=True)


def parse_source_key(value: str) -> tuple[str, ...]:
    parts = json.loads(value)
    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) or not p for p in parts):
        raise ValueError("Invalid source identity")
    return tuple(parts)


def item_id(user_id: str, source: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, source_key(user_id, source, key)))


_ERROR_MESSAGES = {
    "CONFIGURATION_ERROR": "This integration is not configured correctly.",
    "NOT_CONNECTED": "Connect this app to continue.",
    "RELINK_REQUIRED": "Reconnect this app to continue.",
    "ACCOUNT_SELECTION_REQUIRED": "Choose which connected account Margin should use.",
    "ACCOUNT_CHANGED": "The connected account changed. Review the action again.",
    "CHANNEL_SELECTION_REQUIRED": "Choose the Slack channels Margin should read.",
    "CALENDAR_SELECTION_REQUIRED": "Choose the calendars Margin should read and write.",
    "PERMISSION_DENIED": "The connected account does not have the required access.",
    "RATE_LIMITED": "This app is rate limited. Please try again later.",
    "TEMPORARY_UNAVAILABLE": "This app is temporarily unavailable.",
    "PARTIAL_RESULT": "Only part of this source could be read.",
    "SCHEMA_MISMATCH": "This app returned an unsupported response. The adapter needs attention.",
    "SOURCE_NOT_FOUND": "The original source is unavailable or belongs to another account.",
    "INVALID_ACTION": "The requested action is not a valid approved action.",
    "INVALID_WINDOW": "Supply a valid time window with timezones.",
    "CONFLICT_REVIEW_REQUIRED": "Your calendar changed. Review a new time before creating this event.",
    "OUTCOME_UNKNOWN": "The app may have completed this action. Check it before retrying.",
    "RECEIPT_UNAVAILABLE": "The action's result could not be saved. Check the app before retrying.",
    "INPUT_REQUIRED": "The provider needs additional input before this operation can run.",
}


class IntegrationError(Exception):
    """Public-safe failure. Never construct message from a provider exception/body."""

    def __init__(self, code: str, provider: str, message: str | None = None, *,
                 retryable: bool = False, log_id: str | None = None,
                 may_have_written: bool = False, retry_after: float | None = None):
        self.code, self.provider = code, provider
        self.message = message or _ERROR_MESSAGES.get(code, "This operation could not be completed.")
        self.retryable, self.log_id = retryable, log_id
        self.may_have_written, self.retry_after = may_have_written, retry_after
        super().__init__(self.message)

    def __repr__(self) -> str:
        return f"IntegrationError(code={self.code!r}, provider={self.provider!r})"


@dataclass
class ConnectionState:
    provider: Provider
    status: str
    account_id: str | None = None
    settings: dict = field(default_factory=dict)
    candidate_account_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.provider not in {"gmail", "gcal", "slack"} or self.status not in {
            "not_connected", "pending", "active", "relink_required", "selection_required"
        } or (self.status == "active" and not self.account_id) or not isinstance(self.settings, dict):
            raise ValueError("Invalid connection state")
        if self.account_id is not None:
            _identity(self.account_id)
        if not isinstance(self.candidate_account_ids, list):
            raise ValueError("Invalid account candidates")
        _identity(*self.candidate_account_ids)


@dataclass
class ConnectionSettings:
    calendar_ids: list[str] = field(default_factory=list)
    write_calendar_id: str | None = None
    workspace_id: str | None = None
    channel_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.calendar_ids, list) or not isinstance(self.channel_ids, list):
            raise ValueError("Resource selections must be lists")
        _identity(*self.calendar_ids, *self.channel_ids)
        for value in (self.write_calendar_id, self.workspace_id):
            if value is not None:
                _identity(value)


@dataclass
class ToolResult:
    data: dict
    log_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.data, dict):
            raise ValueError("Provider result must be an object")


@dataclass
class ReadBatch(Generic[T]):
    records: list[T] = field(default_factory=list)
    complete: bool = True
    fetched_at: str = field(default_factory=now_iso)
    issues: list[IntegrationError] = field(default_factory=list)
    log_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.fetched_at = iso(self.fetched_at)
        if any(not isinstance(issue, IntegrationError) for issue in self.issues):
            raise ValueError("Read issues must be safe integration errors")
        if self.issues:
            self.complete = False


@dataclass
class CalendarOption:
    id: str
    name: str
    account_id: str
    readable: bool = True
    writable: bool = False
    timezone: str = "UTC"

    def __post_init__(self):
        _identity(self.id, self.account_id)
        if not isinstance(self.name, str):
            raise ValueError("Invalid calendar identity")
        from zoneinfo import ZoneInfo
        ZoneInfo(self.timezone)


@dataclass
class ChannelOption:
    id: str
    name: str
    account_id: str
    workspace_id: str
    readable: bool = True

    def __post_init__(self):
        _identity(self.id, self.account_id, self.workspace_id)
        if not isinstance(self.name, str):
            raise ValueError("Invalid channel identity")


@dataclass
class CalendarRecord:
    item: Item
    calendar_id: str
    blocks_time: bool = True

    def __post_init__(self):
        if self.item.source != "gcal" or self.item.kind != "event" or not self.calendar_id:
            raise ValueError("Invalid calendar record")
        if parse_time(self.item.ends_at) < parse_time(self.item.starts_at):
            raise ValueError("Invalid event interval")


@dataclass(repr=False)
class SourceMessage:
    provider: Provider
    account_id: str
    message_id: str
    sender: str | None
    subject: str | None
    date: str
    body: str
    source_url: str | None = None
    thread_id: str | None = None
    workspace_id: str | None = None
    channel_id: str | None = None
    channel_name: str | None = None
    truncated: bool = False

    def __post_init__(self):
        _identity(self.account_id, self.message_id)
        if self.provider not in ("gmail", "slack"):
            raise ValueError("Invalid message identity")
        self.date = iso(self.date)
        if self.provider == "slack" and (not self.workspace_id or not self.channel_id):
            raise ValueError("Slack messages require workspace and channel identities")
        if self.provider == "slack":
            _identity(self.workspace_id, self.channel_id)
        if not isinstance(self.body, str) or len(self.body) > MAX_MESSAGE_CHARS:
            raise ValueError("Invalid message text")

    @property
    def source_id(self) -> str:
        if self.provider == "slack":
            return source_key(self.account_id, self.workspace_id, self.channel_id, self.message_id)
        return source_key(self.account_id, self.message_id)


@dataclass(repr=False)
class SourceDetail:
    source: str
    subject: str | None
    sender: str | None
    date: str | None
    body: str
    link: str | None
    truncated: bool = False
    channel: str | None = None
    thread_id: str | None = None

    def __post_init__(self):
        if self.source not in {"gmail", "slack"} or not isinstance(self.body, str) or len(self.body) > MAX_MESSAGE_CHARS:
            raise ValueError("Invalid source detail")
        if self.date is not None:
            self.date = iso(self.date)


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "head"}:
            self.hidden += 1
        if tag in {"p", "br", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "head"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "li", "tr"} and not self.hidden:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: object, limit: int = MAX_MESSAGE_CHARS) -> tuple[str, bool]:
    raw = str(value or "")
    parser = _TextParser()
    parser.feed(raw)
    text = re.sub(r"[ \t]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit], len(text) > limit


def safe_link(value: object, provider: str) -> str | None:
    try:
        if any(ord(c) < 32 for c in str(value or "")):
            return None
        u = urlsplit(str(value or ""))
        host = u.hostname or ""
        allowed = ((provider == "gmail" and host == "mail.google.com")
                   or (provider == "gcal" and (host == "calendar.google.com" or (host == "www.google.com" and u.path.startswith("/calendar/"))))
                   or (provider == "slack" and (host.endswith(".slack.com") or host == "app.slack.com")))
        if u.scheme == "https" and allowed and u.username is None and u.password is None and u.port in (None, 443):
            return str(value)
    except ValueError:
        pass
    return None
