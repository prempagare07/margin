"""Sentry init, span helpers, and structured logging. Owner: Prem."""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncGenerator, Generator

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration, SentryLogsHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Init
# ---------------------------------------------------------------------------

def init() -> None:
    """Initialize Sentry. Call once at process startup (bureau, web server)."""
    dsn = os.environ.get("SENTRY_DSN", "")
    if not dsn:
        logger.info("observability: SENTRY_DSN not set — Sentry disabled")
        return

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("ENV", "dev"),
        release=os.environ.get("GIT_SHA", "dev"),
        traces_sample_rate=1.0 if os.environ.get("ENV") == "dev" else 0.1,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        integrations=[
            LoggingIntegration(
                level=logging.INFO,
                event_level=logging.ERROR,
            ),
        ],
        before_send=_before_send,
        before_send_transaction=_before_transaction,
    )

    # Attach SentryLogsHandler to root logger for structured Sentry Logs
    _attach_sentry_logs_handler()
    logger.info("observability: Sentry initialized (env=%s)", os.environ.get("ENV", "dev"))


def _before_send(event: dict, hint: dict) -> dict | None:
    """Strip LLM prompt/response data from breadcrumbs when SENTRY_INCLUDE_PROMPTS=0."""
    from web.privacy import is_private_request
    if is_private_request(event):
        return None
    if os.environ.get("SENTRY_INCLUDE_PROMPTS", "0") == "1":
        return event
    for crumb in event.get("breadcrumbs", {}).get("values", []):
        data = crumb.get("data") or {}
        for key in ("prompt", "messages", "response", "content"):
            data.pop(key, None)
    return event


def _before_transaction(event: dict, hint: dict) -> dict | None:
    from web.privacy import is_private_request
    return None if is_private_request(event) else event


def _attach_sentry_logs_handler() -> None:
    """Attach SentryLogsHandler to root so structured logs appear in Sentry Logs."""
    try:
        root = logging.getLogger()
        # Avoid adding duplicate handlers on reload
        if not any(isinstance(h, SentryLogsHandler) for h in root.handlers):
            handler = SentryLogsHandler()
            handler.setLevel(logging.WARNING)
            root.addHandler(handler)
    except Exception as exc:
        logger.debug("_attach_sentry_logs_handler failed: %s", exc)


# ---------------------------------------------------------------------------
# Span helpers
# ---------------------------------------------------------------------------

@contextmanager
def span(op: str, description: str = "", **data: Any) -> Generator[Any, None, None]:
    """Sync context manager that wraps a block in a Sentry span.

    Usage::
        with span("db.query", "fetch items", user_id=uid):
            ...
    """
    with sentry_sdk.start_span(op=op, description=description) as s:
        for k, v in data.items():
            s.set_data(k, v)
        t0 = time.monotonic()
        try:
            yield s
        except Exception as exc:
            s.set_status("internal_error")
            sentry_sdk.capture_exception(exc)
            raise
        finally:
            s.set_data("duration_ms", int((time.monotonic() - t0) * 1000))


@asynccontextmanager
async def aspan(op: str, description: str = "", **data: Any) -> AsyncGenerator[Any, None]:
    """Async context manager that wraps a block in a Sentry span.

    Usage::
        async with aspan("llm.plan", "step5 call", user_id=uid):
            result, model = await llm_plan(messages)
    """
    with sentry_sdk.start_span(op=op, description=description) as s:
        for k, v in data.items():
            s.set_data(k, v)
        t0 = time.monotonic()
        try:
            yield s
        except Exception as exc:
            s.set_status("internal_error")
            sentry_sdk.capture_exception(exc)
            raise
        finally:
            s.set_data("duration_ms", int((time.monotonic() - t0) * 1000))


def start_transaction(name: str, op: str = "task", **data: Any) -> Any:
    """Start a Sentry transaction. Returns the transaction object (use as context manager)."""
    tx = sentry_sdk.start_transaction(name=name, op=op)
    for k, v in data.items():
        tx.set_data(k, v)
    return tx


# ---------------------------------------------------------------------------
# Structured log helpers (Sentry Logs — warning+ sent to Sentry)
# ---------------------------------------------------------------------------

def log_warning(msg: str, **kw: Any) -> None:
    logger.warning(msg, extra=kw)


def log_error(msg: str, **kw: Any) -> None:
    logger.error(msg, extra=kw)


# ---------------------------------------------------------------------------
# Uptime / cron monitor decorators
# ---------------------------------------------------------------------------

def monitor(slug: str):
    """Wrap an async function with a Sentry cron check-in.

    Usage::
        @monitor("morning-brief")
        async def run_morning_briefs():
            ...
    """
    def decorator(fn):
        import functools

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            with sentry_sdk.monitor(monitor_slug=slug):
                return await fn(*args, **kwargs)

        return wrapper
    return decorator
