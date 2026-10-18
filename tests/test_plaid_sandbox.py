"""Plaid ingestion contracts using provider and persistence doubles; never live bank data."""

import asyncio
import json
import threading
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import pytest

from integrations import plaid_sandbox as plaid


pytestmark = pytest.mark.asyncio
REAL_PROVIDER_CALL = plaid._provider_call


def connection(*, cursor="saved-cursor", status="active", **secret_changes):
    secret = {
        "access_token": "access-sandbox-fixture-private",
        "item_id": "item-alex",
        "environment": "sandbox",
        "cursor": cursor,
        "last_synced_at": "2026-10-18T08:00:00Z" if cursor else None,
    }
    secret.update(secret_changes)
    return {"user_id": "alex", "provider": "plaid", "status": status, "secret": secret}


def transaction(txn_id="txn-1", **changes):
    return {
        "transaction_id": txn_id,
        "account_id": "bank-account-1",
        "date": "2026-10-01",
        "datetime": None,
        "amount": 12.34,
        "iso_currency_code": "USD",
        "unofficial_currency_code": None,
        "merchant_name": "Campus cafe",
        "name": "CAFE 001",
        "pending": False,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"},
        **changes,
    }


def page(*, added=(), modified=(), removed=(), cursor="next-cursor", more=False, request="req-fixture", status="HISTORICAL_UPDATE_COMPLETE"):
    return {
        "added": list(added), "modified": list(modified),
        "removed": [{"transaction_id": value} for value in removed],
        "next_cursor": cursor, "has_more": more, "request_id": request,
        "transactions_update_status": status,
    }


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("PLAID_SECRET", "fixture-secret-never-log")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(plaid.repo, "get_user", AsyncMock(return_value={"user_id": "alex", "tz": "America/Phoenix"}))
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=connection()))
    monkeypatch.setattr(plaid.repo, "get_budget", AsyncMock(return_value={"monthly_income": 1000, "savings_goal": 100, "weekly_cap": None}))
    monkeypatch.setattr(plaid.repo, "get_bills", AsyncMock(return_value=[]))
    monkeypatch.setattr(plaid.repo, "safe_to_spend", AsyncMock(return_value=72.50))
    monkeypatch.setattr(plaid.storage, "initialize_connection", AsyncMock(return_value=connection(cursor=None)))
    monkeypatch.setattr(plaid.storage, "apply_sync", AsyncMock(return_value=True))
    monkeypatch.setattr(plaid.storage, "mark_relink_required", AsyncMock(return_value=True))
    monkeypatch.setattr(plaid, "_provider_call", AsyncMock(side_effect=AssertionError("Unexpected provider call")))


async def test_existing_sandbox_connection_is_reused_without_creating_item():
    result = await plaid.connect_sandbox("alex")
    assert result.item_id == "item-alex"
    plaid._provider_call.assert_not_awaited()
    plaid.storage.initialize_connection.assert_not_awaited()
    assert "access-sandbox" not in repr(result)


async def test_connect_provisions_and_exchanges_once_then_saves_encrypted_connection(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=None))
    plaid._provider_call.side_effect = [
        {"public_token": "public-sandbox-fixture", "request_id": "req-create"},
        {"access_token": "access-sandbox-fixture-private", "item_id": "item-alex", "request_id": "req-exchange"},
    ]
    result = await plaid.connect_sandbox("alex")
    assert result.item_id == "item-alex"
    assert [call.args[0] for call in plaid._provider_call.await_args_list] == ["create_sandbox", "exchange"]
    assert plaid._provider_call.await_args_list[1].kwargs["public_token"] == "public-sandbox-fixture"
    saved = plaid.storage.initialize_connection.await_args
    assert saved.args == ("alex",)
    assert saved.kwargs["item_id"] == "item-alex"
    assert saved.kwargs["access_token"] == "access-sandbox-fixture-private"


@pytest.mark.parametrize("field,value", [("PLAID_ENV", "production"), ("TOKEN_ENCRYPTION_KEY", "invalid")])
async def test_invalid_setup_fails_before_creating_external_item(monkeypatch, field, value):
    monkeypatch.setenv(field, value)
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=None))
    with pytest.raises(plaid.PlaidError):
        await plaid.connect_sandbox("alex")
    plaid._provider_call.assert_not_awaited()
    plaid.storage.initialize_connection.assert_not_awaited()


