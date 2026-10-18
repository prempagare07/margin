"""FastAPI app — Photon webhook, Canvas connect, health. Owner: Prem/Alex."""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from standardwebhooks import Webhook, WebhookVerificationError

from core.observability import init as _sentry_init
_sentry_init()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App with scheduler lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    from channels.scheduler import start_scheduler, stop_scheduler
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Margin", docs_url=None, redoc_url=None, lifespan=_lifespan)

_WEBHOOK_SECRET = os.environ.get("PHOTON_WEBHOOK_SECRET", "")

# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "margin"}


# ---------------------------------------------------------------------------
# Photon webhook
# ---------------------------------------------------------------------------

@app.post("/webhooks/photon")
async def photon_webhook(request: Request) -> Response:
    """Receive Photon inbound-message events.

    Verifies Standard Webhooks signature, returns 200 immediately, and
    dispatches to channels.photon.handle_inbound in the background so
    Photon doesn't time out waiting for a reply.
    """
    body = await request.body()

    # Signature verification
    if _WEBHOOK_SECRET:
        wh = Webhook(_WEBHOOK_SECRET)
        headers = {
            "webhook-id":        request.headers.get("webhook-id", ""),
            "webhook-timestamp": request.headers.get("webhook-timestamp", ""),
            "webhook-signature": request.headers.get("webhook-signature", ""),
        }
        try:
            event = wh.verify(body, headers)
        except WebhookVerificationError as exc:
            logger.warning("photon_webhook: signature invalid: %s", exc)
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        import json as _json
        try:
            event = _json.loads(body)
        except Exception:
            raise HTTPException(status_code=400, detail="Bad JSON")

    # Only handle inbound messages
    event_type = event.get("type", "")
    if event_type not in ("message.received", "message.inbound"):
        return Response(status_code=200)

    data = event.get("data") or event.get("payload") or {}
    phone = data.get("from") or data.get("phone") or data.get("sender", "")
    text  = data.get("body") or data.get("text") or data.get("message", "")

    if not phone or not text:
        logger.info("photon_webhook: missing phone or text — ignoring")
        return Response(status_code=200)

    # Dispatch async so we return 200 immediately
    asyncio.create_task(_dispatch(phone, text))
    return Response(status_code=200)


async def _dispatch(phone: str, text: str) -> None:
    try:
        from channels.photon import handle_inbound
        await handle_inbound(phone, text)
    except Exception as exc:
        logger.exception("photon dispatch error for %s: %s", phone, exc)


# ---------------------------------------------------------------------------
# Canvas connect (Alex's pages — stubs kept here until his code lands)
# ---------------------------------------------------------------------------

@app.get("/connect/canvas", response_class=HTMLResponse)
async def canvas_connect_get(token: str = "") -> HTMLResponse:
    return HTMLResponse(
        "<html><body><h2>Connect Canvas</h2>"
        "<p>Canvas connection page — coming soon.</p></body></html>"
    )


@app.post("/connect/canvas")
async def canvas_connect_post(request: Request) -> JSONResponse:
    return JSONResponse({"ok": False, "error": "not_implemented"}, status_code=501)


@app.get("/connected", response_class=HTMLResponse)
async def connected() -> HTMLResponse:
    return HTMLResponse(
        "<html><body><h2>Connected!</h2>"
        "<p>You can close this tab and return to ASI:One.</p></body></html>"
    )
