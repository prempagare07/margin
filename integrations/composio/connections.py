"""Verify account ownership with Composio; persist only IDs and selected resources."""

from dataclasses import asdict, is_dataclass
import os
from urllib.parse import urlsplit

from composio import SESSION_PRESET_DIRECT_TOOLS

from db import repo
from . import runtime
from .models import ConnectionSettings, ConnectionState, IntegrationError


def _value(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _summary(account):
    """Drop OAuth state before returning SDK data across the adapter boundary."""
    auth = _value(account, "auth_config", {})
    return {"id": _value(account, "id"), "user_id": _value(account, "user_id"),
            "toolkit": _value(_value(account, "toolkit", {}), "slug"),
            "auth_config": _value(auth, "id"), "status": _value(account, "status"),
            "disabled": bool(_value(account, "is_disabled") or _value(auth, "is_disabled"))}


async def _accounts(user_id: str, app: str) -> list[dict]:
    runtime.validate_user(user_id)
    toolkit, auth = runtime.config(app)
    result, cursor = [], None
    async with runtime.read_budget():
        for _ in range(10):
            def fetch(client):
                response = client.connected_accounts.list(user_ids=[user_id], toolkit_slugs=[toolkit],
                    auth_config_ids=[auth], account_type="PRIVATE", limit=100, cursor=cursor)
                return [_summary(a) for a in response.items], response.next_cursor
            rows, cursor = await runtime.sdk_call(fetch, provider=app, operation="connections")
            for row in rows:
                if (row["id"] and row["toolkit"] == toolkit and row["auth_config"] == auth
                        and row["user_id"] in (None, user_id)):
                    result.append(row)
            if not cursor:
                return result
    raise IntegrationError("PARTIAL_RESULT", app)


def _active(account: dict) -> bool:
    return account["status"] == "ACTIVE" and not account["disabled"]


async def _persist(user_id, app, account_id, *, settings=None):
    current = await repo.get_connection(user_id, app)
    if settings is None:
        settings = ((current or {}).get("secret") or {}).get("settings", {}) if (current or {}).get("composio_account_id") == account_id else {}
    await repo.upsert_user(user_id)
    await repo.save_connection(user_id, app, composio_account_id=account_id,
                               secret={"settings": settings} if settings else None, status="active")
    if (current or {}).get("composio_account_id") != account_id:
        runtime.invalidate_sessions(user_id, app)
    return ConnectionState(app, "active", account_id, settings)


async def connection_status(user_id: str, app: str) -> ConnectionState:
    runtime.validate_user(user_id)
    rows = await _accounts(user_id, app)
    saved = await repo.get_connection(user_id, app)
    secret = (saved or {}).get("secret") or {}
    selected = secret.get("pending_account_id") or (saved or {}).get("composio_account_id")
    if selected:
        row = next((r for r in rows if r["id"] == selected), None)
        if row and _active(row):
            if secret.get("pending_account_id"):
                return await _persist(user_id, app, selected)
            if saved["status"] == "relink_required":
                return ConnectionState(app, "relink_required", selected)
            return ConnectionState(app, "active", selected, secret.get("settings", {}))
        if row and row["status"] in {"INITIATED", "INITIALIZING"}:
            return ConnectionState(app, "pending")
        runtime.invalidate_sessions(user_id, app)
        if saved:
            await repo.set_connection_status(user_id, app, saved.get("composio_account_id"), "relink_required")
        return ConnectionState(app, "relink_required", selected)
    active = {r["id"]: r for r in rows if _active(r)}
    if len(active) > 1:
        return ConnectionState(app, "selection_required", candidate_account_ids=sorted(active))
    if active:
        return await _persist(user_id, app, next(iter(active)))
    if any(r["status"] in {"INITIATED", "INITIALIZING"} for r in rows):
        return ConnectionState(app, "pending")
    return ConnectionState(app, "not_connected")


async def is_connected(user_id: str, app: str) -> bool:
    return (await connection_status(user_id, app)).status == "active"


async def require_connection(user_id: str, app: str) -> ConnectionState:
    state = await connection_status(user_id, app)
    if state.status != "active":
        code = {"selection_required": "ACCOUNT_SELECTION_REQUIRED", "relink_required": "RELINK_REQUIRED"}.get(state.status, "NOT_CONNECTED")
        raise IntegrationError(code, app)
    return state


async def select_connected_account(user_id: str, app: str, account_id: str) -> ConnectionState:
    rows = await _accounts(user_id, app)
    if not any(r["id"] == account_id and _active(r) for r in rows):
        raise IntegrationError("PERMISSION_DENIED", app)
    runtime.invalidate_sessions(user_id, app)
    return await _persist(user_id, app, account_id)


async def connect_link(user_id: str, app: str) -> str:
    runtime.validate_user(user_id)
    toolkit, auth = runtime.config(app)
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    try:
        u = urlsplit(base)
        valid = (u.scheme == "https" and u.hostname and u.port in (None, 443)
                 and not (u.username is not None or u.password is not None or u.path or u.query or u.fragment)
                 and not any(ord(c) < 32 for c in base))
    except ValueError:
        valid = False
    if not valid:
        raise IntegrationError("CONFIGURATION_ERROR", app)
    def authorize(client):
        session = client.sessions.create(user_id=user_id, toolkits=[toolkit],
            auth_configs={toolkit: auth}, tools={toolkit: {"enable": []}},
            session_preset=SESSION_PRESET_DIRECT_TOOLS, manage_connections=False,
            sandbox={"enable": False}, proxy_execute=False, instant=False)
        request = session.authorize(toolkit, callback_url=base + "/connected")
        return request.id, request.redirect_url
    account_id, url = await runtime.sdk_call(authorize, provider=app, operation="authorize", retry=False)
    try:
        redirect = urlsplit(url)
        valid = (redirect.scheme == "https"
                 and redirect.hostname in {"connect.composio.dev", "backend.composio.dev", "auth.composio.dev"}
                 and redirect.username is None and redirect.password is None and redirect.port in (None, 443)
                 and not any(ord(c) < 32 for c in url))
    except (ValueError, TypeError):
        valid = False
    if not account_id or not valid:
        raise IntegrationError("SCHEMA_MISMATCH", app)
    saved = await repo.get_connection(user_id, app)
    secret = dict((saved or {}).get("secret") or {})
    secret["pending_account_id"] = account_id
    await repo.upsert_user(user_id)
    await repo.save_connection(user_id, app, composio_account_id=(saved or {}).get("composio_account_id"), secret=secret, status="pending")
    runtime.invalidate_sessions(user_id, app)
    return url


async def save_connection_settings(user_id: str, app: str, settings: ConnectionSettings | dict) -> None:
    from .calendar import list_calendars
    from .messages import list_slack_channels
    selected = await require_connection(user_id, app)
    values = asdict(settings) if is_dataclass(settings) else settings
    if not isinstance(values, dict):
        raise IntegrationError("INVALID_ACTION", app)
    if is_dataclass(settings):
        allowed = {"calendar_ids", "write_calendar_id"} if app == "gcal" else {"workspace_id", "channel_ids"}
        values = {k: v for k, v in values.items() if k in allowed}
    if app == "gcal":
        if set(values) != {"calendar_ids", "write_calendar_id"}:
            raise IntegrationError("INVALID_ACTION", app)
        ids, target = values["calendar_ids"], values["write_calendar_id"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or not isinstance(target, str):
            raise IntegrationError("INVALID_ACTION", app)
        batch = await list_calendars(user_id)
        options = {o.id: o for o in batch.records if o.account_id == selected.account_id}
        if (not batch.complete or any(i not in options or not options[i].readable for i in ids)
                or target not in ids or target not in options or not options[target].writable):
            raise IntegrationError("PERMISSION_DENIED", app)
        values = {"calendar_ids": list(dict.fromkeys(ids)), "write_calendar_id": target}
    elif app == "slack":
        if set(values) != {"workspace_id", "channel_ids"}:
            raise IntegrationError("INVALID_ACTION", app)
        ids, workspace = values["channel_ids"], values["workspace_id"]
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i for i in ids) or not isinstance(workspace, str) or not workspace:
            raise IntegrationError("INVALID_ACTION", app)
        batch = await list_slack_channels(user_id)
        options = {o.id: o for o in batch.records if o.account_id == selected.account_id and o.workspace_id == workspace and o.readable}
        if not batch.complete or any(i not in options for i in ids):
            raise IntegrationError("PERMISSION_DENIED", app)
        values = {"workspace_id": workspace, "channel_ids": list(dict.fromkeys(ids))}
    else:
        raise IntegrationError("INVALID_ACTION", app)
    current = await require_connection(user_id, app)
    if current.account_id != selected.account_id:
        raise IntegrationError("ACCOUNT_CHANGED", app)
    await _persist(user_id, app, selected.account_id, settings=values)
