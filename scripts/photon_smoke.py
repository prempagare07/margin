"""Smoke test: send a test SMS via Photon and verify delivery. Owner: Prem."""
import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()


async def main() -> None:
    phone = os.environ.get("PHOTON_TEST_PHONES", "").split(",")[0].strip()
    if not phone:
        print("Set PHOTON_TEST_PHONES in .env")
        sys.exit(1)

    from integrations.photon_client import send_message

    print(f"Sending smoke test SMS to {phone} ...")
    result = await send_message(phone, "Margin smoke test ✓ — if you got this, Photon is wired up.")
    print(f"Result: {result}")
    if result and result.get("ok") is not False:
        print("PASS")
    else:
        print("FAIL")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
