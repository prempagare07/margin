"""Sandbox-only Plaid ingestion into Tiger Data, with explicit demo provisioning.

Transactions are spending history, not future bills. Provider I/O happens off the
agent loop and outside DB transactions; only a complete page sequence is applied.
"""
from __future__ import annotations

import asyncio
import calendar
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from db import plaid as storage, repo
from shared.protocol import Item

PHOENIX = ZoneInfo("America/Phoenix")
MAX_PAGES = 100
MAX_RECORDS = 50_000
MAX_RESTARTS = 2
SYNC_TIMEOUT = 90
_WORKERS = ThreadPoolExecutor(max_workers=4, thread_name_prefix="plaid")
_STATUSES = {"NOT_READY", "INITIAL_UPDATE_COMPLETE", "HISTORICAL_UPDATE_COMPLETE",
             "TRANSACTIONS_UPDATE_STATUS_UNKNOWN"}


class PlaidError(Exception):
    """Safe public error: never retain raw provider exception text or credentials."""

    def __init__(self, code: str, message: str, request_id: str | None = None):
        self.code = code
        self.request_id = request_id
        super().__init__(message)


@dataclass(frozen=True)
class ConnectionInfo:
    item_id: str


@dataclass(frozen=True)
class SyncResult:
    added: int
    modified: int
    removed: int
    pages: int
    request_ids: tuple[str, ...]
    transactions_update_status: str
    last_synced_at: datetime

    @property
    def ready(self) -> bool:
        return self.transactions_update_status == "HISTORICAL_UPDATE_COMPLETE"


@dataclass(frozen=True)
class BudgetSnapshot:
    safe_to_spend: float
    sync: SyncResult


def _sandbox_only() -> None:
    if os.getenv("PLAID_ENV", "sandbox") != "sandbox":
        raise PlaidError("CONFIGURATION_ERROR", "This integration supports Plaid Sandbox only.")


def _request_id(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def _provider_error(exc: Exception) -> PlaidError:
    # ApiException.__str__ includes the entire response body. Never log or return it.
    try:
        body = json.loads(getattr(exc, "body", "") or "{}")
        code = body.get("error_code") if isinstance(body, dict) else None
        request_id = _request_id(body.get("request_id")) if isinstance(body, dict) else None
    except (TypeError, ValueError):
        code, request_id = None, None
    if code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
        return PlaidError("SYNC_MUTATION", "Transaction history changed during pagination.", request_id)
    if code in {"ITEM_LOGIN_REQUIRED", "INVALID_ACCESS_TOKEN", "ITEM_NOT_FOUND", "ITEM_NOT_SUPPORTED"}:
        return PlaidError("RELINK_REQUIRED", "The Plaid connection needs to be reconnected.", request_id)
    if code == "PRODUCT_NOT_READY":
        return PlaidError("DATA_NOT_READY", "Plaid is still preparing transaction history. Try again shortly.", request_id)
    if code in {"INVALID_CLIENT_ID", "INVALID_SECRET", "INVALID_API_KEYS"}:
        return PlaidError("CONFIGURATION_ERROR", "Check the Plaid Sandbox credentials on the server.", request_id)
    return PlaidError("PROVIDER_UNAVAILABLE", "Plaid is unavailable; spending has not been refreshed.", request_id)


def _call_sync(operation: str, kwargs: dict) -> dict:
    import certifi
    import plaid
    from plaid.api import plaid_api
    from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
    from plaid.model.products import Products
    from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
    from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions
    from plaid.model.transactions_sync_request import TransactionsSyncRequest

    _sandbox_only()
    client_id, secret = os.getenv("PLAID_CLIENT_ID"), os.getenv("PLAID_SECRET")
    if not client_id or not secret:
        raise PlaidError("CONFIGURATION_ERROR", "PLAID_CLIENT_ID and PLAID_SECRET are required.")
    configuration = plaid.Configuration(
        host=plaid.Environment.Sandbox, api_key={"clientId": client_id, "secret": secret},
        # Python.org macOS installs may have no system CA file. Keep TLS
        # verification enabled using Mozilla's bundle (or an explicit CA file).
        ssl_ca_cert=os.getenv("SSL_CERT_FILE") or certifi.where(),
    )
    configuration.retries = 0
    try:
        with plaid.ApiClient(configuration) as client:
            api = plaid_api.PlaidApi(client)
            if operation == "create_sandbox":
                response = api.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
                    institution_id="ins_109508", initial_products=[Products("transactions")],
                    options=SandboxPublicTokenCreateRequestOptions(
                        override_username="user_transactions_dynamic", override_password="pass_good",
                    ),
                ), _request_timeout=(5, 20))
            elif operation == "exchange":
                response = api.item_public_token_exchange(ItemPublicTokenExchangeRequest(
                    public_token=kwargs["public_token"],
                ), _request_timeout=(5, 20))
            elif operation == "sync":
                args = {"access_token": kwargs["access_token"], "count": 500}
                if kwargs["cursor"] is not None:
                    args["cursor"] = kwargs["cursor"]
                response = api.transactions_sync(TransactionsSyncRequest(**args), _request_timeout=(5, 20))
            else:
                raise ValueError("Unknown Plaid operation")
            return response.to_dict()
    except PlaidError:
        raise
    except Exception as exc:
        raise _provider_error(exc) from None


