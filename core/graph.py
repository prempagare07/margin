"""LangGraph orchestration. handle() is the single entry point. Owner: Prem."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from core import approval, gather as gather_bus
from core.observability import aspan
from core.cards import (
    connect_form,
    done as done_card,
    error_card,
    priorities_carousel,
    review as review_card,
    upgrade_card,
)
from core.chat import send_text
from core.llm import fast, plan as llm_plan

logger = logging.getLogger(__name__)
FAKE = os.environ.get("FAKE_SPECIALISTS", "0") == "1"


# ---------------------------------------------------------------------------
# Fixtures (FAKE_SPECIALISTS=1)
# ---------------------------------------------------------------------------

def _fake_items() -> list[dict]:
    now = datetime.now(timezone.utc)

    def _d(days: int, hours: int = 0) -> str:
        return (now + timedelta(days=days, hours=hours)).isoformat()

    return [
        {
            "item_id": "fix-1", "user_id": "fake", "kind": "assignment",
            "title": "HW 4 — Linear Algebra", "course": "MAT 343",
            "due_at": _d(2), "est_hours": 3.0,
            "source": "canvas", "source_id": "hw4",
            "quote": "Chapter 5, problems 1-20", "status": "open",
        },
        {
            "item_id": "fix-2", "user_id": "fake", "kind": "assignment",
            "title": "Lab Report — Circuits", "course": "EEE 202",
            "due_at": _d(3), "est_hours": 2.0,
            "source": "canvas", "source_id": "lab-r",
            "quote": "Include oscilloscope screenshots", "status": "open",
        },
        {
            "item_id": "fix-3", "user_id": "fake", "kind": "assignment",
            "title": "Essay Draft — ENG 102", "course": "ENG 102",
            "due_at": _d(4), "est_hours": 1.5,
            "source": "canvas", "source_id": "essay",
            "quote": "500-word rough draft", "status": "open",
        },
        {
            "item_id": "fix-4", "user_id": "fake", "kind": "email_action",
            "title": "Midterm review — BYAC 110",
            "due_at": _d(1, 3), "est_hours": None,
            "source": "gmail", "source_id": "ta-email",
            "quote": "TA: review session tomorrow at noon in BYAC 110",
            "status": "open",
        },
    ]


def _fake_slots() -> list[list[str]]:
    base = datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
    return [
        [
            (base + timedelta(days=d)).isoformat(),
            (base + timedelta(days=d, hours=3)).isoformat(),
        ]
        for d in range(1, 6)
    ]


# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class GraphState(TypedDict):
    ctx: Any          # uAgent Context — not serialized (no checkpointer)
    sender: str
    user_id: str
    text: str
    channel: str
    selection: dict | None
    intent: str
    items: list[dict]
    free_slots: list[list[str]]
    plan_id: str | None
    actions: list[dict]


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def classify_node(state: GraphState) -> dict:
    sel = state.get("selection") or {}
    action = sel.get("action", "")
    if action:
        return {"intent": action}

    lower = (state["text"] or "").lower()
    if any(k in lower for k in ("plan", "week", "schedule", "organiz", "priorit")):
        return {"intent": "plan_week"}
    if any(k in lower for k in ("upgrade", "pro ", "finals pack", "subscribe")):
        return {"intent": "upgrade"}
    if any(k in lower for k in ("budget", "afford", "spend", "safe to")):
        return {"intent": "budget"}

    try:
        reply = await fast(
            [
                {
                    "role": "system",
                    "content": (
                        "Classify the student message into exactly one of: "
                        "plan_week, open_task, budget, upgrade, snooze, chat. "
                        "Reply with that one word only."
                    ),
                },
                {"role": "user", "content": state["text"]},
            ],
            timeout=8,
        )
        intent = (reply or "chat").strip().lower().split()[0]
        valid = {"plan_week", "open_task", "budget", "upgrade", "snooze", "chat"}
        return {"intent": intent if intent in valid else "chat"}
    except Exception:
        return {"intent": "chat"}


async def gather_node(state: GraphState) -> dict:
    """Gather items + free slots from specialists (or fixtures)."""
    async with aspan("agent.gather", "gather items", user_id=state["user_id"]) as s:
        if FAKE or not _is_agent_ctx(state["ctx"]):
            items, slots = _fake_items(), _fake_slots()
        else:
            items, slots = await _gather_from_specialists(
                state["ctx"], state["user_id"], state["sender"]
            )
        if s:
            s.set_data("item_count", len(items))
        return {"items": items, "free_slots": slots}


def _is_agent_ctx(ctx: Any) -> bool:
    """True when ctx is a real uAgent Context (not PhotonCtx)."""
    return hasattr(ctx, "_agent") or hasattr(ctx, "address")


async def _gather_from_specialists(ctx: Any, user_id: str, sender: str) -> tuple[list, list]:
    """Send GatherRequest to all 4 specialists; collect results with 15s timeout."""
    import asyncio
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from shared.protocol import GatherRequest

    # Load addresses from file (written by bureau._print_addresses)
    addresses = _load_addresses()
    if not addresses:
        logger.warning("gather: no addresses.json — falling back to fixtures")
        return _fake_items(), _fake_slots()

    now = datetime.now(timezone.utc)
    window_start = now.isoformat()
    window_end = (now + timedelta(days=7)).isoformat()

    specialist_keys = ["margin-academic", "margin-inbox", "margin-planner", "margin-life"]
    futures: dict[str, asyncio.Future] = {}

    for key in specialist_keys:
        addr = addresses.get(key)
        if not addr:
            continue
        req_id = str(uuid4())
        fut = gather_bus.register(req_id)
        futures[req_id] = fut
        try:
            await ctx.send(addr, GatherRequest(
                request_id=req_id,
                user_id=user_id,
                window_start=window_start,
                window_end=window_end,
            ))
        except Exception as exc:
            logger.warning("gather: failed to send to %s: %s", key, exc)
            gather_bus.resolve(req_id, None)

    all_items: list = []
    all_slots: list = []

    for req_id, fut in futures.items():
        try:
            result = await asyncio.wait_for(fut, timeout=15)
            if result and not result.error:
                all_items.extend(result.items or [])
                all_slots.extend(result.free_slots or [])
            elif result and result.error:
                logger.warning("gather: specialist error: %s", result.error)
        except asyncio.TimeoutError:
            logger.warning("gather: specialist timed out for request %s", req_id)

    if not all_items:
        logger.info("gather: no real items collected — using fixtures")
        return _fake_items(), _fake_slots()

    return all_items, all_slots or _fake_slots()


def _load_addresses() -> dict:
    """Load agent addresses from agents/addresses.json."""
    import json
    try:
        with open("agents/addresses.json") as f:
            return json.load(f)
    except Exception:
        return {}


async def _execute_action(
    ctx: Any, user_id: str, plan_id: str, action: dict, addresses: dict
) -> tuple[str | None, str | None]:
    """Send an ActionRequest to the right specialist and await ActionResult.

    Returns (link, error). Falls back to fake calendar link when FAKE or no ctx.
    """
    import asyncio
    from uuid import uuid4
    from shared.protocol import ActionRequest

    action_type = action.get("type", "create_event")
    raw_params = action.get("params") or {}

    # Route: create_event/add_reminder → planner, create_draft → inbox
    if action_type == "create_draft":
        specialist = "margin-inbox"
        params = {k: raw_params.get(k, "") for k in ("to", "subject", "body")}
    else:
        specialist = "margin-planner"
        params = {
            "title": raw_params.get("title", "Margin block"),
            "start_iso": raw_params.get("start_iso", ""),
            "end_iso": raw_params.get("end_iso", ""),
            "description": f"Scheduled by Margin (plan {plan_id})",
        }

    if FAKE or not _is_agent_ctx(ctx):
        return "https://calendar.google.com", None

    addr = addresses.get(specialist)
    if not addr:
        return "https://calendar.google.com", None

    aid = action["action_id"]
    fut = gather_bus.register(aid)
    try:
        await ctx.send(addr, ActionRequest(
            action_id=aid,
            plan_id=plan_id,
            user_id=user_id,
            type=action_type,
            params=params,
        ))
        result = await asyncio.wait_for(fut, timeout=15)
        if result and result.ok:
            return result.link or "https://calendar.google.com", None
        return None, result.error if result else "no response"
    except asyncio.TimeoutError:
        return None, "specialist timed out"
    except Exception as exc:
        return None, str(exc)


class _ActionParams(BaseModel):
    title: str
    item_id: str
    start_iso: str
    end_iso: str
    summary_row: str = ""


class _Action(BaseModel):
    action_id: str
    type: str
    params: _ActionParams


class _PlanOut(BaseModel):
    actions: list[_Action]


async def plan_week_node(state: GraphState) -> dict:
    """Call Step 5 to schedule items into free slots."""
    items = state["items"]
    slots = state["free_slots"]

    item_lines = "\n".join(
        f"- [{it['item_id']}] {it['title']}"
        + (f" (due {it['due_at'][:10]})" if it.get("due_at") else "")
        + (f" ~{it['est_hours']}h" if it.get("est_hours") else "")
        for it in items[:8]
    )
    slot_lines = "\n".join(
        f"- {s[0][:16].replace('T', ' ')} → {s[1][:16].replace('T', ' ')}"
        for s in slots[:10]
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are a student planner. Assign each open item to a free slot "
                "and return a JSON object {\"actions\": [...]}. Each action: "
                "{action_id (unique, e.g. 'a1'), type ('create_event'), "
                "params: {title, item_id, start_iso (ISO 8601 UTC), end_iso, "
                "summary_row (≤50 chars for review card)}}. Max 8 actions."
            ),
        },
        {
            "role": "user",
            "content": f"Open items:\n{item_lines}\n\nFree slots:\n{slot_lines}",
        },
    ]

    try:
        result, model = await llm_plan(messages, schema=_PlanOut, timeout=30)
        actions = [a.model_dump() for a in result.actions[:8]]
        logger.info("plan_week: %d actions via %s", len(actions), model)
    except Exception as exc:
        logger.warning("plan_week LLM failed: %s — using fallback", exc)
        actions = []
        for i, item in enumerate(items[:5]):
            slot = slots[i] if i < len(slots) else None
            actions.append({
                "action_id": f"a{i + 1}",
                "type": "create_event",
                "params": {
                    "title": f"Work on: {item['title']}",
                    "item_id": item["item_id"],
                    "start_iso": slot[0] if slot else datetime.now(timezone.utc).isoformat(),
                    "end_iso": slot[1] if slot else datetime.now(timezone.utc).isoformat(),
                    "summary_row": item["title"][:50],
                },
            })

    return {"actions": actions}


async def store_plan_node(state: GraphState) -> dict:
    """Save plan to DB and send review card (ASI:One) or text prompt (Photon)."""
    ctx = state["ctx"]
    sender = state["sender"]
    channel = state["channel"]
    user_id = state["user_id"]
    actions = state["actions"]

    plan_id = await approval.create_plan(user_id, actions)

    rows = [
        {
            "label": (a.get("params") or {}).get("summary_row") or (a.get("params") or {}).get("title", a.get("action_id", "")),
            "value": ((a.get("params") or {}).get("start_iso", ""))[:16].replace("T", " "),
        }
        for a in actions
    ]

    if channel == "asi1":
        await ctx.send(sender, review_card(plan_id, rows))
    else:
        lines = "\n".join(f"• {r['label']} ({r['value']})" for r in rows)
        await send_text(
            ctx, sender,
            f"Here's your plan:\n{lines}\n\nReply YES to approve or NO to cancel.",
        )

    return {"plan_id": plan_id}


async def execute_node(state: GraphState) -> dict:
    """Approve and execute a pending plan."""
    ctx = state["ctx"]
    sender = state["sender"]
    channel = state["channel"]
    user_id = state["user_id"]
    sel = state.get("selection") or {}

    plan_id = sel.get("plan_id", "")
    if not plan_id:
        # Photon YES path
        pending = await approval.pending_plan_for(user_id)
        if pending:
            plan_id = pending["plan_id"]
        else:
            await send_text(ctx, sender, "No pending plan found. Say 'plan my week' to start.")
            return {}

    ok = await approval.approve(plan_id, user_id)
    if not ok:
        await send_text(ctx, sender, "Couldn't approve that plan (already done or cancelled).")
        return {}

    plan = await approval.get_plan(plan_id, user_id)
    if not plan:
        return {}

    raw = plan["actions"]
    actions: list[dict] = raw if isinstance(raw, list) else json.loads(raw)

    addresses = _load_addresses()
    results: list[dict] = []
    for action in actions:
        aid = action["action_id"]
        if not await approval.claim_action(plan_id, aid, user_id):
            continue
        link, error = await _execute_action(ctx, user_id, plan_id, action, addresses)
        await approval.complete_action(plan_id, aid, user_id, {"link": link}, error)
        if link:
            p = action.get("params") or {}
            results.append({"label": p.get("title", action.get("action_id", "Event")), "link": link})

    await approval.mark_done(plan_id, user_id)

    if channel == "asi1":
        await ctx.send(sender, done_card(results))
    else:
        await send_text(ctx, sender, f"Done! Blocked {len(results)} slot(s) in your calendar.")

    return {}


async def cancel_node(state: GraphState) -> dict:
    ctx = state["ctx"]
    sender = state["sender"]
    user_id = state["user_id"]
    sel = state.get("selection") or {}
    plan_id = sel.get("plan_id", "")

    if not plan_id:
        pending = await approval.pending_plan_for(user_id)
        if pending:
            plan_id = pending["plan_id"]

    if plan_id:
        await approval.cancel(plan_id, user_id)

    await send_text(ctx, sender, "Plan cancelled. Say 'plan my week' whenever you're ready.")
    return {}


async def priorities_node(state: GraphState) -> dict:
    ctx = state["ctx"]
    sender = state["sender"]
    channel = state["channel"]
    items = state.get("items") or _fake_items()

    if channel == "asi1":
        await ctx.send(sender, priorities_carousel(items[:5]))
    else:
        lines = "\n".join(
            f"{chr(65 + i)}. {it['title']}" for i, it in enumerate(items[:5])
        )
        await send_text(ctx, sender, f"Your top priorities:\n{lines}")
    return {}


async def upgrade_node(state: GraphState) -> dict:
    ctx = state["ctx"]
    sender = state["sender"]
    channel = state["channel"]
    sel = state.get("selection") or {}
    tier = sel.get("tier")

    # No tier yet — show the upgrade card
    if not tier or channel != "asi1":
        if channel == "asi1":
            await ctx.send(sender, upgrade_card())
        else:
            await send_text(
                ctx, sender,
                "Pro is $4.99 / 30 days. Finals Pack is $1.99 (two weeks during finals). "
                "Open ASI:One to upgrade.",
            )
        return {}

    # Tier selected — create Stripe checkout and send RequestPayment
    from uagents_core.contrib.protocols.payment import Funds, RequestPayment
    from payments.stripe_flow import TIERS, create_checkout

    t = TIERS.get(tier)
    if not t:
        await send_text(ctx, sender, f"Unknown tier: {tier!r}")
        return {}

    try:
        checkout = await create_checkout(
            user_id=state["user_id"],
            tier=tier,
            session_ref=sender,
        )
    except Exception as exc:
        logger.warning("upgrade: checkout failed: %s", exc)
        await send_text(ctx, sender, "Couldn't start checkout right now. Please try again.")
        return {}

    amount = f"{t['amount_cents'] / 100:.2f}"
    req = RequestPayment(
        accepted_funds=[Funds(currency="USD", amount=amount, payment_method="stripe")],
        recipient=str(getattr(ctx, "address", sender)),
        deadline_seconds=1800,
        reference=sender,
        description=f"{t['name']} — ${amount}",
        metadata={"stripe": checkout, "service": f"margin_{tier}"},
    )
    await ctx.send(sender, req)
    await send_text(
        ctx, sender,
        f"Complete checkout to activate {t['name']}. I'll confirm here once paid.",
    )
    return {}


async def chat_node(state: GraphState) -> dict:
    ctx = state["ctx"]
    sender = state["sender"]
    text = state["text"]

    try:
        reply = await fast(
            [
                {
                    "role": "system",
                    "content": (
                        "You are Margin, a concise chief-of-staff for an ASU student. "
                        "Help with academics, schedule, budget, and campus life. "
                        "Keep replies under 3 sentences. "
                        "If they want to plan their week, say: just type 'plan my week'."
                    ),
                },
                {"role": "user", "content": text},
            ],
            timeout=15,
        )
    except Exception:
        reply = "I'm here! Try saying 'plan my week' to block time for your assignments."

    await send_text(ctx, sender, reply or "I'm here! Try 'plan my week'.")
    return {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route(state: GraphState) -> str:
    routes: dict[str, str] = {
        "plan_week":  "gather",
        "approve":    "execute",
        "cancel":     "cancel",
        "upgrade":    "upgrade",
        "open_task":  "priorities",
        "snooze":     "chat",
        "budget":     "chat",
        "chat":       "chat",
    }
    return routes.get(state.get("intent", "chat"), "chat")


# ---------------------------------------------------------------------------
# Build graph once at module level
# ---------------------------------------------------------------------------

def _build() -> Any:
    g = StateGraph(GraphState)
    for name, fn in [
        ("classify",   classify_node),
        ("gather",     gather_node),
        ("plan_week",  plan_week_node),
        ("store_plan", store_plan_node),
        ("execute",    execute_node),
        ("cancel",     cancel_node),
        ("priorities", priorities_node),
        ("upgrade",    upgrade_node),
        ("chat",       chat_node),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify", _route,
        {
            "gather":     "gather",
            "execute":    "execute",
            "cancel":     "cancel",
            "upgrade":    "upgrade",
            "priorities": "priorities",
            "chat":       "chat",
        },
    )
    g.add_edge("gather",     "plan_week")
    g.add_edge("plan_week",  "store_plan")
    for terminal in ("store_plan", "execute", "cancel", "priorities", "upgrade", "chat"):
        g.add_edge(terminal, END)

    return g.compile()


_app = _build()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def handle(
    ctx: Any,
    user_id: str,
    text: str,
    channel: str,
    selection: dict | None = None,
    sender: str = "",
) -> None:
    """Single-turn orchestration. Sends all replies via ctx."""
    state: GraphState = {
        "ctx": ctx,
        "sender": sender,
        "user_id": user_id,
        "text": text or "",
        "channel": channel,
        "selection": selection,
        "intent": "",
        "items": [],
        "free_slots": [],
        "plan_id": None,
        "actions": [],
    }
    try:
        await _app.ainvoke(state)
    except Exception as exc:
        logger.exception("graph.handle error: %s", exc)
        try:
            if channel == "asi1":
                await ctx.send(sender, error_card(str(exc)[:200], "graph"))
            else:
                await send_text(ctx, sender, "Something went wrong. Please try again.")
        except Exception:
            pass
