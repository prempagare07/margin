"""Approved writes with provider boundaries mocked; no live artifacts are created."""

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from integrations.composio import actions
from integrations.composio.models import IntegrationError, source_key
from shared.protocol import ActionRequest, Item

pytestmark = pytest.mark.asyncio


EVENT = {
    "account_id": "ca_calendar", "calendar_id": "study@example.edu",
    "title": "Study physics", "starts_at": "2026-10-19T10:00:00-07:00",
    "ends_at": "2026-10-19T11:30:00-07:00", "location": "BYAC 110",
}
DRAFT = {
    "account_id": "ca_gmail", "to": ["professor@example.edu"],
    "subject": "Question about the review", "body": "Can we review question 4?",
}


class Store:
    def __init__(self):
        self.plan = {
            "plan_id": "plan", "user_id": "alex", "status": "approved",
            "actions": [{"action_id": "action", "type": "create_event", "params": copy.deepcopy(EVENT)}],
        }
        self.execution = {"status": "pending", "result": None, "error": None}
        self.fail_receipt = None
        self.claims = 0

    async def get_plan(self, plan_id, user_id):
        return copy.deepcopy(self.plan) if plan_id == "plan" and user_id == "alex" else None

    async def get_execution(self, plan_id, action_id, user_id):
        return copy.deepcopy(self.execution) if (plan_id, action_id, user_id) == ("plan", "action", "alex") else None

    async def claim(self, plan_id, action_id, user_id):
        await asyncio.sleep(0)
        if self.execution["status"] == "pending" and self.plan["status"] == "approved":
            self.execution["status"] = "running"
            self.claims += 1
            return True
        return False

    async def finish(self, plan_id, action_id, user_id, *, status, result, error):
        if self.fail_receipt == "before":
            raise RuntimeError("private database connection string")
        self.execution.update(status=status, result=copy.deepcopy(result), error=error)
        if self.fail_receipt == "after":
            raise RuntimeError("connection lost after commit")


@pytest.fixture
def setup(monkeypatch):
    store = Store()
    monkeypatch.setattr(actions.repo, "get_plan", store.get_plan)
    monkeypatch.setattr(actions.repo, "get_action_execution", store.get_execution)
    monkeypatch.setattr(actions.repo, "claim_action_execution", store.claim)
    monkeypatch.setattr(actions.repo, "finish_action_execution", store.finish)
    monkeypatch.setattr(actions.repo, "upsert_items", AsyncMock())
    connection = SimpleNamespace(account_id="ca_calendar", settings={
        "calendar_ids": ["study@example.edu"], "write_calendar_id": "study@example.edu",
    })
    monkeypatch.setattr(actions.connections, "require_connection", AsyncMock(return_value=connection))
    target = SimpleNamespace(id="study@example.edu", account_id="ca_calendar", writable=True)
    monkeypatch.setattr(actions.calendar, "list_calendars", AsyncMock(return_value=SimpleNamespace(
        records=[target], complete=True, issues=[], log_ids=[],
    )))
    monkeypatch.setattr(actions.calendar, "fetch_calendar_events", AsyncMock(return_value=SimpleNamespace(
        records=[], complete=True, issues=[], log_ids=[],
    )))
    execute = AsyncMock(return_value=SimpleNamespace(data={"response_data": {
        "id": "event123", "htmlLink": "https://calendar.google.com/calendar/event?eid=real",
    }}, log_id="log_event"))
    monkeypatch.setattr(actions.runtime, "execute", execute)
    return SimpleNamespace(store=store, connection=connection, target=target, execute=execute)


def request(**changes):
    return ActionRequest(**dict({"user_id": "alex", "plan_id": "plan", "action_id": "action",
                                 "type": "create_event", "params": {"title": "Unreviewed injection"}}, **changes))


def draft(setup):
    setup.store.plan["actions"][0].update(type="create_draft", params=copy.deepcopy(DRAFT))
    setup.connection.account_id = "ca_gmail"
    setup.execute.return_value = SimpleNamespace(data={
        "id": "draft123", "message": {"id": "message123", "threadId": "thread123"},
    }, log_id="log_draft")
    return request(type="create_draft")