async def _provider_call(operation: str, **kwargs) -> dict:
    # Bounded workers prevent slow HTTP requests from exhausting agent threads.
    return await asyncio.get_running_loop().run_in_executor(_WORKERS, _call_sync, operation, kwargs)


def _secret(connection: dict | None) -> dict:
    if connection is None:
        raise PlaidError("NOT_CONNECTED", "Connect the demo Plaid Sandbox account before syncing transactions.")
    if connection.get("status") != "active":
        raise PlaidError("RELINK_REQUIRED", "The Plaid connection needs to be reconnected.")
    secret = connection.get("secret")
    if (not isinstance(secret, dict) or secret.get("environment") != "sandbox"
            or not isinstance(secret.get("access_token"), str) or not secret["access_token"]
            or not isinstance(secret.get("item_id"), str) or not secret["item_id"]
            or (secret.get("cursor") is not None and not isinstance(secret["cursor"], str))):
        raise PlaidError("CONNECTION_INVALID", "The saved Plaid Sandbox connection is incomplete.")
    return secret


async def connect_sandbox(user_id: str) -> ConnectionInfo:
    """Explicitly create a demo Item once; never replace a user's existing Item."""
    _sandbox_only()
    try:
        if not await repo.get_user(user_id):
            raise PlaidError("USER_NOT_FOUND", "Use an existing Margin user ID to connect Plaid.")
        existing = await repo.get_connection(user_id, "plaid")
        if existing is not None:
            return ConnectionInfo(_secret(existing)["item_id"])
        # Verify encryption before creating a remote Item we cannot safely store.
        repo._cipher()
        async with asyncio.timeout(SYNC_TIMEOUT):
            public = await _provider_call("create_sandbox")
            token = public.get("public_token")
            if not isinstance(token, str) or not token:
                raise PlaidError("INVALID_RESPONSE", "Plaid did not return a Sandbox token.")
            exchanged = await _provider_call("exchange", public_token=token)
            if any(not isinstance(exchanged.get(k), str) or not exchanged[k]
                   for k in ("access_token", "item_id")):
                raise PlaidError("INVALID_RESPONSE", "Plaid did not return a complete connection.")
            saved = await storage.initialize_connection(
                user_id, item_id=exchanged["item_id"], access_token=exchanged["access_token"],
                environment="sandbox",
            )
            return ConnectionInfo(_secret(saved)["item_id"])
    except PlaidError:
        raise
    except TimeoutError:
        raise PlaidError("TIMEOUT", "Plaid setup timed out. Check the saved connection before trying again.") from None
    except Exception:
        raise PlaidError("STORAGE_ERROR", "The Plaid connection could not be loaded or saved.") from None


def _id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise PlaidError("INVALID_RESPONSE", "Plaid returned an invalid transaction ID.")
    return value


