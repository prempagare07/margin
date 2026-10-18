-- Prem's tables: users (phone verification), plans, briefs, nudges.
-- Run after db/schema.sql once Alex adds shared tables.
-- Idempotent: safe to run twice.

create table if not exists users (
  user_id            text primary key,              -- ASI:One sender address
  phone              text unique,
  phone_verified     boolean not null default false,
  phone_code_hash    text,
  phone_code_expires timestamptz,
  plan               text not null default 'free',  -- free | pro
  pro_until          timestamptz,
  tz                 text not null default 'America/Phoenix',
  created_at         timestamptz not null default now()
);

create table if not exists plans (
  plan_id     text primary key,
  user_id     text references users,
  actions     jsonb not null,
  rows        jsonb,
  results     jsonb,
  status      text not null default 'pending',    -- pending | approved | cancelled | done
  created_at  timestamptz not null default now(),
  executed_at timestamptz
);

create table if not exists briefs (
  brief_id text primary key,
  user_id  text references users,
  sent_at  timestamptz not null default now(),
  letters  jsonb not null                          -- {"A": "<item_id>", "B": ...}
);

-- items must exist before nudges (FK)
create table if not exists items (
  item_id    text primary key,
  user_id    text references users,
  kind       text not null,
  title      text not null,
  course     text,
  due_at     timestamptz,
  est_hours  real,
  source     text not null,
  source_id  text not null,
  source_url text,
  quote      text,
  status     text not null default 'open',
  cost_usd   numeric,
  fetched_at timestamptz not null default now(),
  unique (user_id, source, source_id)
);

create table if not exists nudges (
  nudge_id     text primary key,
  user_id      text references users,
  item_id      text references items,
  sent_at      timestamptz not null default now(),
  options      jsonb not null,
  reply        text,
  action_taken text
);
