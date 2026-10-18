# Margin — Interface Handoff

This file is the source of truth for the interface boundary between Prem's code and Alex's code. Update it whenever an interface changes; mention the update in your commit message.

Alex's step 2 update (October 17, 2026): `Item` now carries event timing and
location. The database signatures below replace the older `plans.rows` /
`plans.results` interface and match the final `db/schema.sql`. These are the
contracts for step 3; `db/repo.py` is still a placeholder.

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

### `db.repo` — async database helpers

Alex will implement these functions in `db.repo`; Prem's approval, graph, and
scheduler modules should target this interface. Database rows are returned as
dictionaries. Every lookup or mutation of a user's plan, execution, or nudge is
scoped by `user_id`, not just its object ID.

```python
from datetime import datetime
from typing import Literal
from psycopg_pool import AsyncConnectionPool

# Connection pool
pool: AsyncConnectionPool  # pool lifecycle is implemented in step 3

# User management
async def upsert_user(user_id: str, **kwargs) -> None: ...
async def get_user(user_id: str) -> dict | None: ...
async def set_phone(user_id: str, phone: str, code_hash: str, expires: datetime) -> None: ...
async def verify_phone(user_id: str) -> None: ...

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

# Budget (needed by core/graph.py → cards)
async def get_budget(user_id: str) -> dict | None: ...
async def save_budget(user_id: str, monthly_income: float, savings_goal: float, weekly_cap: float | None) -> None: ...
async def safe_to_spend(user_id: str) -> float: ...
```

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

- `db/repo.py` is still empty. The signatures and transition rules above are the step 3 implementation target, not functions that callers can import yet.
- Prem must consume the updated protocol and database interface in the orchestrator, approval flow, and scheduler. This file records Alex's handoff; it does not claim Prem's runtime is already updated.
- Step 1 verified read access through Composio for Gmail, Calendar, and Slack. Canvas iCal and the ASU campus-events calendar feed also parsed successfully. Credential presence alone is not a connection check for other providers.
- Plaid, Stripe, Photon webhook verification, and deployment readiness belong to later setup and implementation checks.
