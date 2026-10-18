-- Margin: Tiger Data schema
--
-- Changes from the previous version:
--   users          phone no longer UNIQUE on its own; verified numbers are
--                  unique via a partial index. Added E.164 check, pending
--                  verification code columns, and Photon IDs.
--   items          ends_at may equal starts_at (zero-length events).
--                  starts_at / ends_at / location must also be added to
--                  Item in shared/protocol.py.
--   action_exec.   Primary key is (plan_id, action_id), so per-plan action
--                  IDs like "a1" can't collide across plans.
--   nudges         send_after for scheduled texts ("remind you at 3:30").
--   purchases      New. Stripe payments, idempotent CommitPayment, and the
--                  Finals pack entitlement.
--   transactions   Documented how importers must set ts from Plaid dates.
--   flexible_spent_this_month()
--                  New. The monthly query, using the same rules as
--                  weekly_spend so the two can't drift apart.

BEGIN;

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 1. Users and account preferences
CREATE TABLE public.users (
    user_id text PRIMARY KEY,                -- ASI:One sender address

    -- E.164, e.g. +14805551234. Normalize before storing so Photon
    -- webhook lookups match exactly. Not unique on its own: an unverified
    -- number (typo, re-onboarding) must not block the real owner.
    phone text
        CHECK (phone ~ '^\+[1-9][0-9]{6,14}$'),
    phone_verified boolean NOT NULL DEFAULT false,

    -- Pending 6-digit code. Store a hash, never the code itself.
    -- Clear both columns on success or expiry.
    phone_code_hash text,
    phone_code_expires_at timestamptz,

    text_opted_in_at timestamptz,

    -- Photon identifiers, if its send API needs them per user.
    photon_user_id text,
    photon_space_id text,

    plan text NOT NULL DEFAULT 'free'
        CHECK (plan IN ('free', 'pro')),
    pro_until timestamptz,

    -- weekly_spend and flexible_spent_this_month() use America/Phoenix
    -- regardless of this value; fine while every user is at ASU.
    tz text NOT NULL DEFAULT 'America/Phoenix',
    created_at timestamptz NOT NULL DEFAULT now(),

    CHECK (NOT phone_verified OR phone IS NOT NULL),
    CHECK (text_opted_in_at IS NULL OR phone IS NOT NULL),
    CHECK (phone_code_hash IS NULL OR phone IS NOT NULL),
    CHECK ((phone_code_hash IS NULL) = (phone_code_expires_at IS NULL))
);

-- One account per verified number. The Photon webhook looks users up with
-- WHERE phone = $1 AND phone_verified, which uses this index.
CREATE UNIQUE INDEX users_verified_phone_uidx
    ON public.users(phone)
    WHERE phone_verified;

-- 2. Connected accounts
CREATE TABLE public.connections (
    user_id text NOT NULL REFERENCES public.users(user_id),
    provider text NOT NULL
        CHECK (provider IN ('canvas', 'gmail', 'gcal', 'slack', 'plaid')),
    composio_account_id text,

    -- Fernet-encrypted JSON.
    -- Canvas: {"token": "...", "ical_url": "..."}
    -- Plaid:  {"access_token": "..."}
    -- Store private feed URLs inside this encrypted value.
    secret_enc bytea,

    status text NOT NULL DEFAULT 'active',
    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (user_id, provider)
);