@pytest.mark.parametrize("field", ["PLAID_CLIENT_ID", "PLAID_SECRET"])
async def test_missing_credentials_are_rejected_before_constructing_sdk_api(monkeypatch, field):
    from plaid.api import plaid_api
    monkeypatch.setenv(field, "")
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=None))
    monkeypatch.setattr(plaid, "_provider_call", REAL_PROVIDER_CALL)
    api = Mock(side_effect=AssertionError("SDK should not be constructed without credentials"))
    monkeypatch.setattr(plaid_api, "PlaidApi", api)
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.connect_sandbox("alex")
    assert error.value.code == "CONFIGURATION_ERROR"
    api.assert_not_called()
    plaid.storage.initialize_connection.assert_not_awaited()


async def test_unknown_user_cannot_create_external_item(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_user", AsyncMock(return_value=None))
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=None))
    with pytest.raises(plaid.PlaidError):
        await plaid.connect_sandbox("missing-user")
    plaid._provider_call.assert_not_awaited()


async def test_sync_requires_explicit_connection_and_never_auto_provisions(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=None))
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "NOT_CONNECTED"
    plaid._provider_call.assert_not_awaited()
    plaid.storage.initialize_connection.assert_not_awaited()


async def test_sync_fetches_every_page_before_atomic_save_and_retains_financial_fields():
    plaid._provider_call.side_effect = [
        page(added=[transaction("pending", pending=True)], cursor="page-two", more=True, request="req-1"),
        page(added=[transaction("refund", amount=-4.25)], modified=[transaction("corrected", amount=20.50)],
             removed=["old-transaction"], cursor="complete-cursor", request="req-2"),
    ]
    result = await plaid.sync_transactions("alex")
    assert result.added == 2 and result.modified == 1 and result.removed == 1
    assert result.pages == 2 and result.ready
    assert list(result.request_ids) == ["req-1", "req-2"]
    assert result.last_synced_at is not None
    assert [call.kwargs["cursor"] for call in plaid._provider_call.await_args_list] == ["saved-cursor", "page-two"]
    plaid.storage.apply_sync.assert_awaited_once()
    saved = plaid.storage.apply_sync.await_args
    assert saved.args == ("alex",)
    assert saved.kwargs["expected_secret"] == connection()["secret"]
    assert saved.kwargs["next_cursor"] == "complete-cursor"
    assert saved.kwargs["removed_ids"] == ["old-transaction"]
    rows = {row["txn_id"]: row for row in saved.kwargs["transactions"]}
    assert rows["pending"]["pending"] is True
    assert Decimal(str(rows["refund"]["amount"])) == Decimal("-4.25")
    assert rows["corrected"]["category"] == "FOOD_AND_DRINK"
    assert rows["corrected"]["merchant"] == "Campus cafe"
    assert rows["corrected"]["currency"] == "USD"
    date_only = rows["corrected"]["ts"]
    if isinstance(date_only, str):
        date_only = datetime.fromisoformat(date_only.replace("Z", "+00:00"))
    assert date_only.astimezone(timezone.utc) == datetime(2026, 10, 1, 19, tzinfo=timezone.utc)


async def test_explicit_transaction_datetime_wins_over_date():
    plaid._provider_call.side_effect = [page(added=[transaction(datetime="2026-10-02T01:02:03-07:00")])]
    await plaid.sync_transactions("alex")
    value = plaid.storage.apply_sync.await_args.kwargs["transactions"][0]["ts"]
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert value.astimezone(timezone.utc) == datetime(2026, 10, 2, 8, 2, 3, tzinfo=timezone.utc)


async def test_pending_transaction_is_removed_and_posted_replacement_is_distinct():
    plaid._provider_call.side_effect = [page(
        added=[transaction("posted", pending_transaction_id="pending-old", pending=False)],
        removed=["pending-old"],
    )]
    await plaid.sync_transactions("alex")
    saved = plaid.storage.apply_sync.await_args.kwargs
    assert saved["removed_ids"] == ["pending-old"]
    assert [row["txn_id"] for row in saved["transactions"]] == ["posted"]
    assert saved["transactions"][0]["pending"] is False


