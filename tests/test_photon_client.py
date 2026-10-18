from unittest.mock import AsyncMock

import httpx
import pytest

from channels import photon
from integrations import photon_client
from scripts import photon_setup


def response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://photon.test"))


@pytest.mark.asyncio
async def test_send_message_contract(monkeypatch):
    call = AsyncMock(return_value=response(200, {"id": "message-out"}))
    monkeypatch.setattr(photon_client, "_request", call)
    await photon_client.send_message(
        "space-1", "agent-1", "hello", idempotency_key="brief:one",
        parent_message_id="message-in",
    )
    assert photon_client._headers("key", "brief:one")["Idempotency-Key"] == "brief:one"
    assert call.await_args.args == ("POST", "/messages")
    assert call.await_args.kwargs == {
        "json": {
            "spaceId": "space-1", "senderId": "agent-1",
            "content": {"type": "text", "format": "plain", "text": "hello"},
            "parentMessageId": "message-in",
        },
        "idempotency_key": "brief:one",
    }


@pytest.mark.asyncio
async def test_threaded_send_falls_back_only_for_unsendable_space(monkeypatch):
    call = AsyncMock(side_effect=[
        response(409, {"error": {"code": "SPACE_NOT_SENDABLE"}}),
        response(200, {"id": "message-out"}),
    ])
    monkeypatch.setattr(photon_client, "_request", call)
    await photon_client.send_message(
        "space-1", "agent-1", "hello", idempotency_key="reply:one",
        parent_message_id="message-in",
    )
    first, second = call.await_args_list
    assert first.kwargs["json"]["parentMessageId"] == "message-in"
    assert "parentMessageId" not in second.kwargs["json"]
    assert second.kwargs["idempotency_key"].startswith("margin-unthreaded-")


@pytest.mark.asyncio
async def test_line_assignment_new(monkeypatch):
    monkeypatch.setattr(photon_client, "_request", AsyncMock(return_value=response(
        200, {"assignment": {"handle": "+15555550100", "assignedPhoneNumber": "+15555550999"}}
    )))
    assert await photon_client.ensure_line_assignment("+15555550100") == "+15555550999"


@pytest.mark.asyncio
async def test_line_assignment_existing_is_recovered(monkeypatch):
    request = AsyncMock(side_effect=[
        response(409, {"error": {"code": "ALREADY_ASSIGNED"}}),
        response(200, {"assignments": [
            {"handle": "+15555550100", "assignedPhoneNumber": "+15555550999"}
        ]}),
    ])
    monkeypatch.setattr(photon_client, "_request", request)
    assert await photon_client.ensure_line_assignment("+15555550100") == "+15555550999"
    assert request.await_args_list[1].args == ("GET", "/platforms/imessage/assignments")


@pytest.mark.asyncio
async def test_line_assignment_error(monkeypatch):
    monkeypatch.setattr(photon_client, "_request", AsyncMock(return_value=response(
        500, {"error": {"code": "UNAVAILABLE"}}
    )))
    with pytest.raises(httpx.HTTPStatusError):
        await photon_client.ensure_line_assignment("+15555550100")


@pytest.mark.asyncio
async def test_send_text_skips_without_saved_conversation(monkeypatch):
    monkeypatch.setattr(photon, "send_message", AsyncMock())
    from db import repo
    monkeypatch.setattr(repo, "get_user", AsyncMock(return_value={"user_id": "u"}))
    assert await photon.send_text("u", "hello") is None
    photon.send_message.assert_not_awaited()


def test_setup_requests_use_named_user_agent(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"ok": true}'

    def open_request(request, timeout):
        seen["user_agent"] = request.get_header("User-agent")
        seen["timeout"] = timeout
        return Reply()

    monkeypatch.setattr(photon_setup.urllib.request, "urlopen", open_request)
    assert photon_setup.request_json("https://photon.test", "test-key") == {"ok": True}
    assert seen == {"user_agent": "Margin-Photon-Setup/1.0", "timeout": 15}
