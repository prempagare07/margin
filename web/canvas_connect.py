"""Connect a private Canvas calendar feed to the user in a signed link."""

import hmac
import secrets
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from db import repo
from integrations import canvas_client
from web.connect_state import InvalidState, verify_state

router = APIRouter()
COOKIE = "margin_canvas_csrf"
MAX_FORM_BYTES = 8192
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
}


def _page(title: str, content: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)} · Margin</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f5f4ee;color:#25372e;font:16px/1.6 system-ui,sans-serif}}
main{{max-width:540px;margin:8vh auto;padding:32px}}.brand{{font-weight:750;letter-spacing:-.04em;font-size:28px;margin-bottom:48px}}
.brand span{{color:#7065bd}}.eyebrow{{font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:#68746c;font-weight:650}}
h1{{font-size:36px;line-height:1.15;letter-spacing:-.04em;margin:12px 0 20px}}p,ol{{color:#536258}}
ol{{padding-left:22px}}li{{padding:4px 0}}form{{margin:28px 0;background:white;border:1px solid #dce1d8;padding:24px;border-radius:18px}}
label{{display:block;font-weight:650;margin-bottom:8px}}input[type=url]{{width:100%;font:inherit;padding:12px;border:1px solid #89998c;border-radius:8px}}
input:focus{{outline:3px solid #c8c1ed;outline-offset:2px}}button{{margin-top:18px;width:100%;padding:13px;background:#514587;color:white;border:0;border-radius:9px;font:600 16px system-ui;cursor:pointer}}
button:focus-visible{{outline:3px solid #7f73bd;outline-offset:3px}}.note{{font-size:13px;margin-bottom:0}}.error{{color:#8c2828;background:#fff1ee;padding:14px;border-radius:10px}}
.success{{background:#e4ecdf;padding:20px;border-radius:14px}}footer{{font-size:13px;color:#68746c;margin-top:32px}}
@media(max-width:540px){{main{{margin:24px auto;padding:24px}}.brand{{margin-bottom:32px}}h1{{font-size:32px}}}}
</style></head><body><main><div class="brand"><span>●</span> margin</div>
<div class="eyebrow">Your semester, a little lighter</div><h1>{escape(title)}</h1>{content}
<footer>Margin · Return to your conversation in ASI:One when you’re done.</footer></main></body></html>""", status_code=status, headers=HEADERS)


def _form(token: str, csrf: str, *, error: str = "", status: int = 200) -> HTMLResponse:
    alert = f'<p class="error" role="alert">{escape(error)}</p>' if error else ""
    return _page("Connect Canvas", f"""
<p>Bring your assignment due dates and calendar events into Margin.</p>
<ol><li>Open <strong>Calendar</strong> in Canvas.</li><li>Select <strong>Calendar Feed</strong>.</li>
<li>Copy the link and paste it below.</li></ol>{alert}
<form method="post" action="/connect/canvas">
<input type="hidden" name="token" value="{escape(token, quote=True)}">
<input type="hidden" name="csrf" value="{escape(csrf, quote=True)}">
<label for="ical_url">Canvas calendar feed link</label>
<input id="ical_url" name="ical_url" type="url" required maxlength="2048" autocomplete="off" spellcheck="false"
 placeholder="https://your-school.instructure.com/feeds/…" aria-describedby="privacy">
<p class="note" id="privacy">This is a private link. Margin stores it encrypted and only reads the calendar you share.</p>
<button type="submit">Connect Canvas</button></form>
<p class="note">Calendar feeds show published due dates and events. They don’t tell Margin whether you’ve submitted an assignment.</p>""", status)


def _invalid(exc: Exception, status: int = 400) -> HTMLResponse:
    return _page("Let’s try that again", f'<p role="alert">{escape(str(exc))}</p>', status)


@router.get("/connect/canvas", response_class=HTMLResponse)
async def canvas_connect_get(request: Request, token: str = "") -> HTMLResponse:
    try:
        verify_state(token)
    except InvalidState as exc:
        return _invalid(exc)
    except RuntimeError:
        return _invalid(ValueError("Canvas connection is not configured yet. Please try again later."), 503)
    csrf = secrets.token_urlsafe(32)
    response = _form(token, csrf)
    response.set_cookie(COOKIE, csrf, max_age=900, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/connect/canvas")
    return response


async def _read_form(request: Request) -> dict[str, str]:
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ValueError("Submit the form from your Canvas connection link.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_FORM_BYTES:
            raise ValueError("The submitted form is too large.")
    fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    if set(fields) != {"token", "csrf", "ical_url"} or any(len(v) != 1 for v in fields.values()):
        raise ValueError("Reload your connection link and submit the form again.")
    return {k: v[0] for k, v in fields.items()}


@router.post("/connect/canvas", response_class=HTMLResponse)
async def canvas_connect_post(request: Request) -> HTMLResponse:
    try:
        form = await _read_form(request)
    except (ValueError, UnicodeError):
        return _invalid(ValueError("Reload your connection link and submit the form again."))
    token, csrf = form["token"], form["csrf"]
    try:
        user_id = verify_state(token)
    except InvalidState as exc:
        return _invalid(exc)
    except RuntimeError:
        return _invalid(ValueError("Canvas connection is not configured yet. Please try again later."), 503)
    cookie = request.cookies.get(COOKIE, "")
    if not csrf or not cookie or not hmac.compare_digest(csrf.encode(), cookie.encode()):
        return _invalid(ValueError("Reload your connection link before submitting this form."), 403)
    try:
        url = canvas_client.normalize_feed_url(form["ical_url"])
        user = await repo.get_user(user_id)
        items = await canvas_client.fetch_feed(url, user_id, tz=(user or {}).get("tz", "America/Phoenix"))
        # A slow feed request must not extend the signed link’s lifetime.
        verify_state(token)
        await repo.upsert_user(user_id)
        await repo.save_connection(user_id, "canvas", secret={"ical_url": url}, status="active")
    except canvas_client.CanvasFeedError as exc:
        return _form(token, csrf, error=str(exc), status=422)
    except InvalidState as exc:
        return _invalid(exc)
    except Exception:
        # Never echo exceptions: they may contain a database URL or private feed.
        return _form(token, csrf, error="We couldn’t save your connection. Please try again.", status=503)
    count = len(items)
    detail = (f"Your feed has {count} calendar {'entry' if count == 1 else 'entries'} ready for Margin."
              if count else "Your feed is connected. Canvas hasn’t published any dated entries in it yet.")
    response = _page("Canvas is connected", f'<div class="success"><strong>You’re all set.</strong><p>{detail}</p></div><p>Close this tab and return to ASI:One.</p>')
    response.delete_cookie(COOKIE, path="/connect/canvas")
    return response