async def test_empty_complete_sync_still_advances_cursor_without_fictional_bills():
    plaid._provider_call.side_effect = [page(cursor="new-empty-cursor")]
    result = await plaid.sync_transactions("alex")
    assert result.added == result.modified == result.removed == 0
    assert result.ready
    saved = plaid.storage.apply_sync.await_args.kwargs
    assert saved["transactions"] == [] and saved["removed_ids"] == []
    assert saved["next_cursor"] == "new-empty-cursor"
    assert await plaid.fetch_bills("alex") == []


@pytest.mark.parametrize("bad", [
    transaction(iso_currency_code="EUR"),
    transaction(iso_currency_code=None, unofficial_currency_code="BTC"),
    transaction(amount=float("nan")), transaction(amount=float("inf")),
    transaction(amount=True), transaction(pending="false"),
    transaction(transaction_id=""), transaction(date="not-a-date"),
])
async def test_invalid_final_page_never_persists_earlier_valid_transactions(bad):
    plaid._provider_call.side_effect = [
        page(added=[transaction("valid-first")], cursor="second-page", more=True),
        page(added=[bad]),
    ]
    with pytest.raises(plaid.PlaidError):
        await plaid.sync_transactions("alex")
    plaid.storage.apply_sync.assert_not_awaited()


async def test_provider_failure_after_first_page_keeps_cursor_and_all_rows_unchanged():
    plaid._provider_call.side_effect = [
        page(added=[transaction()], cursor="second-page", more=True),
        plaid.PlaidError("TEMPORARY_UNAVAILABLE", "Plaid is unavailable", request_id="req-failure"),
    ]
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.request_id == "req-failure"
    plaid.storage.apply_sync.assert_not_awaited()


async def test_competing_sync_or_account_change_does_not_report_success():
    plaid._provider_call.side_effect = [page(added=[transaction()])]
    plaid.storage.apply_sync.return_value = False
    with pytest.raises(plaid.PlaidError):
        await plaid.sync_transactions("alex")


async def test_storage_failure_does_not_leak_tokens_or_report_imported_rows(caplog):
    plaid._provider_call.side_effect = [page(added=[transaction()])]
    plaid.storage.apply_sync.side_effect = RuntimeError("access-sandbox-fixture-private SQL parameter exposed")
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "STORAGE_ERROR"
    assert "access-sandbox-fixture-private" not in str(error.value)
    assert "access-sandbox-fixture-private" not in caplog.text


async def test_budget_snapshot_syncs_before_using_database_calculation(monkeypatch):
    plaid._provider_call.side_effect = [page(added=[transaction()])]
    order = []
    async def apply(*args, **kwargs):
        order.append("persist")
        return True
    async def safe(user_id):
        assert user_id == "alex"
        order.append("calculate")
        return 72.50
    monkeypatch.setattr(plaid.storage, "apply_sync", apply)
    monkeypatch.setattr(plaid.repo, "safe_to_spend", safe)
    result = await plaid.budget_snapshot("alex")
    assert order == ["persist", "calculate"]
    assert result.safe_to_spend == 72.50 and result.sync.ready


async def test_budget_snapshot_does_not_claim_zero_spending_before_plaid_is_ready():
    plaid._provider_call.side_effect = [page(status="NOT_READY")]
    with pytest.raises(plaid.PlaidError):
        await plaid.budget_snapshot("alex")
    plaid.repo.safe_to_spend.assert_not_awaited()


async def test_budget_snapshot_requires_budget_configuration(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_budget", AsyncMock(return_value=None))
    with pytest.raises(plaid.PlaidError):
        await plaid.budget_snapshot("alex")
    plaid.repo.safe_to_spend.assert_not_awaited()


async def test_configured_bills_use_stable_owned_ids_and_never_infer_purchases(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_bills", AsyncMock(return_value=[
        {"user_id": "alex", "name": "Rent", "amount": Decimal("850.00"), "due_day": 31},
    ]))
    first = await plaid.fetch_bills("alex")
    second = await plaid.fetch_bills("alex")
    assert len(first) == 1 and first[0].item_id == second[0].item_id
    assert first[0].user_id == "alex" and first[0].kind == "bill"
    assert first[0].cost_usd == 850.00 and first[0].due_at
    plaid._provider_call.assert_not_awaited()
    plaid.storage.initialize_connection.assert_not_awaited()


