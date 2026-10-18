"""Short-lived, purpose-bound Canvas connection links. Signing keys stay on server."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import urlencode, urlsplit

LIFETIME_SECONDS = 15 * 60


class InvalidState(ValueError):
    pass


def _key() -> bytes:
    value = os.environ.get("STATE_SIGNING_KEY", "")
    if len(value.encode()) < 32:
        raise RuntimeError("STATE_SIGNING_KEY must contain at least 32 bytes")
    return value.encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def make_state(user_id: str) -> str:
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 512:
        raise ValueError("A valid ASI:One user ID is required")
    now = int(time.time())
    body = _encode(json.dumps({
        "user_id": user_id, "purpose": "connect_canvas", "iat": now,
        "exp": now + LIFETIME_SECONDS, "nonce": secrets.token_urlsafe(16),
    }, separators=(",", ":")).encode())
    return body + "." + _encode(hmac.digest(_key(), body.encode(), hashlib.sha256))


def verify_state(token: str) -> str:
    key = _key()
    try:
        if not isinstance(token, str) or len(token) > 2048:
            raise ValueError
        body, signature = token.split(".")
        expected = _encode(hmac.digest(key, body.encode(), hashlib.sha256))
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        now = int(time.time())
        if (data["purpose"] != "connect_canvas" or not isinstance(data["user_id"], str)
                or not data["user_id"].strip() or len(data["user_id"]) > 512
                or type(data["iat"]) is not int or type(data["exp"]) is not int
                or data["iat"] > now + 30 or data["exp"] <= now
                or data["exp"] - data["iat"] != LIFETIME_SECONDS):
            raise ValueError
        return data["user_id"]
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise InvalidState("This connection link is invalid or expired. Request a new one in Margin.") from None


def make_connect_url(user_id: str) -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    u = urlsplit(base)
    if (not u.hostname or u.scheme != "https" or u.username or u.password
            or u.query or u.fragment or u.path):
        raise RuntimeError("PUBLIC_BASE_URL must be your public HTTPS origin")
    return base + "/connect/canvas?" + urlencode({"token": make_state(user_id)})
