"""Cache-boundary tests using deterministic, user-scoped repository doubles."""
from unittest.mock import AsyncMock

import pytest

from integrations.composio import cache, models
from shared.protocol import GatherRequest, Item


START = "2026-10-18T00:00:00Z"
END = "2026-10-25T00:00:00Z"
REQUEST = GatherRequest(request_id="req-1", user_id="alex", window_start=START, window_end=END)


def connection(provider="gmail", *, account="ca-alex", status="active", channels=None, calendars=None):
    settings = {}
    if provider == "slack":
        settings = {"workspace_id": "T1", "channel_ids": channels or ["C1"]}
    if provider == "gcal":
        settings = {"calendar_ids": calendars or ["cal-1"], "write_calendar_id": "cal-1"}
    return {"status": status, "composio_account_id": account, "secret": {"settings": settings}}


def item(provider="gmail", *, native="m1", account="ca-alex", user="alex", channel="C1", workspace="T1", calendar="cal-1", **changes):
    if provider == "gmail":
        key, kind = models.source_key(account, native), "email_action"
    elif provider == "slack":
        key, kind = models.source_key(account, workspace, channel, native), "slack_action"
    else:
        key, kind = models.source_key(account, calendar, native), "event"
    values = dict(item_id="old-local-id-" + native, user_id=user, kind=kind, title="Review session",
                  source=provider, source_id=key, due_at="2026-10-20T16:00:00Z", quote="Review in BYAC 110.")
    values.update(changes)
    return Item(**values)


def row(value, *, fetched_at="2026-10-17T10:00:00Z"):
    return {**value.model_dump(), "fetched_at": fetched_at}


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(return_value=connection()))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[]))
    monkeypatch.setattr(cache.repo, "upsert_items", AsyncMock())


async def fallback(provider="gmail"):
    return await cache.cached_gather_result(REQUEST, provider, error=models.IntegrationError("TEMPORARY_UNAVAILABLE", provider))


@pytest.mark.asyncio
async def test_fallback_is_owned_account_scoped_and_uses_oldest_included_timestamp(monkeypatch):
    retained = item(native="kept")
    later = item(native="later")
    rows = [
        row(retained, fetched_at="2026-10-17T10:00:00-07:00"),
        row(later, fetched_at="2026-10-17T20:00:00Z"),
        row(item(account="ca-previous"), fetched_at="2020-01-01T00:00:00Z"),
        row(item(user="other"), fetched_at="2020-01-01T00:00:00Z"),
        row(item(native="outside", due_at=END), fetched_at="2020-01-01T00:00:00Z"),
    ]
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=rows))
    result = await fallback()
    assert {record.item_id for record in result.items} == {retained.item_id, later.item_id}
    assert result.stale_since == "2026-10-17T17:00:00Z"
    assert result.free_slots == []
    assert "gmail: TEMPORARY_UNAVAILABLE" in result.error and "cached" in result.error
    cache.repo.last_items_by_source.assert_awaited_once_with("alex", "gmail")


@pytest.mark.asyncio
async def test_slack_fallback_scopes_workspace_channels_and_account(monkeypatch):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(return_value=connection("slack")))
    good = item("slack", native="1792339200.000001")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[
        row(good), row(item("slack", channel="C2")), row(item("slack", workspace="T-other")),
        row(item("slack", account="ca-other")), row(item("gmail")),
    ]))
    result = await fallback("slack")
    assert [record.source_id for record in result.items] == [good.source_id]
    assert result.agent == "margin-inbox"


@pytest.mark.asyncio
async def test_calendar_fallback_scopes_selected_calendars_and_never_free_slots(monkeypatch):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(return_value=connection("gcal")))
    good = item("gcal", due_at=None, starts_at="2026-10-20T16:00:00Z", ends_at="2026-10-20T17:00:00Z")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(good), row(item("gcal", calendar="cal-removed"))]))
    result = await fallback("gcal")
    assert len(result.items) == 1 and result.items[0].source_id == good.source_id
    assert result.agent == "margin-planner" and result.free_slots == []


