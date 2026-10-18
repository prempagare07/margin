# Margin — Interface Handoff

This file is the source of truth for the interface boundary between Prem's code and Alex's code. Update it whenever an interface changes; mention the update in your commit message.

Alex's steps 2–3 update (October 17, 2026): `Item` carries event timing and
location, and `db/repo.py` now implements the database helpers below against
the final `db/schema.sql`. The older `plans.rows` / `plans.results` interface
does not apply. Phone verification now also requires the submitted code's hash;
the database compares it and checks expiry atomically.

---

## Prem → Alex (what Alex's code can call from Prem's side)

### `core.chat.make_chat_protocol(agent_name, on_text, greeting)`

Builds and returns the `chat_protocol` for a uAgent. Wire it into every specialist agent.

```python
from core.chat import make_chat_protocol

proto = make_chat_protocol(
    agent_name="margin-academic",
    on_text=my_handler,   # async def handler(ctx, sender: str, text: str, selection: dict | None)
    greeting="margin-academic online",
)
agent.include(proto, publish_manifest=True)
```

### `core.cards.*` — card builders

All card builders return a `ChatMessage`. Pass the result directly to `ctx.send()`.

```python
from core.cards import priorities_carousel, task_detail, review, done, error

msg = priorities_carousel(items, tz="America/Phoenix")
await ctx.send(sender, msg)
```

### `channels.photon.send_text(user_id: str, body: str) -> None`

Send an iMessage/SMS to a verified user from Alex's Life agent (e.g., overspend nudge).

```python
from channels.photon import send_text
await send_text(user_id, "You've spent 70% of your weekly budget.")
```

---

## Alex → Prem (what Prem's side expects from Alex's code)

### Composio integration specification

[COMPOSIO_SPEC.md](COMPOSIO_SPEC.md) defines the adapter interface and
behavior/acceptance requirements for account connections, Gmail/Slack reads,
Calendar availability, source retrieval, and approved event/draft creation. It
also assigns execution-claim ownership and records the remaining agent/approval
wiring. The facade is implemented in `integrations/composio_tools.py`; see
[COMPOSIO.md](COMPOSIO.md) for exact stored action params, settings, cache
helpers, tests, and live read verification. Composio writes are claimed by the
adapter: the orchestrator must approve with `repo.approve_plan`, then call
`execute_approved_action` without pre-claiming the action. Agent extraction,
gather assembly, calendar action conversion, and adapter-owned execution are now
wired into the orchestrator.

Calendar connection and planning behavior:

- After Google Calendar becomes active, `auto_select_calendars` reads every
  writable calendar and selects the writable primary calendar as the write
  target. A single writable non-primary calendar is accepted as a fallback;
  multiple writable calendars without a primary require an explicit choice.
- Stored Calendar actions use `type="create_event"` with params `account_id`,
  `calendar_id`, `title`, `starts_at`, `ends_at`, optional `description`, and
  `purpose="study_block"`. Model-only fields such as `item_id`, `start_iso`,
  `end_iso`, and `summary_row` are never persisted.
- `execute_node` approves the plan and sends each `ActionRequest` without first
  claiming or completing its execution. The Composio write adapter exclusively
  owns the durable claim and receipt.
- The merged onboarding Connect flow saves the form and then sends one link
  card. Its Canvas URL uses the signed `token` query parameter; Gmail, Google
  Calendar, and Slack use provider keys `gmail`, `gcal`, and `slack`.

### Canvas feed reader and connection page

Implemented in `integrations/canvas_client.py`, `web/canvas_connect.py`, and
`web/connect_state.py`. See [CANVAS.md](CANVAS.md) for setup and agent wiring.
Academic can call `await canvas_client.gather(request)` and return its
`GatherResult` directly. Issue a signed connection link using
`make_connect_url(authenticated_sender)`. The page validates and encrypts a
calendar feed under that user; no Canvas API token is required. `PUBLIC_BASE_URL`
must be the deployed HTTPS origin before issuing public links. The specialist
handler and orchestrator connection-card wiring remain with their owners.

### `db.repo` — async database helpers

These functions are implemented in `db.repo`; Prem's approval, graph, and
scheduler modules should target this interface. Database rows are returned as
dictionaries. Every lookup or mutation of a user's plan, execution, or nudge is
scoped by `user_id`, not just its object ID.