async def test_unapproved_and_unowned_actions_never_contact_provider(setup):
    for req in [request(user_id="other"), request(plan_id="forged"), request(action_id="missing"),
                request(type="create_draft"), request(type="fetch_source")]:
        result = await actions.execute_approved_action(req)
        assert not result.ok and result.error == "INVALID_ACTION"
    setup.store.plan["status"] = "pending"
    result = await actions.execute_approved_action(request())
    assert result.error == "PLAN_NOT_APPROVED"
    setup.execute.assert_not_awaited()
    actions.connections.require_connection.assert_not_awaited()
    assert setup.store.claims == 0


async def test_concurrent_approvals_dispatch_once_and_done_plan_replays(setup):
    first, second = await asyncio.gather(actions.execute_approved_action(request()),
                                         actions.execute_approved_action(request()))
    assert first.ok or second.ok
    assert setup.execute.await_count == setup.store.claims == 1
    saved = copy.deepcopy(setup.store.execution["result"])
    setup.store.plan["status"] = "done"
    setup.connection.account_id = "a_different_account"
    replay = await actions.execute_approved_action(request())
    assert replay.ok and replay.data == saved
    assert replay.data["event_id"] == "event123"
    assert setup.execute.await_count == 1


async def test_exact_stored_event_inputs_and_one_margin_prefix(setup):
    setup.store.plan["actions"][0]["params"]["title"] = "[Margin] [Margin] Study physics"
    result = await actions.execute_approved_action(request(params={"calendar_id": "attacker", "title": "Override"}))
    assert result.ok and result.link == "https://calendar.google.com/calendar/event?eid=real"
    call = setup.execute.call_args
    assert call.args[:3] == ("alex", "gcal", "event_create")
    assert call.kwargs == {"write": True, "expected_account_id": "ca_calendar"}
    assert "Override" not in str(call.args[3]) and "attacker" not in str(call.args[3])
    assert "[Margin] Study physics" in call.args[3].values()
    assert call.args[3]["start_datetime"] == "2026-10-19T17:00:00Z"
    assert call.args[3]["end_datetime"] == "2026-10-19T18:30:00Z"
    assert call.args[3]["create_meeting_room"] is False
    assert call.args[3]["attendees"] == [] and call.args[3]["send_updates"] == "none"
    assert result.data["log_id"] == "log_event"
    assert "description" not in result.data


@pytest.mark.parametrize("changes", [
    {"ends_at": "2026-10-19T10:00:00-07:00"},
    {"starts_at": "2026-10-19T10:00:00"},
    {"attendees": ["someone@example.edu"]},
    {"title": "Bad\r\nHeader"},
    {"purpose": []},
])
async def test_invalid_stored_event_fails_durably_before_write(setup, changes):
    setup.store.plan["actions"][0]["params"].update(changes)
    result = await actions.execute_approved_action(request())
    assert result.error == "INVALID_ACTION" and result.data["execution_status"] == "failed"
    assert setup.store.execution["status"] == "failed"
    setup.execute.assert_not_awaited()


async def test_account_or_calendar_change_requires_new_review(setup):
    setup.connection.account_id = "different-account"
    result = await actions.execute_approved_action(request())
    assert result.error == "INVALID_ACTION"
    setup.execute.assert_not_awaited()


async def test_changed_calendar_selection_never_silently_moves_event(setup):
    setup.connection.settings["write_calendar_id"] = "other@example.edu"
    result = await actions.execute_approved_action(request())
    assert result.error == "INVALID_ACTION"
    setup.execute.assert_not_awaited()


async def test_readonly_calendar_rejected_distinct_from_conflict(setup):
    setup.target.writable = False
    result = await actions.execute_approved_action(request())
    assert result.error == "PERMISSION_DENIED"
    setup.execute.assert_not_awaited()


