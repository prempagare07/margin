"""Smoke test: send into an already-linked Photon conversation. Owner: Prem."""
import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True, help="linked Margin user ID")
    args = parser.parse_args()
    from channels.photon import send_text

    print("Sending Photon smoke test to the saved conversation ...")
    result = await send_text(
        args.user_id,
        "Margin smoke test ✓ — if you got this, Photon is wired up.",
        idempotency_key="margin-manual-smoke-v2",
    )
    if result and result.get("ok") is not False:
        print("PASS")
    else:
        print("FAIL")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
