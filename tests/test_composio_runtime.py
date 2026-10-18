"""Exercise SDK/session boundaries without contacting Composio or providers."""

import asyncio
import copy
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from integrations.composio import connections, runtime
from integrations.composio.models import ConnectionState, IntegrationError


class HttpFailure(Exception):
    def __init__(self, status, *, retry_after=None):
        super().__init__("fixture secret draft body and credential must never be logged")
        self.status_code = status
        self.request_id = "request_fixture"
        self.response = SimpleNamespace(headers={} if retry_after is None else {"retry-after": retry_after})


class FakeSDK:
    def __init__(self):
        self.constructor_args = []
        self.create_calls = []
        self.resume_calls = []
        self.executions = []
        self.schemas = copy.deepcopy(runtime.CONTRACTS)
        self.schema_calls = []
        self.result = {"result_type": "completed", "data": {"messages": []}, "log_id": "log_fixture"}
        self.behavior = None
        self.expired = set()

    def client(self, **kwargs):
        self.constructor_args.append(kwargs)
        parent = self

        class Sessions:
            def create(self, **options):
                parent.create_calls.append(copy.deepcopy(options))
                return Session(f"session_{len(parent.create_calls)}")

            def use(self, session_id):
                parent.resume_calls.append(session_id)
                if session_id in parent.expired:
                    parent.expired.remove(session_id)
                    raise HttpFailure(404)
                return Session(session_id)

        class Session:
            def __init__(self, session_id):
                self.session_id = session_id

            def execute(self, slug, *, arguments):
                parent.executions.append((self.session_id, slug, copy.deepcopy(arguments), threading.get_ident()))
                return parent.behavior() if parent.behavior else copy.deepcopy(parent.result)

        def schema(slug):
            parent.schema_calls.append(slug)
            return SimpleNamespace(**copy.deepcopy(parent.schemas[slug]))

        return SimpleNamespace(sessions=Sessions(), tools=SimpleNamespace(get_raw_composio_tool_by_slug=schema))


@pytest.fixture
def sdk(monkeypatch):
    fake = FakeSDK()
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="margin-test-sdk")
    monkeypatch.setattr(runtime, "Composio", fake.client)
    monkeypatch.setattr(runtime, "_executor", executor)
    monkeypatch.setattr(runtime, "_slots", threading.BoundedSemaphore(4))
    monkeypatch.setattr(runtime, "_local", threading.local())
    monkeypatch.setattr(runtime, "_session_ids", {})
    monkeypatch.setattr(runtime, "_session_locks", {})
    monkeypatch.setenv("COMPOSIO_API_KEY", "fixture-project-key")
    for provider, name in runtime.AUTH_ENV.items():
        monkeypatch.setenv(name, "auth_" + provider)
    monkeypatch.setattr(connections, "require_connection", AsyncMock(
        side_effect=lambda user, provider: ConnectionState(provider, "active", f"ca_{user}_{provider}"),
    ))
    yield fake
    executor.shutdown(wait=True)


@pytest.mark.parametrize("write", [False, True])
def test_failed_result_with_no_error_is_not_empty_success(write):
    with pytest.raises(IntegrationError) as error:
        runtime._result({"result_type": "failed", "error": None, "data": None, "log_id": "log_failed"},
                        "gmail", write)
    assert error.value.log_id == "log_failed"
    assert error.value.may_have_written is write
    assert error.value.code == ("OUTCOME_UNKNOWN" if write else "TEMPORARY_UNAVAILABLE")


def test_successful_http_wrapper_with_provider_error_stays_failure():
    with pytest.raises(IntegrationError) as error:
        runtime._result({"result_type": "completed", "data": {
            "successful": True, "data": {"error": {"code": 403, "status": "PERMISSION_DENIED"}},
        }, "log_id": "log_nested"}, "gmail", False)
    assert error.value.code == "PERMISSION_DENIED" and not error.value.retryable


@pytest.mark.parametrize("payload", [
    {"result_type": "completed", "data": []},
    {"result_type": "completed", "data": {"successful": True, "data": None}},
    {"result_type": "input_required", "data": {"url": "https://private-consent.invalid"}},
    {"result_type": "awaiting_approval", "data": {}},
])
def test_malformed_or_input_request_is_never_auto_approved(payload):
    with pytest.raises(IntegrationError):
        runtime._result(payload, "gmail", False)