@pytest.mark.asyncio
async def test_missing_naive_or_invalid_fetch_times_and_undated_rows_are_omitted(monkeypatch):
    no_stamp = row(item(native="missing"))
    no_stamp.pop("fetched_at")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[
        no_stamp,
        row(item(native="naive"), fetched_at="2026-10-17T10:00:00"),
        row(item(native="invalid"), fetched_at="invalid"),
        row(item(native="undated", due_at=None)),
    ]))
    result = await fallback()
    assert result.items == [] and result.stale_since is None
    assert "omitted" in result.error


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "pending", "relink_required"])
async def test_disconnected_fallback_never_reads_old_rows(monkeypatch, status):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(return_value=None if status is None else connection(status=status)))
    result = await fallback()
    assert result.items == [] and result.stale_since is None
    cache.repo.last_items_by_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_account_switch_during_fallback_discards_old_results(monkeypatch):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(side_effect=[connection(), connection(account="ca-new")]))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(item())]))
    result = await fallback()
    assert result.items == [] and result.stale_since is None
    assert result.error == "gmail: ACCOUNT_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,first,last", [
    ("slack", connection("slack", channels=["C1"]), connection("slack", channels=["C2"])),
    ("gcal", connection("gcal", calendars=["cal-1"]), connection("gcal", calendars=["cal-2"])),
])
async def test_same_account_resource_deselection_during_fallback_does_not_leak_old_selection(monkeypatch, provider, first, last):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(side_effect=[first, last]))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(item(provider))]))
    result = await fallback(provider)
    assert result.items == [] and result.stale_since is None
    assert "CHANGED" in result.error or "SELECTION" in result.error


@pytest.mark.asyncio
async def test_cache_write_preserves_existing_ids_local_status_and_current_snapshot_only(monkeypatch):
    incoming = item(native="kept", status="open", due_at="2026-10-20T09:00:00-07:00")
    previous = item(native="kept", item_id="retained-stable-local-id", status="done")
    obsolete = item(native="obsolete")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(previous), row(obsolete)]))
    result = await cache.cache_items("alex", "gmail", "ca-alex", [incoming])
    assert len(result) == 1
    assert result[0].item_id == "retained-stable-local-id" and result[0].status == "done"
    assert result[0].due_at == "2026-10-20T16:00:00Z"
    saved = cache.repo.upsert_items.call_args.args[0]
    assert len(saved) == 1 and saved[0]["item_id"] == models.item_id("alex", "gmail", incoming.source_id)
    assert "body" not in saved[0]
    assert incoming.item_id != result[0].item_id and incoming.status == "open"


@pytest.mark.asyncio
async def test_empty_fresh_read_never_replaces_itself_with_obsolete_cache():
    assert await cache.cache_items("alex", "gmail", "ca-alex", []) == []
    cache.repo.upsert_items.assert_not_awaited()
    cache.repo.last_items_by_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_items_are_one_natural_identity_with_latest_content(monkeypatch):
    first, changed = item(), item(title="Updated office hours")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(first)]))
    result = await cache.cache_items("alex", "gmail", "ca-alex", [first, changed])
    assert len(result) == 1 and result[0].title == "Updated office hours"
    assert len(cache.repo.upsert_items.call_args.args[0]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    item(title=" "), item(title="x" * 501), item(quote="x" * 281),
    item(due_at="2026-10-20T12:00:00"), item(due_at="invalid"),
    item(ends_at="2026-10-20T12:00:00Z"),
    item(starts_at="2026-10-20T12:00:00Z", ends_at="2026-10-20T11:00:00Z"),
    item(cost_usd=-1), item(cost_usd=float("nan")), item(est_hours=float("inf")),
    item(source_id="raw-unqualified-id"), item(source_id='["ca-alex"]'),
    item(source_id='["ca-alex", 1]'), item(account="ca-other"), item(user="other"),
])
async def test_invalid_extraction_cannot_write_any_partial_batch(monkeypatch, bad):
    with pytest.raises(models.IntegrationError) as error:
        await cache.cache_items("alex", "gmail", "ca-alex", [item(native="valid-first"), bad])
    assert error.value.code == "INVALID_ACTION"
    cache.repo.upsert_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_untrusted_source_link_is_removed_before_persistence(monkeypatch):
    value = item(source_url="https://attacker.test/steal")
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(value)]))
    result = await cache.cache_items("alex", "gmail", "ca-alex", [value])
    assert result[0].source_url is None
    assert cache.repo.upsert_items.call_args.args[0][0]["source_url"] is None


@pytest.mark.asyncio
async def test_db_failure_never_returns_success(monkeypatch):
    monkeypatch.setattr(cache.repo, "upsert_items", AsyncMock(side_effect=RuntimeError("database unavailable")))
    with pytest.raises(RuntimeError):
        await cache.cache_items("alex", "gmail", "ca-alex", [item()])


@pytest.mark.asyncio
async def test_account_change_before_and_during_cache_write_is_explicit(monkeypatch):
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(return_value=connection(account="ca-new")))
    with pytest.raises(models.IntegrationError) as error:
        await cache.cache_items("alex", "gmail", "ca-alex", [item()])
    assert error.value.code == "ACCOUNT_CHANGED"
    cache.repo.upsert_items.assert_not_awaited()
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(side_effect=[connection(), connection(account="ca-new")]))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(item())]))
    with pytest.raises(models.IntegrationError) as error:
        await cache.cache_items("alex", "gmail", "ca-alex", [item()])
    assert error.value.code == "ACCOUNT_CHANGED"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,first,last", [
    ("slack", connection("slack", channels=["C1"]), connection("slack", channels=["C2"])),
    ("gcal", connection("gcal", calendars=["cal-1"]), connection("gcal", calendars=["cal-2"])),
])
async def test_resource_deselection_during_cache_write_never_returns_old_selection(monkeypatch, provider, first, last):
    value = item(provider)
    monkeypatch.setattr(cache.repo, "get_connection", AsyncMock(side_effect=[first, last]))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[row(value)]))
    with pytest.raises(models.IntegrationError) as error:
        await cache.cache_items("alex", provider, "ca-alex", [value])
    assert "CHANGED" in error.value.code or "SELECTION" in error.value.code