def _normalize_transaction(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise PlaidError("INVALID_RESPONSE", "Plaid returned an invalid transaction.")
    txn_id = _id(raw.get("transaction_id"))
    if raw.get("iso_currency_code") != "USD" or raw.get("unofficial_currency_code"):
        raise PlaidError("UNSUPPORTED_CURRENCY", "Only USD transactions are supported; this sync was not saved.")
    try:
        if isinstance(raw.get("amount"), bool):
            raise ValueError()
        amount = Decimal(str(raw["amount"]))
        if not amount.is_finite() or abs(amount) > Decimal("9999999999.99"):
            raise ValueError()
        amount = amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        instant = raw.get("datetime")
        if instant is not None:
            ts = repo._datetime(instant)
        else:
            day = raw["date"]
            if isinstance(day, str):
                day = date.fromisoformat(day)
            if not isinstance(day, date) or isinstance(day, datetime):
                raise ValueError()
            ts = datetime.combine(day, time(12), PHOENIX).astimezone(timezone.utc)
        pending = raw["pending"]
        if not isinstance(pending, bool):
            raise ValueError()
        pfc = raw.get("personal_finance_category")
        category = pfc.get("primary") if isinstance(pfc, dict) else None
        merchant = raw.get("merchant_name") or raw.get("name")
        if any(value is not None and not isinstance(value, str) for value in (category, merchant)):
            raise ValueError()
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise PlaidError("INVALID_RESPONSE", "Plaid returned invalid transaction fields; this sync was not saved.") from None
    return dict(txn_id=txn_id, ts=ts, amount=amount, currency="USD", merchant=merchant,
                category=category, pending=pending)


async def _sync_pages(secret: dict) -> tuple[dict, list[str], int, int, int, int, tuple[str, ...], str, str]:
    original_cursor = secret.get("cursor")
    for attempt in range(MAX_RESTARTS + 1):
        cursor = original_cursor
        changes: dict[str, dict | None] = {}
        added = modified = removed = records = 0
        request_ids: list[str] = []
        seen_cursors = {cursor}
        try:
            for page in range(1, MAX_PAGES + 1):
                response = await _provider_call("sync", access_token=secret["access_token"], cursor=cursor)
                if not isinstance(response, dict):
                    raise PlaidError("INVALID_RESPONSE", "Plaid returned an invalid sync response.")
                batches = [response.get(key) for key in ("added", "modified", "removed")]
                next_cursor, has_more = response.get("next_cursor"), response.get("has_more")
                status = response.get("transactions_update_status", "TRANSACTIONS_UPDATE_STATUS_UNKNOWN")
                if (any(not isinstance(batch, list) for batch in batches)
                        or not isinstance(next_cursor, str) or not isinstance(has_more, bool)
                        or not isinstance(status, str) or status not in _STATUSES):
                    raise PlaidError("INVALID_RESPONSE", "Plaid returned invalid sync pagination fields.")
                records += sum(len(batch) for batch in batches)
                if records > MAX_RECORDS:
                    raise PlaidError("SYNC_LIMIT", "Plaid returned too much history; no partial sync was saved.")
                for batch in batches[:2]:
                    for raw in batch:
                        txn = _normalize_transaction(raw)
                        changes[txn["txn_id"]] = txn
                for raw in batches[2]:
                    if not isinstance(raw, dict):
                        raise PlaidError("INVALID_RESPONSE", "Plaid returned an invalid removal.")
                    changes[_id(raw.get("transaction_id"))] = None
                added += len(batches[0])
                modified += len(batches[1])
                removed += len(batches[2])
                request_id = _request_id(response.get("request_id"))
                if request_id:
                    request_ids.append(request_id)
                if not has_more:
                    return (changes, [key for key, value in changes.items() if value is None],
                            added, modified, removed, page, tuple(request_ids), next_cursor, status)
                if not next_cursor or next_cursor in seen_cursors:
                    raise PlaidError("INVALID_RESPONSE", "Plaid pagination did not advance; no partial sync was saved.")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            raise PlaidError("SYNC_LIMIT", "Plaid exceeded the page limit; no partial sync was saved.")
        except PlaidError as exc:
            if exc.code != "SYNC_MUTATION":
                raise
            if attempt == MAX_RESTARTS:
                raise PlaidError("SYNC_UNSTABLE", "Plaid history kept changing. Try syncing again shortly.", exc.request_id) from None
    raise AssertionError("Unreachable")


async def sync_transactions(user_id: str) -> SyncResult:
    """Read updates for the saved Item and atomically apply rows plus cursor.

    Never provisions a new Item. On any page failure, the saved cursor/rows remain
    unchanged. A concurrent sync or connection replacement wins over this batch.
    """
    _sandbox_only()
    secret = None
    try:
        async with asyncio.timeout(SYNC_TIMEOUT):
            secret = _secret(await repo.get_connection(user_id, "plaid"))
            changes, removed_ids, added, modified, removed, pages, request_ids, cursor, status = await _sync_pages(secret)
            synced_at = datetime.now(timezone.utc)
            committed = await storage.apply_sync(
                user_id, expected_secret=secret,
                transactions=[row for row in changes.values() if row is not None],
                removed_ids=removed_ids, next_cursor=cursor, synced_at=synced_at,
                transactions_update_status=status,
            )
            if not committed:
                raise PlaidError("SYNC_CONFLICT", "The saved Plaid connection changed during sync. Try again.")
            return SyncResult(added, modified, removed, pages, request_ids, status, synced_at)
    except PlaidError as exc:
        if exc.code == "RELINK_REQUIRED" and secret is not None:
            try:
                await storage.mark_relink_required(user_id, expected_secret=secret)
            except Exception:
                pass  # Preserve the honest provider failure even if status storage is unavailable.
        raise
    except TimeoutError:
        raise PlaidError("TIMEOUT", "Plaid sync timed out. Try again; the saved cursor will be reused.") from None
    except Exception:
        raise PlaidError("STORAGE_ERROR", "Transaction storage is unavailable; spending could not be refreshed.") from None


async def budget_snapshot(user_id: str) -> BudgetSnapshot:
    """Only quote a spending allowance after configured budget + ready sync."""
    try:
        if await repo.get_budget(user_id) is None:
            raise PlaidError("BUDGET_NOT_CONFIGURED", "Set your income, savings goal, and bills before checking your budget.")
        result = await sync_transactions(user_id)
        if not result.ready:
            raise PlaidError("DATA_NOT_READY", "Plaid is still preparing transaction history. Try again shortly.")
        return BudgetSnapshot(await repo.safe_to_spend(user_id), result)
    except PlaidError:
        raise
    except Exception:
        raise PlaidError("STORAGE_ERROR", "Budget storage is unavailable. Try again shortly.") from None


async def fetch_bills(user_id: str) -> list[Item]:
    """Compatibility Items for user-configured bills; no provider calls/inference.

    The existing Item source enum groups finance under 'plaid'; the source_id and
    quote explicitly distinguish these manual bills from imported transactions.
    """
    try:
        bills = await repo.get_bills(user_id)
        today = datetime.now(PHOENIX).date()
        items = []
        for bill in bills:
            year, month = today.year, today.month
            day = min(bill["due_day"], calendar.monthrange(year, month)[1])
            due = date(year, month, day)
            if due < today:
                year, month = (year + 1, 1) if month == 12 else (year, month + 1)
                due = date(year, month, min(bill["due_day"], calendar.monthrange(year, month)[1]))
            identity = json.dumps([user_id, bill["name"], due.isoformat()], separators=(",", ":"))
            key = str(uuid5(NAMESPACE_URL, "margin:configured-bill:" + identity))
            items.append(Item(
                item_id=key, user_id=user_id, kind="bill", title=bill["name"],
                due_at=datetime.combine(due, time(12), PHOENIX).isoformat(),
                source="plaid", source_id="configured-bill:" + key,
                cost_usd=float(bill["amount"]), status="open",
                quote="User-configured recurring bill; not inferred from a bank transaction.",
            ))
        return items
    except Exception:
        raise PlaidError("STORAGE_ERROR", "Saved bills are unavailable. Try again shortly.") from None
