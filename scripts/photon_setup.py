#!/usr/bin/env python3
"""Discover and configure Margin's Photon webhook using only the stdlib."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
BASE = "https://api.photon.codes/v1/projects"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def request_json(url: str, api_key: str, method: str = "GET", body=None, key=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        # Cloudflare rejects urllib's default Python-urllib signature (1010).
        "User-Agent": "Margin-Photon-Setup/1.0",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    if key:
        headers["Idempotency-Key"] = key
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            payload = json.load(exc)
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            code = error.get("code", "UNKNOWN") if isinstance(error, dict) else "UNKNOWN"
        except Exception:
            code = "UNKNOWN"
        raise RuntimeError(f"Photon request failed: HTTP {exc.code}, code {code}") from None
    except urllib.error.URLError:
        raise RuntimeError("Photon request failed: network or TLS error") from None


def records(payload, *keys: str) -> list[dict]:
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
            return records(data, *keys)
    return []


def newest_version(payload) -> str:
    candidates = []
    if isinstance(payload, dict):
        raw = payload.get("apiVersions", payload.get("versions", payload.get("data", [])))
    else:
        raw = payload
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            candidates.append(item)
        elif isinstance(item, dict) and item.get("selectable", True) and not item.get("deprecated", False):
            value = item.get("version") or item.get("apiVersion") or item.get("name")
            if value:
                candidates.append(str(value))
    if not candidates:
        raise RuntimeError("Photon returned no selectable webhook API version")
    def version_key(value: str):
        return tuple(int(part) for part in re.findall(r"\d+", value)) or (0,)
    return max(candidates, key=version_key)


def inbound_event(payload) -> tuple[str, list[str]]:
    names = []
    for item in records(payload, "eventTypes", "events"):
        name = item.get("name") or item.get("type") or item.get("eventType")
        if name:
            names.append(str(name))
    if isinstance(payload, list):
        names.extend(str(item) for item in payload if isinstance(item, str))
    matches = [name for name in names if "message" in name.lower() and any(
        token in name.lower() for token in ("received", "inbound")
    )]
    if len(matches) != 1:
        return "", sorted(set(names))
    return matches[0], sorted(set(names))


def write_secret(secret: str) -> None:
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    replacement = f"PHOTON_WEBHOOK_SECRET={secret}"
    changed = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*PHOTON_WEBHOOK_SECRET\s*=", line):
            lines[index] = replacement
            changed = True
            break
    if not changed:
        lines.append(replacement)
    ENV_PATH.write_text("\n".join(lines) + "\n")
    os.chmod(ENV_PATH, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    config = {**read_env(ENV_PATH), **os.environ}
    api_key = config.get("PHOTON_API_KEY", "")
    project = config.get("PHOTON_PROJECT_ID", "")
    public = config.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not api_key or not project or not public:
        raise RuntimeError("PHOTON_API_KEY, PHOTON_PROJECT_ID and PUBLIC_BASE_URL are required")
    webhook_url = public + "/webhooks/photon"
    if not webhook_url.startswith("https://"):
        raise RuntimeError("Photon webhook URL must use HTTPS")
    root = f"{BASE}/{quote(project, safe='')}"
    version = newest_version(request_json(root + "/webhooks/api-versions", api_key))
    events_payload = request_json(
        root + "/webhooks/event-types?apiVersion=" + quote(version, safe=""), api_key
    )
    event_type, names = inbound_event(events_payload)
    if not event_type:
        print("Available event types:", file=sys.stderr)
        for name in names:
            print(f"- {name}", file=sys.stderr)
        raise RuntimeError("Could not identify exactly one inbound-message event type")
    schema = request_json(
        root + "/webhooks/event-types/" + quote(event_type, safe="")
        + "/schema?apiVersion=" + quote(version, safe=""), api_key
    )
    destinations_payload = request_json(root + "/webhooks/destinations", api_key)
    existing = next((item for item in records(destinations_payload, "destinations")
                     if item.get("url") == webhook_url), None)
    destination = existing
    status = "dry-run"
    if not args.dry_run:
        docs = ROOT / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "photon_message_received.json").write_text(json.dumps(schema, indent=2) + "\n")
        if existing:
            if not args.rotate:
                raise RuntimeError("A destination already exists for this URL; rerun with --rotate")
            destination_id = existing.get("id") or existing.get("destinationId")
            if not destination_id:
                raise RuntimeError("Existing Photon destination has no ID")
            result = request_json(
                root + "/webhooks/destinations/" + quote(str(destination_id), safe="")
                + "/secret-rotations", api_key, "POST", {},
                "margin-photon-rotate-" + hashlib.sha256(str(destination_id).encode()).hexdigest(),
            )
            status = "rotated"
        else:
            key = "margin-photon-" + hashlib.sha256(
                f"{project}:{webhook_url}:{version}".encode()
            ).hexdigest()
            result = request_json(
                root + "/webhooks/destinations", api_key, "POST",
                {"name": "Margin inbound messages", "url": webhook_url,
                 "apiVersion": version, "enabledEvents": [event_type]}, key,
            )
            destination = result.get("destination", result)
            status = "created"
        secret = result.get("signingSecret") if isinstance(result, dict) else None
        if not secret:
            raise RuntimeError("Photon did not return a signing secret")
        write_secret(str(secret))
    destination_id = (destination or {}).get("id") or (destination or {}).get("destinationId") or "not-created"
    print(f"destination_id={destination_id}")
    print(f"status={status}")
    print(f"api_version={version}")
    print(f"event_type={event_type}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