```python
from datetime import datetime
from typing import Literal
from psycopg_pool import AsyncConnectionPool
from shared.protocol import GatherResult

# Connection pool: one per event loop, opened lazily; no module-global pool
async def get_pool() -> AsyncConnectionPool: ...
async def close_pool() -> None: ...

# User management
async def upsert_user(user_id: str, **kwargs) -> None: ...
async def get_user(user_id: str) -> dict | None: ...
async def get_user_by_phone(phone: str) -> dict | None: ...  # verified numbers only
async def set_phone(user_id: str, phone: str, code_hash: str, expires: datetime) -> None: ...
async def verify_phone(user_id: str, code_hash: str) -> bool: ...

# Connected accounts; private feed URLs belong inside secret
async def save_connection(
    user_id: str, provider: str, *, composio_account_id: str | None = None,
    secret: dict | None = None, status: str = "active",
) -> None: ...
async def get_connection(user_id: str, provider: str) -> dict | None: ...
async def set_connection_status(user_id: str, provider: str, account_id: str | None, status: str) -> None: ...

# Plans (needed by core/approval.py)
async def insert_plan(plan_id: str, user_id: str, actions: list[dict]) -> None: ...
async def get_plan(plan_id: str, user_id: str) -> dict | None: ...
async def pending_plan_for(user_id: str) -> dict | None: ...
async def approve_plan(plan_id: str, user_id: str) -> bool: ...
async def update_plan_status(
    plan_id: str, user_id: str, status: Literal["cancelled", "done"]
) -> bool: ...

# Per-action execution records (action_id is unique within its plan)
async def get_action_execution(
    plan_id: str, action_id: str, user_id: str
) -> dict | None: ...
async def list_action_executions(plan_id: str, user_id: str) -> list[dict]: ...
async def claim_action_execution(plan_id: str, action_id: str, user_id: str) -> bool: ...
async def finish_action_execution(
    plan_id: str,
    action_id: str,
    user_id: str,
    *,
    status: Literal["succeeded", "failed", "unknown"],
    result: dict | None = None,
    error: str | None = None,
) -> None: ...

# Briefs and nudges (needed by channels/scheduler.py)
async def insert_brief(brief_id: str, user_id: str, letters: dict[str, str]) -> None: ...
async def get_today_brief(user_id: str) -> dict | None: ...
async def insert_nudge(
    nudge_id: str,
    user_id: str,
    item_id: str | None,
    options: list | dict,
    *,
    dedupe_key: str,
    plan_id: str | None = None,
    send_after: datetime | None = None,
) -> str: ...  # returns the existing or newly inserted nudge_id
async def due_nudges(now: datetime) -> list[dict]: ...
async def mark_nudge_sent(nudge_id: str, user_id: str, sent_at: datetime) -> None: ...
async def get_pending_nudge(user_id: str) -> dict | None: ...
async def update_nudge(nudge_id: str, user_id: str, reply: str, action_taken: str) -> None: ...

# Items cache (fallback when specialists are slow)
async def last_items_by_source(user_id: str, source: str) -> list[dict]: ...
async def items_in_window(user_id: str, start: str, end: str) -> list[dict]: ...
async def upsert_items(items: list[dict]) -> None: ...
async def get_item(user_id: str, item_id: str) -> dict | None: ...
async def cached_gather_result(
    request_id: str, agent: str, user_id: str, sources: list[str],
    window_start: str, window_end: str, *, error: str,
) -> GatherResult: ...

# Budget (needed by core/graph.py → cards)
async def get_budget(user_id: str) -> dict | None: ...
async def save_budget(user_id: str, monthly_income: float, savings_goal: float, weekly_cap: float | None) -> None: ...
async def get_bills(user_id: str) -> list[dict]: ...
async def save_bill(user_id: str, name: str, amount: float, due_day: int) -> None: ...
async def safe_to_spend(user_id: str) -> float: ...
```

Pool, identity, and connection semantics:

- Entry points load `.env` before calling the helpers. Pools use `DATABASE_URL`,
  UTC sessions, a 10-second acquisition/statement timeout, and up to four
  connections per event loop. Each agent loop and the web loop must call
  `await repo.close_pool()` during shutdown. Importing the module does not open
  a connection. Callers can use `async with (await repo.get_pool()).connection()`
  for additional queries; do not share an async pool across the agent threads.