def test_result_unwraps_only_documented_envelope_and_preserves_log():
    result = runtime._result({"result_type": "completed", "data": {
        "successful": True, "data": {"messages": []}, "error": None,
    }, "log_id": "log_read"}, "gmail", False)
    assert result.data == {"messages": []} and result.log_id == "log_read"
    assert runtime._result({"result_type": "completed", "data": {}}, "gmail", False).log_id is None


def test_write_failure_classification_ignores_words_inside_message_content():
    # A failed session may carry an echoed input. Its draft text is not evidence
    # that Google definitely rejected a write for a particular reason.
    with pytest.raises(IntegrationError) as error:
        runtime._result({"result_type": "failed", "error": None, "data": {
            "body": "I need permission to access the review; the file was not found.",
        }}, "gmail", True)
    assert error.value.code == "OUTCOME_UNKNOWN" and error.value.may_have_written


def test_project_authentication_and_provider_revocation_are_distinct():
    assert runtime._exception(HttpFailure(401), "gmail").code == "CONFIGURATION_ERROR"
    with pytest.raises(IntegrationError) as error:
        runtime._result({"result_type": "completed", "data": {"ok": False, "error": "token_revoked"}},
                        "slack", False)
    assert error.value.code == "RELINK_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,write", [("gmail_send", False), ("gmail_draft", False),
                                             ("gmail_list", True), ("COMPOSIO_REMOTE_BASH_TOOL", True)])
async def test_unknown_tools_and_wrong_access_profile_fail_before_sdk(sdk, operation, write):
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", operation, {}, write=write)
    assert error.value.code == "INVALID_ACTION"
    assert sdk.executions == [] and sdk.constructor_args == []
    connections.require_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_reviewed_account_fails_before_sdk(sdk):
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_draft", {}, write=True,
                              expected_account_id="old-account")
    assert error.value.code == "ACCOUNT_CHANGED" and not error.value.may_have_written
    assert sdk.executions == [] and sdk.constructor_args == []


@pytest.mark.asyncio
async def test_schema_drift_fails_before_tool_or_session_creation(sdk):
    sdk.schemas["GMAIL_CREATE_EMAIL_DRAFT"]["input_parameters"]["required"] = ["new_required_field"]
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_draft", {"subject": "Reviewed"}, write=True)
    assert error.value.code == "SCHEMA_MISMATCH" and not error.value.may_have_written
    assert sdk.executions == [] and sdk.create_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"unknown_provider_override": True}, {"is_html": "not_boolean"}])
async def test_invalid_argument_contract_fails_before_dispatch(sdk, arguments):
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_draft", arguments, write=True)
    assert error.value.code == "INVALID_ACTION" and not error.value.may_have_written
    assert sdk.executions == [] and sdk.create_calls == []


@pytest.mark.asyncio
async def test_sessions_are_scoped_reused_and_never_grant_send_delete_or_sandbox(sdk):
    await runtime.execute("alex", "gmail", "gmail_list", {})
    await runtime.execute("alex", "gmail", "gmail_list", {})
    await runtime.execute("other", "gmail", "gmail_list", {})
    await runtime.execute("alex", "gcal", "calendar_list", {})
    await runtime.execute("alex", "gmail", "gmail_draft", {}, write=True)
    assert len(sdk.create_calls) == 4 and sdk.resume_calls == ["session_1"]
    assert all(kwargs == {"timeout": 10, "max_retries": 0} for kwargs in sdk.constructor_args)
    identities = []
    for options in sdk.create_calls:
        toolkit = options["toolkits"][0]
        allowed = options["tools"][toolkit]["enable"]
        assert all("SEND" not in tool and "DELETE" not in tool and "REMOTE" not in tool for tool in allowed)
        assert options["sandbox"] == {"enable": False} and options["proxy_execute"] is False
        assert options["manage_connections"] is False and options["instant"] is False
        identities.append((options["user_id"], tuple(options["connected_accounts"][toolkit])))
    assert identities == [("alex", ("ca_alex_gmail",)), ("other", ("ca_other_gmail",)),
                          ("alex", ("ca_alex_gcal",)), ("alex", ("ca_alex_gmail",))]
    assert "GMAIL_CREATE_EMAIL_DRAFT" not in sdk.create_calls[0]["tools"]["gmail"]["enable"]
    write_tools = sdk.create_calls[-1]["tools"]["gmail"]["enable"]
    assert "GMAIL_CREATE_EMAIL_DRAFT" in write_tools and "GMAIL_GET_DRAFT" in write_tools
    assert "GMAIL_FETCH_EMAILS" not in write_tools


