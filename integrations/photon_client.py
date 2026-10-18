"""Small async client for the Photon project API. Owner: Prem."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.photon.codes/v1"
_MAX_CHARS = 1500
_RETRYABLE = {408, 425, 429, 500, 502, 503, 504}


def _config() -> tuple[str, str]:
    return os.environ.get("PHOTON_PROJECT_ID", ""), os.environ.get("PHOTON_API_KEY", "")


def _headers(api_key: str, idempotency_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _error_code(response: httpx.Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        return str(error.get("code", "UNKNOWN")) if isinstance(error, dict) else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


async def _request(
    method: str,
    path: str,
    *,
    idempotency_key: str | None = None,
    json: dict[str, Any] | None = None,
    retries: int = 2,
) -> httpx.Response:
    project_id, api_key = _config()
    if not project_id or not api_key:
        raise RuntimeError("Photon is not configured")
    url = f"{_BASE}/projects/{project_id}{path}"
    attempts = retries + 1 if method == "GET" or idempotency_key else 1
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
        for attempt in range(attempts):
            try:
                response = await client.request(
                    method, url, json=json, headers=_headers(api_key, idempotency_key)
                )
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt + 1 >= attempts:
                    logger.warning("photon request failed status=network_error")
                    raise
                await asyncio.sleep(0.2 * (2**attempt))
                continue
            if response.status_code not in _RETRYABLE or attempt + 1 >= attempts:
                return response
            await asyncio.sleep(0.2 * (2**attempt))
    raise RuntimeError("Photon request failed")


def _items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return _items(data, *keys)
    return []


async def send_message(
    space_id: str,
    sender_id: str,
    text: str,
    *,
    idempotency_key: str,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    """Send one plain-text message into an existing Photon conversation."""
    if not space_id or not sender_id or not idempotency_key:
        raise ValueError("space_id, sender_id and idempotency_key are required")
    payload: dict[str, Any] = {
        "spaceId": space_id,
        "senderId": sender_id,
        "content": {"type": "text", "format": "plain", "text": text[:_MAX_CHARS]},
    }
    if parent_message_id:
        payload["parentMessageId"] = parent_message_id
    response = await _request("POST", "/messages", json=payload, idempotency_key=idempotency_key)
    if response.is_error:
        logger.warning("photon send failed status=%s code=%s", response.status_code, _error_code(response))
        response.raise_for_status()
    result = response.json()
    return result if isinstance(result, dict) else {"data": result}


async def ensure_line_assignment(phone: str) -> str:
    """Assign a shared iMessage line to ``phone`` and return that line."""
    key = "margin-line-" + hashlib.sha256(phone.encode()).hexdigest()
    response = await _request(
        "POST", "/platforms/imessage/assignments", json={"handle": phone}, idempotency_key=key
    )
    if response.is_success:
        payload = response.json()
        assignment = payload.get("assignment", payload) if isinstance(payload, dict) else {}
        number = assignment.get("assignedPhoneNumber") if isinstance(assignment, dict) else None
        if isinstance(number, str) and number:
            return number
        raise RuntimeError("Photon assignment response was incomplete")
    code = _error_code(response)
    if response.status_code in {400, 409, 422}:
        listed = await _request("GET", "/platforms/imessage/assignments")
        if listed.is_success:
            for assignment in _items(listed.json(), "assignments", "items"):
                if assignment.get("handle") == phone and assignment.get("assignedPhoneNumber"):
                    return str(assignment["assignedPhoneNumber"])
    logger.warning("photon assignment failed status=%s code=%s", response.status_code, code)
    response.raise_for_status()
    raise RuntimeError("Photon assignment failed")
