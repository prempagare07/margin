# Margin

A chief-of-staff agent for college students, with ASI:One chat and iMessage/SMS reminders.

This repository currently contains the project structure and placeholder files from the LA Hacks plan and Alex's steps. Implementation follows Alex's action items.

```text
margin/
├── shared/
│   └── protocol.py          # Shared message contract (Alex + Prem)
├── agents/
│   ├── bureau.py            # Starts all five agents (Prem)
│   ├── margin_agent.py      # Orchestrator (Prem)
│   ├── academic.py          # Academic specialist (Alex)
│   ├── inbox.py             # Inbox specialist (Alex)
│   ├── planner.py           # Planner specialist (Alex)
│   └── life.py              # Life specialist (Alex)
├── core/
│   ├── chat.py              # Acknowledgements and selection parsing (Prem)
│   ├── cards.py             # Card builders (Prem)
│   ├── graph.py             # LangGraph orchestration (Prem)
│   ├── approval.py          # Stored plans and approved execution (Prem)
│   └── llm.py               # Step 5 and ASI1 fallback (Prem)
├── channels/
│   ├── photon.py            # Texting adapter, briefs and questions (Prem)
│   └── scheduler.py         # Morning brief and evening sweep (Prem)
├── integrations/
│   ├── canvas_client.py     # Canvas API and iCal fallback (Devin / Alex)
│   ├── events_feed.py       # SunDevilSync event feed (Devin / Alex)
│   ├── photon_client.py     # Photon API and webhook verification (Devin / Prem)
│   ├── plaid_sandbox.py     # Sandbox transaction ingestion (Devin / Alex)
│   └── composio_tools.py    # Account links and integration tools (Alex)
├── payments/
│   └── stripe_flow.py       # Payment protocol (Alex)
├── db/
│   ├── schema.sql          # Shared database schema (Alex)
│   └── repo.py             # Database queries (Alex)
├── web/
│   └── server.py           # Webhooks, connect pages and health (Alex + Prem)
└── deploy/
    ├── docker-compose.yml  # Deployment services (Devin, then Prem)
    └── Caddyfile           # TLS and reverse proxy (Devin, then Prem)
```

Python package directories also contain `__init__.py` files. Secrets belong in an ignored local `.env` file and should be shared through a password manager.