- Create the user first with `upsert_user`. Optional profile fields are `tz`,
  `plan`, `pro_until`, `photon_user_id`, `photon_space_id`, and `text_opted_in_at`.
  Omitted fields retain existing values. The trusted payment flow owns changes
  to paid-plan fields; never pass unfiltered form or model output as kwargs.
- `set_phone` accepts E.164 numbers and resets verification/opt-in. It stores a
  hash, never a plain code. `verify_phone(user_id, code_hash)` returns `True`
  only if that hash matches the current unexpired challenge, then clears the
  challenge. The caller hashes the submitted code using the same scheme as
  when issuing it. Expired challenges are cleared; a changed challenge cannot
  be verified using the old hash. Connect now issues a high-entropy possession
  challenge consumed by `verify_phone_link` after a signed inbound Photon message.
  See [ONBOARDING.md](ONBOARDING.md). Reminder opt-in UI remains separate.
- Connection providers are `canvas`, `gmail`, `gcal`, `slack`, and `plaid`.
  `save_connection` replaces the snapshot: `None` clears an optional field.
  Fernet uses `TOKEN_ENCRYPTION_KEY`; private Canvas feed URLs go in
  `secret={"ical_url": ...}` and tokens in `secret={"token": ...}`.
  `get_connection` returns decrypted JSON under `secret`, never `secret_enc`.
  Keep this result out of cards, prompts, and logs. Invalid ciphertext/key
  raises an error rather than returning an apparently usable connection.
- Missing reads return `None` (or an empty list). Invalid inputs and invalid
  transitions raise `ValueError`; missing targets for required mutations raise
  `LookupError`. Database/constraint errors propagate. Writes roll back on
  errors and are not automatically retried or replaced with in-memory state.
  Callers decide when to show a connection/error card or use cached reads.

Item and cache semantics:

- Item reads return UTC ISO strings for `due_at`, `starts_at`, `ends_at`, and
  `fetched_at`, plus numeric `cost_usd`, ready for `Item` construction. Other
  database dictionaries retain Python `datetime` / `Decimal` values.
- Upserts use `(user_id, source, source_id)` and retain the original `item_id`,
  so existing brief/nudge references remain valid. Incoming IDs must be globally
  unique for new items. A source refresh preserves local `done` / `snoozed`
  statuses. Each batch is all-or-nothing. Incoming snapshots replace optional
  source fields, and a successful refresh sets `fetched_at` to database time.
- Windows are half-open `[start, end)`: include deadlines in the window and
  overlapping events (including zero-length events at the start). An event
  ending exactly at the window start is excluded. Undated items remain
  available through `last_items_by_source`; window queries exclude them.
- `cached_gather_result` filters the window by the requested sources, includes
  the supplied error, and sets `stale_since` to the oldest returned fetch time.
  An empty cache has no staleness timestamp and still includes the error.
  Cached events do not establish free slots; Planner must recompute those.
  Source-removal/snapshot reconciliation is left to the integration layer.
- For Composio, use `composio_tools.cached_gather_result` instead: it also filters
  the selected account and calendars/channels. Use `composio_tools.cache_items`
  to retain saved IDs and local completion state in returned fresh Items. See
  [COMPOSIO.md](COMPOSIO.md) for current-lookback undated messages.

Plan and execution semantics:

- `plans` stores `actions`, `status`, `created_at`, `approved_at`, and
  `executed_at`, alongside its IDs. There are no `rows` or `results` columns.
  Each stored action must have an `action_id` unique within that plan, a `type`
  from `ActionRequest`, and `params`. Build review-card rows from those stored
  actions; the `rows` argument of `core.cards.review()` remains presentation data.
- `approve_plan` atomically changes an owned `pending` plan to `approved`, sets
  `approved_at`, and creates its `pending` action execution records. Return
  `True` only for the caller that made the transition; duplicate approval clicks
  must not start another execution.
- `claim_action_execution` checks ownership, membership in the stored approved
  plan, and atomically changes one execution from `pending` to `running`, setting
  `started_at`. Only a successful claim permits a provider call. Use the stored
  action's parameters, not parameters supplied by a card click or message.
- Store receipts in `action_executions.result` (a JSON object), errors in `error`,
  and outcomes in `status`. Finishing a known outcome sets `completed_at`;
  `succeeded` requires it. A timeout with an uncertain provider outcome is
  `unknown`, not proof of failure. Do not automatically retry `running`,
  `succeeded`, `failed`, or `unknown` executions; reconcile uncertain outcomes
  before deciding on another attempt. A database claim alone cannot guarantee
  exactly-once effects at an external provider.