async def test_new_conflict_and_incomplete_read_never_claim_free_time(setup):
    event = Item(item_id="busy", user_id="alex", kind="event", title="Busy", source="gcal",
                 source_id="native", starts_at="2026-10-19T17:00:00Z", ends_at="2026-10-19T18:00:00Z")
    actions.calendar.fetch_calendar_events.return_value.records = [SimpleNamespace(item=event, blocks_time=True)]
    result = await actions.execute_approved_action(request())
    assert result.error == "CONFLICT_REVIEW_REQUIRED"
    setup.execute.assert_not_awaited()
    setup.store.execution.update(status="pending", result=None, error=None)
    actions.calendar.fetch_calendar_events.return_value.complete = False
    result = await actions.execute_approved_action(request())
    assert result.error == "PARTIAL_RESULT"
    setup.execute.assert_not_awaited()


async def test_empty_nonblocking_or_boundary_events_do_not_conflict(setup):
    records = []
    for begins, finishes, blocks in [
        ("2026-10-19T16:00:00Z", "2026-10-19T17:00:00Z", True),
        ("2026-10-19T18:30:00Z", "2026-10-19T19:30:00Z", True),
        ("2026-10-19T17:00:00Z", "2026-10-19T17:00:00Z", True),
        ("2026-10-19T17:00:00Z", "2026-10-19T18:00:00Z", False),
    ]:
        item = Item(item_id=begins, user_id="alex", kind="event", title="Busy", source="gcal",
                    source_id=begins, starts_at=begins, ends_at=finishes)
        records.append(SimpleNamespace(item=item, blocks_time=blocks))
    actions.calendar.fetch_calendar_events.return_value.records = records
    result = await actions.execute_approved_action(request())
    assert result.ok and setup.execute.await_count == 1


async def test_provider_timeout_stays_unknown_on_replay(setup):
    req = draft(setup)
    setup.execute.side_effect = IntegrationError("TEMPORARY_UNAVAILABLE", "gmail",
                                                 log_id="log_timeout", may_have_written=True)
    first = await actions.execute_approved_action(req)
    second = await actions.execute_approved_action(req)
    assert first.error == second.error == "OUTCOME_UNKNOWN"
    assert first.data["execution_status"] == second.data["execution_status"] == "unknown"
    assert first.data["log_id"] == "log_timeout"
    setup.execute.assert_awaited_once()


async def test_known_provider_rejection_is_failed_not_unknown(setup):
    setup.execute.side_effect = IntegrationError("PERMISSION_DENIED", "gcal", log_id="log_denied")
    result = await actions.execute_approved_action(request())
    assert result.error == "PERMISSION_DENIED" and result.data["execution_status"] == "failed"
    await actions.execute_approved_action(request())
    setup.execute.assert_awaited_once()


async def test_cancellation_after_dispatch_records_unknown_and_does_not_resend(setup):
    entered = asyncio.Event()

    async def pending(*args, **kwargs):
        entered.set()
        await asyncio.Future()

    setup.execute.side_effect = pending
    task = asyncio.create_task(actions.execute_approved_action(request()))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert setup.store.execution["status"] == "unknown"
    replay = await actions.execute_approved_action(request())
    assert not replay.ok and replay.error == "OUTCOME_UNKNOWN"
    setup.execute.assert_awaited_once()


async def test_receipt_failure_preserves_remote_receipt_and_blocks_second_write(setup):
    setup.store.fail_receipt = "before"
    result = await actions.execute_approved_action(request())
    assert not result.ok and result.error == "RECEIPT_UNAVAILABLE"
    assert result.data["execution_status"] == "unknown" and result.data["event_id"] == "event123"
    assert result.data["remote_outcome"] == "succeeded"
    assert setup.store.execution["status"] == "running"
    replay = await actions.execute_approved_action(request())
    assert replay.error == "EXECUTION_RUNNING"
    setup.execute.assert_awaited_once()


async def test_receipt_timeout_after_commit_replays_success(setup):
    setup.store.fail_receipt = "after"
    result = await actions.execute_approved_action(request())
    assert result.ok and result.data["execution_status"] == "succeeded"
    setup.execute.assert_awaited_once()