async def test_mutation_restarts_from_original_cursor_and_discards_partial_attempt():
    plaid._provider_call.side_effect = [
        page(added=[transaction("discarded")], cursor="aborted-page", more=True, request="req-discarded"),
        plaid.PlaidError("SYNC_MUTATION", "History changed", request_id="req-mutation"),
        page(added=[transaction("kept")], cursor="retry-page", more=True, request="req-retry-1"),
        page(modified=[transaction("changed")], cursor="retry-complete", request="req-retry-2"),
    ]
    result = await plaid.sync_transactions("alex")
    assert [call.kwargs["cursor"] for call in plaid._provider_call.await_args_list] == [
        "saved-cursor", "aborted-page", "saved-cursor", "retry-page",
    ]
    assert result.added == result.modified == 1 and result.pages == 2
    assert list(result.request_ids) == ["req-retry-1", "req-retry-2"]
    saved = plaid.storage.apply_sync.await_args.kwargs
    assert {row["txn_id"] for row in saved["transactions"]} == {"kept", "changed"}
    assert saved["next_cursor"] == "retry-complete"


async def test_repeated_mutation_is_bounded_and_never_saves_partial_pages():
    plaid._provider_call.side_effect = plaid.PlaidError("SYNC_MUTATION", "History changed")
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "SYNC_UNSTABLE"
    assert plaid._provider_call.await_count == 3  # original attempt plus two restarts
    plaid.storage.apply_sync.assert_not_awaited()


async def test_coalescing_deltas_respects_provider_page_order():
    plaid._provider_call.side_effect = [
        page(added=[transaction("corrected", amount=10), transaction("removed-later")], removed=["restored"],
             cursor="second", more=True),
        page(added=[transaction("restored", amount=5)], modified=[transaction("corrected", amount=11)],
             removed=["removed-later"]),
    ]
    await plaid.sync_transactions("alex")
    saved = plaid.storage.apply_sync.await_args.kwargs
    rows = {row["txn_id"]: row for row in saved["transactions"]}
    assert set(rows) == {"corrected", "restored"}
    assert rows["corrected"]["amount"] == Decimal("11.00")
    assert saved["removed_ids"] == ["removed-later"]


@pytest.mark.parametrize("cursor", ["saved-cursor", ""])
async def test_nonadvancing_cursor_cannot_loop_or_commit_partial_history(cursor):
    plaid._provider_call.side_effect = [page(added=[transaction()], cursor=cursor, more=True)]
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "INVALID_RESPONSE"
    assert plaid._provider_call.await_count == 1
    plaid.storage.apply_sync.assert_not_awaited()


async def test_page_limit_keeps_saved_cursor_unchanged(monkeypatch):
    monkeypatch.setattr(plaid, "MAX_PAGES", 2)
    plaid._provider_call.side_effect = [page(cursor="next-1", more=True), page(cursor="next-2", more=True)]
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "SYNC_LIMIT"
    assert plaid._provider_call.await_count == 2
    plaid.storage.apply_sync.assert_not_awaited()


async def test_record_limit_keeps_saved_cursor_unchanged(monkeypatch):
    monkeypatch.setattr(plaid, "MAX_RECORDS", 1)
    plaid._provider_call.side_effect = [page(added=[transaction("one"), transaction("two")])]
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "SYNC_LIMIT"
    plaid.storage.apply_sync.assert_not_awaited()


async def test_relink_error_marks_only_the_connection_that_was_read():
    plaid._provider_call.side_effect = plaid.PlaidError("RELINK_REQUIRED", "Reconnect", request_id="req-relink")
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "RELINK_REQUIRED" and error.value.request_id == "req-relink"
    plaid.storage.mark_relink_required.assert_awaited_once_with("alex", expected_secret=connection()["secret"])
    plaid.storage.apply_sync.assert_not_awaited()


async def test_pending_initial_history_does_not_report_budget_ready(monkeypatch):
    monkeypatch.setattr(plaid.repo, "get_connection", AsyncMock(return_value=connection(cursor=None)))
    plaid._provider_call.side_effect = [page(cursor="", status="NOT_READY")]
    result = await plaid.sync_transactions("alex")
    assert not result.ready and result.added == 0
    assert plaid._provider_call.await_args.kwargs["cursor"] is None
    assert plaid.storage.apply_sync.await_args.kwargs["next_cursor"] == ""


