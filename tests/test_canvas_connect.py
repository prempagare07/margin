"""Signed connect flow, browser protections, and secret-safe failures."""

import logging
import re
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from core.observability import _before_send, _before_transaction
from web import canvas_connect as connect, connect_state as state
from web.privacy import CanvasAccessFilter
from tests.test_canvas_client import FEED, ASSIGNMENT, ics, parse


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("STATE_SIGNING_KEY", "test-only-signing-key-" * 3)


@pytest_asyncio.fixture
async def browser(monkeypatch):
    monkeypatch.setattr(connect.repo, "get_user", AsyncMock(return_value={"tz": "America/Phoenix"}))
    monkeypatch.setattr(connect.repo, "upsert_user", AsyncMock())
    monkeypatch.setattr(connect.repo, "save_connection", AsyncMock())
    monkeypatch.setattr(connect.canvas_client, "fetch_feed", AsyncMock(return_value=parse(ics(ASSIGNMENT))))
    app = FastAPI()
    app.include_router(connect.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="https://margin.test") as client:
        yield client


async def form(browser, user="asi-alex"):
    token = state.make_state(user)
    page = await browser.get("/connect/canvas", params={"token": token})
    assert page.status_code == 200
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    return {"token": token, "csrf": csrf, "ical_url": FEED}


def test_signed_state_tampering_expiry_and_purpose(monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1000)
    token = state.make_state("asi-alex")
    assert state.verify_state(token) == "asi-alex"
    for invalid in (token + "x", "other." + token.split(".")[1], "", "☃.☃"):
        with pytest.raises(state.InvalidState):
            state.verify_state(invalid)
    monkeypatch.setattr(state.time, "time", lambda: 1900)
    with pytest.raises(state.InvalidState):
        state.verify_state(token)
    monkeypatch.setattr(state.time, "time", lambda: 900)
    with pytest.raises(state.InvalidState):
        state.verify_state(token)


def test_connect_url_requires_configured_https_origin(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://margin.example/")
    assert state.make_connect_url("alex").startswith("https://margin.example/connect/canvas?token=")
    for base in ("", "http://example.com", "https://user:pass@example.com", "https://example.com/path"):
        monkeypatch.setenv("PUBLIC_BASE_URL", base)
        with pytest.raises(RuntimeError):
            state.make_connect_url("alex")
    monkeypatch.delenv("STATE_SIGNING_KEY")
    with pytest.raises(RuntimeError):
        state.make_state("alex")


@pytest.mark.asyncio
async def test_get_requires_signed_link_and_has_private_headers(browser, monkeypatch):
    assert (await browser.get("/connect/canvas")).status_code == 400
    response = await browser.get("/connect/canvas", params={"token": state.make_state("alex")})
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "HttpOnly" in response.headers["set-cookie"] and "Secure" in response.headers["set-cookie"]
    assert 'name="ical_url"' in response.text and 'name="user_id"' not in response.text
    monkeypatch.delenv("STATE_SIGNING_KEY")
    assert (await browser.get("/connect/canvas")).status_code == 503


@pytest.mark.asyncio
async def test_connect_validates_before_saving_only_signed_user(browser):
    data = await form(browser)
    response = await browser.post("/connect/canvas", data=data)
    assert response.status_code == 200 and "Canvas is connected" in response.text
    assert FEED not in response.text and data["token"] not in response.text
    connect.canvas_client.fetch_feed.assert_awaited_once_with(FEED, "asi-alex", tz="America/Phoenix")
    connect.repo.upsert_user.assert_awaited_once_with("asi-alex")
    connect.repo.save_connection.assert_awaited_once_with("asi-alex", "canvas", secret={"ical_url": FEED}, status="active")
    assert connect.COOKIE not in browser.cookies


@pytest.mark.asyncio
async def test_csrf_missing_cookie_or_wrong_value_prevents_fetch(browser):
    data = await form(browser)
    data["csrf"] = "wrong"
    assert (await browser.post("/connect/canvas", data=data)).status_code == 403
    browser.cookies.clear()
    assert (await browser.post("/connect/canvas", data=data)).status_code == 403
    connect.canvas_client.fetch_feed.assert_not_called()
    connect.repo.save_connection.assert_not_called()


@pytest.mark.asyncio
async def test_bad_form_url_extra_identity_and_large_body_never_save(browser):
    data = await form(browser)
    assert (await browser.post("/connect/canvas", json=data)).status_code == 400
    assert (await browser.post("/connect/canvas", data={**data, "user_id": "victim"})).status_code == 400
    assert (await browser.post("/connect/canvas", data={**data, "ical_url": "x" * 9000})).status_code == 400
    response = await browser.post("/connect/canvas", data={**data, "ical_url": "https://127.0.0.1/private"})
    assert response.status_code == 422
    connect.canvas_client.fetch_feed.assert_not_called()
    connect.repo.save_connection.assert_not_called()


@pytest.mark.asyncio
async def test_expired_during_fetch_does_not_save(browser, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1000)
    data = await form(browser)
    async def slow(*args, **kwargs):
        monkeypatch.setattr(state.time, "time", lambda: 1900)
        return []
    connect.canvas_client.fetch_feed.side_effect = slow
    response = await browser.post("/connect/canvas", data=data)
    assert response.status_code == 400 and "expired" in response.text
    connect.repo.save_connection.assert_not_called()


@pytest.mark.asyncio
async def test_invalid_feed_and_database_errors_are_safe_and_retryable(browser):
    data = await form(browser)
    connect.canvas_client.fetch_feed.side_effect = connect.canvas_client.CanvasFeedError("Copy a fresh Calendar Feed link.")
    response = await browser.post("/connect/canvas", data=data)
    assert response.status_code == 422 and FEED not in response.text
    connect.repo.save_connection.assert_not_called()
    connect.canvas_client.fetch_feed.side_effect = None
    connect.repo.save_connection.side_effect = RuntimeError("database-secret " + FEED)
    response = await browser.post("/connect/canvas", data=data)
    assert response.status_code == 503
    assert "database-secret" not in response.text and FEED not in response.text
    assert 'name="ical_url"' in response.text


@pytest.mark.asyncio
async def test_empty_feed_is_valid_and_explained(browser):
    connect.canvas_client.fetch_feed.return_value = []
    response = await browser.post("/connect/canvas", data=await form(browser))
    assert response.status_code == 200 and "hasn’t published any dated entries" in response.text


def test_connection_credentials_are_not_sent_to_sentry_or_access_logs(monkeypatch):
    event = {"request": {"url": "https://margin.test/connect/canvas", "query_string": "token=private", "data": FEED}}
    # The opt-in to prompt capture must never override credential protection.
    monkeypatch.setenv("SENTRY_INCLUDE_PROMPTS", "1")
    assert _before_send(event, {}) is None and _before_transaction(event, {}) is None
    normal = {"request": {"url": "https://margin.test/health"}}
    assert _before_send(normal, {}) == normal and _before_transaction(normal, {}) == normal
    record = logging.LogRecord("uvicorn.access", 20, "", 1, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1", "GET", "/connect/canvas?token=private", "1.1", 200), None)
    assert CanvasAccessFilter().filter(record)
    assert "private" not in record.getMessage() and "/connect/canvas" in record.getMessage()
