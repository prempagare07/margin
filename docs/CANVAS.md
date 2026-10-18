# Canvas calendar feed connection

Canvas is implemented in calendar-feed mode; no personal Canvas API token is
required. The feed reader and web routes are ready for the Academic agent to use.
The agent's message handler and delivery of the connection link remain its
owner's integration work.

## Run and connect

The web server and agents must load the same `.env` settings:

- `DATABASE_URL`: the configured Tiger Data connection string.
- `TOKEN_ENCRYPTION_KEY`: the existing Fernet key. Keep it stable; replacing it
  without migrating saved secrets makes existing connections unreadable.
- `STATE_SIGNING_KEY`: a random secret of at least 32 bytes.
- `PUBLIC_BASE_URL`: the deployed HTTPS origin, e.g. `https://margin.example.com`.
- `CANVAS_BASE_URL`: the school's Canvas origin if it uses a custom hostname.
  Standard `*.instructure.com` Canvas hosts are accepted automatically.

Start the web server with `.venv/bin/uvicorn web.server:app --host 0.0.0.0 --port 8000`.
Generate a link in the trusted agent handler from the authenticated ASI:One sender
address, never from an arbitrary user ID supplied in a message or browser form:

```python
from web.connect_state import make_connect_url

link = make_connect_url(sender)
# Return this link to that sender in your existing ASI:One response.
```

Links expire after 15 minutes. Open the link, choose **Calendar → Calendar Feed**
in Canvas, and paste the private feed URL into Margin. The page accepts HTTPS or
webcal links, validates the feed, and stores it encrypted in the signed user's
`connections` row (`provider="canvas"`, `secret={"ical_url": ...}`). A valid empty
calendar can connect. Invalid feeds and failed saves show a retryable error.
Opening `/connect/canvas` without a signed link intentionally fails.

For a local-only browser check, load `.env`, call `make_state("your-test-user")`,
and open `http://127.0.0.1:8000/connect/canvas?token=<generated-state>`. Keep that
link private. This bypasses only the public-origin URL builder, not verification.
Submitting the page writes to the configured database; use a test user you own.

`CANVAS_ICAL_URL` is optional local smoke-test input and never a fallback feed for
other users. `CANVAS_TEST_TOKEN` is unused by this reader.

## Academic integration

```python
from integrations import canvas_client

async def handle_gather(ctx, sender, request):
    result = await canvas_client.gather(request)
    await ctx.send(sender, result)
```

Register this handler with the shared `GatherRequest` / `GatherResult` protocol.
Only accept gather messages from the trusted Margin orchestrator; a user ID in
an unauthenticated message is not authorization to read another user's feed.
The helper returns `agent="margin-academic"` and the incoming request ID. It
fetches the user's saved connection, caches all current entries, and returns
items in the requested half-open time window. Local `done` / `snoozed` states
and existing cached item IDs survive a refresh. Failures return cached items
with `stale_since` and `error`; disconnected users get a connection error.
If storage fails after a successful fetch, fresh items still return with a cache
error. Each agent event loop must close its database pool during shutdown.

Lower-level helpers are `await get_items(user_id)` and
`await get_assignments(user_id)`. They read live data without caching. Testing
tools can use `fetch_feed(url, user_id, tz=...)` or pure `parse_feed(bytes,
user_id, origin=..., tz=...)`. Keep private URLs out of prompts and logs.

## Data and privacy boundaries

- Assignments become `source="canvas_ical"`, `kind="assignment"`, with UTC
  `due_at`. Calendar events use `starts_at` / `ends_at` / `location` instead.
- Assignment links identify the same assignment across due-date overrides;
  overrides take precedence. Item IDs are stable and scoped to the user.
- A date-only assignment has no invented deadline time: its all-day interval
  and evidence quote preserve the published date. Floating times use the feed's
  declared timezone or the user's timezone (default America/Phoenix).
- Calendar feeds do not establish submission status, grades, or announcements.
  An `open` item is actionable, not evidence of a missing submission.
- Cancelled and undated entries are skipped. Unexpanded recurrence rules are
  explicitly rejected. Only events actually published by Canvas can be read.
- Cache refreshes do not delete older stored entries; an outage may show a
  removed assignment from an earlier fetch, explicitly labeled stale.
- URL validation allows only Canvas feed paths on approved hosts. Requests use
  HTTPS, bounded size/time, two transient retries, and no redirects.
- Signed links are bearer credentials valid for 15 minutes. Both GET and POST
  verify them. POST additionally checks a browser CSRF cookie. Forms and responses
  are not cached; private links are not echoed after submission.
- Uvicorn access logs remove connection query strings. Sentry omits connection
  request events and transactions; it does not capture request bodies or local
  variables. The feed fetch emits a generic span without its private URL.
  Configure deployment proxies/CDNs to omit query strings for `/connect/canvas`
  too, and do not enable HTTP wire/debug logging for private feed requests.

## Verification

Run `.venv/bin/python -m pytest -q tests/test_canvas_client.py tests/test_canvas_connect.py`.
These tests use synthetic feeds and mocked network/database boundaries. The
additional end-to-end connection/cache test in `tests/test_repo_db.py` uses a
disposable local PostgreSQL database, as described in `docs/HANDOFF.md`; it never
uses Tiger Data or a real user's feed.
