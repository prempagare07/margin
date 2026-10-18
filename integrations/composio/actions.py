"""Execute only reviewed, durably claimed Calendar events and Gmail drafts.

The stored plan is the authority. The request identifies an action; its params
are intentionally never read. A running or unresolved execution is never retried.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import Any

from db import repo
from shared.protocol import ActionRequest, ActionResult

from . import calendar, connections, messages, runtime
from .models import IntegrationError, iso, parse_source_key, parse_time, safe_link, validate_window


@dataclass(frozen=True, repr=False)
class EventParams:
    account_id: str
    calendar_id: str
    title: str
    starts_at: str
    ends_at: str
    description: str | None = None
    location: str | None = None
    purpose: str = "study_block"


@dataclass(frozen=True, repr=False)
class DraftParams:
    account_id: str
    to: tuple[str, ...]
    subject: str
    body: str
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    source_item_id: str | None = None
    thread_id: str | None = None


def _invalid(provider: str) -> IntegrationError:
    return IntegrationError("INVALID_ACTION", provider)


def _text(value: Any, provider: str, *, limit: int, header: bool = False) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or "\x00" in value or (header and any(ord(char) < 32 or ord(char) == 127 for char in value))):
        raise _invalid(provider)
    return value


def _optional_text(value: Any, provider: str, *, limit: int) -> str | None:
    return None if value is None else _text(value, provider, limit=limit)


def _recipients(value: Any, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > 100 or (required and not value):
        raise _invalid("gmail")
    result = []
    for candidate in value:
        candidate = _text(candidate, "gmail", limit=320, header=True)
        try:
            address = Address(addr_spec=candidate)
            if (candidate != candidate.strip() or not address.username or not address.domain
                    or "." not in address.domain or address.addr_spec != candidate
                    or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", part)
                           for part in address.domain.split("."))):
                raise ValueError
        except (ValueError, IndexError, HeaderParseError):
            raise _invalid("gmail") from None
        result.append(candidate)
    return tuple(result)


def _event_params(value: Any) -> EventParams:
    required = {"account_id", "calendar_id", "title", "starts_at", "ends_at"}
    optional = {"description", "location", "purpose"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise _invalid("gcal")
    try:
        start, end = validate_window(value["starts_at"], value["ends_at"])
    except (TypeError, ValueError):
        raise _invalid("gcal") from None
    purpose = value.get("purpose", "study_block")
    if not isinstance(purpose, str) or purpose not in {"study_block", "event"}:
        raise _invalid("gcal")
    title = _text(value["title"], "gcal", limit=500, header=True).strip()
    while title.startswith("[Margin]"):
        title = title[len("[Margin]"):].lstrip()
    if not title:
        raise _invalid("gcal")
    return EventParams(
        account_id=_text(value["account_id"], "gcal", limit=512, header=True),
        calendar_id=_text(value["calendar_id"], "gcal", limit=1024, header=True),
        title=f"[Margin] {title}", starts_at=iso(start), ends_at=iso(end),
        description=_optional_text(value.get("description"), "gcal", limit=12000),
        location=_optional_text(value.get("location"), "gcal", limit=1000), purpose=purpose,
    )


def _draft_params(value: Any) -> DraftParams:
    required = {"account_id", "to", "subject", "body"}
    optional = {"cc", "bcc", "source_item_id", "thread_id"}
    if not isinstance(value, dict) or not required <= value.keys() or value.keys() - required - optional:
        raise _invalid("gmail")
    source = _optional_text(value.get("source_item_id"), "gmail", limit=512)
    thread = _optional_text(value.get("thread_id"), "gmail", limit=512)
    if thread and not source:
        raise _invalid("gmail")
    return DraftParams(
        account_id=_text(value["account_id"], "gmail", limit=512, header=True),
        to=_recipients(value["to"], required=True),
        cc=_recipients(value.get("cc", [])), bcc=_recipients(value.get("bcc", [])),
        subject=_text(value["subject"], "gmail", limit=500, header=True),
        body=_text(value["body"], "gmail", limit=24000),
        source_item_id=source, thread_id=thread,
    )


def _result(action_id: str, status: str, *, data: dict | None = None,
            error: str | None = None) -> ActionResult:
    receipt = dict(data or {})
    receipt["execution_status"] = status
    return ActionResult(action_id=action_id, ok=status == "succeeded",
                        link=receipt.get("link"), data=receipt, error=error)


def _replay(action_id: str, execution: dict) -> ActionResult:
    status = execution.get("status")
    data = execution.get("result") or {}
    if not isinstance(data, dict):
        return _result(action_id, "unknown", error="RECEIPT_UNAVAILABLE")
    if status == "succeeded":
        required_id = "event_id" if data.get("action_type") == "create_event" else "draft_id"
        if not isinstance(data.get(required_id), str) or not data[required_id]:
            return _result(action_id, "unknown", error="RECEIPT_UNAVAILABLE")
        return _result(action_id, "succeeded", data=data)
    if status == "failed":
        return _result(action_id, "failed", data=data, error=execution.get("error") or "INVALID_ACTION")
    if status == "running":
        return _result(action_id, "running", data=data, error="EXECUTION_RUNNING")
    return _result(action_id, "unknown", data=data, error=execution.get("error") or "OUTCOME_UNKNOWN")


async def _persist(request: ActionRequest, status: str, receipt: dict,
                   error: str | None = None) -> ActionResult:
    """A failed receipt write cannot turn a remote success into a safe retry."""
    receipt = dict(receipt, execution_status=status)
    try:
        await repo.finish_action_execution(request.plan_id, request.action_id, request.user_id,
                                           status=status, result=receipt, error=error)
        return _result(request.action_id, status, data=receipt, error=error)
    except asyncio.CancelledError:
        raise
    except Exception:
        # A database timeout may have happened after COMMIT. Replay that durable
        # result if readable; otherwise retain an unresolved, non-retryable claim.
        try:
            saved = await repo.get_action_execution(request.plan_id, request.action_id, request.user_id)
            if saved and saved.get("status") in {"succeeded", "failed"}:
                return _replay(request.action_id, saved)
        except Exception:
            saved = None
        unresolved = dict(receipt, execution_status="unknown")
        if status == "succeeded":
            unresolved["remote_outcome"] = "succeeded"
        if saved and saved.get("status") == "running":
            try:
                await repo.finish_action_execution(
                    request.plan_id, request.action_id, request.user_id,
                    status="unknown", result=unresolved, error="RECEIPT_UNAVAILABLE",
                )
            except Exception:
                pass
        return _result(request.action_id, "unknown", data=unresolved, error="RECEIPT_UNAVAILABLE")


async def _event_preflight(user_id: str, params: EventParams, connection: Any) -> None:
    settings = connection.settings
    if (settings.get("write_calendar_id") != params.calendar_id
            or params.calendar_id not in settings.get("calendar_ids", [])):
        raise _invalid("gcal")
    calendars = await calendar.list_calendars(user_id)
    if not calendars.complete:
        raise IntegrationError("PARTIAL_RESULT", "gcal")
    target = next((option for option in calendars.records if option.id == params.calendar_id
                   and option.account_id == params.account_id), None)
    if target is None or not target.writable:
        raise IntegrationError("PERMISSION_DENIED", "gcal")
    if params.purpose != "study_block":
        return
    batch = await calendar.fetch_calendar_events(user_id, start=params.starts_at, end=params.ends_at)
    if not batch.complete:
        raise IntegrationError("PARTIAL_RESULT", "gcal")
    start, end = validate_window(params.starts_at, params.ends_at)
    for record in batch.records:
        if not record.blocks_time:
            continue
        try:
            busy_start = parse_time(record.item.starts_at)
            busy_end = parse_time(record.item.ends_at)
        except (TypeError, ValueError):
            raise IntegrationError("SCHEMA_MISMATCH", "gcal") from None
        if busy_start < busy_end and busy_start < end and busy_end > start:
            raise IntegrationError("CONFLICT_REVIEW_REQUIRED", "gcal")


async def _draft_preflight(user_id: str, params: DraftParams) -> str | None:
    if params.source_item_id is None:
        return None
    source = await repo.get_item(user_id, params.source_item_id)
    if source is None or source.get("source") != "gmail":
        raise _invalid("gmail")
    try:
        key = parse_source_key(source["source_id"])
    except (KeyError, TypeError, ValueError):
        raise _invalid("gmail") from None
    if len(key) != 2 or key[0] != params.account_id:
        raise _invalid("gmail")
    detail = await messages.fetch_source(user_id, params.source_item_id)
    if not detail.thread_id or (params.thread_id is not None and params.thread_id != detail.thread_id):
        raise _invalid("gmail")
    if not isinstance(detail.subject, str) or _reply_subject(detail.subject) != _reply_subject(params.subject):
        raise _invalid("gmail")
    return detail.thread_id


def _reply_subject(value: str) -> str:
    # Gmail's draft tool starts a NEW thread if subject is passed with thread_id.
    # For a verified reply we omit subject, then verify the inherited subject.
    return re.sub(r"^(?:\s*re\s*:\s*)+", "", value, flags=re.IGNORECASE).strip()


def _event_arguments(params: EventParams) -> dict:
    # Exact fields from GOOGLECALENDAR_CREATE_EVENT 20261010_00. In particular,
    # conference creation defaults to True in that tool, so explicitly turn it off.
    result = {
        "calendar_id": params.calendar_id, "summary": params.title,
        "start_datetime": params.starts_at, "end_datetime": params.ends_at,
        "timezone": "UTC", "eventType": "default", "transparency": "opaque",
        "attendees": [], "exclude_organizer": True,
        "create_meeting_room": False, "send_updates": "none",
    }
    if params.description is not None:
        result["description"] = params.description
    if params.location is not None:
        result["location"] = params.location
    return result


def _draft_arguments(params: DraftParams, thread_id: str | None) -> dict:
    # Exact fields from GMAIL_CREATE_EMAIL_DRAFT 20261015_00. The tool accepts
    # plain text via is_html=False, then converts it to email HTML internally.
    result = {
        "user_id": "me", "recipient_email": params.to[0],
        "extra_recipients": list(params.to[1:]), "cc": list(params.cc), "bcc": list(params.bcc),
        "body": params.body, "is_html": False,
    }
    if thread_id is None:
        result["subject"] = params.subject
    else:
        result["thread_id"] = thread_id
    return result


def _created_id(value: Any, provider: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise IntegrationError("SCHEMA_MISMATCH", provider, may_have_written=True)
    return value


def _event_receipt(data: Any, params: EventParams) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("response_data"), dict):
        raise IntegrationError("SCHEMA_MISMATCH", "gcal", may_have_written=True)
    event = data["response_data"]
    native_id = _created_id(event.get("id"), "gcal")
    if event.get("status") == "cancelled":
        raise IntegrationError("SCHEMA_MISMATCH", "gcal", may_have_written=True)
    link = safe_link(event.get("htmlLink") or event.get("display_url") or data.get("display_url"), "gcal")
    return {"event_id": native_id, "calendar_id": params.calendar_id, "link": link,
            "confirmation": "Event created."}


def _draft_receipt(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("message"), dict):
        raise IntegrationError("SCHEMA_MISMATCH", "gmail", may_have_written=True)
    native_id = _created_id(data.get("id"), "gmail")
    message = data["message"]
    link = safe_link(data.get("display_url") or message.get("display_url"), "gmail")
    folder = "https://mail.google.com/mail/u/0/#drafts"
    receipt = {"draft_id": native_id, "link": link or folder,
               "link_kind": "draft" if link and link != folder else "drafts_folder",
               "confirmation": "Draft created."}
    for native_field, field in (("id", "message_id"), ("threadId", "thread_id")):
        if message.get(native_field) is not None:
            receipt[field] = _created_id(message[native_field], "gmail")
    return receipt


def _draft_subject(message: dict) -> str | None:
    if isinstance(message.get("subject"), str):
        return message["subject"]
    payload = message.get("payload")
    headers = payload.get("headers") if isinstance(payload, dict) else None
    if isinstance(headers, list):
        values = [header.get("value") for header in headers if isinstance(header, dict)
                  and isinstance(header.get("name"), str) and header["name"].lower() == "subject"]
        if len(values) == 1 and isinstance(values[0], str):
            return values[0]
    return None


async def _verify_reply(user_id: str, params: DraftParams, thread_id: str,
                        data: dict, receipt: dict) -> None:
    if receipt.get("thread_id") != thread_id:
        raise IntegrationError("CONTENT_MISMATCH", "gmail", may_have_written=True)
    subject = _draft_subject(data["message"])
    if subject is None:
        result = await runtime.execute(
            user_id, "gmail", "gmail_get_draft", {"user_id": "me", "draft_id": receipt["draft_id"],
                                                       "format": "metadata"},
            expected_account_id=params.account_id,
        )
        receipt["verification_log_id"] = result.log_id
        verified = result.data
        if (not isinstance(verified, dict) or verified.get("id") != receipt["draft_id"]
                or not isinstance(verified.get("message"), dict)
                or verified["message"].get("threadId") != thread_id):
            raise IntegrationError("CONTENT_MISMATCH", "gmail", may_have_written=True)
        subject = _draft_subject(verified["message"])
    if subject is None or _reply_subject(subject) != _reply_subject(params.subject):
        raise IntegrationError("CONTENT_MISMATCH", "gmail", may_have_written=True)


async def _refresh_event_cache(user_id: str, params: EventParams, receipt: dict) -> None:
    try:
        batch = await calendar.fetch_calendar_events(user_id, start=params.starts_at, end=params.ends_at)
        if not batch.complete:
            raise IntegrationError("PARTIAL_RESULT", "gcal")
        await repo.upsert_items([record.item.model_dump() for record in batch.records])
    except asyncio.CancelledError:
        raise
    except Exception:
        receipt["cache_refresh_error"] = "CACHE_REFRESH_FAILED"


async def execute_approved_action(request: ActionRequest) -> ActionResult:
    """Dispatch one approved stored action, or return its durable prior outcome."""
    if request.type not in {"create_event", "create_draft"}:
        return _result(request.action_id, "not_started", error="INVALID_ACTION")
    provider = "gcal" if request.type == "create_event" else "gmail"
    try:
        plan = await repo.get_plan(request.plan_id, request.user_id)
        if plan is None:
            return _result(request.action_id, "not_started", error="INVALID_ACTION")
        matches = [action for action in plan.get("actions", [])
                   if isinstance(action, dict) and action.get("action_id") == request.action_id]
        if len(matches) != 1 or matches[0].get("type") != request.type:
            return _result(request.action_id, "not_started", error="INVALID_ACTION")
        stored = matches[0]
        execution = await repo.get_action_execution(request.plan_id, request.action_id, request.user_id)
        if execution and execution.get("status") in {"succeeded", "failed", "running", "unknown"}:
            return _replay(request.action_id, execution)
        if plan.get("status") != "approved" or not execution or execution.get("status") != "pending":
            return _result(request.action_id, "not_started", error="PLAN_NOT_APPROVED")
        if not await repo.claim_action_execution(request.plan_id, request.action_id, request.user_id):
            execution = await repo.get_action_execution(request.plan_id, request.action_id, request.user_id)
            return (_replay(request.action_id, execution) if execution
                    else _result(request.action_id, "not_started", error="INVALID_ACTION"))
    except asyncio.CancelledError:
        raise
    except Exception:
        return _result(request.action_id, "not_started", error="DATABASE_UNAVAILABLE")

    receipt = {"provider": provider, "action_type": request.type}
    dispatched = False
    write_returned = False
    try:
        params = _event_params(stored.get("params")) if provider == "gcal" else _draft_params(stored.get("params"))
        receipt["account_id"] = params.account_id
        connection = await connections.require_connection(request.user_id, provider)
        if connection.account_id != params.account_id:
            raise _invalid(provider)
        if isinstance(params, EventParams):
            await _event_preflight(request.user_id, params, connection)
            arguments = _event_arguments(params)
            operation = "event_create"
        else:
            thread_id = await _draft_preflight(request.user_id, params)
            arguments = _draft_arguments(params, thread_id)
            operation = "gmail_draft"
        # runtime disables SDK retries for writes and records whether an error
        # happened after actual dispatch. A generic error after this point is
        # conservatively unresolved, including coroutine cancellation.
        dispatched = True
        response = await runtime.execute(request.user_id, provider, operation, arguments,
                                         write=True, expected_account_id=params.account_id)
        write_returned = True
        receipt["log_id"] = response.log_id
        receipt.update(_event_receipt(response.data, params) if isinstance(params, EventParams)
                       else _draft_receipt(response.data))
        if isinstance(params, EventParams):
            await _refresh_event_cache(request.user_id, params, receipt)
        elif thread_id is not None:
            await _verify_reply(request.user_id, params, thread_id, response.data, receipt)
        return await _persist(request, "succeeded", receipt)
    except asyncio.CancelledError:
        status = "unknown" if dispatched else "failed"
        error = "OUTCOME_UNKNOWN" if dispatched else "ACTION_CANCELLED"
        await asyncio.shield(_persist(request, status, receipt, error))
        raise
    except IntegrationError as error:
        if error.log_id:
            receipt["verification_log_id" if write_returned else "log_id"] = error.log_id
        unknown = error.may_have_written or write_returned
        if write_returned:
            receipt["verification_error"] = error.code
        return await _persist(request, "unknown" if unknown else "failed", receipt,
                              "OUTCOME_UNKNOWN" if unknown else error.code)
    except Exception:
        return await _persist(request, "unknown" if dispatched else "failed", receipt,
                              "OUTCOME_UNKNOWN" if dispatched else "TEMPORARY_UNAVAILABLE")
