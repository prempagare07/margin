"""Read-only provider proof through Margin's facade, with LOCAL connection storage.

No drafts, events, channel posts, OAuth links, or auth configs are created.
This script never prints message text, addresses, credentials, or SDK objects.
Run from the repository: python -m scripts.composio_smoke --help
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import os

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

from db import repo
from integrations import composio_tools as api
from integrations.composio import runtime
from integrations.composio.models import iso


async def main(args):
    if args.list_users:
        users = {}
        for provider in runtime.TOOLKITS:
            toolkit, auth = runtime.config(provider)
            def discover(client):
                response = client.connected_accounts.list(toolkit_slugs=[toolkit], auth_config_ids=[auth],
                    statuses=["ACTIVE"], account_type="PRIVATE", limit=100)
                return [(a.user_id, a.status) for a in response.items], response.next_cursor
            rows, cursor = await runtime.sdk_call(discover, provider=provider, operation="demo_identity")
            for user_id, status in rows:
                if user_id and status == "ACTIVE":
                    users.setdefault(user_id, []).append(provider)
            if cursor:
                raise api.IntegrationError("PARTIAL_RESULT", provider)
        print(json.dumps({"existing_user_ids": {key: sorted(set(value)) for key, value in users.items()}}))
        return 0
    info = conninfo_to_dict(args.local_test_db)
    if (info.get("host") not in {"localhost", "127.0.0.1", "::1"}
            or info.get("dbname") != "margin_test" or "hostaddr" in info or "service" in info):
        raise ValueError("Smoke test storage must be a localhost database named margin_test")
    os.environ["DATABASE_URL"] = args.local_test_db
    selected = list(runtime.TOOLKITS) if args.provider == "all" else [args.provider]
    failed = False
    try:
        for provider in selected:
            try:
                state = await api.connection_status(args.user_id, provider)
                if state.status != "active":
                    print(json.dumps({"provider": provider, "connection": state.status}))
                    failed = True
                    continue
                if provider == "gmail":
                    now = datetime.now(timezone.utc)
                    batch = await api.fetch_gmail_messages(args.user_id, since=iso(now - timedelta(days=7)), until=iso(now))
                    operation = "GMAIL_FETCH_EMAILS"
                elif provider == "gcal":
                    batch = await api.list_calendars(args.user_id)
                    operation = "GOOGLECALENDAR_LIST_CALENDARS"
                else:
                    batch = await api.list_slack_channels(args.user_id)
                    operation = "SLACK_TEST_AUTH + SLACK_LIST_ALL_CHANNELS"
                print(json.dumps({"provider": provider, "operation": operation, "records": len(batch.records),
                    "complete": batch.complete, "issues": [issue.code for issue in batch.issues], "log_ids": batch.log_ids}))
                failed |= not batch.complete or not batch.log_ids
            except api.IntegrationError as error:
                print(json.dumps({"provider": provider, "error": error.code, "log_id": error.log_id}))
                failed = True
    finally:
        await repo.close_pool()
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--user-id", help="Exact EXISTING Composio user ID; never remap a demo account to a student")
    identity.add_argument("--list-users", action="store_true", help="List safe identity metadata for configured auth configs")
    parser.add_argument("--provider", choices=["all", "gmail", "gcal", "slack"], default="all")
    parser.add_argument("--local-test-db", default="postgresql://margin_test:margin_test@127.0.0.1:55432/margin_test",
                        help="Existing disposable local DB with Margin schema; never Tiger Data")
    args = parser.parse_args()
    load_dotenv(".env")
    logging.basicConfig(level=logging.WARNING)
    for name in ("httpx", "httpcore", "composio", "composio_client"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        raise SystemExit(asyncio.run(main(args)))
    except api.IntegrationError as error:
        print(json.dumps({"error": error.code, "provider": error.provider}))
        raise SystemExit(1)
    except Exception as error:
        # Exception strings/tracebacks can contain secrets or response bodies.
        print(json.dumps({"error": "SMOKE_SETUP_FAILED", "exception_type": type(error).__name__}))
        raise SystemExit(1)
