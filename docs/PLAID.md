# Plaid Sandbox transaction integration

Margin imports a demo bank account's spending history into the existing Tiger Data
`transactions` table. It does not infer future bills from purchases. The Life
agent syncs before quoting a spending allowance, then uses `db.repo.safe_to_spend`.
The integration is Sandbox-only; it cannot connect a real bank account.

## Setup and run

The existing `db/schema.sql` is sufficient; no migration is required. Install
`requirements.txt`, then configure these values in the local `.env`:

```dotenv
DATABASE_URL=<Tiger Data PostgreSQL connection string>
TOKEN_ENCRYPTION_KEY=<the existing stable Fernet key>
PLAID_CLIENT_ID=<Plaid client ID>
PLAID_SECRET=<Plaid Sandbox secret>
PLAID_ENV=sandbox
```

Use the exact existing Margin user ID (the ASI sender address), so the imported
transactions belong to the same user as the budget. The script does not create a
real user or remap another provider's identity. Provision the demo connection once:

```bash
.venv/bin/python -m scripts.plaid_sync --user-id '<ASI_SENDER_ADDRESS>' --connect
```

The default destination is `DATABASE_URL` from `.env`. `--connect` uses Plaid's
`user_transactions_dynamic` at First Platypus Bank (`ins_109508`); an existing
connection is reused. Later syncs need no `--connect`:

```bash
.venv/bin/python -m scripts.plaid_sync --user-id '<ASI_SENDER_ADDRESS>' --budget
```

Save income, savings goal, optional weekly cap, and recurring bills using
`db.repo.save_budget` / `save_bill`. `--budget` reports `BUDGET_NOT_CONFIGURED` if
these settings are absent. It never substitutes invented income or bills.

A new Sandbox Item can initially report `NOT_READY` or `INITIAL_UPDATE_COMPLETE`.
Run sync again shortly afterward. Exit code 0 means history is ready; 2 means
history is still loading; 1 means the sync/setup failed. Output contains counts,
status, timestamp, and Plaid request IDs, never access tokens, cursors, or merchant
details. An explicitly requested budget amount is printed only after ready sync.

On Python installations without a system CA file, the SDK uses the `certifi` CA
bundle. `SSL_CERT_FILE` can select a deployment's trusted CA file. TLS verification
always remains enabled.

## Public API

`integrations/plaid_sandbox.py` exposes:

- `connect_sandbox(user_id) -> ConnectionInfo`: explicitly provision a demo Item;
  preserve an existing connection. Requires an existing user.
- `sync_transactions(user_id) -> SyncResult`: sync an existing connection; never
  provision one as a side effect of a budget read. Returns counts, page count,
  request IDs, last successful sync time, readiness status, and `ready`.
- `budget_snapshot(user_id) -> BudgetSnapshot`: require a saved budget, finish a
  sync with `HISTORICAL_UPDATE_COMPLETE`, then calculate `safe_to_spend` from DB.
- `fetch_bills(user_id) -> list[Item]`: compatibility wrapper for user-configured
  recurring bills; does not call Plaid. Uses stable per-user/month IDs and next
  due dates with month-end clamping. The existing Item enum groups finance under
  `source="plaid"`; `source_id="configured-bill:…"` and the quote explicitly mark
  manual bills. No purchase is turned into a bill.
- `PlaidError`: safe `code`, user-facing message, and optional request ID. Callers
  must not replace an error with zero spending or fictional transactions.

Life's budget and event-card paths consume `budget_snapshot`. If finance is
unavailable, event titles/prices can still be shown without a spending allowance.
Gather preserves available event/bill items and includes a safe error when sync
fails. Configured bills remain readable without a Plaid connection.

## Persistence and sync guarantees

`db/plaid.py` stores `{access_token, item_id, environment, cursor, last_synced_at,
transactions_update_status}` inside the existing Fernet-encrypted
`connections.secret_enc`. One connection is supported per Margin user. Tokens
remain server-side. Initialization never overwrites an existing Item/cursor.
Concurrent initial provisioning may create an unused Sandbox Item, but only one
connection is selected and only its transactions can be committed.

