"""Connect/phone routing and validation without provider calls or database access."""
import asyncio
import base64
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from standardwebhooks import Webhook
from uagents_core.contrib.protocols.chat import ChatMessage, MetadataContent, TextContent

from core import chat, graph, onboarding
from channels import photon
from integrations import photon_client


FORM = {"action": "connect", "phone": "+1 (480) 555-1234", "monthly_income": "1500.50",
        "savings_goal": "100", "weekly_cap": "0", "bills": "Rent | 850 | 1; Utilities | 65.25 | 31"}


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(onboarding.repo, "save_onboarding", AsyncMock(return_value=False))
    monkeypatch.setattr(chat, "fast", AsyncMock(side_effect=AssertionError("No LLM for form data")))
    monkeypatch.setattr(graph, "fast", AsyncMock(side_effect=AssertionError("No LLM for Connect")))
    monkeypatch.setattr(photon, "send_message", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(photon_client, "ensure_line_assignment", AsyncMock(return_value="+14805559999"))
    monkeypatch.setattr(onboarding.repo, "verify_phone_link", AsyncMock(return_value="trusted-user"))
    monkeypatch.setattr(onboarding.repo, "get_user_by_phone", AsyncMock(return_value={"user_id": "trusted-user"}))
    monkeypatch.setattr(onboarding.repo, "upsert_user", AsyncMock())
    monkeypatch.setattr(graph, "_get_onboarding_step", AsyncMock(return_value=[]))
    monkeypatch.setattr(graph, "_send_next_app_card_with_context", AsyncMock())


def context():
    return SimpleNamespace(send=AsyncMock())


def texts(ctx):
    return "\n".join(c.text for call in ctx.send.await_args_list for c in call.args[1].content if isinstance(c, TextContent))


@pytest.mark.parametrize("query,letter", [
    ("A", "A"),
    ("b?", "B"),
    ("what's C?", "C"),
    ("tell me about D", "D"),
    ("calendar reminder E", "E"),
    ("what is reminder A on my calendar?", "A"),
])
def test_brief_letter_queries_are_deterministic(query, letter):
    assert graph._brief_letter(query) == letter


@pytest.mark.asyncio
async def test_photon_brief_query_returns_calendar_details_without_llm(monkeypatch):
    from db import repo

    monkeypatch.setattr(repo, "get_recent_messages", AsyncMock(return_value=[]))
    monkeypatch.setattr(repo, "get_user", AsyncMock(return_value={
        "user_id": "trusted-user", "tz": "America/Phoenix",
    }))
    monkeypatch.setattr(repo, "save_message", AsyncMock())
    monkeypatch.setattr(repo, "get_today_brief", AsyncMock(return_value={
        "letters": {"B": "calendar-block"},
    }))
    monkeypatch.setattr(repo, "get_item", AsyncMock(return_value={
        "item_id": "calendar-block",
        "title": "Study for circuits",
        "starts_at": "2026-10-19T18:00:00Z",
        "ends_at": "2026-10-19T19:30:00Z",
        "location": "Hayden Library",
        "source": "gcal",
        "source_id": "event-1",
    }))
    ctx = context()

    await graph.handle(
        ctx, "trusted-user", "calendar reminder B", "photon", None, "trusted-user"
    )

    answer = texts(ctx)
    assert "B — Study for circuits" in answer
    assert "When:" in answer and "Hayden Library" in answer
    graph.fast.assert_not_awaited()


@pytest.mark.asyncio
async def test_connect_routes_without_llm_and_ignores_payload_identity_and_entitlements(monkeypatch):
    ctx = context()
    await graph.handle(ctx, "trusted-user", "", "asi1", {**FORM, "user_id": "victim", "plan": "pro", "phone_verified": True}, "trusted-user")
    call = onboarding.repo.save_onboarding.await_args
    assert call.args == ("trusted-user",)
    assert call.kwargs["phone"] == "+14805551234"
    assert call.kwargs["monthly_income"] == Decimal("1500.50")
    assert call.kwargs["weekly_cap"] == 0
    assert call.kwargs["bills"][1] == {"name": "Utilities", "amount": Decimal("65.25"), "due_day": 31}
    assert "plan" not in call.kwargs and "phone_verified" not in call.kwargs
    code = texts(ctx).split("LINK ")[1].split()[0]
    assert len(code) == 6 and code not in call.kwargs["code_hash"]
    assert call.kwargs["code_hash"] == onboarding.link_code_hash(code)
    assert 590 < (call.kwargs["expires"] - datetime.now(timezone.utc)).total_seconds() <= 600
    assert "saved" in texts(ctx) and "+14805559999" in texts(ctx)
    graph.fast.assert_not_awaited()
    photon.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_phone_does_not_issue_new_link():
    onboarding.repo.save_onboarding.return_value = True
    result = await onboarding.save("trusted-user", FORM)
    assert result.phone_verified and result.link_code is None


@pytest.mark.parametrize("change", [
    {"phone": "4805551234"}, {"phone": None}, {"monthly_income": "NaN"},
    {"monthly_income": -1}, {"monthly_income": True}, {"monthly_income": "1.001"},
    {"monthly_income": "10000000000"}, {"savings_goal": "Infinity"}, {"weekly_cap": -1},
    {"bills": "Rent | 100 | 32"}, {"bills": "Rent 100 on the first"},
    {"bills": [{"name": "Rent", "amount": 30, "due_day": True}]},
    {"bills": [{"name": "", "amount": 30, "due_day": 1}]},
    {"bills": "Rent | 100 | 1; rent | 200 | 2"},
    {"bills": [{"name": "x", "amount": -1, "due_day": 1}]},
    {"fields": ["not-a-map"]}, {"values": {"phone": "+14805559999"}},
])
@pytest.mark.asyncio
async def test_invalid_forms_fail_before_any_persistence(change):
    with pytest.raises(onboarding.InvalidSubmission):
        await onboarding.save("trusted-user", {**FORM, **change})
    onboarding.repo.save_onboarding.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_defaults_and_structured_bills():
    await onboarding.save("trusted-user", {"phone": "+14805551234", "monthly_income": 0,
        "bills": [{"name": "Insurance", "amount": 0, "due_day": "15"}]})
    fields = onboarding.repo.save_onboarding.await_args.kwargs
    assert fields["savings_goal"] == 0 and fields["weekly_cap"] is None
    assert fields["bills"][0]["due_day"] == 15


@pytest.mark.asyncio
async def test_graph_reports_save_failure_without_success_or_private_details(caplog):
    onboarding.repo.save_onboarding.side_effect = RuntimeError("secret +14805551234 1500.50")
    ctx = context()
    await graph.handle(ctx, "trusted-user", "", "asi1", FORM, "trusted-user")
    assert "could not be saved" in texts(ctx) and "LINK " not in texts(ctx)
    assert "secret" not in caplog.text and "1500.50" not in texts(ctx)


@pytest.mark.parametrize("channel,sender", [("photon", "trusted-user"), ("asi1", "other-user")])
@pytest.mark.asyncio
async def test_only_authenticated_asi_sender_can_onboard(channel, sender):
    await graph.handle(context(), "trusted-user", "", channel, FORM, sender)
    onboarding.repo.save_onboarding.assert_not_awaited()


@pytest.mark.parametrize("wrapper", ["flat", "values", "fields", "form_data", "selection"])
@pytest.mark.asyncio
async def test_form_envelopes_and_mentions_parse_without_model(wrapper):
    fields = {k: v for k, v in FORM.items() if k != "action"}
    data = FORM if wrapper == "flat" else ({"selection": {"action": "connect"}, "values": fields}
            if wrapper == "selection" else {"action": "connect", wrapper: fields})
    parsed = await chat.parse_selection("@margin " + json.dumps(data))
    await onboarding.save("trusted-user", parsed)
    assert onboarding.repo.save_onboarding.await_args.kwargs["monthly_income"] == Decimal("1500.50")
    chat.fast.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_form_never_goes_to_model():
    parsed = await chat.parse_selection('{"action":"connect", "phone": "+14805551234", "monthly_income": ')
    assert parsed["action"] == "connect" and parsed["_invalid"]
    chat.fast.assert_not_awaited()


@pytest.mark.asyncio
async def test_protocol_accepts_metadata_form_submission_once():
    receiver = AsyncMock()
    proto = chat.make_chat_protocol("test", receiver, "Hello")
    handler = proto._signed_message_handlers[ChatMessage.build_schema_digest(ChatMessage)]
    ctx = context()
    msg = ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[
        TextContent(type="text", text="Submitted the form"),
        MetadataContent(type="metadata", metadata={"selection": json.dumps({"action": "connect"}),
            "values": json.dumps({k: v for k, v in FORM.items() if k != "action"})}),
    ])
    await handler(ctx, "trusted-user", msg)
    receiver.assert_awaited_once()
    assert receiver.await_args.args[1] == "trusted-user"
    await onboarding.save("trusted-user", receiver.await_args.args[3])
    assert onboarding.repo.save_onboarding.await_args.kwargs["phone"] == "+14805551234"


