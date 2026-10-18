"""Calendar provider normalization and availability without live account reads."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from integrations.composio import calendar
from integrations.composio.models import IntegrationError


def event(native_id="one", start="2026-10-19T09:00:00Z", end="2026-10-19T10:00:00Z", **kwargs):
    return {
        "id": native_id,
        "summary": "Class",
        "start": {"dateTime": start},
        "end": {"dateTime": end},
        **kwargs,
    }


def normalize(value, **kwargs):
    return calendar._normalize_event(
        value,
        user_id=kwargs.get("user_id", "alex"),
        account_id=kwargs.get("account_id", "account-one"),
        calendar_id=kwargs.get("calendar_id", "classes"),
        calendar_timezone=kwargs.get("tz", "UTC"),
    )


def slots(events, start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z", tz="UTC"):
    return calendar.compute_free_slots(events, start=start, end=end, tz=tz)


def test_adjacent_busy_intervals_merge_and_exact_hour_is_available():
    records = [normalize(event()), normalize(event("two", "2026-10-19T10:00:00Z", "2026-10-19T11:00:00Z"))]
    assert slots(records) == [
        ["2026-10-19T08:00:00Z", "2026-10-19T09:00:00Z"],
        ["2026-10-19T11:00:00Z", "2026-10-19T22:00:00Z"],
    ]
    assert slots([normalize(event(start="2026-10-19T08:59:00Z"))]) == [
        ["2026-10-19T10:00:00Z", "2026-10-19T22:00:00Z"]
    ]


def test_overlap_overnight_and_query_edges_are_clipped():
    records = [
        normalize(event("night", "2026-10-18T22:00:00Z", "2026-10-19T09:00:00Z")),
        normalize(event("overlap", "2026-10-19T08:30:00Z", "2026-10-19T10:00:00Z")),
        normalize(event("outside", "2026-10-19T12:00:00Z", "2026-10-19T14:00:00Z")),
    ]
    assert slots(records, start="2026-10-19T08:30:00Z", end="2026-10-19T12:00:00Z") == [
        ["2026-10-19T10:00:00Z", "2026-10-19T12:00:00Z"]
    ]


def test_all_day_uses_calendar_timezone_and_exclusive_end_across_dst():
    record = normalize(event(start=None, end=None) | {
        "start": {"date": "2026-11-01"}, "end": {"date": "2026-11-02"}
    }, tz="America/Los_Angeles")
    assert record.item.starts_at == "2026-11-01T07:00:00Z"
    assert record.item.ends_at == "2026-11-02T08:00:00Z"
    assert slots([record], start="2026-11-01T07:00:00Z", end="2026-11-02T08:00:00Z", tz="America/Los_Angeles") == []
    assert slots([], start="2026-10-31T07:00:00Z", end="2026-11-02T08:00:00Z", tz="America/Los_Angeles") == [
        ["2026-10-31T15:00:00Z", "2026-11-01T05:00:00Z"],
        ["2026-11-01T16:00:00Z", "2026-11-02T06:00:00Z"],
    ]


def test_transparent_declined_zero_and_cancelled_events_do_not_block():
    assert normalize({"id": "cancelled", "status": "cancelled"}) is None
    records = [
        normalize(event("free", transparency="transparent")),
        normalize(event("declined", attendees=[{"self": True, "responseStatus": "declined"}])),
        normalize(event("zero", end="2026-10-19T09:00:00Z")),
    ]
    assert all(not r.blocks_time for r in records)
    assert slots(records) == [["2026-10-19T08:00:00Z", "2026-10-19T22:00:00Z"]]
    tentative = normalize(event(status="tentative", attendees=[{"self": True, "responseStatus": "tentative"}]))
    assert tentative.blocks_time


def test_private_event_hides_details_but_blocks_time_and_keeps_verified_link():
    record = normalize(event(visibility="private", summary="Sensitive", description="private body", location="secret", htmlLink="https://calendar.google.com/calendar/event?eid=example"))
    assert record.item.title == "Busy" and record.item.quote is None and record.item.location is None
    assert record.blocks_time and record.item.source_url.endswith("eid=example")
    assert normalize(event(htmlLink="https://evil.invalid/calendar/event?eid=x")).item.source_url is None


def test_recurring_occurrences_keep_separate_stable_account_scoped_ids():
    first = normalize(event("series_20261019", recurringEventId="series"))
    second = normalize(event("series_20261020", recurringEventId="series"))
    assert first.item.item_id != second.item.item_id
    assert first.item.item_id == normalize(event("series_20261019", start="2026-10-19T11:00:00Z", end="2026-10-19T12:00:00Z", summary="Changed")).item.item_id
    assert first.item.item_id != normalize(event("series_20261019"), account_id="account-two").item.item_id
    assert first.item.item_id != normalize(event("series_20261019"), user_id="other").item.item_id
    assert json.loads(first.item.source_id) == ["account-one", "classes", "series_20261019"]
    with pytest.raises(IntegrationError):
        normalize(event(recurrence=["RRULE:FREQ=WEEKLY"]))


def test_floating_times_and_plain_text_are_normalized():
    record = normalize(event(start="2026-10-19T09:00:00", end="2026-10-19T10:00:00", summary="<b>Class</b>", description="<p>Study</p><script>ignore</script><p>Chapter 1</p>"), tz="America/Phoenix")
    assert record.item.starts_at == "2026-10-19T16:00:00Z"
    assert record.item.title == "Class" and record.item.quote == "Study Chapter 1"


@pytest.mark.parametrize("wall_time", ["2026-03-08T02:30:00", "2026-11-01T01:30:00"])
def test_ambiguous_or_nonexistent_floating_dst_times_fail_instead_of_inventing(wall_time):
    with pytest.raises(IntegrationError):
        normalize(event(start=wall_time, end=wall_time), tz="America/Los_Angeles")


@pytest.mark.parametrize("start,end", [
    ("2026-10-19T00:00:00", "2026-10-20T00:00:00Z"),
    ("2026-10-20T00:00:00Z", "2026-10-19T00:00:00Z"),
    ("2026-10-19T00:00:00Z", "2026-10-19T00:00:00Z"),
])
def test_invalid_windows_are_rejected(start, end):
    with pytest.raises((IntegrationError, ValueError)):
        slots([], start=start, end=end)


@asynccontextmanager
async def budget():
    yield


def result(data, log_id="log-calendar-fixture"):
    return SimpleNamespace(data=data, log_id=log_id)


def native_calendar(native_id="classes", role="owner", tz="UTC", **kwargs):
    return {"id": native_id, "summary": "Classes", "accessRole": role, "timeZone": tz, **kwargs}


@pytest.fixture
def provider(monkeypatch):
    # Fake only the adapter boundary: captured tool shapes are exercised below.
    import integrations.composio as package
    runtime = SimpleNamespace(read_budget=budget, execute=AsyncMock(side_effect=AssertionError("Unexpected provider call")))
    connections = SimpleNamespace(require_connection=AsyncMock(return_value=SimpleNamespace(
        account_id="account-one", settings={"calendar_ids": ["classes"]}
    )))
    monkeypatch.setattr(package, "runtime", runtime, raising=False)
    monkeypatch.setattr(package, "connections", connections, raising=False)
    return runtime, connections


@pytest.mark.asyncio
async def test_calendar_discovery_pagination_permissions_and_deduplication(provider):
    runtime, connections = provider
    runtime.execute.side_effect = [
        result({"calendars": [native_calendar(), native_calendar("busy", "freeBusyReader")], "next_page_token": "cursor-two"}),
        result({"calendars": [native_calendar(), native_calendar("reader", "reader"), native_calendar("removed", deleted=True)]}),
    ]
    batch = await calendar.list_calendars("alex")
    assert batch.complete and not batch.issues and len(batch.records) == 3
    options = {c.id: c for c in batch.records}
    assert options["classes"].writable and options["classes"].account_id == "account-one"
    assert options["reader"].readable and not options["reader"].writable
    assert not options["busy"].readable
    assert runtime.execute.call_args_list[1].args[3]["page_token"] == "cursor-two"
    assert all(c.kwargs["expected_account_id"] == "account-one" for c in runtime.execute.call_args_list)
    connections.require_connection.assert_awaited_once_with("alex", "gcal")


@pytest.mark.asyncio
async def test_event_reads_expand_recurrence_paginate_dedupe_and_filter_edges(provider):
    runtime, _ = provider
    runtime.execute.side_effect = [
        result({"calendars": [native_calendar()]}),
        result({"items": [event(), event("outside", "2026-10-20T00:00:00Z", "2026-10-20T01:00:00Z")], "nextPageToken": "page-two"}),
        result({"items": [event(summary="Updated"), event("second", "2026-10-19T12:00:00Z", "2026-10-19T13:00:00Z")]}),
    ]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert batch.complete and len(batch.records) == 2 and batch.records[0].item.title == "Updated"
    first_args = runtime.execute.call_args_list[1].args[3]
    assert first_args["singleEvents"] is True and first_args["showDeleted"] is False
    assert first_args["calendarId"] == "classes" and first_args["timeMin"] == "2026-10-19T00:00:00Z"
    assert runtime.execute.call_args_list[2].args[3]["pageToken"] == "page-two"


@pytest.mark.asyncio
async def test_unselected_calendar_and_invalid_windows_make_no_provider_calls(provider):
    runtime, connections = provider
    connections.require_connection.return_value.settings = {}
    with pytest.raises(IntegrationError) as error:
        await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert error.value.code == "CALENDAR_SELECTION_REQUIRED"
    runtime.execute.assert_not_awaited()
    connections.require_connection.reset_mock()
    with pytest.raises(IntegrationError) as error:
        await calendar.fetch_calendar_events("alex", start="2026-10-19", end="2026-10-20T00:00:00Z")
    assert error.value.code == "INVALID_WINDOW"
    connections.require_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_one_missing_or_denied_calendar_keeps_other_events_but_is_incomplete(provider):
    runtime, connections = provider
    connections.require_connection.return_value.settings = {"calendar_ids": ["missing", "denied", "classes"]}
    runtime.execute.side_effect = [
        result({"calendars": [native_calendar(), native_calendar("denied")]}),
        IntegrationError("PERMISSION_DENIED", "gcal", log_id="log-denied"),
        result({"items": [event()]}),
    ]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not batch.complete and len(batch.records) == 1
    assert [i.code for i in batch.issues] == ["PERMISSION_DENIED", "PERMISSION_DENIED"]
    assert "log-denied" in batch.log_ids


@pytest.mark.asyncio
async def test_bad_event_is_partial_while_empty_list_is_complete(provider):
    runtime, _ = provider
    runtime.execute.side_effect = [result({"calendars": [native_calendar()]}), result({"items": [event(), {"id": "bad"}]})]
    partial = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not partial.complete and len(partial.records) == 1 and partial.issues[0].code == "SCHEMA_MISMATCH"
    runtime.execute.side_effect = [result({"calendars": [native_calendar()]}), result({"items": []})]
    empty = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert empty.complete and empty.records == [] and empty.issues == []


@pytest.mark.asyncio
async def test_caps_and_repeated_cursors_disclose_incomplete_coverage(provider, monkeypatch):
    runtime, _ = provider
    monkeypatch.setattr(calendar, "MAX_EVENTS", 1)
    runtime.execute.side_effect = [result({"calendars": [native_calendar()]}), result({"items": [event(), event("two")]})]
    capped = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert len(capped.records) == 1 and not capped.complete and capped.issues[0].code == "PARTIAL_RESULT"
    runtime.execute.side_effect = [
        result({"calendars": [native_calendar()]}),
        result({"items": [event()], "nextPageToken": "repeat"}),
        result({"items": [event()], "nextPageToken": "repeat"}),
    ]
    repeated = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert len(repeated.records) == 1 and not repeated.complete


@pytest.mark.asyncio
async def test_page_budget_applies_across_calendars(provider, monkeypatch):
    runtime, connections = provider
    monkeypatch.setattr(calendar, "MAX_PAGES", 1)
    connections.require_connection.return_value.settings = {"calendar_ids": ["classes", "job"]}
    runtime.execute.side_effect = [result({"calendars": [native_calendar(), native_calendar("job")]}), result({"items": [event()]})]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not batch.complete and len(batch.records) == 1 and runtime.execute.await_count == 2


@pytest.mark.asyncio
async def test_changed_account_discards_old_account_results(provider):
    runtime, connections = provider
    connections.require_connection.return_value.settings = {"calendar_ids": ["classes", "job"]}
    runtime.execute.side_effect = [
        result({"calendars": [native_calendar(), native_calendar("job")]}), result({"items": [event()]}),
        IntegrationError("ACCOUNT_CHANGED", "gcal"),
    ]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not batch.complete and batch.records == [] and batch.issues[0].code == "ACCOUNT_CHANGED"


@pytest.mark.asyncio
async def test_adapter_arguments_match_captured_schema(provider):
    from pathlib import Path
    import jsonschema

    schemas = json.loads((Path(__file__).parent / "fixtures/composio/tool_schemas.json").read_text())
    runtime, _ = provider
    runtime.execute.side_effect = [result({"calendars": [native_calendar()]}), result({"items": [event()]})]
    await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    operations = {"calendar_list": "GOOGLECALENDAR_LIST_CALENDARS", "event_list": "GOOGLECALENDAR_EVENTS_LIST"}
    for call in runtime.execute.call_args_list:
        schema = schemas[operations[call.args[2]]]["input_parameters"]
        assert set(call.args[3]) <= set(schema["properties"])
        jsonschema.validate(call.args[3], schema)


@pytest.mark.asyncio
async def test_provider_calendar_id_rewriting_is_never_allowed(provider):
    runtime, connections = provider
    connections.require_connection.return_value.settings = {"calendar_ids": ["ca_ambiguous"]}
    runtime.execute.side_effect = [result({"calendars": [native_calendar("ca_ambiguous")]})]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not batch.complete and batch.issues[0].code == "SCHEMA_MISMATCH"
    assert runtime.execute.await_count == 1


@pytest.mark.asyncio
async def test_malformed_discovery_cannot_assert_empty_availability(provider):
    runtime, _ = provider
    runtime.execute.side_effect = [result({"calendars": [native_calendar(role=["reader"])]})]
    batch = await calendar.fetch_calendar_events("alex", start="2026-10-19T00:00:00Z", end="2026-10-20T00:00:00Z")
    assert not batch.complete and batch.records == [] and batch.issues[0].code == "SCHEMA_MISMATCH"
    assert runtime.execute.await_count == 1