async def test_database_loss_before_claim_fails_closed(setup, monkeypatch):
    monkeypatch.setattr(actions.repo, "claim_action_execution", AsyncMock(side_effect=RuntimeError("db private")))
    result = await actions.execute_approved_action(request())
    assert not result.ok and result.error == "DATABASE_UNAVAILABLE"
    setup.execute.assert_not_awaited()


async def test_missing_event_identifier_is_unresolved(setup):
    setup.execute.return_value.data = {"response_data": {"htmlLink": "https://calendar.google.com/calendar/"}}
    result = await actions.execute_approved_action(request())
    assert result.error == "OUTCOME_UNKNOWN" and result.data["execution_status"] == "unknown"
    await actions.execute_approved_action(request())
    setup.execute.assert_awaited_once()


async def test_refresh_failure_does_not_repeat_confirmed_create(setup):
    actions.calendar.fetch_calendar_events.side_effect = [
        SimpleNamespace(complete=True, records=[]), IntegrationError("TEMPORARY_UNAVAILABLE", "gcal"),
    ]
    result = await actions.execute_approved_action(request())
    assert result.ok and result.data["cache_refresh_error"] == "CACHE_REFRESH_FAILED"
    replay = await actions.execute_approved_action(request())
    assert replay.ok and replay.data["event_id"] == "event123"
    setup.execute.assert_awaited_once()


async def test_draft_preserves_reviewed_content_without_send_or_body_receipt(setup):
    req = draft(setup)
    result = await actions.execute_approved_action(req)
    assert result.ok and result.data["draft_id"] == "draft123"
    assert result.data["message_id"] == "message123" and result.data["thread_id"] == "thread123"
    assert result.data["confirmation"] == "Draft created."
    assert result.data["link_kind"] == "drafts_folder"
    assert result.link == "https://mail.google.com/mail/u/0/#drafts"
    assert "Can we review" not in str(result.data)
    assert setup.execute.call_args.args[:3] == ("alex", "gmail", "gmail_draft")
    assert setup.execute.call_args.kwargs == {"write": True, "expected_account_id": "ca_gmail"}


@pytest.mark.parametrize("changes", [
    {"to": []}, {"to": "professor@example.edu"}, {"to": ["x@example.edu\r\nBcc: other@example.edu"]},
    {"to": ["x@example..edu"]}, {"subject": "Hello\nBcc: other@example.edu"},
    {"attachments": ["file"]}, {"thread_id": "unowned-thread"},
])
async def test_invalid_drafts_never_dispatch(setup, changes):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(changes)
    result = await actions.execute_approved_action(req)
    assert result.error == "INVALID_ACTION" and not result.ok
    setup.execute.assert_not_awaited()


