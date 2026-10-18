"""Canvas parsing, fetch boundaries, and per-user cache behavior; no live accounts."""

from unittest.mock import AsyncMock

import httpx
import pytest

from integrations import canvas_client as canvas
from shared.protocol import GatherRequest, GatherResult

ORIGIN = "https://school.instructure.com"
FEED = ORIGIN + "/feeds/calendars/fictional-private-token.ics"


def ics(*events):
    return ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Margin Tests//EN\r\n" + "".join(
        "BEGIN:VEVENT\r\n" + event.replace("\n", "\r\n") + "\r\nEND:VEVENT\r\n"
        for event in events) + "END:VCALENDAR\r\n").encode()


ASSIGNMENT = """UID:event-assignment-123
SUMMARY:Homework [CSE 310]
DTSTART:20261019T170000Z
DTEND:20261019T170000Z
URL:https://school.instructure.com/courses/99/assignments/123
DESCRIPTION:<p>Read chapter 3.</p>
SEQUENCE:1"""


def parse(content, user="alex", **kwargs):
    return canvas.parse_feed(content, user, origin=ORIGIN, **kwargs)


def test_overrides_keep_one_assignment_and_stable_id_across_dates_and_users():
    override = ASSIGNMENT.replace("event-assignment-123", "event-assignment-override-456").replace("20261019", "20261020")
    original = parse(ics(ASSIGNMENT))[0]
    items = parse(ics(ASSIGNMENT, override))
    assert len(items) == 1
    item = items[0]
    assert item.item_id == original.item_id
    assert item.item_id != parse(ics(override), "other")[0].item_id
    assert item.due_at == "2026-10-20T17:00:00Z" and item.starts_at is None
    assert item.title == "Homework" and item.course == "CSE 310"
    assert item.quote == "Read chapter 3." and item.status == "open"
    assert parse(ics(override, ASSIGNMENT))[0] == item


def test_events_floating_times_all_day_and_timezone_conversion():
    events = parse(ics(
        "UID:event-1\nSUMMARY:Review\nDTSTART;TZID=America/New_York:20261102T100000\nDTEND;TZID=America/New_York:20261102T110000\nLOCATION:BYAC 110",
        "UID:event-2\nSUMMARY:Office hours\nDTSTART:20261019T100000\nDURATION:PT1H",
        "UID:event-assignment-3\nSUMMARY:Essay\nDTSTART;VALUE=DATE:20261020",
    ))
    by_title = {i.title: i for i in events}
    assert by_title["Review"].starts_at == "2026-11-02T15:00:00Z"
    assert by_title["Review"].ends_at == "2026-11-02T16:00:00Z"
    assert by_title["Review"].location == "BYAC 110" and by_title["Review"].due_at is None
    assert by_title["Office hours"].starts_at == "2026-10-19T17:00:00Z"
    essay = by_title["Essay"]
    assert essay.due_at is None and "time not provided" in essay.quote
    assert essay.starts_at == "2026-10-20T07:00:00Z" and essay.ends_at == "2026-10-21T07:00:00Z"


def test_empty_cancelled_and_undated_entries():
    assert parse(ics()) == []
    assert parse(ics(ASSIGNMENT + "\nSTATUS:CANCELLED", "UID:undated\nSUMMARY:No date")) == []


@pytest.mark.parametrize("content", [b"<html>Login</html>", ics(ASSIGNMENT + "\nRRULE:FREQ=WEEKLY"),
    ics("UID:bad\nSUMMARY:Bad\nDTSTART:20261019T170000Z\nDTEND:20261018T170000Z"),
    ics("UID:bad\nSUMMARY:Bad\nDTSTART:not-a-date"), b"x" * (canvas.MAX_FEED_BYTES + 1)])
def test_invalid_or_unexpanded_feeds_fail_explicitly(content):
    with pytest.raises(canvas.CanvasFeedError):
        parse(content)


def test_private_urls_and_markup_do_not_escape_into_items():
    item = parse(ics(f"UID:{FEED}\nSUMMARY:<b>Lecture</b>\nDTSTART:20261019T170000Z\nURL:{FEED}\nDESCRIPTION:Subscribe {FEED}"))[0]
    assert item.source_url is None and item.title == "Lecture"
    assert "fictional-private-token" not in item.model_dump_json()


@pytest.mark.parametrize("url", ["http://school.instructure.com/feeds/calendars/a.ics",
    "https://evil.test/feeds/calendars/a.ics", "https://127.0.0.1/feeds/calendars/a.ics",
    "https://school.instructure.com.evil.test/feeds/calendars/a.ics",
    "https://user:pass@school.instructure.com/feeds/calendars/a.ics",
    FEED + "?token=x", FEED + "#fragment", ORIGIN + "/courses/99", FEED.replace(".com/", ".com:444/")])
def test_fetch_url_rejects_arbitrary_hosts_credentials_paths_and_redirect_targets(url, monkeypatch):
    monkeypatch.delenv("CANVAS_BASE_URL", raising=False)
    monkeypatch.delenv("CANVAS_ICAL_URL", raising=False)
    with pytest.raises(canvas.CanvasFeedError):
        canvas.normalize_feed_url(url)


def test_webcal_and_operator_configured_school_host(monkeypatch):
    assert canvas.normalize_feed_url(FEED.replace("https:", "webcal:")) == FEED
    monkeypatch.setenv("CANVAS_BASE_URL", "https://canvas.example.edu")
    assert canvas.normalize_feed_url("https://canvas.example.edu/feeds/calendars/abc.ics").startswith("https://canvas.example.edu/")


