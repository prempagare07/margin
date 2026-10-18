from __future__ import annotations

# Plaid sandbox transaction ingestion. Owners: Devin / Alex.

import asyncio
import calendar
import logging
import os
from datetime import datetime, timezone
from uuid import uuid4

from shared.protocol import Item

log = logging.getLogger(__name__)


def _end_of_month_utc() -> str:
    now = datetime.now(timezone.utc)
    last_day = calendar.monthrange(now.year, now.month)[1]
    return now.replace(day=last_day, hour=23, minute=59, second=59, microsecond=0).isoformat()


def _fake_bills(user_id: str) -> list[Item]:
    return [
        Item(
            item_id=str(uuid4()),
            user_id=user_id,
            kind="bill",
            title="Rent – $850",
            due_at=_end_of_month_utc(),
            source="plaid",
            source_id="plaid-rent-fixture",
            cost_usd=850.0,
            status="open",
        )
    ]


async def fetch_bills(user_id: str) -> list[Item]:
    client_id = os.getenv("PLAID_CLIENT_ID", "")
    secret = os.getenv("PLAID_SECRET", "")
    env_name = os.getenv("PLAID_ENV", "sandbox")

    if not client_id or not secret:
        return _fake_bills(user_id)

    try:
        import plaid  # type: ignore
        from plaid.api import plaid_api  # type: ignore
        from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest  # type: ignore
        from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest  # type: ignore
        from plaid.model.transactions_sync_request import TransactionsSyncRequest  # type: ignore

        env_map = {
            "sandbox": plaid.Environment.Sandbox,
            "development": plaid.Environment.Development,
            "production": plaid.Environment.Production,
        }
        plaid_env = env_map.get(env_name, plaid.Environment.Sandbox)

        configuration = plaid.Configuration(
            host=plaid_env,
            api_key={"clientId": client_id, "secret": secret},
        )

        def _sync() -> list[Item]:
            api_client = plaid.ApiClient(configuration)
            pc = plaid_api.PlaidApi(api_client)

            pt_req = SandboxPublicTokenCreateRequest(
                institution_id="ins_109508",
                initial_products=["transactions"],
            )
            pt_resp = pc.sandbox_public_token_create(pt_req)
            public_token = pt_resp["public_token"]

            ex_resp = pc.item_public_token_exchange(
                ItemPublicTokenExchangeRequest(public_token=public_token)
            )
            access_token = ex_resp["access_token"]

            sync_resp = pc.transactions_sync(
                TransactionsSyncRequest(access_token=access_token)
            )
            transactions = sync_resp.get("added", [])

            items: list[Item] = []
            for txn in transactions:
                amount = txn.get("amount", 0)
                if amount > 50:
                    name = txn.get("name", "Bill")
                    txn_id = txn.get("transaction_id", str(uuid4()))
                    date_str = str(txn.get("date", _end_of_month_utc()))
                    items.append(Item(
                        item_id=str(uuid4()),
                        user_id=user_id,
                        kind="bill",
                        title=f"{name} – ${amount:.0f}",
                        due_at=date_str,
                        source="plaid",
                        source_id=str(txn_id),
                        cost_usd=float(amount),
                        status="open",
                    ))
            return items if items else _fake_bills(user_id)

        return await asyncio.get_event_loop().run_in_executor(None, _sync)

    except Exception as exc:
        log.warning("plaid fetch_bills failed: %s", exc)
        return _fake_bills(user_id)