- `finish_action_execution` accepts a claimed `running` execution, or resolves
  an `unknown` execution after provider reconciliation. An identical terminal
  receipt is an idempotent no-op; a different terminal receipt is rejected.
- `update_plan_status` returns whether a transition occurred: only `pending` may
  become `cancelled`; only `approved` may become `done`, once every action is
  `succeeded` or `failed`. The latter sets `executed_at`. Keep plans with pending,
  running, or unknown executions out of `done`.
- `ActionResult` carries `action_id`, not `plan_id`. Correlate it with the
  originating `ActionRequest` and retain `(user_id, plan_id, action_id)` for all
  database operations. Never look up an execution globally by `action_id` alone.

Nudge semantics:

- `dedupe_key` is nonempty and stable for the logical nudge, such as
  `overspend:2026-10-12` or `reminder:<plan_id>:<action_id>`. The unique key is
  `(user_id, dedupe_key)`; an insertion conflict returns the existing nudge's ID
  without resetting its sent/reply state.
- `item_id` may be `None` for budget nudges; `plan_id` may be `None` for a nudge
  unrelated to a plan. Supplied item and plan IDs must belong to that user.
  Plain reminders use `options=[]`.
- `send_after=None` means immediately eligible. `due_nudges` selects
  `sent_at IS NULL AND (send_after IS NULL OR send_after <= now)`.
  Set `sent_at` only after a successful send. `get_pending_nudge` selects the
  latest sent, unanswered nudge with reply options for that user; it must not
  return a future reminder that has not been sent.
- `due_nudges` is for the trusted scheduler across all users. It does not claim
  messages for delivery: use one dispatch worker or a provider idempotency key
  based on `nudge_id`. Deduplicating stored nudges alone does not prevent two
  concurrent senders from sending the same message.

Budget semantics:

- `safe_to_spend` uses the existing `public.flexible_spent_this_month` function,
  with its posted, nonremoved USD expense filters. All month/day calculations
  use America/Phoenix, matching the schema. Bills due today or later are still
  reserved; a day-31 bill clamps to the month's final day.
- Weeks left is `max(1, inclusive_days_remaining / 7)`, retaining fractional
  weeks as in Alex's plan. A zero weekly cap is honored. Results are rounded to
  cents; negative values indicate a deficit. Missing budget settings return
  `0.0`; callers can use `get_budget` to distinguish this from a configured zero.

Prem wiring notes:

- `core/approval.py` currently has its own database pool and persistence path.
  Migrate that complete path to these helpers together: `insert_plan` stores
  only the plan; `approve_plan` creates execution rows atomically with approval.
  Do not also create those execution rows when inserting the plan.
- Map approval's `claim_action` to `claim_action_execution`, `complete_action`
  to `finish_action_execution`, and cancel/done transitions to
  `update_plan_status`. Preserve the user and plan IDs throughout. The helpers
  are ready, but the caller wiring is a separate integration change.

Database helper tests:

`python -m pytest -q` runs pure tests and skips database tests unless
`TEST_DATABASE_URL` is explicitly set. The database suite accepts only a
localhost database named `margin_test`; it never loads `.env`. It **truncates
test data between cases**. Use a disposable container, for example:

```sh
docker run --detach --rm --name margin-db-tests \
  -e POSTGRES_DB=margin_test -e POSTGRES_USER=margin_test \
  -e POSTGRES_PASSWORD=margin_test -p 127.0.0.1:55432:5432 postgres:16
# Wait until: docker exec margin-db-tests pg_isready -U margin_test
TEST_DATABASE_URL=postgresql://margin_test:margin_test@127.0.0.1:55432/margin_test \
  .venv/bin/python -m pytest -q
docker stop margin-db-tests
```

The suite loads the application tables, constraints, indexes, and monthly
spending function from `db/schema.sql`. PostgreSQL-only tests omit TimescaleDB
hypertable/continuous-aggregate setup, which these helpers do not modify. Do
not point these tests at Tiger Data or run `scripts/db_init.py` to test helpers.

### `shared/protocol.py` — message contract

Both owners must use the same version of these classes. Alex's step 2 update adds
three optional fields to `Item`, matching the existing columns in `public.items`:

