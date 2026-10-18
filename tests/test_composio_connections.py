"""Connection ownership/selection contracts with an in-memory repository boundary."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from integrations.composio import calendar, connections, messages
from integrations.composio.models import (
    CalendarOption, ChannelOption, ConnectionSettings, ConnectionState,
    IntegrationError, ReadBatch,
)


def account(account_id="ca-one", *, user="alex", toolkit="gmail", auth="auth-gmail", status="ACTIVE", disabled=False):
    return {
        "id": account_id, "user_id": user, "toolkit": {"slug": toolkit},
        "auth_config": {"id": auth}, "status": status, "is_disabled": disabled,
        "state": {"access_token": "fixture-oauth-secret"},
    }


def stored(account_id="ca-one", *, settings=None, status="active", pending=None):
    secret = {"settings": deepcopy(settings)} if settings else {}
    if pending:
        secret["pending_account_id"] = pending
    return {"composio_account_id": account_id, "status": status, "secret": secret or None}


@pytest.fixture
def service(monkeypatch):
    for name, value in {
        "COMPOSIO_API_KEY": "fixture-project-key",
        "GMAIL_AUTH_CONFIG_ID": "auth-gmail",
        "GCAL_AUTH_CONFIG_ID": "auth-calendar",
        "SLACK_AUTH_CONFIG_ID": "auth-slack",
        "PUBLIC_BASE_URL": "https://margin.test",
    }.items():
        monkeypatch.setenv(name, value)
    store = {}

    async def get(user_id, app):
        return deepcopy(store.get((user_id, app)))

    async def save(user_id, app, *, composio_account_id=None, secret=None, status="active"):
        store[user_id, app] = {
            "composio_account_id": composio_account_id,
            "secret": deepcopy(secret), "status": status,
        }

    async def set_status(user_id, app, account_id, status):
        value = store.get((user_id, app))
        if value and value["composio_account_id"] == account_id:
            value["status"] = status

    monkeypatch.setattr(connections.repo, "get_connection", AsyncMock(side_effect=get))
    monkeypatch.setattr(connections.repo, "save_connection", AsyncMock(side_effect=save))
    monkeypatch.setattr(connections.repo, "upsert_user", AsyncMock())
    monkeypatch.setattr(connections.repo, "set_connection_status", AsyncMock(side_effect=set_status))
    invalidate = Mock()
    monkeypatch.setattr(connections.runtime, "invalidate_sessions", invalidate)
    authorize = Mock(return_value=SimpleNamespace(id="ca-pending", redirect_url="https://connect.composio.dev/link/fixture"))
    sdk = SimpleNamespace(
        connected_accounts=SimpleNamespace(list=Mock(return_value=SimpleNamespace(items=[], next_cursor=None))),
        sessions=SimpleNamespace(create=Mock(return_value=SimpleNamespace(authorize=authorize))),
    )

    async def call(function, **kwargs):
        return function(sdk)

    sdk_call = AsyncMock(side_effect=call)
    monkeypatch.setattr(connections.runtime, "sdk_call", sdk_call)
    return SimpleNamespace(store=store, sdk=sdk, sdk_call=sdk_call, invalidate=invalidate, authorize=authorize)


def rows(service, *accounts):
    service.sdk.connected_accounts.list.return_value = SimpleNamespace(items=list(accounts), next_cursor=None)


@pytest.mark.asyncio
async def test_query_and_local_filters_enforce_user_toolkit_auth_config(service):
    rows(service, account(), account("wrong-user", user="other"), account("wrong-toolkit", toolkit="slack"), account("wrong-project", auth="other-auth"))
    state = await connections.connection_status("alex", "gmail")
    assert state.status == "active" and state.account_id == "ca-one"
    call = service.sdk.connected_accounts.list.call_args.kwargs
    assert call["user_ids"] == ["alex"] and call["toolkit_slugs"] == ["gmail"]
    assert call["auth_config_ids"] == ["auth-gmail"] and call["account_type"] == "PRIVATE"
    assert "fixture-oauth-secret" not in repr(state)
    assert "fixture-oauth-secret" not in repr(service.store)
    assert set(service.store) == {("alex", "gmail")}


@pytest.mark.asyncio
async def test_users_and_google_apps_never_share_selections(service):
    rows(service, account("ca-alex"))
    await connections.connection_status("alex", "gmail")
    rows(service, account("ca-other", user="other"))
    await connections.connection_status("other", "gmail")
    rows(service, account("ca-alex"))  # Even a malformed backend cross-toolkit result is excluded.
    calendar_state = await connections.connection_status("alex", "gcal")
    assert service.store["alex", "gmail"]["composio_account_id"] == "ca-alex"
    assert service.store["other", "gmail"]["composio_account_id"] == "ca-other"
    assert calendar_state.status == "not_connected" and ("alex", "gcal") not in service.store


@pytest.mark.asyncio
async def test_multiple_active_accounts_require_explicit_selection(service):
    rows(service, account(), account("ca-two"))
    state = await connections.connection_status("alex", "gmail")
    assert state.status == "selection_required" and state.account_id is None
    assert state.candidate_account_ids == ["ca-one", "ca-two"]
    assert service.store == {}
    assert not await connections.is_connected("alex", "gmail")
    with pytest.raises(IntegrationError) as error:
        await connections.require_connection("alex", "gmail")
    assert error.value.code == "ACCOUNT_SELECTION_REQUIRED"
    selected = await connections.select_connected_account("alex", "gmail", "ca-two")
    assert selected.account_id == "ca-two" and selected.status == "active"
    assert (await connections.connection_status("alex", "gmail")).account_id == "ca-two"


@pytest.mark.asyncio
async def test_selecting_unowned_inactive_or_disabled_accounts_fails(service):
    rows(service, account("other", user="other"), account("pending", status="INITIATED"), account("disabled", disabled=True))
    for account_id in ("other", "pending", "disabled", "missing"):
        with pytest.raises(IntegrationError) as error:
            await connections.select_connected_account("alex", "gmail", account_id)
        assert error.value.code == "PERMISSION_DENIED"
    assert service.store == {}


@pytest.mark.asyncio
async def test_forged_saved_account_reference_does_not_bypass_ownership(service):
    service.store["alex", "gmail"] = stored("ca-other")
    rows(service, account("ca-other", user="other"))
    state = await connections.connection_status("alex", "gmail")
    assert state.status == "relink_required"
    with pytest.raises(IntegrationError) as error:
        await connections.require_connection("alex", "gmail")
    assert error.value.code == "RELINK_REQUIRED"
    assert service.store["alex", "gmail"]["status"] == "relink_required"


@pytest.mark.asyncio
async def test_account_listing_paginates_before_selecting_and_caps_incomplete_lists(service):
    service.sdk.connected_accounts.list.side_effect = [
        SimpleNamespace(items=[account()], next_cursor="next-page"),
        SimpleNamespace(items=[account("ca-two")], next_cursor=None),
    ]
    assert (await connections.connection_status("alex", "gmail")).status == "selection_required"
    assert service.sdk.connected_accounts.list.call_args_list[1].kwargs["cursor"] == "next-page"
    service.sdk.connected_accounts.list.side_effect = None
    service.sdk.connected_accounts.list.return_value = SimpleNamespace(items=[account()], next_cursor="never-finishes")
    with pytest.raises(IntegrationError) as error:
        await connections.connection_status("alex", "gmail")
    assert error.value.code == "PARTIAL_RESULT" and service.store == {}


@pytest.mark.asyncio
async def test_status_refresh_and_same_account_selection_preserve_settings(service):
    settings = {"calendar_ids": ["classes"], "write_calendar_id": "personal"}
    service.store["alex", "gcal"] = stored(settings=settings)
    rows(service, account(toolkit="googlecalendar", auth="auth-calendar"))
    state = await connections.connection_status("alex", "gcal")
    assert state.settings == settings
    state = await connections.select_connected_account("alex", "gcal", "ca-one")
    assert state.settings == settings and service.store["alex", "gcal"]["secret"] == {"settings": settings}


@pytest.mark.asyncio
async def test_switching_account_clears_resource_settings_and_invalidates_sessions(service):
    service.store["alex", "slack"] = stored(settings={"workspace_id": "T-old", "channel_ids": ["C-old"]})
    rows(service, account("ca-two", toolkit="slack", auth="auth-slack"))
    selected = await connections.select_connected_account("alex", "slack", "ca-two")
    assert selected.settings == {} and service.store["alex", "slack"]["secret"] is None
    service.invalidate.assert_called_with("alex", "slack")


@pytest.mark.asyncio
async def test_connect_link_is_restricted_and_persists_pending_without_proving_consent(service):
    link = await connections.connect_link("alex", "gmail")
    assert link == "https://connect.composio.dev/link/fixture"
    kwargs = service.sdk.sessions.create.call_args.kwargs
    assert kwargs["user_id"] == "alex" and kwargs["toolkits"] == ["gmail"]
    assert kwargs["auth_configs"] == {"gmail": "auth-gmail"}
    assert kwargs["tools"] == {"gmail": {"enable": []}}
    assert kwargs["sandbox"] == {"enable": False} and not kwargs["proxy_execute"]
    service.authorize.assert_called_once_with("gmail", callback_url="https://margin.test/connected?app=Gmail")
    assert service.sdk_call.call_args.kwargs["retry"] is False
    saved = service.store["alex", "gmail"]
    assert saved["status"] == "pending" and saved["composio_account_id"] is None
    assert saved["secret"] == {"pending_account_id": "ca-pending"}
    rows(service, account("ca-pending", status="INITIATED"))
    assert (await connections.connection_status("alex", "gmail")).status == "pending"
    assert not await connections.is_connected("alex", "gmail")
    assert saved["status"] == "pending"


@pytest.mark.asyncio
async def test_verified_pending_account_becomes_active_only_after_remote_confirmation(service):
    await connections.connect_link("alex", "gmail")
    rows(service, account("ca-pending"))
    state = await connections.connection_status("alex", "gmail")
    assert state.status == "active" and state.account_id == "ca-pending"
    assert service.store["alex", "gmail"]["status"] == "active"
    assert service.store["alex", "gmail"]["secret"] is None


@pytest.mark.asyncio
async def test_new_consent_keeps_old_account_settings_until_verified_then_resets(service):
    settings = {"calendar_ids": ["old-calendar"], "write_calendar_id": "old-calendar"}
    service.store["alex", "gcal"] = stored(settings=settings)
    await connections.connect_link("alex", "gcal")
    pending = service.store["alex", "gcal"]
    assert pending["composio_account_id"] == "ca-one" and pending["secret"]["settings"] == settings
    rows(service, account("ca-pending", toolkit="googlecalendar", auth="auth-calendar"))
    active = await connections.connection_status("alex", "gcal")
    assert active.account_id == "ca-pending" and active.settings == {}
    assert service.store["alex", "gcal"]["secret"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("remote", [None, "EXPIRED", "FAILED", "INACTIVE", "DISABLED"])
async def test_revoked_missing_or_disabled_saved_accounts_require_relink(service, remote):
    service.store["alex", "gmail"] = stored()
    rows(service, *([] if remote is None else [account(status=remote)]))
    state = await connections.connection_status("alex", "gmail")
    assert state.status == "relink_required" and state.account_id == "ca-one"
    assert service.store["alex", "gmail"]["status"] == "relink_required"
    service.invalidate.assert_called_with("alex", "gmail")


@pytest.mark.asyncio
async def test_configuration_or_service_failure_does_not_become_disconnected(service, monkeypatch):
    service.sdk_call.side_effect = IntegrationError("TEMPORARY_UNAVAILABLE", "gmail", retryable=True)
    with pytest.raises(IntegrationError) as error:
        await connections.is_connected("alex", "gmail")
    assert error.value.code == "TEMPORARY_UNAVAILABLE"
    monkeypatch.delenv("GMAIL_AUTH_CONFIG_ID")
    with pytest.raises(IntegrationError) as error:
        await connections.is_connected("alex", "gmail")
    assert error.value.code == "CONFIGURATION_ERROR"
    assert service.store == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["http://margin.test", "https://margin.test/path", "https://name:password@margin.test", "https://margin.test?query=1", ""])
async def test_invalid_callback_origin_fails_before_sdk(service, monkeypatch, origin):
    monkeypatch.setenv("PUBLIC_BASE_URL", origin)
    with pytest.raises(IntegrationError) as error:
        await connections.connect_link("alex", "gmail")
    assert error.value.code == "CONFIGURATION_ERROR"
    service.sdk_call.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect", ["http://connect.composio.dev/link", "https://evil.test/link", "https://name:password@connect.composio.dev/link", "https://:password@connect.composio.dev/link", "https://connect.composio.dev:444/link"])
async def test_unsafe_consent_redirect_is_rejected_without_persistence(service, redirect):
    service.authorize.return_value.redirect_url = redirect
    with pytest.raises(IntegrationError) as error:
        await connections.connect_link("alex", "gmail")
    assert error.value.code == "SCHEMA_MISMATCH"
    assert service.store == {}


@pytest.mark.asyncio
async def test_calendar_settings_only_accept_owned_readable_ids_and_writable_target(service, monkeypatch):
    rows(service, account(toolkit="googlecalendar", auth="auth-calendar"))
    options = [
        CalendarOption("classes", "Classes", "ca-one", readable=True, writable=False),
        CalendarOption("personal", "Personal", "ca-one", writable=True),
        CalendarOption("foreign", "Other user's calendar", "ca-other", writable=True),
        CalendarOption("busy-only", "Busy", "ca-one", readable=False),
    ]
    discovery = AsyncMock(return_value=ReadBatch(records=options))
    monkeypatch.setattr(calendar, "list_calendars", discovery)
    settings = ConnectionSettings(calendar_ids=["classes", "classes", "personal"], write_calendar_id="personal")
    await connections.save_connection_settings("alex", "gcal", settings)
    assert service.store["alex", "gcal"]["secret"] == {"settings": {"calendar_ids": ["classes", "personal"], "write_calendar_id": "personal"}}
    for ids, target in [(["classes"], "personal"), (["foreign"], "personal"), (["classes"], "classes"), (["busy-only"], "personal"), (["missing"], "personal")]:
        with pytest.raises(IntegrationError) as error:
            await connections.save_connection_settings("alex", "gcal", {"calendar_ids": ids, "write_calendar_id": target})
        assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_channel_settings_are_account_and_workspace_scoped(service, monkeypatch):
    rows(service, account(toolkit="slack", auth="auth-slack"))
    options = [
        ChannelOption("C-project", "project", "ca-one", "T-one"),
        ChannelOption("C-club", "club", "ca-one", "T-one"),
        ChannelOption("C-foreign", "foreign", "ca-other", "T-one"),
        ChannelOption("C-other-workspace", "elsewhere", "ca-one", "T-two"),
        ChannelOption("C-private", "private", "ca-one", "T-one", readable=False),
    ]
    monkeypatch.setattr(messages, "list_slack_channels", AsyncMock(return_value=ReadBatch(records=options)))
    await connections.save_connection_settings("alex", "slack", {"workspace_id": "T-one", "channel_ids": ["C-project", "C-club", "C-project"]})
    expected = {"workspace_id": "T-one", "channel_ids": ["C-project", "C-club"]}
    assert service.store["alex", "slack"]["secret"] == {"settings": expected}
    for channel_id in ("C-foreign", "C-other-workspace", "C-private", "missing"):
        with pytest.raises(IntegrationError) as error:
            await connections.save_connection_settings("alex", "slack", {"workspace_id": "T-one", "channel_ids": [channel_id]})
        assert error.value.code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_partial_discovery_and_unexpected_settings_arguments_fail_closed(service, monkeypatch):
    rows(service, account(toolkit="googlecalendar", auth="auth-calendar"))
    monkeypatch.setattr(calendar, "list_calendars", AsyncMock(return_value=ReadBatch(
        records=[CalendarOption("classes", "Classes", "ca-one", writable=True)], complete=False,
    )))
    with pytest.raises(IntegrationError) as error:
        await connections.save_connection_settings("alex", "gcal", {"calendar_ids": ["classes"], "write_calendar_id": "classes"})
    assert error.value.code == "PERMISSION_DENIED"
    for value in ([], {"calendar_ids": []}, {"calendar_ids": ["classes"], "write_calendar_id": "classes", "tool_slug": "SEND_EMAIL"}):
        with pytest.raises(IntegrationError) as error:
            await connections.save_connection_settings("alex", "gcal", value)
        assert error.value.code == "INVALID_ACTION"


@pytest.mark.asyncio
async def test_account_change_during_resource_discovery_requires_new_selection(service, monkeypatch):
    monkeypatch.setattr(connections, "require_connection", AsyncMock(side_effect=[
        ConnectionState("gcal", "active", "ca-one"), ConnectionState("gcal", "active", "ca-two"),
    ]))
    monkeypatch.setattr(calendar, "list_calendars", AsyncMock(return_value=ReadBatch(
        records=[CalendarOption("classes", "Classes", "ca-one", writable=True)],
    )))
    with pytest.raises(IntegrationError) as error:
        await connections.save_connection_settings("alex", "gcal", {"calendar_ids": ["classes"], "write_calendar_id": "classes"})
    assert error.value.code == "ACCOUNT_CHANGED" and service.store == {}
