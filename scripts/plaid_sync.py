"""Connect a demo Plaid Sandbox Item, or sync its transactions into Margin.

Uses DATABASE_URL from .env unless --local-test-db is explicitly supplied.
Never prints credentials, cursors, account details, or transaction descriptions.
"""
import argparse
import asyncio
import json
import logging
import os

from dotenv import load_dotenv
from psycopg.conninfo import conninfo_to_dict

from db import repo
from integrations.plaid_sandbox import PlaidError, connect_sandbox, sync_transactions


async def main(args) -> int:
    if args.local_test_db:
        info = conninfo_to_dict(args.local_test_db)
        if (info.get("host") not in {"localhost", "127.0.0.1", "::1"}
                or info.get("dbname") != "margin_test" or "hostaddr" in info or "service" in info):
            raise ValueError("Local test storage must be a localhost database named margin_test")
        os.environ["DATABASE_URL"] = args.local_test_db
    if args.create_demo_user and (not args.local_test_db or not args.user_id.startswith("plaid-demo-")):
        raise ValueError("Creating demo users requires local test storage and a plaid-demo- user ID")
    try:
        if args.create_demo_user:
            await repo.upsert_user(args.user_id)
        if args.connect:
            await connect_sandbox(args.user_id)
        result = await sync_transactions(args.user_id)
        output = dict(
            environment="sandbox", storage="local_test" if args.local_test_db else "DATABASE_URL",
            added=result.added, modified=result.modified, removed=result.removed,
            pages=result.pages, ready=result.ready,
            transactions_update_status=result.transactions_update_status,
            synced_at=result.last_synced_at.isoformat(), request_ids=result.request_ids,
        )
        if args.budget:
            if not result.ready:
                output["budget_error"] = "DATA_NOT_READY"
            elif await repo.get_budget(args.user_id) is None:
                output["budget_error"] = "BUDGET_NOT_CONFIGURED"
            else:
                output["safe_to_spend"] = await repo.safe_to_spend(args.user_id)
        print(json.dumps(output))
        return 0 if result.ready else 2
    finally:
        await repo.close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Exact existing Margin user ID (ASI sender address)")
    parser.add_argument("--connect", action="store_true", help="Create a Sandbox Item if no connection exists")
    parser.add_argument("--budget", action="store_true", help="Also show the configured budget after a ready sync")
    parser.add_argument("--local-test-db", help="Override storage with an existing local margin_test database")
    parser.add_argument("--create-demo-user", action="store_true", help="Local testing only: create a plaid-demo- user")
    args = parser.parse_args()
    load_dotenv(".env")
    logging.basicConfig(level=logging.CRITICAL)
    try:
        raise SystemExit(asyncio.run(main(args)))
    except PlaidError as error:
        print(json.dumps({"error": error.code, "message": str(error), "request_id": error.request_id}))
        raise SystemExit(1)
    except Exception:
        # SDK, encryption, and DB exceptions may contain secret or personal data.
        print(json.dumps({"error": "SETUP_FAILED", "message": "Check database, encryption key, and command arguments."}))
        raise SystemExit(1)