| Field | Python type / default | Meaning |
|---|---|---|
| `starts_at` | `Optional[str] = None` | Event start, ISO 8601 in UTC |
| `ends_at` | `Optional[str] = None` | Event end, ISO 8601 in UTC |
| `location` | `Optional[str] = None` | Room, venue, or meeting location |

Keep `due_at` for deadlines; it is not a substitute for event timing. When an
end is present, a start is required and the end must be at or after the start,
including zero-length events. Integrations normalize and validate timestamps
before storage; the wire fields remain strings, as with `due_at`. Items without
event timing can omit all three fields. Do not invent missing times or a location.

```python
from shared.protocol import Item, GatherRequest, GatherResult, ActionRequest, ActionResult
```

**Prem integration checklist:** use this `Item` when constructing and consuming
`GatherResult`; carry the three fields through cache writes and card inputs.
The optional fields preserve parsing of older payloads, but adding them changes
the uAgents schema digests for `Item` and `GatherResult`. Update and restart both
specialists and orchestrator together, and republish their protocol manifests
when registering the updated handlers. No database migration is needed.

### Specialist agents — address discovery

`agents/bureau.py` writes `agents/addresses.json` on startup. Prem's orchestrator reads it at startup to get specialist addresses.

```json
{
  "margin": "agent1q...",
  "margin-academic": "agent1q...",
  "margin-inbox": "agent1q...",
  "margin-planner": "agent1q...",
  "margin-life": "agent1q..."
}
```

### `core/observability.span(name)` — Sentry span helper

Alex can wrap external calls in a Sentry span:

```python
from core.observability import span

async with span("tool.canvas"):
    result = await canvas_client.get_assignments(user_id)
```

---

## Schema differences (Alex's db/schema.sql vs. original plan) — Prem must adapt

Alex's schema is the source of truth. Key changes Prem's code must handle:

1. **`items` has `starts_at`, `ends_at`, `location`** — now also present as optional fields in `Item`; consume the updated shared contract as described above.

2. **`action_executions` is a separate table** (not `results` jsonb in `plans`). PK is `(plan_id, action_id)`. Prem's `core/approval.py` must write action results there, not into `plans.results`.

3. **`nudges.dedupe_key` and `send_after`** — every nudge requires a stable deduplication key. Pass an optional scheduled send time and include immediately eligible (`NULL`) times when querying due reminders.

4. **`purchases` table** — Stripe payments land here. Alex's `payments/stripe_flow.py` owns writes; Prem reads `plan` and `pro_until` from `users`.

5. **`flexible_spent_this_month(user_id)`** is a Postgres function — Prem calls it for budget math, does not reimplement it.

6. **`db/migrations/001_prem.sql` is now redundant** — Alex's schema covers all the same tables. Do not run the migration separately.

## Open questions / blockers

- `db/repo.py` is implemented and has local PostgreSQL integration tests. The existing Tiger Data schema is unchanged.
- Prem must consume the updated protocol and database interface in the orchestrator, approval flow, and scheduler. This file records Alex's handoff; it does not claim Prem's runtime is already updated.
- Step 1 verified read access through Composio for Gmail, Calendar, and Slack. Canvas iCal and the ASU campus-events calendar feed also parsed successfully. Credential presence alone is not a connection check for other providers.
- Stripe, Photon webhook verification, and deployment readiness belong to later setup and implementation checks.

## Plaid transaction integration

See [PLAID.md](PLAID.md) for setup, API contracts, failure behavior, and test commands.
`integrations.plaid_sandbox.sync_transactions(user_id)` now persists real Sandbox
transaction deltas and its encrypted cursor atomically through `db/plaid.py`.
Provision the demo Item explicitly with `scripts.plaid_sync --connect`; reads do
not create bank connections. Life's `budget_snapshot` path uses the imported rows
through `db.repo.safe_to_spend`, and `fetch_bills` reads configured recurring bills.
No schema migration is needed. Full agent orchestration and scheduler integration
remain separate from this adapter's verification.

## User onboarding persistence

The Connect action now saves the authenticated ASI sender, budget, recurring bills,
and pending phone link in one transaction through `repo.save_onboarding`. The
existing `upsert_user`, `save_budget`, `save_bill`, and `set_phone` helpers accept an
optional shared `conn` for this transaction. Phone verification is separate from
account creation, so Plaid can provision as soon as the Connect save succeeds.
See [ONBOARDING.md](ONBOARDING.md) for the card format, phone-link flow, required
Photon signature configuration, and tests. No schema migration is needed.