async def test_sync_timeout_cannot_persist_partial_results(monkeypatch):
    monkeypatch.setattr(plaid, "SYNC_TIMEOUT", 0.005)
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()
    plaid._provider_call.side_effect = stalled
    with pytest.raises(plaid.PlaidError) as error:
        await plaid.sync_transactions("alex")
    assert error.value.code == "TIMEOUT"
    plaid.storage.apply_sync.assert_not_awaited()


@pytest.mark.parametrize("provider_code,expected", [
    ("TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION", "SYNC_MUTATION"),
    ("ITEM_LOGIN_REQUIRED", "RELINK_REQUIRED"),
    ("INVALID_ACCESS_TOKEN", "RELINK_REQUIRED"),
    ("INVALID_SECRET", "CONFIGURATION_ERROR"),
    ("PRODUCT_NOT_READY", "DATA_NOT_READY"),
    ("INTERNAL_SERVER_ERROR", "PROVIDER_UNAVAILABLE"),
])
async def test_sdk_error_mapping_never_exposes_provider_body(provider_code, expected, caplog):
    from plaid.exceptions import ApiException
    exc = ApiException(status=400, reason="private reason")
    exc.body = json.dumps({
        "error_code": provider_code, "request_id": "req-safe-id",
        "error_message": "private bank error access-sandbox-fixture-private",
        "display_message": "Customer account 123456",
    })
    error = plaid._provider_error(exc)
    assert error.code == expected and error.request_id == "req-safe-id"
    assert "access-sandbox" not in str(error) and "123456" not in str(error)
    assert "private bank" not in caplog.text


async def test_malformed_provider_error_cannot_echo_untrusted_request_id():
    exc = RuntimeError("private exception text")
    exc.body = '{"request_id":"access-token with spaces", "error_message":"private data"}'
    error = plaid._provider_error(exc)
    assert error.request_id is None and error.code == "PROVIDER_UNAVAILABLE"
    assert "private" not in str(error)


@pytest.mark.parametrize("ca_override", [None, "", "/tmp/margin-test-custom-ca.pem"])
async def test_real_sdk_request_models_use_sandbox_dynamic_user_and_saved_cursor(monkeypatch, ca_override):
    import certifi
    import plaid as plaid_sdk
    from plaid.api import plaid_api

    if ca_override is None:
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    else:
        monkeypatch.setenv("SSL_CERT_FILE", ca_override)
    expected_ca = ca_override or certifi.where()
    seen = []
    loop_thread = threading.get_ident()

    def respond(operation, result):
        def call(request, **options):
            seen.append((operation, request.to_dict(), options, threading.get_ident()))
            return SimpleNamespace(to_dict=lambda: result)
        return call

    api = SimpleNamespace(
        sandbox_public_token_create=respond("create", {"public_token": "public-fixture"}),
        item_public_token_exchange=respond("exchange", {"item_id": "item-alex", "access_token": "access-fixture"}),
        transactions_sync=respond("sync", page()),
    )
    hosts = []

    def api_factory(client):
        hosts.append(client.configuration.host)
        assert client.configuration.retries == 0
        # Missing macOS system certificates must use a CA bundle, never disable
        # verification. Deployments may explicitly supply their own trusted CA.
        assert client.configuration.ssl_ca_cert == expected_ca
        assert client.configuration.verify_ssl is True
        return api

    monkeypatch.setattr(plaid_api, "PlaidApi", api_factory)
    await REAL_PROVIDER_CALL("create_sandbox")
    await REAL_PROVIDER_CALL("exchange", public_token="public-fixture")
    await REAL_PROVIDER_CALL("sync", access_token="access-fixture", cursor=None)
    await REAL_PROVIDER_CALL("sync", access_token="access-fixture", cursor="saved-cursor")
    assert hosts == [plaid_sdk.Environment.Sandbox] * 4
    create = seen[0][1]
    assert create["institution_id"] == "ins_109508"
    assert str(create["initial_products"][0]) == "transactions"
    assert create["options"]["override_username"] == "user_transactions_dynamic"
    assert create["options"]["override_password"] == "pass_good"
    assert seen[1][1]["public_token"] == "public-fixture"
    assert "cursor" not in seen[2][1]
    assert seen[3][1]["cursor"] == "saved-cursor"
    assert seen[3][1]["count"] == 500
    assert all(options["_request_timeout"] == (5, 20) for _, _, options, _ in seen)
    assert all(thread != loop_thread for _, _, _, thread in seen)

