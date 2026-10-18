# Margin — Your AI Chief of Staff

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Margin is a multi-agent AI chief of staff for college students. Talk to it in ASI:One, get your week planned, inbox triaged, and a daily SMS brief — all without leaving the conversation.

## What it does

- **7 AM brief** via iMessage/SMS: lettered action items (A, B, C…) pulled from Canvas, Gmail, Slack, and Google Calendar
- **Week planner**: finds free slots, schedules study blocks and events, writes Google Calendar entries after approval
- **Inbox triage**: surfaces action items from Gmail and Slack as interactive cards in ASI:One
- **Budget tracker**: "safe to spend this week" from Plaid transaction data
- **Campus events**: SunDevilSync feed surfaced as cards
- **Approval-first writes**: every calendar event or draft requires explicit user approval
- **Pro tier** ($4.99/30 days) via uAgents Payment Protocol + Stripe embedded checkout

## Agents

All agents implement the Agent Chat Protocol (ACP) and are registered on Agentverse under Innovation Lab.

| Agent | Address | Role |
|---|---|---|
| `margin` | `agent1qdfscf9rpmmpq40uyd694uz6dve2e0jza02s5dez07lk5wnf3dnn6cnndt4` | Orchestrator — cross-source planning, briefs, payments |
| `margin-academic` | `agent1qgv4tqnwkq904u3n99ar65ypgkakge7954ycsqvwv46aj5l0p75wwy6r5ly` | Canvas assignments, deadlines, announcements |
| `margin-inbox` | `agent1q0sx7tpzp20hftlf0g4d0z4vy5ec3qlse25rpdq3kqjpsu4egupqcr6pl75` | Gmail + Slack action items |
| `margin-planner` | `agent1q02acypnywajw6klm9mnzgxy4j59t2egtx80vv48q9av64cv8ck6kcd6vpa` | Google Calendar + free-slot scheduling |
| `margin-life` | `agent1qfpdgnuqh5e9vnqumvrm2hj9jvm8yja35af08aqvawhnykrt5c4zvl2vkxl` | Campus events + budget (Plaid sandbox) |

## Architecture

```
ASI:One / Photon SMS
        │
   margin (orchestrator)
   ├── LangGraph handle() — intent → gather → plan → review → execute
   ├── core/approval.py  — store plan, require approval, execute once
   └── ctx.send_and_receive() to specialists:
       ├── margin-academic  (Canvas REST + iCal)
       ├── margin-inbox     (Gmail + Slack via Composio)
       ├── margin-planner   (Google Calendar via Composio)
       └── margin-life      (SunDevilSync + Plaid sandbox)

Web: FastAPI on https://inappmargin.vip
  POST /webhooks/photon   — inbound SMS
  GET  /connect/canvas    — Canvas OAuth
  GET  /health            — uptime probe
```

**Models**: Step 5 Preview (StepFun) for planning/summaries · ASI1-mini for intent classification and card copy · fallback chain on every call

**Storage**: TimescaleDB on Tiger Data · plans, approvals, entitlements

**Observability**: Sentry — per-user correlation ID on every message, uptime monitor on `/health`, spans around every specialist call

## Quick start

```bash
cp .env.example .env   # fill in keys
pip install -r requirements.txt
python scripts/db_init.py
python -m agents.bureau          # all 5 agents
uvicorn web.server:app --port 8080
```

## Running tests

```bash
pytest tests/test_approval.py tests/test_e2e_flow.py tests/test_payments.py -q
```

## Deployment

Deployed on Vultr (Ubuntu 24.04) with Docker Compose + Caddy (auto-TLS):

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Live at `https://inappmargin.vip`

## Fetch.ai tracks

- ASI:One Agent Challenge — primary track
- Payment Protocol integration (Pro tier, Finals pack)
- ASI:One Interactive Cards (carousel, detail, review, form, custom)

## Team

- **Prem Pagare** — orchestrator, LangGraph, Photon SMS, cards, deployment
- **Alex** — specialist agents, Composio integrations, Canvas, Plaid, DB schema