@pytest.mark.asyncio
async def test_expired_session_rebuilds_same_user_account_profile(sdk):
    await runtime.execute("alex", "gmail", "gmail_list", {})
    sdk.expired.add("session_1")
    await runtime.execute("alex", "gmail", "gmail_list", {})
    assert sdk.create_calls[0] == sdk.create_calls[1]
    assert sdk.executions[-1][0] == "session_2"


@pytest.mark.asyncio
async def test_invalidation_rebuilds_only_target_user_provider(sdk):
    await runtime.execute("alex", "gmail", "gmail_list", {})
    await runtime.execute("other", "gmail", "gmail_list", {})
    runtime.invalidate_sessions("alex", "gmail")
    await runtime.execute("other", "gmail", "gmail_list", {})
    await runtime.execute("alex", "gmail", "gmail_list", {})
    assert len(sdk.create_calls) == 3 and sdk.resume_calls == ["session_2"]


@pytest.mark.asyncio
async def test_rate_limit_read_retries_within_budget_without_leaking_body(sdk, caplog):
    calls = 0

    def rate_limited():
        nonlocal calls
        calls += 1
        if calls < 3:
            raise HttpFailure(429, retry_after="0.001")
        return sdk.result

    sdk.behavior = rate_limited
    with caplog.at_level(logging.INFO, logger=runtime.__name__):
        result = await runtime.execute("alex", "gmail", "gmail_list", {})
    assert result.log_id == "log_fixture" and len(sdk.executions) == 3
    assert "request_fixture" in caplog.text and "log_fixture" in caplog.text
    assert "fixture secret" not in caplog.text and "credential" not in caplog.text


@pytest.mark.asyncio
async def test_exhausted_read_retries_stop_at_three_attempts(sdk):
    sdk.behavior = lambda: (_ for _ in ()).throw(HttpFailure(503, retry_after="0"))
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_list", {})
    assert error.value.code == "TEMPORARY_UNAVAILABLE" and len(sdk.executions) == 3


@pytest.mark.asyncio
async def test_permission_rejection_does_not_retry(sdk):
    sdk.behavior = lambda: (_ for _ in ()).throw(HttpFailure(403))
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_list", {})
    assert error.value.code == "PERMISSION_DENIED" and len(sdk.executions) == 1


@pytest.mark.asyncio
async def test_retry_after_longer_than_remaining_budget_is_not_waited(sdk):
    sdk.behavior = lambda: (_ for _ in ()).throw(HttpFailure(429, retry_after="10"))
    began = time.monotonic()
    with pytest.raises(IntegrationError) as error:
        async with runtime.read_budget(.1):
            await runtime.execute("alex", "gmail", "gmail_list", {})
    assert error.value.code == "RATE_LIMITED" and len(sdk.executions) == 1
    assert time.monotonic() - began < .5


@pytest.mark.asyncio
async def test_write_connection_loss_dispatches_once_and_becomes_unknown(sdk):
    sdk.behavior = lambda: (_ for _ in ()).throw(ConnectionError("private request payload"))
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_draft", {"body": "private draft"}, write=True)
    assert error.value.code == "OUTCOME_UNKNOWN" and error.value.may_have_written
    assert len(sdk.executions) == 1