The adapter reads all `/transactions/sync` pages using the saved cursor. It
coalesces added, modified, and removed records in page order. If Plaid reports
`TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION`, it discards that attempt and starts
again from the original cursor (at most two restarts). Repeated cursors, malformed
pages, more than 100 pages / 50,000 updates per attempt, and a 90-second operation
budget fail without saving partial updates.

Only after all pages succeed does one DB transaction lock the user's connection,
compare its original secret snapshot, apply the deltas, and save the new encrypted
cursor. A changed connection or concurrent sync yields `SYNC_CONFLICT`. Retry from
the newly saved cursor. Provider I/O is outside DB transactions and off the agent
loop. A provider thread that finishes after cancellation cannot write to the DB.

- Added/modified rows replace the prior `(user_id, txn_id)` row. This also handles
  corrected timestamps despite the Timescale primary key including `ts`.
- Removed records are marked `is_removed`; reintroduced records clear that flag.
- Pending-to-posted changes follow Plaid's added/removed records. Pending charges
  are stored but excluded by the existing spending queries.
- Plaid timestamps are retained as UTC instants. A date-only value becomes noon
  in `America/Phoenix`, keeping purchases in the correct local day/month/week.
- Actual USD currency is required. Missing, unofficial, or non-USD currency fails
  the entire batch without advancing the cursor. No currency is assumed or
  converted. Signed amounts and primary personal-finance categories are retained.
- Existing budget SQL counts positive, posted, nonremoved flexible USD expenses;
  excludes income, transfers, rent/utilities, and loan payments. Refunds are stored
  but do not reduce the MVP's gross-spending metric.

Missing credentials, missing connection, expired access, storage failure, and
initial history loading are explicit outcomes. Expired access marks only the
unchanged Item `relink_required`; no failed connection is silently replaced.
Recovery via Plaid Link/update mode is outside this Sandbox demo integration.

## Verification

Offline adapter/SDK and Life caller tests:

```bash
.venv/bin/python -m pytest -q tests/test_plaid_sandbox.py tests/test_life_plaid.py
```

Disposable local PostgreSQL integration tests (these truncate the test DB):

```bash
TEST_DATABASE_URL=postgresql://margin_test:margin_test@127.0.0.1:55432/margin_test \
  .venv/bin/python -m pytest -q tests/test_repo_db.py
```

These cover real encryption, isolation, concurrent initialization/sync, complete
rollback, cursor reuse, changed dates, removals/reintroduction, pending charges,
and the full adapter-to-DB-to-`safe_to_spend` path. The local PostgreSQL suite uses
the production table schema and spending function; Timescale continuous aggregate
refresh behavior is not exercised there.

For live Sandbox verification without writing to Tiger Data:

```bash
.venv/bin/python -m scripts.plaid_sync --user-id plaid-demo-local --connect \
  --local-test-db postgresql://margin_test:margin_test@127.0.0.1:55432/margin_test \
  --create-demo-user
```

Only local `margin_test` storage permits `--create-demo-user`, and the user ID must
start with `plaid-demo-`. Run again once ready, then repeat with the same user:
an unchanged response should have zero deltas and preserve transaction totals.
Do not run the destructive DB tests concurrently with this live smoke check.

## Remaining project integration boundaries

The orchestrator's current specialist wait is 15 seconds, shorter than a possible
multi-page Plaid sync. Prem's orchestration work should account for sync latency
and preserve partial/error Gather results. This implementation does not change
that timeout, scheduled polling/webhooks, the scheduler's separate budget code,
production Plaid Link, multiple bank Items per user, or provider reconnection UI.

Reference contracts: [Plaid Transactions API](https://plaid.com/docs/api/products/transactions/),
[Sandbox API](https://plaid.com/docs/api/sandbox/), and
[pagination mutation errors](https://plaid.com/docs/errors/transactions/).