@pytest.mark.asyncio
async def test_fetch_retries_transient_errors_and_does_not_log_url(caplog, monkeypatch):
    monkeypatch.setattr(canvas.asyncio, "sleep", AsyncMock())
    responses = [503, 429, 200]
    def handle(request):
        assert request.method == "GET"
        return httpx.Response(responses.pop(0), content=ics(ASSIGNMENT))
    items = await canvas.fetch_feed(FEED, "alex", transport=httpx.MockTransport(handle))
    assert len(items) == 1 and responses == []
    assert "fictional-private-token" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 401, 403, 404])
async def test_fetch_does_not_follow_redirects_or_retry_invalid_credentials(status):
    calls = []
    def handle(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "http://127.0.0.1/secret"}, text=FEED)
    with pytest.raises(canvas.CanvasFeedError) as exc:
        await canvas.fetch_feed(FEED, "alex", transport=httpx.MockTransport(handle))
    assert len(calls) == 1 and FEED not in str(exc.value)


@pytest.mark.asyncio
async def test_transport_timeouts_are_bounded_and_public_safe(monkeypatch):
    monkeypatch.setattr(canvas.asyncio, "sleep", AsyncMock())
    calls = []
    def handle(request):
        calls.append(request)
        raise httpx.ReadTimeout(FEED)
    with pytest.raises(canvas.CanvasFeedError) as exc:
        await canvas.fetch_feed(FEED, "alex", transport=httpx.MockTransport(handle))
    assert len(calls) == 3 and FEED not in str(exc.value)


@pytest.mark.asyncio
async def test_oversized_response_is_rejected(monkeypatch):
    monkeypatch.setattr(canvas, "MAX_FEED_BYTES", 30)
    with pytest.raises(canvas.CanvasFeedError, match="too large"):
        await canvas.fetch_feed(FEED, "alex", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 31)))


@pytest.mark.asyncio
async def test_saved_feed_is_user_scoped_and_never_falls_back_to_demo_env(monkeypatch):
    monkeypatch.setenv("CANVAS_ICAL_URL", FEED)
    read = AsyncMock(return_value=None)
    fetch = AsyncMock()
    monkeypatch.setattr(canvas.repo, "get_connection", read)
    monkeypatch.setattr(canvas, "fetch_feed", fetch)
    with pytest.raises(canvas.CanvasNotConnected):
        await canvas.get_items("other")
    fetch.assert_not_called()
    read.return_value = {"status": "active", "secret": {"ical_url": FEED}}
    monkeypatch.setattr(canvas.repo, "get_user", AsyncMock(return_value={"tz": "America/New_York"}))
    await canvas.get_items("alex")
    read.assert_awaited_with("alex", "canvas")
    fetch.assert_awaited_once_with(FEED, "alex", tz="America/New_York")


def request(**kwargs):
    return GatherRequest(**dict({"request_id": "req", "user_id": "alex", "window_start": "2026-10-19T17:00:00Z", "window_end": "2026-10-20T17:00:00Z"}, **kwargs))


@pytest.mark.asyncio
async def test_gather_caches_full_feed_preserves_local_state_and_filters_window(monkeypatch):
    items = parse(ics(ASSIGNMENT, ASSIGNMENT.replace("123", "124").replace("20261019", "20261020")))
    monkeypatch.setattr(canvas, "get_items", AsyncMock(return_value=items))
    write = AsyncMock()
    monkeypatch.setattr(canvas.repo, "upsert_items", write)
    monkeypatch.setattr(canvas.repo, "last_items_by_source", AsyncMock(return_value=[{
        "source_id": items[0].source_id, "item_id": "existing-id", "status": "done"}]))
    result = await canvas.gather(request())
    assert len(write.call_args.args[0]) == 2
    assert len(result.items) == 1 and result.items[0].status == "done" and result.items[0].item_id == "existing-id"
    assert result.error is None and result.stale_since is None


@pytest.mark.asyncio
async def test_gather_fallback_is_stale_and_disconnected_users_get_connect_error(monkeypatch):
    monkeypatch.setattr(canvas, "get_items", AsyncMock(side_effect=canvas.CanvasFeedError("offline")))
    fallback = GatherResult(request_id="req", agent="margin-academic", stale_since="2026-10-18T00:00:00Z", error="offline")
    cached = AsyncMock(return_value=fallback)
    monkeypatch.setattr(canvas.repo, "cached_gather_result", cached)
    assert await canvas.gather(request()) == fallback
    assert cached.call_args.args[2:4] == ("alex", ["canvas_ical"])
    cached.reset_mock()
    monkeypatch.setattr(canvas, "get_items", AsyncMock(side_effect=canvas.CanvasNotConnected("Connect Canvas")))
    assert (await canvas.gather(request())).error == "Connect Canvas"
    cached.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_window_and_cache_failure_do_not_masquerade_as_empty_success(monkeypatch):
    read = AsyncMock(return_value=parse(ics(ASSIGNMENT)))
    monkeypatch.setattr(canvas, "get_items", read)
    assert (await canvas.gather(request(window_end="2026-10-18"))).error
    read.assert_not_called()
    monkeypatch.setattr(canvas.repo, "upsert_items", AsyncMock(side_effect=RuntimeError("offline")))
    result = await canvas.gather(request())
    assert len(result.items) == 1 and "could not be cached" in result.error