@pytest.mark.asyncio
async def test_link_consumed_before_normal_graph_and_bound_to_signed_sender(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(graph, "handle", dispatch)
    code = "a1b2c3"
    await photon.handle_inbound("+1 (480) 555-1234", f"LINK {code}", "msg-1", "space-1", "agent-1", verified_webhook=True)
    onboarding.repo.verify_phone_link.assert_awaited_once_with("+14805551234", onboarding.link_code_hash(code))
    dispatch.assert_not_awaited()
    onboarding.repo.upsert_user.assert_awaited_once()
    assert onboarding.repo.upsert_user.await_args.kwargs["photon_space_id"] == "space-1"
    assert onboarding.repo.upsert_user.await_args.kwargs["photon_user_id"] == "agent-1"
    assert onboarding.repo.upsert_user.await_args.kwargs["text_opted_in_at"] is not None
    assert "phone is linked" in photon.send_message.await_args.args[2]
    assert photon.send_message.await_args.kwargs["parent_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_unsigned_phone_input_cannot_link_or_dispatch(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(graph, "handle", dispatch)
    await photon.handle_inbound("+14805551234", "LINK A1B2C3", "msg-1", "space-1", "agent-1")
    onboarding.repo.verify_phone_link.assert_not_awaited()
    onboarding.repo.get_user_by_phone.assert_not_awaited()
    dispatch.assert_not_awaited()
    photon.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_phone_identity_always_comes_from_database(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(graph, "handle", dispatch)
    onboarding.repo.get_user_by_phone.return_value.update(photon_space_id="saved-space", photon_user_id="saved-agent")
    await photon.handle_inbound("+14805551234", "hi", "msg-2", "incoming-space", "incoming-agent", verified_webhook=True)
    assert dispatch.await_args.kwargs["user_id"] == "trusted-user"
    onboarding.repo.get_user_by_phone.return_value = None
    dispatch.reset_mock()
    await photon.handle_inbound("+14805551234", "hi", "msg-3", "incoming-space", "incoming-agent", verified_webhook=True)
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_bad_link_does_not_claim_success_or_access_user_data(monkeypatch):
    onboarding.repo.verify_phone_link.return_value = None
    monkeypatch.setattr(graph, "handle", AsyncMock())
    await photon.handle_inbound("+14805551234", "LINK A1B2C3", "msg-4", "space-1", "agent-1", verified_webhook=True)
    assert "invalid" in photon.send_message.await_args.args[2]
    onboarding.repo.get_user_by_phone.assert_not_awaited()
    graph.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_requires_real_signature_before_phone_linking(monkeypatch):
    monkeypatch.setenv("SENTRY_DSN", "")
    from web import server
    secret = "whsec_" + base64.b64encode(b"local-webhook-secret-for-test-only").decode()
    monkeypatch.setenv("PHOTON_WEBHOOK_SECRET", secret)
    server._PROCESSED_PHOTON_MESSAGES.clear()
    dispatch = AsyncMock()
    monkeypatch.setattr(server, "_dispatch", dispatch)
    payload = (Path(__file__).parent / "fixtures/photon/message_received.json").read_text()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="https://margin.test") as client:
        assert (await client.post("/webhooks/photon", content=payload)).status_code == 401
        dispatch.assert_not_awaited()
        now = datetime.now(timezone.utc)
        headers = {"webhook-id": "event-one", "webhook-timestamp": str(int(now.timestamp())),
                   "webhook-signature": Webhook(secret).sign("event-one", now, payload)}
        assert (await client.post("/webhooks/photon", content=payload, headers=headers)).status_code == 200
        await asyncio.sleep(0)
        dispatch.assert_awaited_once_with(
            "+14805551234", "LINK A1B2C3", "msg-webhook", "space-1", "agent-1",
            verified_webhook=True,
        )
        dispatch.reset_mock()
        monkeypatch.setenv("PHOTON_WEBHOOK_SECRET", "")
        assert (await client.post("/webhooks/photon", content=payload)).status_code == 503
        dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_webhook_uses_received_by_from_live_inbound_snapshot(monkeypatch):
    from web import server
    secret = "whsec_" + base64.b64encode(b"local-webhook-secret-for-test-only").decode()
    monkeypatch.setenv("PHOTON_WEBHOOK_SECRET", secret)
    server._PROCESSED_PHOTON_MESSAGES.clear()
    dispatch = AsyncMock()
    monkeypatch.setattr(server, "_dispatch", dispatch)
    event = json.loads((Path(__file__).parent / "fixtures/photon/message_received.json").read_text())
    message = event["data"]["object"]
    message.pop("addressedTo")
    message["receivedBy"] = {"id": "agent-live", "kind": "agent"}
    message["space"]["participantIds"] = []
    payload = json.dumps(event)
    now = datetime.now(timezone.utc)
    headers = {
        "webhook-id": "event-live-shape",
        "webhook-timestamp": str(int(now.timestamp())),
        "webhook-signature": Webhook(secret).sign("event-live-shape", now, payload),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="https://margin.test"
    ) as client:
        assert (await client.post("/webhooks/photon", content=payload, headers=headers)).status_code == 200
        await asyncio.sleep(0)
    dispatch.assert_awaited_once_with(
        "+14805551234", "LINK A1B2C3", "msg-webhook", "space-1", "agent-live",
        verified_webhook=True,
    )


@pytest.mark.asyncio
async def test_webhook_ignores_agent_echo_and_duplicate_delivery(monkeypatch):
    from web import server
    secret = "whsec_" + base64.b64encode(b"local-webhook-secret-for-test-only").decode()
    monkeypatch.setenv("PHOTON_WEBHOOK_SECRET", secret)
    server._PROCESSED_PHOTON_MESSAGES.clear()
    dispatch = AsyncMock()
    monkeypatch.setattr(server, "_dispatch", dispatch)
    event = json.loads((Path(__file__).parent / "fixtures/photon/message_received.json").read_text())

    async def post(client, value, delivery):
        payload = json.dumps(value)
        now = datetime.now(timezone.utc)
        headers = {"webhook-id": delivery, "webhook-timestamp": str(int(now.timestamp())),
                   "webhook-signature": Webhook(secret).sign(delivery, now, payload)}
        return await client.post("/webhooks/photon", content=payload, headers=headers)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="https://margin.test") as client:
        assert (await post(client, event, "delivery-one")).status_code == 200
        assert (await post(client, event, "delivery-two")).status_code == 200
        await asyncio.sleep(0)
        dispatch.assert_awaited_once()
        dispatch.reset_mock()
        event["data"]["object"]["id"] = "msg-echo"
        event["data"]["object"]["sentBy"]["kind"] = "agent"
        assert (await post(client, event, "delivery-three")).status_code == 200
        await asyncio.sleep(0)
        dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_connected_page_renders_css_and_escapes_app_name():
    from web import server

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="https://margin.test"
    ) as client:
        response = await client.get("/connected", params={"app": '<script>alert("x")</script>'})
    assert response.status_code == 200
    assert "body{" in response.text and "body{{" not in response.text
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
