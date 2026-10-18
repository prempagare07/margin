"""Account-scoped Item caching for the agent-owned extraction/gather boundary."""

from __future__ import annotations

import math
from copy import deepcopy

from db import repo
from shared.protocol import GatherRequest, GatherResult, Item
from .models import IntegrationError, item_id, iso, parse_source_key, parse_time, safe_link, source_key, validate_window


def _belongs(item: Item, user_id: str, provider: str, account_id: str, settings: dict) -> bool:
    try:
        key = parse_source_key(item.source_id)
        if item.user_id != user_id or item.source != provider or key[0] != account_id:
            return False
        if provider == "gmail":
            return len(key) == 2 and item.kind == "email_action"
        if provider == "gcal":
            return len(key) == 3 and item.kind == "event" and key[1] in settings.get("calendar_ids", [])
        if provider == "slack":
            return (len(key) == 4 and item.kind == "slack_action"
                    and key[1] == settings.get("workspace_id") and key[2] in settings.get("channel_ids", []))
    except (TypeError, ValueError, IndexError):
        pass
    return False


def _validated(item: Item) -> Item:
    value = Item.model_validate(item.model_dump())
    value.source_id = source_key(*parse_source_key(value.source_id))
    if not value.title.strip() or len(value.title) > 500 or (value.quote and len(value.quote) > 280):
        raise ValueError("Invalid item title or evidence length")
    for field in ("due_at", "starts_at", "ends_at"):
        if getattr(value, field) is not None:
            setattr(value, field, iso(getattr(value, field)))
    if value.ends_at and (not value.starts_at or parse_time(value.ends_at) < parse_time(value.starts_at)):
        raise ValueError("Invalid item interval")
    for number in (value.cost_usd, value.est_hours):
        if number is not None and (not math.isfinite(number) or number < 0):
            raise ValueError("Invalid extracted quantity")
    value.source_url = safe_link(value.source_url, value.source)
    return value


async def cache_items(user_id: str, provider: str, account_id: str, items: list[Item]) -> list[Item]:
    """Persist only validated current results and return their retained IDs/statuses.

    The agent must derive identities/quotes from the actual SourceMessages, not
    model-supplied identity fields. Full message bodies never enter this helper.
    DB failures propagate; callers must report the failure rather than claim a
    complete cache refresh. An empty current read returns empty, not old rows.
    """
    connection = await repo.get_connection(user_id, provider)
    if not connection or connection["status"] != "active" or connection.get("composio_account_id") != account_id:
        raise IntegrationError("ACCOUNT_CHANGED", provider)
    settings = deepcopy((connection.get("secret") or {}).get("settings", {}))
    current: dict[str, Item] = {}
    try:
        for item in items:
            if not isinstance(item, Item) or not _belongs(item, user_id, provider, account_id, settings):
                raise ValueError("Item belongs to a different source")
            value = _validated(item)
            value.item_id = item_id(user_id, provider, value.source_id)
            current[value.source_id] = value
    except (TypeError, ValueError):
        raise IntegrationError("INVALID_ACTION", provider) from None
    if not current:
        return []
    await repo.upsert_items([item.model_dump() for item in current.values()])
    saved = {row["source_id"]: row for row in await repo.last_items_by_source(user_id, provider)}
    for key, item in current.items():
        row = saved[key]
        item.item_id, item.status = row["item_id"], row["status"]
    # A concurrent account switch may finish during the database operations.
    latest = await repo.get_connection(user_id, provider)
    if ((latest or {}).get("status") != "active" or latest.get("composio_account_id") != account_id
            or ((latest.get("secret") or {}).get("settings", {})) != settings):
        raise IntegrationError("ACCOUNT_CHANGED", provider)
    return list(current.values())


def filter_items(items: list[Item], *, start: str, end: str,
                 undated_source_ids: set[str] | None = None) -> list[Item]:
    """Undated live Items require IDs of messages verified inside the lookback."""
    lower, upper = validate_window(start, end)
    selected = []
    for item in items:
        due = parse_time(item.due_at) if item.due_at else None
        begins = parse_time(item.starts_at) if item.starts_at else None
        ends = parse_time(item.ends_at) if item.ends_at else begins
        if ((due is not None and lower <= due < upper)
                or (begins is not None and (lower <= begins < upper or begins < lower < ends))
                or (due is None and begins is None and item.source_id in (undated_source_ids or set()))):
            selected.append(item)
    return selected


async def cached_gather_result(request: GatherRequest, provider: str, *, error: IntegrationError) -> GatherResult:
    """Explicitly stale, currently selected-account fallback; never free slots.

    Undated cache rows have no durable source date in today's schema, so they
    cannot establish seven-day message coverage and are omitted explicitly.
    """
    agent = "margin-planner" if provider == "gcal" else "margin-inbox"
    result = GatherResult(request_id=request.request_id, agent=agent, error=f"{provider}: {error.code}")
    validate_window(request.window_start, request.window_end)
    connection = await repo.get_connection(request.user_id, provider)
    if not connection or connection["status"] != "active" or not connection.get("composio_account_id"):
        return result
    account_id = connection["composio_account_id"]
    settings = deepcopy((connection.get("secret") or {}).get("settings", {}))
    retained, times, omitted = [], [], False
    for row in await repo.last_items_by_source(request.user_id, provider):
        try:
            item = _validated(Item.model_validate(row))
            if not _belongs(item, request.user_id, provider, account_id, settings):
                continue
            if not item.due_at and not item.starts_at:
                omitted = True
                continue
            if filter_items([item], start=request.window_start, end=request.window_end):
                stamp = parse_time(row["fetched_at"])
                retained.append(item)
                times.append(stamp)
        except (KeyError, TypeError, ValueError):
            omitted = True
    latest = await repo.get_connection(request.user_id, provider)
    if ((latest or {}).get("status") != "active" or latest.get("composio_account_id") != account_id
            or ((latest.get("secret") or {}).get("settings", {})) != settings):
        result.error = f"{provider}: ACCOUNT_CHANGED"
        return result
    result.items = retained
    result.stale_since = iso(min(times)) if times else None
    if retained:
        result.error += "; showing cached items that may have changed."
    if omitted:
        result.error += "; omitted cached items with unverifiable dates or invalid data."
    return result