async def test_reply_requires_owned_item_in_reviewed_mailbox(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"]["source_item_id"] = "item"
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("another-account", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock())
    result = await actions.execute_approved_action(req)
    assert result.error == "INVALID_ACTION"
    actions.messages.fetch_source.assert_not_awaited()
    setup.execute.assert_not_awaited()


async def test_reply_uses_verified_source_thread(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(source_item_id="item", thread_id="thread123")
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("ca_gmail", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock(return_value=SimpleNamespace(
        thread_id="thread123", subject=DRAFT["subject"],
    )))
    setup.execute.return_value.data["message"]["payload"] = {
        "headers": [{"name": "Subject", "value": "Re: " + DRAFT["subject"]}],
    }
    result = await actions.execute_approved_action(req)
    assert result.ok
    assert setup.execute.call_args.args[3]["thread_id"] == "thread123"
    assert "subject" not in setup.execute.call_args.args[3]
    actions.repo.get_item.assert_awaited_once_with("alex", "item")


async def test_reply_thread_mismatch_requires_review(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(source_item_id="item", thread_id="forged")
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("ca_gmail", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock(return_value=SimpleNamespace(thread_id="real")))
    result = await actions.execute_approved_action(req)
    assert result.error == "INVALID_ACTION"
    setup.execute.assert_not_awaited()


async def test_reply_verification_read_failure_keeps_created_draft_unresolved(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(source_item_id="item", thread_id="thread123")
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("ca_gmail", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock(return_value=SimpleNamespace(
        thread_id="thread123", subject=DRAFT["subject"],
    )))
    setup.execute.side_effect = [setup.execute.return_value,
                                 IntegrationError("TEMPORARY_UNAVAILABLE", "gmail", log_id="log_verify")]
    result = await actions.execute_approved_action(req)
    assert result.error == "OUTCOME_UNKNOWN" and result.data["execution_status"] == "unknown"
    assert result.data["draft_id"] == "draft123"
    assert setup.execute.call_args.args[2] == "gmail_get_draft"
    assert not setup.execute.call_args.kwargs.get("write", False)
    assert setup.execute.await_count == 2
    await actions.execute_approved_action(req)
    assert setup.execute.await_count == 2


async def test_reply_metadata_read_confirms_reviewed_subject(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(source_item_id="item", thread_id="thread123")
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("ca_gmail", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock(return_value=SimpleNamespace(
        thread_id="thread123", subject=DRAFT["subject"],
    )))
    setup.execute.side_effect = [setup.execute.return_value, SimpleNamespace(data={
        "id": "draft123", "message": {"threadId": "thread123", "subject": "Re: " + DRAFT["subject"]},
    }, log_id="log_verify")]
    result = await actions.execute_approved_action(req)
    assert result.ok and result.data["verification_log_id"] == "log_verify"
    assert result.data["log_id"] == "log_draft"
    assert setup.execute.call_args.args[3] == {"user_id": "me", "draft_id": "draft123", "format": "metadata"}


async def test_reply_silent_new_thread_fallback_is_not_reported_as_success(setup, monkeypatch):
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(source_item_id="item", thread_id="thread123")
    monkeypatch.setattr(actions.repo, "get_item", AsyncMock(return_value={
        "source": "gmail", "source_id": source_key("ca_gmail", "message123"),
    }))
    monkeypatch.setattr(actions.messages, "fetch_source", AsyncMock(return_value=SimpleNamespace(
        thread_id="thread123", subject=DRAFT["subject"],
    )))
    setup.execute.return_value.data["message"]["threadId"] = "new_thread"
    result = await actions.execute_approved_action(req)
    assert result.error == "OUTCOME_UNKNOWN"
    assert result.data["verification_error"] == "CONTENT_MISMATCH"
    assert result.data["draft_id"] == "draft123"
    setup.execute.assert_awaited_once()


async def test_arguments_validate_against_captured_tool_schemas(setup):
    import jsonschema

    schemas = json.loads((Path(__file__).parent / "fixtures/composio/tool_schemas.json").read_text())
    await actions.execute_approved_action(request())
    jsonschema.validate(setup.execute.call_args.args[3], schemas["GOOGLECALENDAR_CREATE_EVENT"]["input_parameters"])
    jsonschema.validate({"successful": True, "data": setup.execute.return_value.data},
                        schemas["GOOGLECALENDAR_CREATE_EVENT"]["output_parameters"])
    setup.store.execution.update(status="pending", result=None, error=None)
    req = draft(setup)
    setup.store.plan["actions"][0]["params"].update(to=["one@example.edu", "two@example.edu"],
                                                   cc=["three@example.edu"], bcc=["four@example.edu"])
    await actions.execute_approved_action(req)
    args = setup.execute.call_args.args[3]
    assert args["recipient_email"] == "one@example.edu" and args["extra_recipients"] == ["two@example.edu"]
    assert args["cc"] == ["three@example.edu"] and args["bcc"] == ["four@example.edu"]
    jsonschema.validate(args, schemas["GMAIL_CREATE_EMAIL_DRAFT"]["input_parameters"])
    jsonschema.validate({"successful": True, "data": setup.execute.return_value.data},
                        schemas["GMAIL_CREATE_EMAIL_DRAFT"]["output_parameters"])
