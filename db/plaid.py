"""Atomic persistence for a user's encrypted Plaid connection and sync cursor.

Fetch every provider page before calling ``apply_sync``. Keeping the imported
rows and cursor in one transaction makes a failed or racing sync safe to retry.
"""

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

from db import repo


_TRANSACTION_FIELDS = {"ts", "txn_id", "amount", "currency", "merchant", "category", "pending"}


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _secret(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Plaid connection secret must be an object")
    for field in ("item_id", "access_token"):
        _identifier(value.get(field), field)
    if value.get("environment") != "sandbox":
        raise ValueError("Only the Plaid sandbox environment is supported")
    if value.get("cursor") is not None and not isinstance(value["cursor"], str):
        raise ValueError("cursor must be a string or null")
    if value.get("last_synced_at") is not None:
        repo._datetime(value["last_synced_at"])
    return value


def _decode(row: dict) -> dict:
    result = dict(row)
    encrypted = result.pop("secret_enc")
    result["secret"] = (
        json.loads(repo._cipher().decrypt(bytes(encrypted)))
        if encrypted is not None else None
    )
    return result


def _matches(row: dict | None, expected: dict) -> bool:
    if row is None or row["status"] != "active":
        return False
    current = _decode(row)["secret"]
    return isinstance(current, dict) and current == expected


async def initialize_connection(
    user_id: str, *, item_id: str, access_token: str, environment: str = "sandbox",
) -> dict:
    """Save a new Item once; return the existing connection if another exists.

    This never creates a user, replaces another Item, or clears a saved cursor.
    Tokens and the cursor are inside the existing Fernet-encrypted secret field.
    """
    _identifier(user_id, "user_id")
    secret = _secret({
        "access_token": access_token, "item_id": item_id,
        "environment": environment, "cursor": None, "last_synced_at": None,
    })
    encrypted = repo._cipher().encrypt(repo._json(secret).encode())
    async with (await repo.get_pool()).connection() as conn:
        await conn.execute(
            """INSERT INTO public.connections (user_id, provider, secret_enc, status)
               SELECT user_id, 'plaid', %s, 'active' FROM public.users WHERE user_id = %s
               ON CONFLICT (user_id, provider) DO NOTHING""", (encrypted, user_id),
        )
        row = await (await conn.execute(
            "SELECT * FROM public.connections WHERE user_id = %s AND provider = 'plaid'",
            (user_id,),
        )).fetchone()
        if row is None:
            raise LookupError("User not found")
        return _decode(row)


def _transaction(raw: dict) -> tuple:
    if not isinstance(raw, dict) or raw.keys() - _TRANSACTION_FIELDS:
        raise ValueError("Invalid normalized Plaid transaction")
    txn_id = _identifier(raw.get("txn_id"), "txn_id")
    ts = repo._datetime(raw.get("ts"))
    if raw.get("currency") != "USD":
        raise ValueError("Only USD transactions are supported")
    if isinstance(raw.get("amount"), bool):
        raise ValueError("Transaction amount must be finite")
    try:
        amount = Decimal(str(raw.get("amount")))
    except InvalidOperation:
        raise ValueError("Transaction amount must be finite") from None
    if not amount.is_finite():
        raise ValueError("Transaction amount must be finite")
    pending = raw.get("pending", False)
    if not isinstance(pending, bool):
        raise ValueError("Transaction pending flag must be a boolean")
    for field in ("merchant", "category"):
        if raw.get(field) is not None and not isinstance(raw[field], str):
            raise ValueError(f"Transaction {field} must be text or null")
    return ts, txn_id, amount, "USD", raw.get("merchant"), raw.get("category"), pending


async def apply_sync(
    user_id: str, *, expected_secret: dict, transactions: list[dict],
    removed_ids: list[str], next_cursor: str, synced_at: datetime,
    transactions_update_status: str | None = None,
) -> bool:
    """Commit one complete sync only if its starting connection is unchanged.

    Return false for a changed/deleted/revoked connection or a competing sync.
    Provider deltas should already be coalesced in page order by the caller.
    Removals win if an ID occurs in both arguments; the retained tombstone is
    excluded from spending. Reintroduced records clear their old tombstones.
    """
    _identifier(user_id, "user_id")
    _secret(expected_secret)
    if not isinstance(next_cursor, str):
        raise ValueError("next_cursor must be a string")
    if transactions_update_status is not None:
        _identifier(transactions_update_status, "transactions_update_status")
    synced_at = repo._datetime(synced_at)
    rows = [_transaction(raw) for raw in transactions]
    removals = [_identifier(value, "removed transaction ID") for value in removed_ids]
    async with (await repo.get_pool()).connection() as conn:
        row = await (await conn.execute(
            """SELECT * FROM public.connections
               WHERE user_id = %s AND provider = 'plaid' FOR UPDATE""", (user_id,),
        )).fetchone()
        if not _matches(row, expected_secret):
            return False
        for transaction in rows:
            # A Timescale primary key includes ts, so an upsert would leave the
            # original row counted when Plaid corrects a transaction's date.
            await conn.execute(
                "DELETE FROM public.transactions WHERE user_id = %s AND txn_id = %s",
                (user_id, transaction[1]),
            )
            await conn.execute(
                """INSERT INTO public.transactions
                   (user_id, ts, txn_id, amount, currency, merchant, category, pending, is_removed)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false)""",
                (user_id, *transaction),
            )
        if removals:
            await conn.execute(
                """UPDATE public.transactions SET is_removed = true
                   WHERE user_id = %s AND txn_id = ANY(%s)""", (user_id, removals),
            )
        secret = _decode(row)["secret"]
        secret.update(cursor=next_cursor, last_synced_at=repo._iso(synced_at))
        if transactions_update_status is not None:
            secret["transactions_update_status"] = transactions_update_status
        encrypted = repo._cipher().encrypt(repo._json(secret).encode())
        await conn.execute(
            """UPDATE public.connections SET secret_enc = %s, updated_at = now()
               WHERE user_id = %s AND provider = 'plaid'""", (encrypted, user_id),
        )
        return True


async def mark_relink_required(user_id: str, *, expected_secret: dict) -> bool:
    """Record expired Item access without invalidating a newer connection/sync."""
    _identifier(user_id, "user_id")
    _secret(expected_secret)
    async with (await repo.get_pool()).connection() as conn:
        row = await (await conn.execute(
            """SELECT * FROM public.connections
               WHERE user_id = %s AND provider = 'plaid' FOR UPDATE""", (user_id,),
        )).fetchone()
        if not _matches(row, expected_secret):
            return False
        await conn.execute(
            """UPDATE public.connections SET status = 'relink_required', updated_at = now()
               WHERE user_id = %s AND provider = 'plaid'""", (user_id,),
        )
        return True
