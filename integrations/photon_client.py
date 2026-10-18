"""Photon HTTP client — send iMessage/SMS. Owner: Prem."""
from __future__ import annotations

import logging
import os
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.photon.codes/v1"
_PROJECT_ID = os.environ.get("PHOTON_PROJECT_ID", "")
_API_KEY = os.environ.get("PHOTON_API_KEY", "")

# 160-char SMS limit; split if longer
_MAX_CHARS = 1500


def _headers(idempotency_key: str | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {_API_KEY}",
        "Content-Type": "application/json",
    }
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


async def send_message(to: str, body: str, idempotency_key: str | None = None) -> dict:
    """Send an iMessage/SMS to a phone number (E.164).

    Truncates gracefully; uses a random idempotency key if none supplied.
    Returns the Photon API response dict.
    """
    if not _PROJECT_ID or not _API_KEY:
        logger.warning("photon_client: PROJECT_ID or API_KEY missing — skipping send")
        return {"ok": False, "error": "not_configured"}

    body = body[:_MAX_CHARS]
    ikey = idempotency_key or str(uuid4())

    url = f"{_BASE}/projects/{_PROJECT_ID}/messages"
    payload = {"to": to, "body": body}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json=payload, headers=_headers(ikey))
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("photon send_message HTTP %s: %s", exc.response.status_code, exc.response.text[:200])
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("photon send_message error: %s", exc)
        return {"ok": False, "error": str(exc)}


async def get_user(phone: str) -> dict | None:
    """Look up a Photon user by E.164 phone number. Returns None if not found."""
    if not _PROJECT_ID or not _API_KEY:
        return None
    url = f"{_BASE}/projects/{_PROJECT_ID}/users"
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(url, params={"phone": phone}, headers=_headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            users = data.get("data") or data.get("users") or []
            return users[0] if users else None
    except Exception as exc:
        logger.warning("photon get_user error: %s", exc)
        return None