-- 3. Assignments, announcements, extracted actions and events
CREATE TABLE public.items (
    -- Generate a globally unique ID, not a provider's raw ID.
    item_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(user_id),

    kind text NOT NULL CHECK (kind IN (
        'assignment',
        'announcement',
        'email_action',
        'slack_action',
        'event',
        'bill'
    )),

    title text NOT NULL,
    course text,
    due_at timestamptz,

    -- Event timing for scheduling and cached results.
    -- Keep these three in sync with Item in shared/protocol.py.
    starts_at timestamptz,
    ends_at timestamptz,
    location text,

    est_hours real
        CHECK (est_hours >= 0 AND est_hours < 'Infinity'::real),

    source text NOT NULL CHECK (source IN (
        'canvas',
        'canvas_ical',
        'gmail',
        'slack',
        'gcal',
        'sundevilsync',
        'plaid'
    )),

    -- Namespace IDs, e.g. assignment:123 or announcement:123.
    -- Include channel/calendar identifiers where needed.
    source_id text NOT NULL,
    source_url text,
    quote text,

    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'submitted', 'done', 'snoozed')),

    cost_usd numeric(12,2)
        CHECK (cost_usd >= 0 AND cost_usd <> 'NaN'::numeric),

    fetched_at timestamptz NOT NULL DEFAULT now(),

    UNIQUE (user_id, source, source_id),
    UNIQUE (user_id, item_id),

    -- >= so zero-length calendar or feed events don't fail a batch upsert.
    CHECK (
        ends_at IS NULL
        OR (starts_at IS NOT NULL AND ends_at >= starts_at)
    )
);

CREATE INDEX items_user_due_idx
    ON public.items(user_id, due_at);

CREATE INDEX items_user_start_idx
    ON public.items(user_id, starts_at);

-- 4. Proposed plans and approval state
CREATE TABLE public.plans (
    plan_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(user_id),

    -- Array of proposed actions, each containing an action_id that is
    -- unique within the plan.
    actions jsonb NOT NULL
        CHECK (jsonb_typeof(actions) = 'array'),

    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'cancelled', 'done')),

    created_at timestamptz NOT NULL DEFAULT now(),
    approved_at timestamptz,
    executed_at timestamptz,

    UNIQUE (user_id, plan_id)
);

CREATE INDEX plans_user_status_idx
    ON public.plans(user_id, status, created_at);

-- 5. Individual action execution records
-- The application must:
--   * Check that the action belongs to the stored, approved plan.
--   * Atomically claim a pending execution before calling a provider.
--   * Reconcile unknown provider outcomes before retrying.
-- ActionResult carries only action_id, so match results using the
-- plan_id from the request (or generate action_id as a UUID anyway).
CREATE TABLE public.action_executions (
    plan_id text NOT NULL,
    action_id text NOT NULL,
    user_id text NOT NULL,

    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending',
        'running',
        'succeeded',
        'failed',
        'unknown'
    )),

    -- Store provider IDs, links and other execution receipts.
    result jsonb
        CHECK (jsonb_typeof(result) = 'object'),

    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,

    PRIMARY KEY (plan_id, action_id),

    FOREIGN KEY (user_id, plan_id)
        REFERENCES public.plans(user_id, plan_id),

    CHECK (status <> 'succeeded' OR completed_at IS NOT NULL)
);

CREATE INDEX action_executions_plan_idx
    ON public.action_executions(user_id, plan_id);

-- 6. Morning briefs and letter-to-item mappings
CREATE TABLE public.briefs (
    brief_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(user_id),
    sent_at timestamptz NOT NULL DEFAULT now(),

    -- Example: {"A": "item-id-1", "B": "item-id-2"}
    letters jsonb NOT NULL
        CHECK (jsonb_typeof(letters) = 'object')
);

CREATE INDEX briefs_user_sent_idx
    ON public.briefs(user_id, sent_at DESC);