@pytest.mark.asyncio
async def test_coroutine_timeout_does_not_cancel_or_repeat_synchronous_write(sdk):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=.3)
        finished.set()
        return sdk.result

    sdk.behavior = slow
    try:
        with pytest.raises(IntegrationError) as error:
            async with runtime.read_budget(.04):
                await runtime.execute("alex", "gmail", "gmail_draft", {}, write=True)
        assert started.is_set() and not finished.is_set()
        assert error.value.code == "OUTCOME_UNKNOWN" and error.value.may_have_written
        assert len(sdk.executions) == 1
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, .2)
    assert len(sdk.executions) == 1


@pytest.mark.asyncio
async def test_slow_synchronous_sdk_keeps_event_loop_responsive(sdk):
    started, release = threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=.3)
        return sdk.result

    sdk.behavior = slow
    task = asyncio.create_task(runtime.execute("alex", "gmail", "gmail_list", {}))
    try:
        assert await asyncio.to_thread(started.wait, .2)
        assert not task.done()
        # This coroutine can run while the synchronous provider call is waiting.
        await asyncio.sleep(.005)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result.log_id == "log_fixture"
    assert sdk.executions[0][3] != threading.get_ident()


@pytest.mark.asyncio
async def test_cancelled_await_keeps_single_inflight_write(sdk):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        started.set()
        release.wait(timeout=.3)
        finished.set()
        return sdk.result

    sdk.behavior = slow
    task = asyncio.create_task(runtime.execute("alex", "gmail", "gmail_draft", {}, write=True))
    try:
        assert await asyncio.to_thread(started.wait, .2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not finished.is_set() and len(sdk.executions) == 1
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, .2)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["_schema", "_session"])
async def test_setup_timeout_prevents_background_worker_from_later_writing(sdk, monkeypatch, phase):
    started, release = threading.Event(), threading.Event()
    original = getattr(runtime, phase)

    def delayed(*args, **kwargs):
        started.set()
        release.wait(timeout=.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, phase, delayed)
    try:
        with pytest.raises(IntegrationError) as error:
            async with runtime.read_budget(.04):
                await runtime.execute("alex", "gmail", "gmail_draft", {"subject": "Reviewed"}, write=True)
        assert started.is_set()
        assert not error.value.may_have_written
        assert sdk.executions == []
    finally:
        release.set()
        # Join the real worker so the assertion covers work after the caller's
        # timeout, not just the instant when the coroutine stopped waiting.
        await asyncio.to_thread(runtime._executor.shutdown, wait=True)
    assert sdk.executions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["_schema", "_session"])
async def test_setup_cancellation_prevents_late_provider_dispatch(sdk, monkeypatch, phase):
    started, release = threading.Event(), threading.Event()
    original = getattr(runtime, phase)

    def delayed(*args, **kwargs):
        started.set()
        release.wait(timeout=.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, phase, delayed)
    task = asyncio.create_task(runtime.execute("alex", "gmail", "gmail_draft", {}, write=True))
    try:
        assert await asyncio.to_thread(started.wait, .2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sdk.executions == []
    finally:
        release.set()
        await asyncio.to_thread(runtime._executor.shutdown, wait=True)
    assert sdk.executions == []


@pytest.mark.asyncio
async def test_provider_revocation_persists_account_guard_and_invalidates_only_its_sessions(sdk, monkeypatch):
    from db import repo

    status = AsyncMock()
    monkeypatch.setattr(repo, "set_connection_status", status)
    await runtime.execute("alex", "gmail", "gmail_list", {})
    await runtime.execute("other", "gmail", "gmail_list", {})
    await runtime.execute("alex", "gcal", "calendar_list", {})
    sdk.result = {"result_type": "completed", "data": {"successful": False,
                  "error": {"code": "token_revoked"}}, "log_id": "log_revoked"}
    with pytest.raises(IntegrationError) as error:
        await runtime.execute("alex", "gmail", "gmail_list", {})
    assert error.value.code == "RELINK_REQUIRED" and error.value.log_id == "log_revoked"
    status.assert_awaited_once_with("alex", "gmail", "ca_alex_gmail", "relink_required")
    assert all(key[:2] != ("alex", "gmail") for key in runtime._session_ids)
    assert any(key[:2] == ("other", "gmail") for key in runtime._session_ids)
    assert any(key[:2] == ("alex", "gcal") for key in runtime._session_ids)
