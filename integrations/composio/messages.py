"""Read-only Gmail and selected-channel Slack adapters.

Bodies are transient evidence. This module never persists message content, changes
labels, joins channels, sends messages, or resolves a URL found in message text.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any

from db import repo

from . import connections, runtime
from .models import (
    ChannelOption,
    IntegrationError,
    ReadBatch,
    SourceDetail,
    SourceMessage,
    iso,
    parse_time,
    plain_text,
    safe_link,
    validate_window,
)

MAX_GMAIL_MESSAGES = 100
MAX_SLACK_MESSAGES_PER_CHANNEL = 100
MAX_PAGES = 10
MAX_TEXT_CHARACTERS = 12_000
_MAX_MIME_CHARACTERS = 256_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SLACK_TS = re.compile(r"^[0-9]{1,16}\.[0-9]{1,6}$")
_ACCOUNT_ERRORS = {"ACCOUNT_CHANGED", "NOT_CONNECTED", "RELINK_REQUIRED", "ACCOUNT_SELECTION_REQUIRED"}


def _schema(provider: str, log_id: str | None = None) -> IntegrationError:
    return IntegrationError("SCHEMA_MISMATCH", provider, log_id=log_id)


def _partial(provider: str, log_id: str | None = None) -> IntegrationError:
    return IntegrationError("PARTIAL_RESULT", provider, log_id=log_id)


def _log(batch: ReadBatch, result: Any) -> None:
    if result.log_id and result.log_id not in batch.log_ids:
        batch.log_ids.append(result.log_id)


def _issue(batch: ReadBatch, issue: IntegrationError) -> None:
    batch.complete = False
    batch.issues.append(issue)
    if issue.log_id and issue.log_id not in batch.log_ids:
        batch.log_ids.append(issue.log_id)


def _string(value: Any, provider: str, *, limit: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise _schema(provider)
    return value


def _optional_text(value: Any, *, limit: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text, _ = plain_text(value, limit=limit)
    return text or None


def _sent_date(value: Any) -> datetime:
    """Composio/Gmail dates are either aware ISO/RFC dates or epoch millis."""
    try:
        if isinstance(value, str) and value.isdigit():
            return _EPOCH + timedelta(milliseconds=int(value))
        if isinstance(value, str):
            try:
                return parse_time(value)
            except (ValueError, IntegrationError):
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                    return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass
    raise _schema("gmail")


def _slack_date(value: Any) -> datetime:
    """Never round a native Slack identifier through a floating-point number."""
    if not isinstance(value, str) or not _SLACK_TS.fullmatch(value):
        raise _schema("slack")
    try:
        return _EPOCH + timedelta(microseconds=int(Decimal(value) * 1_000_000))
    except (InvalidOperation, OverflowError, ValueError):
        raise _schema("slack") from None


def _epoch_string(value: datetime) -> str:
    delta = value - _EPOCH
    micros = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    return f"{micros // 1_000_000}.{micros % 1_000_000:06d}"


def _mime_body(payload: dict[str, Any]) -> tuple[str, bool]:
    """Decode inline MIME text only; attachment bodies are never requested."""
    plain: list[str] = []
    html: list[str] = []
    consumed = 0
    clipped = False

    def visit(part: Any, depth: int = 0) -> None:
        nonlocal consumed, clipped
        if not isinstance(part, dict) or depth > 20:
            raise _schema("gmail")
        if part.get("filename"):
            return
        body = part.get("body", {})
        if not isinstance(body, dict):
            raise _schema("gmail")
        mime = part.get("mimeType", "")
        encoded = body.get("data")
        if encoded is not None and mime in {"text/plain", "text/html"}:
            if not isinstance(encoded, str):
                raise _schema("gmail")
            if consumed >= _MAX_MIME_CHARACTERS:
                clipped = True
                return
            # A provider can return a large inline body; bound decoding memory.
            available = _MAX_MIME_CHARACTERS - consumed
            if len(encoded) > available:
                encoded = encoded[: available - available % 4]
                clipped = True
            consumed += len(encoded)
            try:
                decoded = base64.b64decode(
                    encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
                ).decode("utf-8", errors="replace")
            except (ValueError, binascii.Error):
                raise _schema("gmail") from None
            (plain if mime == "text/plain" else html).append(decoded)
        parts = part.get("parts", [])
        if not isinstance(parts, list):
            raise _schema("gmail")
        for child in parts:
            visit(child, depth + 1)

    visit(payload)
    text, truncated = plain_text("\n".join(plain or html), limit=MAX_TEXT_CHARACTERS)
    return text, truncated or clipped


def _slack_text(value: Any) -> tuple[str, bool]:
    if not isinstance(value, str):
        raise _schema("slack")
    # Slack mrkdwn uses angle brackets for mentions and links, not HTML tags.
    value = re.sub(r"<@([A-Z0-9]+)>", r"@\1", value)
    value = re.sub(r"<#([A-Z0-9]+)(?:\|([^>]+))?>", lambda m: "#" + (m[2] or m[1]), value)
    value = re.sub(r"<(https?://[^>|]+)(?:\|([^>]+))?>", lambda m: m[2] or m[1], value)
    return plain_text(value, limit=MAX_TEXT_CHARACTERS)


def _slack_message(
    raw: dict[str, Any], account_id: str, workspace_id: str, channel_id: str,
    *, parent_ts: str | None = None,
) -> SourceMessage | None:
    # Ignore channel lifecycle events/deletions; a changed message carries its
    # current text and original ts in the nested message object.
    subtype = raw.get("subtype")
    if subtype == "message_changed":
        raw = raw.get("message")
        if not isinstance(raw, dict):
            raise _schema("slack")
    elif subtype in {"message_deleted", "tombstone", "channel_join", "channel_leave", "channel_name", "channel_purpose", "channel_topic"}:
        return None
    if raw.get("type", "message") not in {"message", "group"}:
        return None
    timestamp = _string(raw.get("ts"), "slack")
    sent = _slack_date(timestamp)
    body, truncated = _slack_text(raw.get("text", ""))
    if not body:
        return None  # No attachment processing in this adapter.
    thread = raw.get("thread_ts") or parent_ts
    if thread is not None:
        _slack_date(thread)
    author = raw.get("user") or raw.get("bot_id")
    if author is not None:
        author = _string(author, "slack")
    return SourceMessage(
        provider="slack",
        account_id=account_id,
        message_id=timestamp,
        thread_id=thread,
        workspace_id=workspace_id,
        channel_id=channel_id,
        channel_name=None,
        sender=author,
        subject=None,
        date=iso(sent),
        body=body,
        source_url=safe_link(raw.get("permalink"), "slack"),
        truncated=truncated,
    )


def _source_parts(item: dict[str, Any], user_id: str) -> tuple[str, list[str]]:
    provider = item.get("source")
    if item.get("user_id") != user_id or provider not in {"gmail", "slack"}:
        raise IntegrationError("SOURCE_NOT_FOUND", "gmail" if provider != "slack" else "slack")
    try:
        parts = json.loads(item["source_id"])
    except (KeyError, ValueError, TypeError):
        raise IntegrationError("SOURCE_NOT_FOUND", provider) from None
    size = 2 if provider == "gmail" else 4
    if not isinstance(parts, list) or len(parts) != size or any(not isinstance(value, str) or not value or len(value) > 512 for value in parts):
        raise IntegrationError("SOURCE_NOT_FOUND", provider)
    return provider, parts


def _detail(message: SourceMessage) -> SourceDetail:
    return SourceDetail(
        source=message.provider,
        subject=message.subject,
        sender=message.sender,
        date=message.date,
        body=message.body,
        link=message.source_url,
        truncated=message.truncated,
        channel=message.channel_name or message.channel_id,
        thread_id=message.thread_id,
    )


def _gmail_message(raw: dict[str, Any], account_id: str) -> SourceMessage:
    """Map the captured Composio MessageBody schema, not an invented wrapper."""
    native_id = _string(raw.get("messageId"), "gmail")
    payload = raw.get("payload")
    body = None
    truncated = False
    headers: dict[str, str] = {}
    if payload is not None:
        if not isinstance(payload, dict):
            raise _schema("gmail")
        body, truncated = _mime_body(payload)
        raw_headers = payload.get("headers", [])
        if not isinstance(raw_headers, list):
            raise _schema("gmail")
        for entry in raw_headers:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("value"), str):
                raise _schema("gmail")
            headers.setdefault(entry["name"].lower(), entry["value"])
    if not body and raw.get("messageText") is not None:
        if not isinstance(raw["messageText"], str):
            raise _schema("gmail")
        body, truncated = plain_text(raw["messageText"], limit=MAX_TEXT_CHARACTERS)
    if body is None:
        raise _schema("gmail")  # Metadata/snippets alone are not full evidence.
    sender = raw.get("sender") or headers.get("from")
    if sender is not None and not isinstance(sender, str):
        raise _schema("gmail")
    thread = raw.get("threadId")
    return SourceMessage(
        provider="gmail", account_id=account_id, message_id=native_id,
        thread_id=_string(thread, "gmail") if thread is not None else None,
        sender=" ".join(sender.split())[:500] if sender else None,
        subject=_optional_text(raw.get("subject") or headers.get("subject")),
        date=iso(_sent_date(raw.get("messageTimestamp") or headers.get("date"))),
        body=body,
        source_url=safe_link(raw.get("display_url"), "gmail"),
        truncated=truncated,
    )


def _gmail_received(raw: dict[str, Any]) -> bool:
    labels = raw.get("labelIds")
    if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
        raise _schema("gmail")
    excluded = {"SPAM", "TRASH", "DRAFT"}
    return not (excluded.intersection(labels) or ("SENT" in labels and "INBOX" not in labels))


def _window(start: str, end: str, provider: str) -> tuple[datetime, datetime]:
    try:
        return validate_window(start, end)
    except (TypeError, ValueError, OverflowError):
        raise IntegrationError("INVALID_WINDOW", provider) from None


async def _get_gmail(user_id: str, account_id: str, message_id: str, batch: ReadBatch | None = None) -> SourceMessage:
    result = await runtime.execute(
        user_id, "gmail", "gmail_get",
        {"message_id": message_id, "user_id": "me", "format": "full"},
        expected_account_id=account_id,
    )
    if batch is not None:
        _log(batch, result)
    message = _gmail_message(result.data, account_id)
    if message.message_id != message_id:
        raise _schema("gmail", result.log_id)
    return message


async def fetch_gmail_messages(user_id: str, *, since: str, until: str) -> ReadBatch[SourceMessage]:
    lower, upper = _window(since, until, "gmail")
    batch = ReadBatch(records=[])
    records: dict[str, SourceMessage] = {}
    cursor = None
    seen_cursors: set[str] = set()
    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, "gmail")
        for page in range(MAX_PAGES):
            args = {
                "user_id": "me", "max_results": MAX_GMAIL_MESSAGES,
                "verbose": True, "ids_only": False, "include_payload": True,
                "include_spam_trash": False,
                # Provider filtering is broadened by one second, then exact aware
                # boundaries and labels are enforced on each returned message.
                "query": f"after:{math.floor(lower.timestamp()) - 1} before:{math.ceil(upper.timestamp())} -in:spam -in:trash -in:drafts {{-in:sent in:inbox}}",
            }
            if cursor:
                args["page_token"] = cursor
            try:
                result = await runtime.execute(user_id, "gmail", "gmail_list", args, expected_account_id=connection.account_id)
                _log(batch, result)
                rows = result.data.get("messages")
                if not isinstance(rows, list):
                    raise _schema("gmail", result.log_id)
                for raw in rows:
                    try:
                        if not isinstance(raw, dict):
                            raise _schema("gmail", result.log_id)
                        if not _gmail_received(raw):
                            continue
                        native_id = _string(raw.get("messageId"), "gmail")
                        # The captured list tool explicitly allows null payload.
                        # Fetch the single message only when no complete body was returned.
                        if raw.get("payload") is None and raw.get("messageText") is None:
                            message = await _get_gmail(user_id, connection.account_id, native_id, batch)
                        else:
                            message = _gmail_message(raw, connection.account_id)
                        if not lower <= parse_time(message.date) < upper:
                            continue
                        if native_id not in records and len(records) >= MAX_GMAIL_MESSAGES:
                            _issue(batch, _partial("gmail", result.log_id))
                            continue
                        records[native_id] = message
                    except IntegrationError as error:
                        if error.code in _ACCOUNT_ERRORS:
                            raise
                        _issue(batch, error)
                cursor = result.data.get("nextPageToken")
                if cursor is not None and not isinstance(cursor, str):
                    raise _schema("gmail", result.log_id)
                if not cursor:
                    break
                if cursor in seen_cursors or len(records) >= MAX_GMAIL_MESSAGES or page == MAX_PAGES - 1:
                    _issue(batch, _partial("gmail", result.log_id))
                    break
                seen_cursors.add(cursor)
            except IntegrationError as error:
                _issue(batch, error)
                if error.code in _ACCOUNT_ERRORS:
                    records.clear()
                break
    batch.records = sorted(records.values(), key=lambda record: parse_time(record.date), reverse=True)
    return batch


def _slack_cursor(data: dict[str, Any]) -> str:
    metadata = data.get("response_metadata") or {}
    if not isinstance(metadata, dict):
        raise _schema("slack")
    cursor = metadata.get("next_cursor") or ""
    if not isinstance(cursor, str):
        raise _schema("slack")
    return cursor


def _slack_rows(data: dict[str, Any], name: str) -> list[dict[str, Any]]:
    if data.get("ok") is not True or not isinstance(data.get(name), list):
        raise _schema("slack")
    if any(not isinstance(row, dict) for row in data[name]):
        raise _schema("slack")
    return data[name]


async def _workspace(user_id: str, account_id: str, batch: ReadBatch | None = None) -> str:
    result = await runtime.execute(user_id, "slack", "slack_identity", {}, expected_account_id=account_id)
    if batch is not None:
        _log(batch, result)
    if result.data.get("ok") is not True:
        raise _schema("slack", result.log_id)
    return _string(result.data.get("team_id"), "slack")


async def list_slack_channels(user_id: str) -> ReadBatch[ChannelOption]:
    batch = ReadBatch(records=[])
    records: dict[str, ChannelOption] = {}
    cursor = None
    seen: set[str] = set()
    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, "slack")
        workspace_id = await _workspace(user_id, connection.account_id, batch)
        for page in range(MAX_PAGES):
            args = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                args["cursor"] = cursor
            try:
                result = await runtime.execute(user_id, "slack", "slack_channels", args, expected_account_id=connection.account_id)
                _log(batch, result)
                for raw in _slack_rows(result.data, "channels"):
                    if raw.get("is_im") or raw.get("is_mpim") or raw.get("is_archived"):
                        continue
                    if raw.get("context_team_id") not in {None, workspace_id}:
                        continue
                    channel_id = _string(raw.get("id"), "slack")
                    name = _string(raw.get("name"), "slack")
                    records[channel_id] = ChannelOption(
                        id=channel_id, name=name, account_id=connection.account_id,
                        workspace_id=workspace_id,
                        readable=not raw.get("is_private") or raw.get("is_member") is True,
                    )
                cursor = _slack_cursor(result.data)
                if not cursor:
                    break
                if cursor in seen or page == MAX_PAGES - 1:
                    _issue(batch, _partial("slack", result.log_id))
                    break
                seen.add(cursor)
            except IntegrationError as error:
                _issue(batch, error)
                if error.code in _ACCOUNT_ERRORS:
                    records.clear()
                break
    batch.records = sorted(records.values(), key=lambda option: (option.name, option.id))
    return batch


def _selected_slack(connection: Any) -> tuple[str, list[str]]:
    settings = connection.settings
    workspace = settings.get("workspace_id")
    channels = settings.get("channel_ids")
    if (not isinstance(workspace, str) or not workspace
        or not isinstance(channels, list) or not channels
        or any(not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]+", channel) for channel in channels)):
        raise IntegrationError("CHANNEL_SELECTION_REQUIRED", "slack")
    return workspace, list(dict.fromkeys(channels))


class _SlackPages:
    """One pagination allowance for discovery, history, threads, and search."""
    def __init__(self) -> None:
        self.used = 0

    def take(self) -> None:
        if self.used >= MAX_PAGES:
            raise _partial("slack")
        self.used += 1


async def _selected_channel_names(user_id: str, account_id: str, workspace_id: str,
                                  channel_ids: list[str], batch: ReadBatch,
                                  pages: _SlackPages) -> dict[str, str]:
    names: dict[str, str] = {}
    cursor = None
    seen: set[str] = set()
    try:
        while True:
            pages.take()
            arguments = {"types": "public_channel,private_channel", "exclude_archived": True, "limit": 200}
            if cursor:
                arguments["cursor"] = cursor
            result = await runtime.execute(user_id, "slack", "slack_channels", arguments, expected_account_id=account_id)
            _log(batch, result)
            for raw in _slack_rows(result.data, "channels"):
                channel_id = raw.get("id")
                if channel_id not in channel_ids or raw.get("is_im") or raw.get("is_mpim") or raw.get("is_archived"):
                    continue
                if raw.get("context_team_id") not in {None, workspace_id}:
                    continue
                name = _string(raw.get("name"), "slack")
                if not re.fullmatch(r"[a-z0-9_-]+", name):
                    raise _schema("slack", result.log_id)
                names[channel_id] = name
            if len(names) == len(channel_ids):
                return names
            cursor = _slack_cursor(result.data)
            if not cursor:
                _issue(batch, IntegrationError("PERMISSION_DENIED", "slack", log_id=result.log_id))
                return names
            if cursor in seen:
                raise _partial("slack", result.log_id)
            seen.add(cursor)
    except IntegrationError as error:
        if error.code in _ACCOUNT_ERRORS:
            raise
        _issue(batch, error)
        return names


async def _recent_channel_search(user_id: str, account_id: str, workspace_id: str,
                                 channel_id: str, channel_name: str,
                                 lower: datetime, upper: datetime,
                                 records: dict[str, SourceMessage], batch: ReadBatch,
                                 pages: _SlackPages) -> None:
    """Supplement history with in-window replies whose parent may be older."""
    # Slack date searches use local calendar days. Widen across timezone offsets,
    # then enforce exact aware timestamps, channel ID, and workspace on results.
    after = (lower.date() - timedelta(days=1)).isoformat()
    before = (upper.date() + timedelta(days=2)).isoformat()
    try:
        for page in range(1, MAX_PAGES + 1):
            pages.take()
            result = await runtime.execute(
                user_id, "slack", "slack_search",
                {"query": f"in:#{channel_name} after:{after} before:{before}",
                 "page": page, "count": 100, "sort": "timestamp", "sort_dir": "desc",
                 "highlight": False, "auto_paginate": False},
                expected_account_id=account_id,
            )
            _log(batch, result)
            container = result.data.get("messages")
            if result.data.get("ok") is not True or not isinstance(container, dict):
                raise _schema("slack", result.log_id)
            matches, paging = container.get("matches"), container.get("paging")
            if not isinstance(matches, list) or not isinstance(paging, dict):
                raise _schema("slack", result.log_id)
            for raw in matches:
                try:
                    if not isinstance(raw, dict) or not isinstance(raw.get("channel"), dict):
                        raise _schema("slack", result.log_id)
                    if raw["channel"].get("id") != channel_id or raw.get("team") not in {None, workspace_id}:
                        continue
                    message = _slack_message(raw, account_id, workspace_id, channel_id)
                    if message is None or not lower <= parse_time(message.date) < upper:
                        continue
                    message.channel_name = channel_name
                    existing = records.get(message.message_id)
                    if existing:
                        # History is fresher than a possibly lagging search index;
                        # search may enrich context but must not undo a known edit.
                        existing.channel_name = channel_name
                        existing.source_url = existing.source_url or message.source_url
                        existing.thread_id = existing.thread_id or message.thread_id
                    elif len(records) < MAX_SLACK_MESSAGES_PER_CHANNEL:
                        records[message.message_id] = message
                    else:
                        _issue(batch, _partial("slack", result.log_id))
                except IntegrationError as error:
                    _issue(batch, error)
            total_pages = paging.get("pages")
            if not isinstance(total_pages, int) or isinstance(total_pages, bool) or total_pages < 0:
                raise _schema("slack", result.log_id)
            if page >= total_pages:
                return
            if len(records) >= MAX_SLACK_MESSAGES_PER_CHANNEL:
                raise _partial("slack", result.log_id)
        raise _partial("slack")
    except IntegrationError as error:
        if error.code in _ACCOUNT_ERRORS:
            raise
        _issue(batch, error)


async def fetch_slack_messages(user_id: str, *, since: str, until: str) -> ReadBatch[SourceMessage]:
    """Read selected-channel history, threads, and recent replies to older roots.

    History normally omits permalinks. Keep those links nullable here and resolve
    them through fetch_source when requested, rather than adding one network call
    per message or fabricating a URL from user text.
    """
    lower, upper = _window(since, until, "slack")
    batch = ReadBatch(records=[])
    pages = _SlackPages()
    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, "slack")
        workspace_id, channels = _selected_slack(connection)
        if await _workspace(user_id, connection.account_id, batch) != workspace_id:
            raise IntegrationError("ACCOUNT_CHANGED", "slack")
        try:
            names = await _selected_channel_names(user_id, connection.account_id, workspace_id, channels, batch, pages)
        except IntegrationError as error:
            _issue(batch, error)
            return batch
        for channel_id in channels:
            records: dict[str, SourceMessage] = {}
            threads: list[str] = []
            # Each tuple is one selected-channel history or known parent thread.
            queue: list[tuple[str, str | None]] = [("slack_history", None)]
            for operation, parent_ts in queue:
                cursor = None
                seen: set[str] = set()
                while True:
                    if pages.used >= MAX_PAGES or len(records) >= MAX_SLACK_MESSAGES_PER_CHANNEL:
                        _issue(batch, _partial("slack"))
                        break
                    args = {"channel": channel_id, "oldest": _epoch_string(lower), "latest": _epoch_string(upper), "inclusive": True, "limit": MAX_SLACK_MESSAGES_PER_CHANNEL}
                    if parent_ts:
                        args["ts"] = parent_ts
                    if cursor:
                        args["cursor"] = cursor
                    pages.take()
                    try:
                        result = await runtime.execute(user_id, "slack", operation, args, expected_account_id=connection.account_id)
                        _log(batch, result)
                        for raw in _slack_rows(result.data, "messages"):
                            try:
                                message = _slack_message(raw, connection.account_id, workspace_id, channel_id, parent_ts=parent_ts)
                                if message is None or not lower <= parse_time(message.date) < upper:
                                    continue
                                message.channel_name = names.get(channel_id)
                                if message.message_id not in records and len(records) >= MAX_SLACK_MESSAGES_PER_CHANNEL:
                                    _issue(batch, _partial("slack", result.log_id))
                                    continue
                                records[message.message_id] = message
                                if operation == "slack_history" and isinstance(raw.get("reply_count"), int) and raw["reply_count"] > 0:
                                    root = message.thread_id or message.message_id
                                    if root not in threads:
                                        threads.append(root)
                                        queue.append(("slack_replies", root))
                            except IntegrationError as error:
                                _issue(batch, error)
                        cursor = _slack_cursor(result.data)
                        if not cursor:
                            if result.data.get("has_more"):
                                _issue(batch, _partial("slack", result.log_id))
                            break
                        if cursor in seen:
                            _issue(batch, _partial("slack", result.log_id))
                            break
                        seen.add(cursor)
                    except IntegrationError as error:
                        _issue(batch, error)
                        if error.code in _ACCOUNT_ERRORS:
                            batch.records.clear()
                            return batch
                        break
            if channel_id in names:
                try:
                    await _recent_channel_search(user_id, connection.account_id, workspace_id,
                                                 channel_id, names[channel_id], lower, upper,
                                                 records, batch, pages)
                except IntegrationError as error:
                    _issue(batch, error)
                    batch.records.clear()
                    return batch
            batch.records.extend(records.values())
    batch.records.sort(key=lambda record: (parse_time(record.date), record.channel_id or ""), reverse=True)
    return batch


async def _source_permalink(user_id: str, account_id: str, channel_id: str, message: SourceMessage) -> None:
    if message.source_url:
        return
    result = await runtime.execute(
        user_id, "slack", "slack_permalink",
        {"channel": channel_id, "message_ts": message.message_id},
        expected_account_id=account_id,
    )
    if result.data.get("ok") is not True or result.data.get("channel") not in {None, channel_id}:
        raise _schema("slack", result.log_id)
    message.source_url = safe_link(result.data.get("permalink"), "slack")


async def _find_slack_source(user_id: str, account_id: str, workspace_id: str, channel_id: str, timestamp: str) -> SourceMessage:
    """Read a root directly, or locate an exact reply through scoped search.

    Slack history omits non-broadcast replies and the stored source key intentionally
    excludes mutable thread metadata. Search is therefore a source lookup fallback,
    never a substitute for selected-channel history coverage.
    """
    sent = _slack_date(timestamp)
    result = await runtime.execute(
        user_id, "slack", "slack_history",
        {"channel": channel_id, "oldest": timestamp, "latest": timestamp, "inclusive": True, "limit": 1},
        expected_account_id=account_id,
    )
    for raw in _slack_rows(result.data, "messages"):
        message = _slack_message(raw, account_id, workspace_id, channel_id)
        if message and message.message_id == timestamp:
            await _source_permalink(user_id, account_id, channel_id, message)
            return message

    # Resolve an accessible channel ID to its current provider-returned name. The
    # search tool documents names; arbitrary query text never crosses this API.
    channels = await list_slack_channels(user_id)
    selected = next((channel for channel in channels.records if channel.id == channel_id and channel.account_id == account_id and channel.workspace_id == workspace_id), None)
    if selected is None:
        if not channels.complete:
            raise channels.issues[0]
        raise IntegrationError("SOURCE_NOT_FOUND", "slack")
    if not selected.readable:
        raise IntegrationError("PERMISSION_DENIED", "slack")
    if not re.fullmatch(r"[a-z0-9_-]+", selected.name):
        raise _schema("slack")
    # Date search uses workspace-local days. A buffer covers all UTC offsets;
    # the exact native ts and channel ID, not the search day, select the source.
    after = (sent.date() - timedelta(days=1)).isoformat()
    before = (sent.date() + timedelta(days=2)).isoformat()
    for page in range(1, MAX_PAGES + 1):
        result = await runtime.execute(
            user_id, "slack", "slack_search",
            {"query": f"in:#{selected.name} after:{after} before:{before}", "page": page, "count": 100, "sort": "timestamp", "sort_dir": "desc", "highlight": False, "auto_paginate": False},
            expected_account_id=account_id,
        )
        container = result.data.get("messages")
        if result.data.get("ok") is not True or not isinstance(container, dict):
            raise _schema("slack", result.log_id)
        matches, paging = container.get("matches"), container.get("paging")
        if not isinstance(matches, list) or not isinstance(paging, dict):
            raise _schema("slack", result.log_id)
        for raw in matches:
            if not isinstance(raw, dict) or not isinstance(raw.get("channel"), dict):
                raise _schema("slack", result.log_id)
            # Even provider search results are independently scoped before use.
            if raw["channel"].get("id") != channel_id or raw.get("team") not in {None, workspace_id} or raw.get("ts") != timestamp:
                continue
            message = _slack_message(raw, account_id, workspace_id, channel_id)
            if message:
                message.channel_name = selected.name
                await _source_permalink(user_id, account_id, channel_id, message)
                return message
        pages = paging.get("pages")
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0:
            raise _schema("slack", result.log_id)
        if page >= pages:
            raise IntegrationError("SOURCE_NOT_FOUND", "slack", log_id=result.log_id)
    raise _partial("slack", result.log_id)


async def fetch_source(user_id: str, item_id: str) -> SourceDetail:
    """Resolve only an owned cached item and its still-selected account namespace."""
    item = await repo.get_item(user_id, item_id)
    if item is None:
        raise IntegrationError("SOURCE_NOT_FOUND", "gmail")
    provider, parts = _source_parts(item, user_id)
    async with runtime.read_budget():
        connection = await connections.require_connection(user_id, provider)
        if connection.account_id != parts[0]:
            raise IntegrationError("SOURCE_NOT_FOUND", provider)
        if provider == "gmail":
            return _detail(await _get_gmail(user_id, connection.account_id, parts[1]))
        workspace_id, channels = _selected_slack(connection)
        if parts[1] != workspace_id or parts[2] not in channels:
            raise IntegrationError("SOURCE_NOT_FOUND", "slack")
        if await _workspace(user_id, connection.account_id) != workspace_id:
            raise IntegrationError("ACCOUNT_CHANGED", "slack")
        return _detail(await _find_slack_source(user_id, connection.account_id, workspace_id, parts[2], parts[3]))