-- 7. Nudges, reminders, replies and duplicate prevention
CREATE TABLE public.nudges (
    nudge_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(user_id),

    -- Nullable for budget nudges that do not reference an item.
    item_id text,
    plan_id text,

    -- Stable per logical nudge. Examples:
    --   overspend:2026-10-12          (the local Monday date)
    --   reminder:<plan_id>:<action_id>
    dedupe_key text NOT NULL
        CHECK (length(trim(dedupe_key)) > 0),

    created_at timestamptz NOT NULL DEFAULT now(),

    -- Scheduled sends ("remind you at 3:30"). NULL means send now.
    -- The scheduler sends rows WHERE sent_at IS NULL AND send_after <= now().
    send_after timestamptz,

    -- Set only after the message is actually sent.
    sent_at timestamptz,

    -- Use '[]' for plain reminders with no reply options.
    options jsonb NOT NULL
        CHECK (jsonb_typeof(options) IN ('array', 'object')),

    reply text,
    action_taken text,

    UNIQUE (user_id, dedupe_key),

    -- These composite foreign keys enforce matching ownership.
    FOREIGN KEY (user_id, item_id)
        REFERENCES public.items(user_id, item_id),

    FOREIGN KEY (user_id, plan_id)
        REFERENCES public.plans(user_id, plan_id)
);

CREATE INDEX nudges_user_created_idx
    ON public.nudges(user_id, created_at DESC);

CREATE INDEX nudges_scheduled_idx
    ON public.nudges(send_after)
    WHERE sent_at IS NULL AND send_after IS NOT NULL;

-- 8. Budget preferences, in USD
CREATE TABLE public.budgets (
    user_id text PRIMARY KEY REFERENCES public.users(user_id),

    monthly_income numeric(12,2) NOT NULL
        CHECK (
            monthly_income >= 0
            AND monthly_income <> 'NaN'::numeric
        ),

    savings_goal numeric(12,2) NOT NULL DEFAULT 0
        CHECK (
            savings_goal >= 0
            AND savings_goal <> 'NaN'::numeric
        ),

    -- NULL means no explicit cap; zero is a valid cap.
    -- In Python, test `weekly_cap is not None`, not truthiness.
    weekly_cap numeric(12,2)
        CHECK (
            weekly_cap >= 0
            AND weekly_cap <> 'NaN'::numeric
        )
);

-- 9. Recurring monthly bills, in USD
CREATE TABLE public.bills (
    user_id text NOT NULL REFERENCES public.users(user_id),
    name text NOT NULL,

    amount numeric(12,2) NOT NULL
        CHECK (amount >= 0 AND amount <> 'NaN'::numeric),

    -- Application should clamp to month-end in shorter months.
    due_day integer NOT NULL
        CHECK (due_day BETWEEN 1 AND 31),

    PRIMARY KEY (user_id, name)
);

-- 10. Stripe purchases (Pro 30-day pass, Finals pack)
-- On CommitPayment, after confirming the session is paid with Stripe:
--   INSERT ... ON CONFLICT (stripe_session_id) DO NOTHING RETURNING 1;
-- Only if a row came back, grant the product. For Pro:
--   pro_until = GREATEST(COALESCE(pro_until, now()), now()) + INTERVAL '30 days'
-- A replayed CommitPayment then can't extend Pro twice.
CREATE TABLE public.purchases (
    stripe_session_id text PRIMARY KEY,
    user_id text NOT NULL REFERENCES public.users(user_id),

    product text NOT NULL
        CHECK (product IN ('pro_30d', 'finals_pack')),

    amount_usd numeric(12,2) NOT NULL
        CHECK (amount_usd >= 0 AND amount_usd <> 'NaN'::numeric),

    paid_at timestamptz NOT NULL DEFAULT now(),

    -- Set when the Finals pack run is used. NULL means unused.
    consumed_at timestamptz,

    CHECK (consumed_at IS NULL OR product = 'finals_pack')
);

CREATE INDEX purchases_user_idx
    ON public.purchases(user_id, product, paid_at DESC);