@pytest.mark.asyncio
async def test_invalid_cached_quotes_and_intervals_never_become_source_evidence(monkeypatch):
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(return_value=[
        row(item(quote="private body " * 50)),
        row(item(due_at="2026-10-20T16:00:00")),
        row(item(starts_at="2026-10-20T16:00:00Z", ends_at="2026-10-20T15:00:00Z")),
        row(item(cost_usd=float("nan"))),
        row(item(source_id="unqualified-native-id")),
    ]))
    result = await fallback()
    assert result.items == [] and result.stale_since is None
    assert "omitted" in result.error


@pytest.mark.asyncio
async def test_noncanonical_json_identity_cannot_create_duplicate_items(monkeypatch):
    canonical = item()
    alternate = item(source_id='[ "ca-alex", "m1" ]')
    stored = []
    async def save(rows):
        stored.extend(rows)
    async def load(user_id, provider):
        return stored
    monkeypatch.setattr(cache.repo, "upsert_items", AsyncMock(side_effect=save))
    monkeypatch.setattr(cache.repo, "last_items_by_source", AsyncMock(side_effect=load))
    try:
        result = await cache.cache_items("alex", "gmail", "ca-alex", [canonical, alternate])
    except models.IntegrationError as error:
        # Rejecting a noncanonical identity before write is also a safe contract.
        assert error.code == "INVALID_ACTION" and stored == []
    else:
        assert len(result) == len(stored) == 1
        assert result[0].source_id == canonical.source_id


def test_filter_window_edges_overlap_and_verified_undated_live_ids():
    at_start = item(native="start", due_at=START)
    at_end = item(native="end", due_at=END)
    overlap = item(native="overlap", due_at=None, starts_at="2026-10-17T23:00:00Z", ends_at="2026-10-18T01:00:00Z")
    ends_at_start = item(native="ends-at-start", due_at=None, starts_at="2026-10-17T23:00:00Z", ends_at=START)
    verified = item(native="verified", due_at=None)
    old_unknown = item(native="unknown", due_at=None)
    result = cache.filter_items([at_start, at_end, overlap, ends_at_start, verified, old_unknown], start=START, end=END, undated_source_ids={verified.source_id})
    assert {value.source_id for value in result} == {at_start.source_id, overlap.source_id, verified.source_id}


@pytest.mark.parametrize("start,end", [(START, START), (END, START), ("2026-10-18T00:00:00", END)])
def test_filter_rejects_naive_or_reversed_windows(start, end):
    with pytest.raises(ValueError):
        cache.filter_items([], start=start, end=end)


@pytest.mark.parametrize("make", [
    lambda: models.SourceMessage(provider="gmail", account_id=42, message_id="m1", sender=None, subject=None, date=START, body=""),
    lambda: models.SourceMessage(provider="gmail", account_id="ca", message_id=42, sender=None, subject=None, date=START, body=""),
    lambda: models.ConnectionState(provider="gmail", status="active", account_id=42),
    lambda: models.CalendarOption(id=42, name="Classes", account_id="ca"),
    lambda: models.ChannelOption(id="C1", name=42, account_id="ca", workspace_id="T1"),
])
def test_typed_values_reject_nonstring_native_identity_and_name(make):
    with pytest.raises(ValueError):
        make()


def test_source_identity_json_encoding_is_unambiguous_and_user_ids_are_separated():
    first, second = models.source_key("a,b", "c"), models.source_key("a", "b,c")
    assert first != second
    assert models.parse_source_key(first) == ("a,b", "c")
    assert models.item_id("alex", "gmail", first) != models.item_id("other", "gmail", first)


def test_private_source_bodies_and_error_messages_are_absent_from_reprs():
    message = models.SourceMessage(provider="gmail", account_id="ca", message_id="m", sender="private@example.edu", subject="private subject", date=START, body="PRIVATE BODY")
    detail = models.SourceDetail(source="gmail", subject=None, sender=None, date=START, body="PRIVATE BODY", link=None)
    error = models.IntegrationError("PERMISSION_DENIED", "gmail")
    assert "PRIVATE BODY" not in repr(message) + repr(detail)
    assert "private@example.edu" not in repr(message)
    assert "PERMISSION_DENIED" in repr(error)
