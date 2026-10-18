"""Bounded, user-scoped Composio sessions. No arbitrary execution is public."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from functools import wraps

import sentry_sdk
from composio import Composio, SESSION_PRESET_DIRECT_TOOLS
from jsonschema import Draft202012Validator

from .models import IntegrationError, ToolResult

logger = logging.getLogger(__name__)
TOOLKITS = {"gmail": "gmail", "gcal": "googlecalendar", "slack": "slack"}
AUTH_ENV = {"gmail": "GMAIL_AUTH_CONFIG_ID", "gcal": "GCAL_AUTH_CONFIG_ID", "slack": "SLACK_AUTH_CONFIG_ID"}
REGISTRY = {
    "gmail": {"gmail_list": "GMAIL_FETCH_EMAILS", "gmail_get": "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID",
              "gmail_draft": "GMAIL_CREATE_EMAIL_DRAFT", "gmail_get_draft": "GMAIL_GET_DRAFT",
              "gmail_list_drafts": "GMAIL_LIST_DRAFTS"},
    "gcal": {"calendar_list": "GOOGLECALENDAR_LIST_CALENDARS", "event_list": "GOOGLECALENDAR_EVENTS_LIST",
             "event_get": "GOOGLECALENDAR_EVENTS_GET", "event_create": "GOOGLECALENDAR_CREATE_EVENT"},
    "slack": {"slack_channels": "SLACK_LIST_ALL_CHANNELS", "slack_history": "SLACK_FETCH_CONVERSATION_HISTORY",
              "slack_replies": "SLACK_FETCH_MESSAGE_THREAD_FROM_A_CONVERSATION",
              "slack_permalink": "SLACK_RETRIEVE_MESSAGE_PERMALINK_URL", "slack_identity": "SLACK_TEST_AUTH",
              "slack_search": "SLACK_SEARCH_MESSAGES"},
}
WRITES = {"gmail_draft", "event_create"}
WRITE_PROFILES = {
    "gmail": {"gmail_draft", "gmail_get", "gmail_get_draft", "gmail_list_drafts"},
    "gcal": {"event_create", "calendar_list", "event_list", "event_get"},
    "slack": set(),
}
CONTRACTS = json.loads(Path(__file__).with_name("schema_contracts.json").read_text())
_deadline: ContextVar[float | None] = ContextVar("composio_deadline", default=None)
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="margin-composio")
_slots = threading.BoundedSemaphore(4)
_local = threading.local()
_session_ids: dict[tuple, str] = {}
_session_locks: dict[tuple, threading.Lock] = {}
_lock = threading.Lock()


def config(provider: str) -> tuple[str, str]:
    if provider not in TOOLKITS or not os.environ.get("COMPOSIO_API_KEY"):
        raise IntegrationError("CONFIGURATION_ERROR", provider)
    auth = os.environ.get(AUTH_ENV[provider], "")
    if not auth:
        raise IntegrationError("CONFIGURATION_ERROR", provider)
    return TOOLKITS[provider], auth


def validate_user(user_id: str) -> None:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 512:
        raise IntegrationError("INVALID_ACTION", "composio")


def _client():
    key = os.environ.get("COMPOSIO_API_KEY", "")
    if not key:
        raise IntegrationError("CONFIGURATION_ERROR", "composio")
    fingerprint = hashlib.sha256(key.encode()).hexdigest()
    if getattr(_local, "fingerprint", None) != fingerprint:
        _local.client = Composio(timeout=10, max_retries=0)
        _local.fingerprint = fingerprint
        _local.checked = set()
    return _local.client


@asynccontextmanager
async def read_budget(seconds: float = 30):
    """Nested discovery/pagination calls share one wall-clock budget."""
    current = _deadline.get()
    token = _deadline.set(min(current, time.monotonic() + seconds) if current else time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _exception(exc: Exception, provider: str, *, write: bool = False) -> IntegrationError:
    if isinstance(exc, IntegrationError):
        return exc
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    request_id = getattr(exc, "request_id", None)
    if name == "ToolInputRequiredError":
        return IntegrationError("INPUT_REQUIRED", provider, log_id=request_id)
    if status == 401:
        return IntegrationError("CONFIGURATION_ERROR", provider, log_id=request_id)
    if status == 403:
        return IntegrationError("PERMISSION_DENIED", provider, log_id=request_id)
    if status == 404:
        return IntegrationError("SOURCE_NOT_FOUND", provider, log_id=request_id)
    if status in (400, 422):
        return IntegrationError("SCHEMA_MISMATCH", provider, log_id=request_id)
    retry_after = None
    try:
        retry_after = max(0, min(30, float(exc.response.headers.get("retry-after"))))
    except (AttributeError, TypeError, ValueError):
        pass
    return IntegrationError("OUTCOME_UNKNOWN" if write else ("RATE_LIMITED" if status == 429 else "TEMPORARY_UNAVAILABLE"),
                            provider, retryable=not write, log_id=request_id,
                            may_have_written=write, retry_after=retry_after)


async def sdk_call(function, *, provider: str, operation: str, retry: bool = True):
    """Run metadata/read operations off-loop. No provider content is logged."""
    async with read_budget():
        for attempt in range(3 if retry else 1):
            remaining = _deadline.get() - time.monotonic()
            if remaining <= 0:
                raise IntegrationError("TEMPORARY_UNAVAILABLE", provider, retryable=True)
            while not _slots.acquire(blocking=False):
                if time.monotonic() >= _deadline.get():
                    raise IntegrationError("TEMPORARY_UNAVAILABLE", provider, retryable=True)
                await asyncio.sleep(.02)
            def run():
                try:
                    return function(_client())
                finally:
                    _slots.release()
            future = _executor.submit(run)
            wrapped = asyncio.wrap_future(future)
            # A timed-out shielded worker can finish later; consume its exception
            # without logging provider bodies or creating an unhandled-task log.
            wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            try:
                with sentry_sdk.start_span(op="tool.composio", name=f"{provider}.{operation}") as span:
                    span.set_data("attempt", attempt + 1)
                    result = await asyncio.wait_for(asyncio.shield(wrapped),
                                                    timeout=max(.001, _deadline.get() - time.monotonic()))
                    span.set_data("outcome", "completed")
                    span.set_data("log_id", getattr(result, "log_id", None))
                    return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = _exception(exc, provider)
                span.set_data("outcome", error.code)
                span.set_data("log_id", error.log_id)
                logger.warning("Composio %s.%s failed (%s); log_id=%s", provider, operation, error.code, error.log_id)
                delay = error.retry_after if error.retry_after is not None else .25 * 2 ** attempt
                if not retry or not error.retryable or attempt == 2 or time.monotonic() + delay >= _deadline.get():
                    raise error from None
                await asyncio.sleep(delay)


def invalidate_sessions(user_id: str, provider: str) -> None:
    with _lock:
        for key in list(_session_ids):
            if key[:2] == (user_id, provider):
                _session_ids.pop(key, None)


def _session(client, user_id: str, provider: str, account_id: str, write: bool):
    toolkit, auth = config(provider)
    key = (user_id, provider, account_id, write, auth, getattr(_local, "fingerprint", ""))
    with _lock:
        guard = _session_locks.setdefault(key, threading.Lock())
    with guard:
        existing = _session_ids.get(key)
        if existing:
            try:
                return client.sessions.use(existing)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
                _session_ids.pop(key, None)
        allowed = [slug for op, slug in REGISTRY[provider].items()
                   if (op in WRITE_PROFILES[provider] if write else op not in WRITES)]
        session = client.sessions.create(
            user_id=user_id, toolkits=[toolkit], tools={toolkit: {"enable": allowed}},
            auth_configs={toolkit: auth}, connected_accounts={toolkit: [account_id]},
            session_preset=SESSION_PRESET_DIRECT_TOOLS, manage_connections=False,
            sandbox={"enable": False}, proxy_execute=False, instant=False,
        )
        _session_ids[key] = session.session_id
        return session


def _schema(client, slug: str, arguments: dict, provider: str):
    contract = CONTRACTS[slug]
    if slug not in getattr(_local, "checked", set()):
        actual = client.tools.get_raw_composio_tool_by_slug(slug)
        if actual.version != contract["version"] or actual.input_parameters != contract["input_parameters"]:
            raise IntegrationError("SCHEMA_MISMATCH", provider)
        _local.checked = getattr(_local, "checked", set()) | {slug}
    schema = contract["input_parameters"]
    if set(arguments) - set(schema.get("properties", {})):
        raise IntegrationError("INVALID_ACTION", provider)
    if not Draft202012Validator(schema).is_valid(arguments):
        raise IntegrationError("INVALID_ACTION", provider)


def _provider_failure(body, provider: str, log_id: str | None, write: bool) -> IntegrationError:
    # Only exact structured error codes establish a definite rejection. Echoed
    # message/draft content and free-form exception text are not error evidence.
    def codes(value):
        if isinstance(value, (str, int)):
            return {str(value).lower()}
        if isinstance(value, list):
            return set().union(*(codes(v) for v in value)) if value else set()
        if isinstance(value, dict):
            return set().union(*(codes(v) for k, v in value.items()
                if k in {"error", "errors", "code", "status", "reason", "error_code", "status_code"}))
        return set()
    found = codes(body)
    if found & {"invalid_auth", "token_revoked", "token_expired", "invalid_credentials", "unauthenticated", "401"}:
        return IntegrationError("RELINK_REQUIRED", provider, log_id=log_id)
    if found & {"missing_scope", "not_in_channel", "permission_denied", "forbidden", "access_denied", "403"}:
        return IntegrationError("PERMISSION_DENIED", provider, log_id=log_id)
    if found & {"not_found", "channel_not_found", "message_not_found", "404"}:
        return IntegrationError("SOURCE_NOT_FOUND", provider, log_id=log_id)
    limited = bool(found & {"ratelimited", "rate_limited", "rate_limit_exceeded", "429"})
    return IntegrationError("OUTCOME_UNKNOWN" if write else ("RATE_LIMITED" if limited else "TEMPORARY_UNAVAILABLE"),
                            provider, retryable=not write, log_id=log_id, may_have_written=write)


def _result(response, provider: str, write: bool) -> ToolResult:
    raw = response if isinstance(response, dict) else response.model_dump()
    log_id = raw.get("log_id") or None
    if raw.get("error"):
        raise _provider_failure(raw["error"], provider, log_id, write)
    if raw.get("result_type") != "completed":
        if raw.get("result_type") in {"input_required", "awaiting_approval"}:
            raise IntegrationError("INPUT_REQUIRED", provider, log_id=log_id)
        if raw.get("result_type") == "failed":
            raise _provider_failure(raw.get("error") or raw.get("data"), provider, log_id, write)
        raise IntegrationError("SCHEMA_MISMATCH", provider, log_id=log_id, may_have_written=write)
    data = raw.get("data")
    if not isinstance(data, dict):
        raise IntegrationError("SCHEMA_MISMATCH", provider, log_id=log_id, may_have_written=write)
    if "successful" in data:
        if data["successful"] is not True or data.get("error"):
            raise _provider_failure(data, provider, log_id, write)
        data = data.get("data")
    if not isinstance(data, dict):
        raise IntegrationError("SCHEMA_MISMATCH", provider, log_id=log_id, may_have_written=write)
    if data.get("ok") is False or data.get("error"):
        raise _provider_failure(data, provider, log_id, write)
    return ToolResult(data, log_id)


async def execute(user_id: str, provider: str, operation: str, arguments: dict, *,
                  write: bool = False, expected_account_id: str | None = None) -> ToolResult:
    """Internal allowlisted execution; write=True is used only by the claimed dispatcher."""
    from .connections import require_connection
    validate_user(user_id)
    if operation not in REGISTRY.get(provider, {}) or write != (operation in WRITES):
        raise IntegrationError("INVALID_ACTION", provider)
    async with read_budget():
        connection = await require_connection(user_id, provider)
        if expected_account_id is not None and connection.account_id != expected_account_id:
            raise IntegrationError("ACCOUNT_CHANGED", provider)
        slug = REGISTRY[provider][operation]
        dispatched = threading.Event()
        abandoned = threading.Event()
        dispatch_lock = threading.Lock()
        deadline = _deadline.get()
        def run(client):
            _schema(client, slug, arguments, provider)
            session = _session(client, user_id, provider, connection.account_id, write)
            with dispatch_lock:
                if abandoned.is_set() or time.monotonic() >= deadline:
                    raise IntegrationError("TEMPORARY_UNAVAILABLE", provider, retryable=not write)
                dispatched.set()
            try:
                response = session.execute(slug, arguments=arguments)
            except Exception as exc:
                raise _exception(exc, provider, write=write) from None
            return _result(response, provider, write)
        try:
            result = await sdk_call(run, provider=provider, operation=operation, retry=not write)
        except asyncio.CancelledError:
            with dispatch_lock:
                abandoned.set()
            raise
        except IntegrationError as error:
            with dispatch_lock:
                abandoned.set()
            if error.code == "RELINK_REQUIRED":
                from db import repo
                invalidate_sessions(user_id, provider)
                await repo.set_connection_status(user_id, provider, connection.account_id, "relink_required")
            if write and dispatched.is_set() and error.retryable:
                raise IntegrationError("OUTCOME_UNKNOWN", provider, may_have_written=True, log_id=error.log_id) from None
            raise
        logger.info("Composio %s.%s completed; log_id=%s", provider, operation, result.log_id)
        return result


def observe_read(provider: str, operation: str, function):
    """Facade-level coverage telemetry, containing only counts and opaque IDs."""
    @wraps(function)
    async def observed(user_id, *args, **kwargs):
        with sentry_sdk.start_span(op="integration.composio", name=f"{provider}.{operation}") as span:
            span.set_data("user_correlation", hashlib.sha256(str(user_id).encode()).hexdigest()[:16])
            span.set_data("provider", provider)
            try:
                result = await function(user_id, *args, **kwargs)
            except IntegrationError as error:
                span.set_data("outcome", error.code)
                span.set_data("log_id", error.log_id)
                raise
            if hasattr(result, "records"):
                span.set_data("result_count", len(result.records))
                span.set_data("complete", result.complete)
                span.set_data("log_ids", result.log_ids or None)
                span.set_data("outcome", "completed" if result.complete else "partial")
            else:
                span.set_data("outcome", "completed")
            return result
    return observed