-- 11. Imported financial transactions
--
-- How importers must set ts, on every import:
--   * Use Plaid's datetime when present.
--   * Otherwise Plaid gives a date only. Store it as 12:00 America/Phoenix:
--       datetime.combine(d, time(12), tzinfo=ZoneInfo("America/Phoenix"))
--     Never insert the bare date: in a UTC session it becomes 17:00 the
--     previous day in Phoenix, which moves Monday purchases into the
--     previous week.
--   * If Plaid reports a modified transaction with a different date,
--     DELETE the old row by (user_id, txn_id) and INSERT the new one in
--     the same database transaction. The primary key can't catch a
--     changed ts.
--   * Removed transactions: UPDATE ... SET is_removed = true
--     WHERE user_id = $1 AND txn_id = $2.
CREATE TABLE public.transactions (
    user_id text NOT NULL REFERENCES public.users(user_id),
    ts timestamptz NOT NULL,
    txn_id text NOT NULL,

    -- Plaid convention: positive = money out; negative = money in/refund.
    amount numeric(12,2) NOT NULL
        CHECK (amount <> 'NaN'::numeric),

    -- Importers must supply the actual source currency.
    -- This MVP supports USD only.
    currency text NOT NULL
        CHECK (currency = 'USD'),

    merchant text,
    category text,                       -- Plaid personal_finance_category.primary
    pending boolean NOT NULL DEFAULT false,

    -- Mark removed source records instead of counting them.
    is_removed boolean NOT NULL DEFAULT false,

    -- TimescaleDB requires the partitioning column in the primary key.
    PRIMARY KEY (user_id, ts, txn_id)
);

-- Convert transactions to a TimescaleDB hypertable.
SELECT create_hypertable(
    'public.transactions',
    by_range('ts')
);

CREATE INDEX transactions_user_txn_idx
    ON public.transactions(user_id, txn_id);

-- Weekly flexible spending:
--   * Monday-to-Monday in America/Phoenix.
--   * Positive, posted USD expenses only.
--   * Excludes income, transfers, rent/utilities and loan payments.
--   * Includes uncategorized expenses.
--   * Refunds do not reduce this MVP's gross-spending metric.
--
-- flexible_spent_this_month() below uses the same rules.
-- Change both together.
CREATE MATERIALIZED VIEW public.weekly_spend
WITH (
    timescaledb.continuous,
    timescaledb.materialized_only = false
) AS
SELECT
    user_id,
    time_bucket(
        INTERVAL '7 days',
        ts,
        'America/Phoenix'::text
    ) AS week,
    SUM(amount) AS spent
FROM public.transactions
WHERE amount > 0
  AND currency = 'USD'
  AND NOT pending
  AND NOT is_removed
  AND COALESCE(category, 'UNCATEGORIZED') NOT IN (
      'INCOME',
      'TRANSFER_IN',
      'TRANSFER_OUT',
      'RENT_AND_UTILITIES',
      'LOAN_PAYMENTS'
  )
GROUP BY
    user_id,
    time_bucket(
        INTERVAL '7 days',
        ts,
        'America/Phoenix'::text
    )
WITH NO DATA;

-- Refresh completed historical buckets every 15 minutes.
-- Real-time aggregation includes the current unmaterialized week.
SELECT add_continuous_aggregate_policy(
    'public.weekly_spend',
    start_offset => INTERVAL '1 month',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '15 minutes'
);

-- Flexible spend so far this month, with exactly the weekly_spend rules.
-- The month starts in America/Phoenix, not the session time zone.
-- Call from db/repo.py: SELECT public.flexible_spent_this_month($1)
CREATE FUNCTION public.flexible_spent_this_month(p_user_id text)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(SUM(amount), 0)
    FROM public.transactions
    WHERE user_id = p_user_id
      AND ts >= date_trunc('month', now(), 'America/Phoenix')
      AND amount > 0
      AND currency = 'USD'
      AND NOT pending
      AND NOT is_removed
      AND COALESCE(category, 'UNCATEGORIZED') NOT IN (
          'INCOME',
          'TRANSFER_IN',
          'TRANSFER_OUT',
          'RENT_AND_UTILITIES',
          'LOAN_PAYMENTS'
      );
$$;

COMMIT;

-- After loading Plaid sandbox history, materialize weeks older than the
-- policy's one-month window once. Run outside a transaction block:
--
-- CALL refresh_continuous_aggregate(
--     'public.weekly_spend', NULL, now() - INTERVAL '1 hour');
