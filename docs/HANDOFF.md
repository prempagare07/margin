# Margin — Interface Handoff

This file is the source of truth for the interface boundary between Prem's code and Alex's code. Update it whenever an interface changes; mention the update in your commit message.

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

Prem's approval and graph modules expect these functions from `db.repo`:

```python
# Connection pool
pool: psycopg.AsyncConnectionPool  # or a get_pool() accessor

# User management
async def upsert_user(user_id: str, **kwargs) -> None: ...
async def get_user(user_id: str) -> dict | None: ...
async def set_phone(user_id: str, phone: str, code_hash: str, expires: datetime) -> None: ...
async def verify_phone(user_id: str) -> None: ...

# Plans (needed by core/approval.py)
async def insert_plan(plan_id: str, user_id: str, actions: list, rows: list) -> None: ...
async def get_plan(plan_id: str) -> dict | None: ...
async def update_plan_status(plan_id: str, status: str, executed_at: datetime | None = None, results: dict | None = None) -> list | None: ...
async def pending_plan_for(user_id: str) -> dict | None: ...

# Briefs and nudges (needed by channels/scheduler.py)
async def insert_brief(brief_id: str, user_id: str, letters: dict) -> None: ...
async def get_today_brief(user_id: str) -> dict | None: ...
async def insert_nudge(nudge_id: str, user_id: str, item_id: str, options: dict) -> None: ...
async def get_pending_nudge(user_id: str) -> dict | None: ...
async def update_nudge(nudge_id: str, reply: str, action_taken: str) -> None: ...

# Items cache (fallback when specialists are slow)
async def last_items_by_source(user_id: str, source: str) -> list[dict]: ...
async def items_in_window(user_id: str, start: str, end: str) -> list[dict]: ...
async def upsert_items(items: list[dict]) -> None: ...

# Budget (needed by core/graph.py → cards)
async def get_budget(user_id: str) -> dict | None: ...
async def save_budget(user_id: str, monthly_income: float, savings_goal: float, weekly_cap: float | None) -> None: ...
async def safe_to_spend(user_id: str) -> float: ...
```

### `shared/protocol.py` — message contract

Prem uses exactly the classes as pushed. Current state matches the agreed contract. **Do not change without coordinating.**

```python
from shared.protocol import Item, GatherRequest, GatherResult, ActionRequest, ActionResult
```

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

## Open questions / blockers

- `db/schema.sql` is empty — Alex needs to add the full schema before `scripts/db_init.py` can run.
- `db/repo.py` is empty — Prem's approval module uses the interface above; will add a mock until Alex's version lands.
- `DATABASE_URL` not yet set in `.env` — waiting for Tiger Data credentials.
- Agent seeds (`MARGIN_SEED` … `LIFE_SEED`) not yet set — both teammates must use the same seeds so agent addresses are stable.
