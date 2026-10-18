"""FastAPI app — Photon webhook, Canvas connect, health. Owner: Prem/Alex."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import os

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from standardwebhooks import Webhook, WebhookVerificationError

from core.observability import init as _sentry_init
from web.canvas_connect import router as canvas_router
from web.privacy import install_access_filter
install_access_filter()
_sentry_init()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App with scheduler lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def _lifespan(app: FastAPI):
    from channels.scheduler import start_scheduler, stop_scheduler
    from db.repo import close_pool
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        await close_pool()


app = FastAPI(title="Margin", docs_url=None, redoc_url=None, lifespan=_lifespan)
app.include_router(canvas_router)

_PROCESSED_PHOTON_MESSAGES: OrderedDict[str, None] = OrderedDict()
_PHOTON_DEDUPE_LIMIT = 2048

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

    # Sender phone possession is trusted only after signature verification.
    webhook_secret = os.environ.get("PHOTON_WEBHOOK_SECRET", "")
    if webhook_secret:
        try:
            wh = Webhook(webhook_secret)
        except (ValueError, TypeError):
            raise HTTPException(status_code=503, detail="Photon webhook verification is not configured") from None
        headers = {
            "webhook-id":        request.headers.get("webhook-id", ""),
            "webhook-timestamp": request.headers.get("webhook-timestamp", ""),
            "webhook-signature": request.headers.get("webhook-signature", ""),
        }
        try:
            event = wh.verify(body, headers)
        except WebhookVerificationError:
            arrived = sorted(name for name in headers if request.headers.get(name) is not None)
            logger.warning("photon_webhook: invalid signature headers=%s", ",".join(arrived))
            raise HTTPException(status_code=401, detail="Invalid signature")
        except ValueError:
            raise HTTPException(status_code=400, detail="Bad JSON") from None
    else:
        raise HTTPException(status_code=503, detail="Photon webhook verification is not configured")

    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Bad event")

    if event.get("type") != "message.received":
        return Response(status_code=200)

    data = event.get("data")
    message = data.get("object") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return Response(status_code=200)
    content = message.get("content")
    sent_by = message.get("sentBy")
    space = message.get("space")
    if not all(isinstance(value, dict) for value in (content, sent_by, space)):
        return Response(status_code=200)
    if content.get("type") != "text" or sent_by.get("kind") == "agent":
        return Response(status_code=200)

    message_id = message.get("id")
    sender_handle = sent_by.get("handle")
    sender_id = sent_by.get("id")
    text = content.get("text")
    conversation_id = space.get("id")
    addressed_to = message.get("addressedTo")
    agent_user_id = addressed_to.get("id") if isinstance(addressed_to, dict) else None
    if not agent_user_id:
        participants = space.get("participantIds")
        if isinstance(participants, list):
            agent_user_id = next(
                (item for item in participants if isinstance(item, str) and item != sender_id), None
            )
    if not all(isinstance(value, str) and value for value in (
        message_id, sender_handle, text, conversation_id, agent_user_id,
    )):
        return Response(status_code=200)
    if message_id in _PROCESSED_PHOTON_MESSAGES:
        return Response(status_code=200)
    _PROCESSED_PHOTON_MESSAGES[message_id] = None
    while len(_PROCESSED_PHOTON_MESSAGES) > _PHOTON_DEDUPE_LIMIT:
        _PROCESSED_PHOTON_MESSAGES.popitem(last=False)

    # Dispatch async so we return 200 immediately
    asyncio.create_task(_dispatch(
        sender_handle, text, message_id, conversation_id, agent_user_id,
        verified_webhook=True,
    ))
    return Response(status_code=200)


async def _dispatch(
    sender_handle: str,
    text: str,
    message_id: str,
    conversation_id: str,
    agent_user_id: str,
    *,
    verified_webhook: bool = False,
) -> None:
    try:
        from channels.photon import handle_inbound
        await handle_inbound(
            sender_handle, text, message_id, conversation_id, agent_user_id,
            verified_webhook=verified_webhook,
        )
    except Exception:
        logger.warning("photon dispatch failed")


# ---------------------------------------------------------------------------
# Stripe payment complete redirect
# ---------------------------------------------------------------------------

@app.get("/payments/complete", response_class=HTMLResponse)
async def payments_complete(
    session_id: str = "",
    user: str = "",
    tier: str = "",
) -> HTMLResponse:
    from payments.stripe_flow import TIERS
    tier_name = TIERS.get(tier, {}).get("name", "Margin Pro")
    return HTMLResponse(
        f"<html><body style='font-family:sans-serif;max-width:480px;margin:60px auto;text-align:center'>"
        f"<h2>Payment received!</h2>"
        f"<p><strong>{tier_name}</strong> is now being activated.</p>"
        f"<p>Return to ASI:One — Margin will confirm there in a moment.</p>"
        f"</body></html>"
    )


# ---------------------------------------------------------------------------
# Shared completion page
# ---------------------------------------------------------------------------

_CONNECTED_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Connected — Margin</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
        background:#0f0f0f;color:#e8e8e8;display:flex;align-items:center;
        justify-content:center;min-height:100vh;margin:0;text-align:center;padding:24px;}}
  .card{{background:#1a1a1a;border-radius:16px;padding:40px 32px;max-width:400px;width:100%;}}
  .check{{font-size:56px;margin-bottom:16px;}}
  h2{{margin:0 0 12px;font-size:22px;font-weight:600;}}
  p{{margin:0;color:#aaa;font-size:15px;line-height:1.5;}}
  .app{{display:inline-block;background:#2a2a2a;border-radius:8px;padding:4px 12px;
        margin-top:16px;font-size:13px;color:#7dd87d;font-weight:500;}}
</style>
</head>
<body>
<div class="card">
  <div class="check">✅</div>
  <h2>{app} Connected!</h2>
  <p>Your account is linked. Return to <strong>ASI:One</strong> and tap<br>
  <em>"I’ve connected everything"</em> to continue.</p>
  <div class="app">{app} ✓</div>
</div>
</body>
</html>"""


@app.get("/connected", response_class=HTMLResponse)
async def connected(app: str = "App") -> HTMLResponse:
    safe_app = app[:32].replace("<", "").replace(">", "").replace("&", "").replace(‘"’, "")
    if not safe_app:
        safe_app = "App"
    return HTMLResponse(_CONNECTED_HTML.format(app=safe_app))
