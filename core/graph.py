"""LangGraph orchestration. handle() is the single entry point. Owner: Prem."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel

from core import approval, gather as gather_bus
from core.observability import aspan
from core.cards import (
    app_connection_card,
    connect_form,
    done as done_card,
    error_card,
    pending_connect_card,
    priorities_carousel,
    reset_confirm_card,
    review as review_card,
    summary_card,
    upgrade_card,
    welcome_card,
)
from core.chat import send_text
from core.llm import fast, plan as llm_plan

logger = logging.getLogger(__name__)


def _fake_specialists() -> bool:
    """Read at call time so tests and entrypoints can select fixture mode safely."""
    return os.environ.get("FAKE_SPECIALISTS", "0") == "1"


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
    gather_error: str | None
    history: list[dict]
    user_ctx: dict


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

async def classify_node(state: GraphState) -> dict:
    sel = state.get("selection") or {}
    action = sel.get("action", "")
    if action:
        logger.info("classify: action=%s", action)
        return {"intent": action}

    lower = (state["text"] or "").lower().strip()
    logger.info("classify: text=%r", lower[:120])

    # Greetings always go to chat — never reclassify via LLM
    if lower in {"hi", "hello", "hey", "start", "help", "get started", "setup", "set up", ""}:
        return {"intent": "chat"}

    if any(k in lower for k in ("summary", "what happened", "update me", "brief me", "what's new", "whats new", "catch me up", "catch up")):
        return {"intent": "summary"}
    if any(k in lower for k in ("reset", "clear account", "start over", "disconnect all", "delete account", "remove all")):
        return {"intent": "reset"}
    if any(k in lower for k in ("plan", "week", "schedule", "organiz", "priorit", "assign", "task", "deadline", "due")):
        return {"intent": "plan_week"}
    if any(k in lower for k in ("upgrade", "pro ", "finals pack", "subscribe")):
        return {"intent": "upgrade"}
    if any(k in lower for k in ("budget", "afford", "spend", "safe to")):
        return {"intent": "budget"}
    if any(k in lower for k in ("connect", "linked", "app", "canvas", "gmail", "slack", "calendar", "integrat")):
        return {"intent": "check_links"}

    # Build history context for LLM (last 2 turns)
    history = state.get("history") or []
    history_context = ""
    if history:
        turns = history[-2:]
        history_context = "\n".join(f"{t['role'].capitalize()}: {t['content'][:100]}" for t in turns)

    try:
        messages = [
            {
                "role": "system",
                "content": (
                    "Classify the student message into exactly one of: "
                    "plan_week, open_task, budget, upgrade, snooze, summary, reset, chat. "
                    "Reply with that one word only. "
                    "Use plan_week for any request to plan, schedule, organize, or block time. "
                    "Use summary for any request for a daily update, brief, or summary of what's happening. "
                    "Use reset for any request to disconnect all apps, clear account, or start over. "
                    "Use budget for any question about spending, affordability, or finances."
                    + (f"\n\nRecent conversation:\n{history_context}" if history_context else "")
                ),
            },
            {"role": "user", "content": state["text"]},
        ]
        reply = await fast(messages, timeout=8)
        intent = (reply or "chat").strip().lower().split()[0]
        valid = {"plan_week", "open_task", "budget", "upgrade", "snooze", "summary", "reset", "chat"}
        logger.info("classify: llm_intent=%s", intent)
        return {"intent": intent if intent in valid else "chat"}
    except Exception:
        return {"intent": "chat"}


async def gather_node(state: GraphState) -> dict:
    """Gather items + free slots from specialists (or fixtures)."""
    async with aspan("agent.gather", "gather items", user_id=state["user_id"]) as s:
        if _fake_specialists():
            items, slots, gather_error = _fake_items(), _fake_slots(), None
        else:
            items, slots, gather_error = await _gather_direct(state["user_id"])
        if s:
            s.set_data("item_count", len(items))
        logger.info("gather: item_count=%d error=%s", len(items), gather_error)
        return {"items": items, "free_slots": slots, "gather_error": gather_error}


def _is_agent_ctx(ctx: Any) -> bool:
    """True when ctx is a real uAgent Context (not PhotonCtx)."""
    return not hasattr(ctx, "_phone")  # PhotonCtx has _phone; real Context does not


async def _gather_direct(user_id: str) -> tuple[list[dict], list[list[str]], str | None]:
    """Call all specialist gather logic in-process — no mailbox round-trips."""
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from shared.protocol import GatherRequest, GatherResult

    now = datetime.now(timezone.utc)
    req = GatherRequest(
        request_id=str(uuid4()),
        user_id=user_id,
        window_start=now.isoformat(),
        window_end=(now + timedelta(days=7)).isoformat(),
    )

    results = await asyncio.gather(
        _gather_academic(req),
        _gather_inbox(req),
        _gather_planner(req),
        _gather_life(req),
        return_exceptions=True,
    )

    labels = ["academic", "inbox", "planner", "life"]
    all_items: list[dict] = []
    all_slots: list[list[str]] = []
    planner_error: str | None = None

    for label, result in zip(labels, results):
        if isinstance(result, Exception):
            logger.warning("gather_direct: %s raised %s", label, result)
            if label == "planner":
                planner_error = str(result)
            continue
        if result.error:
            logger.warning("gather_direct: %s error=%s", label, result.error)
            if label == "planner":
                planner_error = result.error
        all_items.extend(
            item.model_dump() if hasattr(item, "model_dump") else dict(item)
            for item in (result.items or [])
        )
        all_slots.extend(result.free_slots or [])

    return all_items, all_slots, planner_error


async def _gather_academic(req: Any) -> Any:
    from shared.protocol import GatherResult
    try:
        from integrations import canvas_client
        return await canvas_client.gather(req)
    except Exception as exc:
        logger.warning("gather_direct: academic: %s", exc)
        return GatherResult(request_id=req.request_id, agent="academic", error=str(exc))


async def _gather_inbox(req: Any) -> Any:
    from shared.protocol import GatherResult, Item
    from integrations.composio_tools import (
        fetch_gmail_messages, fetch_slack_messages,
        cache_items, filter_items, cached_gather_result,
        IntegrationError, item_id as make_item_id,
    )

    def _msg_to_item(msg, uid: str) -> Item:
        source = "slack" if msg.provider == "slack" else "gmail"
        return Item(
            item_id=make_item_id(uid, source, msg.source_id),
            user_id=uid,
            kind="slack_action" if source == "slack" else "email_action",
            title=msg.subject or "(no subject)",
            source=source,
            source_id=msg.source_id,
            source_url=getattr(msg, "source_url", None),
            quote=(msg.body[:200].strip() if getattr(msg, "body", None) else None),
            status="open",
        )

    items: list = []
    for fetch_fn, provider in (
        (fetch_gmail_messages, "gmail"),
        (fetch_slack_messages, "slack"),
    ):
        try:
            batch = await fetch_fn(req.user_id, since=req.window_start, until=req.window_end)
            if batch.records:
                acct = batch.records[0].account_id
                raw = [_msg_to_item(m, req.user_id) for m in batch.records]
                cached = await cache_items(req.user_id, provider, acct, raw)
                items.extend(filter_items(cached, start=req.window_start, end=req.window_end))
        except IntegrationError as exc:
            fallback = await cached_gather_result(req, provider, error=exc)
            items.extend(fallback.items or [])
        except Exception as exc:
            logger.warning("gather_direct: inbox %s: %s", provider, exc)
    return GatherResult(request_id=req.request_id, agent="inbox", items=items)


async def _gather_planner(req: Any) -> Any:
    from shared.protocol import GatherResult
    from integrations.composio_tools import (
        auto_select_calendars, fetch_calendar_events, compute_free_slots,
        cache_items, filter_items, cached_gather_result,
        IntegrationError,
    )
    from db import repo

    async def _fetch() -> GatherResult:
        batch = await fetch_calendar_events(req.user_id, start=req.window_start, end=req.window_end)
        connection = await repo.get_connection(req.user_id, "gcal")
        account_id = (connection or {}).get("composio_account_id")
        if not account_id:
            raise IntegrationError("NOT_CONNECTED", "gcal")
        raw = [r.item if hasattr(r, "item") else r for r in batch.records]
        items = await cache_items(req.user_id, "gcal", account_id, raw)
        items = filter_items(items, start=req.window_start, end=req.window_end)
        free_slots = (
            compute_free_slots(batch.records, start=req.window_start,
                               end=req.window_end, tz="America/Phoenix")
            if batch.complete else []
        )
        return GatherResult(request_id=req.request_id, agent="planner",
                            items=items, free_slots=free_slots)

    try:
        return await _fetch()
    except IntegrationError as exc:
        if getattr(exc, "code", "") == "CALENDAR_SELECTION_REQUIRED":
            # Auto-select the primary calendar and retry once
            logger.info("gather_direct: planner: auto-selecting calendars for %s", req.user_id[:20])
            try:
                await auto_select_calendars(req.user_id)
                return await _fetch()
            except Exception as retry_exc:
                logger.warning("gather_direct: planner auto-select retry failed: %s", retry_exc)
        result = await cached_gather_result(req, "gcal", error=exc)
        return result
    except Exception as exc:
        logger.warning("gather_direct: planner: %s", exc)
        return GatherResult(request_id=req.request_id, agent="planner", error=str(exc))


async def _gather_life(req: Any) -> Any:
    from shared.protocol import GatherResult
    items: list = []
    errors: list[str] = []
    try:
        from integrations.events_feed import fetch_events
        items.extend(await fetch_events(req.user_id, req.window_end))
    except Exception as exc:
        logger.warning("gather_direct: life events: %s", exc)
        errors.append("Campus events unavailable.")
    try:
        from integrations.plaid_sandbox import fetch_bills, sync_transactions, PlaidError
        items.extend(await fetch_bills(req.user_id))
        synced = await sync_transactions(req.user_id)
        if not synced.ready:
            errors.append("Bank transactions still updating.")
    except Exception as exc:
        logger.warning("gather_direct: life plaid: %s", exc)
        errors.append("Bank transactions unavailable.")
    return GatherResult(request_id=req.request_id, agent="life", items=items,
                        error=" ".join(errors) or None)


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
) -> Any:
    """Send an ActionRequest to the right specialist and await ActionResult.

    Returns the specialist's ActionResult. Fixture links exist only in fake mode.
    """
    import asyncio
    from shared.protocol import ActionRequest, ActionResult

    action_type = action.get("type", "create_event")
    raw_params = action.get("params") or {}

    # Route: create_event/add_reminder → planner, create_draft → inbox
    if action_type == "create_draft":
        specialist = "margin-inbox"
    else:
        specialist = "margin-planner"

    if _fake_specialists():
        return ActionResult(
            action_id=action["action_id"], ok=True,
            link="https://calendar.google.com", data={"execution_status": "succeeded"},
        )
    if not _is_agent_ctx(ctx):
        return ActionResult(
            action_id=action["action_id"], ok=False, error="SPECIALIST_UNAVAILABLE",
        )

    addr = addresses.get(specialist)
    if not addr:
        return ActionResult(
            action_id=action["action_id"], ok=False, error="SPECIALIST_UNAVAILABLE",
        )

    aid = action["action_id"]
    fut = gather_bus.register(aid)
    try:
        await ctx.send(addr, ActionRequest(
            action_id=aid,
            plan_id=plan_id,
            user_id=user_id,
            type=action_type,
            params=raw_params,
        ))
        result = await asyncio.wait_for(fut, timeout=15)
        return result or ActionResult(action_id=aid, ok=False, error="NO_RESPONSE")
    except asyncio.TimeoutError:
        return ActionResult(action_id=aid, ok=False, error="SPECIALIST_TIMEOUT")
    except Exception:
        return ActionResult(action_id=aid, ok=False, error="SPECIALIST_UNAVAILABLE")


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
    # Only schedule Canvas/Gmail/Slack items — GCal events are just constraints
    items = [it for it in state["items"] if it.get("source") != "gcal"]
    # Deduplicate by source_id (same assignment from multiple syncs)
    seen_src: set[str] = set()
    deduped: list[dict] = []
    for it in items:
        key = it.get("source_id") or it.get("item_id", "")
        if key not in seen_src:
            seen_src.add(key)
            deduped.append(it)
    items = deduped
    slots = state["free_slots"]
    # Only abort if we have nothing to work with
    if not items or not slots:
        return {"actions": []}

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
                "summary_row (≤50 chars for review card)}}. Max 8 actions. "
                "Create at most one study block per item. Do not repeat the same item_id in multiple actions."
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
            if i >= len(slots):
                break
            slot = slots[i]
            actions.append({
                "action_id": f"a{i + 1}",
                "type": "create_event",
                "params": {
                    "title": f"Work on: {item['title']}",
                    "item_id": item["item_id"],
                    "start_iso": slot[0],
                    "end_iso": slot[1],
                    "summary_row": item["title"][:50],
                },
            })

    return {"actions": actions}


def _stored_calendar_actions(
    proposed: list[dict], slots: list[list[str]], *, account_id: str, calendar_id: str,
) -> list[dict]:
    """Convert model output to the exact reviewed Calendar adapter contract."""
    parsed_slots: list[tuple[datetime, datetime]] = []
    for slot in slots:
        try:
            begins = datetime.fromisoformat(slot[0].replace("Z", "+00:00"))
            ends = datetime.fromisoformat(slot[1].replace("Z", "+00:00"))
            if begins.tzinfo is not None and ends > begins:
                parsed_slots.append((begins.astimezone(timezone.utc), ends.astimezone(timezone.utc)))
        except (IndexError, TypeError, ValueError):
            continue

    result: list[dict] = []
    seen: set[str] = set()
    for action in proposed:
        params = action.get("params") if isinstance(action, dict) else None
        action_id = action.get("action_id") if isinstance(action, dict) else None
        if (not isinstance(params, dict) or not isinstance(action_id, str) or not action_id
                or action_id in seen or action.get("type") != "create_event"):
            continue
        try:
            begins = datetime.fromisoformat(params["start_iso"].replace("Z", "+00:00"))
            ends = datetime.fromisoformat(params["end_iso"].replace("Z", "+00:00"))
            if begins.tzinfo is None or ends.tzinfo is None:
                continue
            begins, ends = begins.astimezone(timezone.utc), ends.astimezone(timezone.utc)
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if ends <= begins or ends - begins < timedelta(minutes=30):
            continue
        if not any(slot_start <= begins and ends <= slot_end for slot_start, slot_end in parsed_slots):
            continue
        title = params.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        seen.add(action_id)
        result.append({
            "action_id": action_id,
            "type": "create_event",
            "params": {
                "account_id": account_id,
                "calendar_id": calendar_id,
                "title": title.strip(),
                "starts_at": begins.isoformat().replace("+00:00", "Z"),
                "ends_at": ends.isoformat().replace("+00:00", "Z"),
                "description": "Scheduled by Margin after your approval.",
                "purpose": "study_block",
            },
        })
    return result


def _review_window(params: dict, tz: str) -> str:
    try:
        zone = ZoneInfo(tz)
        start = datetime.fromisoformat(params["starts_at"].replace("Z", "+00:00")).astimezone(zone)
        end = datetime.fromisoformat(params["ends_at"].replace("Z", "+00:00")).astimezone(zone)
        return f"{start.strftime('%a %-m/%-d, %-I:%M %p')}–{end.strftime('%-I:%M %p')}"
    except (KeyError, TypeError, ValueError):
        return "Time unavailable"


async def store_plan_node(state: GraphState) -> dict:
    """Save plan to DB and send review card (ASI:One) or text prompt (Photon)."""
    ctx = state["ctx"]
    sender = state["sender"]
    channel = state["channel"]
    user_id = state["user_id"]
    if state.get("gather_error"):
        await send_text(ctx, sender, "Margin couldn't read your calendar. Check the Google Calendar connection and try again.")
        return {}
    if not state.get("items"):
        await send_text(ctx, sender, "Margin didn't find any open items to schedule this week.")
        return {}
    if not state.get("free_slots"):
        await send_text(ctx, sender, "Margin couldn't find an open calendar slot this week.")
        return {}

    from db import repo
    if _fake_specialists():
        account_id, calendar_id, tz = "fixture-account", "primary", "America/Phoenix"
    else:
        connection = await repo.get_connection(user_id, "gcal")
        settings = ((connection or {}).get("secret") or {}).get("settings") or {}
        account_id = (connection or {}).get("composio_account_id")
        calendar_id = settings.get("write_calendar_id")
        if (not connection or connection.get("status") != "active"
                or not isinstance(account_id, str) or not isinstance(calendar_id, str)):
            await send_text(ctx, sender, "Connect Google Calendar and choose a writable calendar before planning your week.")
            return {}
        await repo.upsert_user(user_id)
        user = await repo.get_user(user_id)
        tz = (user or {}).get("tz") or "America/Phoenix"

    actions = _stored_calendar_actions(
        state["actions"], state["free_slots"],
        account_id=account_id, calendar_id=calendar_id,
    )
    if not actions:
        await send_text(ctx, sender, "Margin couldn't build a valid study block inside your free calendar slots.")
        return {}

    plan_id = await approval.create_plan(user_id, actions)

    rows = [
        {
            "label": (a.get("params") or {}).get("title", a.get("action_id", "")),
            "value": _review_window(a.get("params") or {}, tz),
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
        action_result = await _execute_action(ctx, user_id, plan_id, action, addresses)
        params = action.get("params") or {}
        label = params.get("title", action.get("action_id", "Event"))
        results.append({
            "label": label,
            "link": action_result.link if action_result.ok else None,
            "error": None if action_result.ok else (action_result.error or "ACTION_FAILED"),
        })

    try:
        await asyncio.wait_for(approval.mark_done(plan_id, user_id), timeout=12)
    except Exception:
        pass  # DB hiccup — don't block the done card

    if channel == "asi1":
        await ctx.send(sender, done_card(results))
    else:
        succeeded = sum(not result["error"] for result in results)
        failed = [result["label"] for result in results if result["error"]]
        message = f"Created {succeeded} calendar block(s)."
        if failed:
            message += " Failed: " + ", ".join(failed) + "."
        await send_text(ctx, sender, message)

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


async def summary_node(state: GraphState) -> dict:
    """Gather all sources and return a rich daily brief on demand."""
    ctx = state["ctx"]
    sender = state["sender"]
    user_id = state["user_id"]
    channel = state["channel"]

    await send_text(ctx, sender, "Pulling your updates — one moment...")

    try:
        items, slots, _ = await _gather_direct(user_id)
    except Exception:
        items, slots = [], []

    if not items:
        await send_text(ctx, sender, "Nothing to report right now — connect Canvas, Gmail, or Slack to get started.")
        return {}

    now = datetime.now(timezone.utc)
    item_lines = []
    for it in items[:15]:
        line = f"• [{it.get('source','?').upper()}] {it['title']}"
        if it.get("due_at"):
            try:
                due = datetime.fromisoformat(it["due_at"].replace("Z", "+00:00"))
                diff = (due - now).days
                line += f" — due in {diff}d" if diff >= 0 else " — OVERDUE"
            except Exception:
                pass
        if it.get("quote"):
            line += f"\n  {it['quote'][:100]}"
        item_lines.append(line)

    prompt_items = "\n".join(item_lines)
    try:
        reply = await fast(
            [
                {
                    "role": "system",
                    "content": (
                        f"You are Margin, a proactive chief-of-staff for an ASU student. "
                        f"Today is {now.strftime('%A, %B %-d')}. "
                        "Given the student's current items from Canvas, Gmail, Slack, and Calendar, "
                        "write a concise daily brief in 4-6 bullet points. "
                        "Lead with what's most urgent. Mention deadlines, pending emails needing action, "
                        "and any schedule conflicts. Be specific with names, dates, and courses. "
                        "End with one proactive suggestion (e.g. 'Want me to block time for X?')."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Here are my current items:\n\n{prompt_items}",
                },
            ],
            timeout=20,
        )
    except Exception:
        reply = None

    if channel == "asi1":
        await ctx.send(sender, summary_card(reply or "\n".join(item_lines[:6]), items[:6], len(slots)))
    else:
        await send_text(ctx, sender, reply or f"Here's what's on your plate:\n\n{chr(10).join(item_lines[:6])}")

    if reply:
        try:
            from db import repo
            await repo.save_message(user_id, "assistant", reply[:500], intent="summary", channel=channel)
        except Exception:
            pass
    return {}


async def reset_node(state: GraphState) -> dict:
    """Clear all connections and user data for a fresh start."""
    ctx = state["ctx"]
    sender = state["sender"]
    user_id = state["user_id"]

    sel = state.get("selection") or {}
    if sel.get("action") == "reset_confirm":
        try:
            from db import repo
            from integrations.composio import runtime as composio_runtime
            # Invalidate cached Composio sessions so OAuth state is clean on reconnect
            for provider in ("gmail", "gcal", "slack"):
                try:
                    composio_runtime.invalidate_sessions(user_id, provider)
                except Exception:
                    pass
            # Delete all user data from DB
            await repo.delete_user(user_id)
            logger.info("reset: deleted user_id=%s", user_id)
        except Exception as exc:
            logger.warning("reset: failed for user_id=%s: %s", user_id, exc)
            await send_text(ctx, sender, "Reset failed — please try again or contact support.")
            return {}
        await send_text(ctx, sender, "Account reset ✓ All connections cleared. Say 'hi' to start fresh.")
    else:
        await ctx.send(sender, reset_confirm_card())
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


_ONBOARDING_ORDER = ["Canvas", "Gmail", "Google Calendar", "Slack"]


async def _get_onboarding_step(user_id: str) -> list[str]:
    """Return ordered list of apps not yet connected for this user."""
    from db import repo
    from integrations import composio_tools

    remaining: list[str] = []
    try:
        row = await repo.get_connection(user_id, "canvas")
        if not (row and row.get("status") == "active"):
            remaining.append("Canvas")
    except Exception:
        remaining.append("Canvas")

    for provider, label in (("gmail", "Gmail"), ("gcal", "Google Calendar"), ("slack", "Slack")):
        try:
            st = await composio_tools.connection_status(user_id, provider)
            if st.status != "active":
                remaining.append(label)
        except Exception:
            remaining.append(label)

    return remaining


async def _check_single_app(user_id: str, app_name: str) -> bool:
    """Return True if the named app is fully connected."""
    from db import repo
    from integrations import composio_tools

    if app_name == "Canvas":
        try:
            row = await repo.get_connection(user_id, "canvas")
            return bool(row and row.get("status") == "active")
        except Exception:
            return False

    _prov = {"Gmail": "gmail", "Google Calendar": "gcal", "Slack": "slack"}
    provider = _prov.get(app_name)
    if not provider:
        return False
    try:
        st = await composio_tools.connection_status(user_id, provider)
        if st.status == "active":
            if provider == "gcal" and not st.settings.get("calendar_ids"):
                try:
                    await composio_tools.auto_select_calendars(user_id)
                except Exception:
                    pass
            return True
        return False
    except Exception:
        return False


async def _send_link_card(ctx: Any, sender: str, user_id: str) -> None:
    from core.cards import link_buttons
    from integrations import composio_tools
    from web.connect_state import make_connect_url

    links: dict[str, str] = {}
    try:
        links["Canvas"] = make_connect_url(user_id)
    except Exception as exc:
        logger.warning("connect: canvas link failed code=%s", getattr(exc, "code", "CONFIGURATION_ERROR"))
    for provider, label in (("gmail", "Gmail"), ("gcal", "Google Calendar"), ("slack", "Slack")):
        try:
            links[label] = await composio_tools.connect_link(user_id, provider)
        except Exception as exc:
            logger.warning("connect: link failed provider=%s code=%s", provider, getattr(exc, "code", "LINK_FAILED"))
    await ctx.send(sender, link_buttons(links))


async def connect_node(state: GraphState) -> dict:
    """Save onboarding once, then walk user through apps one at a time."""
    from core import onboarding

    ctx, sender = state["ctx"], state["sender"]
    if state["channel"] != "asi1" or not sender or sender != state["user_id"]:
        await send_text(ctx, sender, "Open Margin in ASI:One to save your account details and link your phone.")
        return {}

    selection = state.get("selection") or {}
    user_id = state["user_id"]

    # "Try again" for a specific app — regenerate its link and re-show card
    if selection.get("action") == "connect_app" and "app" in selection:
        app = selection["app"]
        remaining = list(selection.get("remaining") or [])
        logger.info("connect: regenerating link for app=%s", app)
        await _send_next_app_card(ctx, sender, user_id, [app] + remaining)
        return {}

    # "Get started" click — show setup form
    if set(selection) <= {"action"}:
        await ctx.send(sender, connect_form())
        return {}

    # Form submitted — save and advance
    try:
        result = await onboarding.save(sender, selection)
    except onboarding.InvalidSubmission as exc:
        await send_text(ctx, sender, str(exc))
        await ctx.send(sender, connect_form())
        return {}
    except Exception:
        logger.warning("connect: account persistence failed")
        await send_text(ctx, sender, "Your account details could not be saved. Please try the Connect form again.")
        return {}

    if result.phone_verified:
        phone_msg = "Account saved ✓ Your phone is already linked — you'll get the 7 AM brief by text."
    else:
        try:
            from integrations.photon_client import ensure_line_assignment
            phone = onboarding.phone_number(onboarding.submission_fields(selection).get("phone"))
            assigned_number = await ensure_line_assignment(phone)
        except Exception:
            logger.warning("connect: Photon line assignment unavailable")
            phone_msg = (
                "Account saved! iMessage linking isn't available right now — "
                "you can reconnect your phone later from settings."
            )
        else:
            phone_msg = (
                "Account saved ✓\n\n"
                "To link your phone, send this iMessage to "
                f"{assigned_number} within 10 minutes:\n\nLINK {result.link_code}\n\n"
                "Once received, Margin will confirm and turn on your 7 AM briefs."
            )
    await send_text(ctx, sender, phone_msg)

    # Step through apps sequentially — one card per send to avoid rate limits
    await asyncio.sleep(2)
    remaining = await _get_onboarding_step(user_id)
    prefix = (f"Now let's connect your apps — {len(remaining)} to go. You can skip any and add them later."
              if remaining else "")
    await _send_next_app_card_with_context(ctx, sender, user_id, remaining, prefix=prefix)
    return {}


async def check_links_node(state: GraphState) -> dict:
    """Verify connection status and advance the sequential onboarding flow."""
    ctx, sender, user_id = state["ctx"], state["sender"], state["user_id"]
    sel = state.get("selection") or {}
    app = sel.get("app", "")
    remaining = list(sel.get("remaining") or [])

    # Sequential check: user tapped "Done — <App> connected ✓"
    if app:
        logger.info("check_links: verifying app=%s remaining=%s", app, remaining)
        ok = await _check_single_app(user_id, app)
        if ok:
            # Success: embed confirmation in next card's narration (one send)
            await _send_next_app_card_with_context(ctx, sender, user_id, remaining,
                prefix=f"{app} connected ✓")
        else:
            await send_text(ctx, sender,
                f"Hmm, {app} doesn't seem connected yet. "
                "Complete the authorization in the browser, then say \"check apps\" to try again.")
        return {}

    # Legacy "I've connected everything" or "are my apps connected" query
    remaining_all = await _get_onboarding_step(user_id)
    connected_names = [a for a in _ONBOARDING_ORDER if a not in remaining_all]

    if not remaining_all:
        await send_text(ctx, sender,
            "All apps connected ✓ — Canvas, Gmail, Google Calendar, Slack. "
            "Say 'plan my week' to get started!")
        return {}

    names = ", ".join(connected_names) if connected_names else "none yet"
    prefix = f"Connected: {names} ✓" if connected_names else "No apps connected yet."
    await _send_next_app_card_with_context(ctx, sender, user_id, remaining_all, prefix=prefix)
    return {}


_ALL_APPS = ["Canvas", "Gmail", "Google Calendar", "Slack"]

_APP_DESCRIPTIONS = {
    "Canvas": "Pull your assignments and deadlines into the 7 AM brief.",
    "Gmail": "Surface action items from TA emails and club announcements.",
    "Google Calendar": "Find free slots and block study time — you approve every event.",
    "Slack": "Catch action items from club channels and group chats.",
}


async def _send_app_links_card(ctx, sender: str, user_id: str,
                               connected_apps: set[str] | None = None) -> None:
    """Show link_buttons card with all app OAuth links at once.

    connected_apps: if provided, use this set; otherwise check status live.
    """
    from core.cards import link_buttons
    from integrations.composio_tools import connect_link, connection_status
    base = os.environ.get("PUBLIC_BASE_URL", "https://inappmargin.vip")

    # Check which apps are already connected if not provided
    if connected_apps is None:
        connected_apps = set()
        # apps with an existing pending/active connection in DB (don't overwrite)
        _has_pending: set[str] = set()
        try:
            from db import repo
            row = await repo.get_connection(user_id, "canvas")
            if row and row.get("status") == "active":
                connected_apps.add("Canvas")
        except Exception as exc:
            logger.warning("app_links: canvas db check failed: %s", exc)

        _prov_to_label = {"gmail": "Gmail", "gcal": "Google Calendar", "slack": "Slack"}
        for provider, label in _prov_to_label.items():
            try:
                st = await connection_status(user_id, provider)
                logger.info("app_links: %s status=%s", label, st.status)
                if st.status == "active":
                    connected_apps.add(label)
                elif st.status in ("pending", "relink_required"):
                    _has_pending.add(label)
            except Exception as exc:
                logger.warning("app_links: %s status check failed code=%s", label,
                               getattr(exc, "code", exc))
    else:
        _has_pending = set()

    # Build links only for unconnected apps
    links: dict[str, str] = {}
    _composio_provider = {"Gmail": "gmail", "Google Calendar": "gcal", "Slack": "slack"}

    if "Canvas" not in connected_apps:
        try:
            from web.connect_state import make_connect_url
            links["Canvas"] = make_connect_url(user_id)
        except Exception as exc:
            logger.warning("app_links: canvas url failed: %s", exc)
            links["Canvas"] = f"{base}/connect/canvas"

    for label, provider in _composio_provider.items():
        if label in connected_apps:
            continue
        if label in _has_pending:
            # OAuth flow is in progress — generating a new link would overwrite
            # pending_account_id and orphan the current flow.  Keep a placeholder
            # so the card renders the button with a fresh link ONLY if user explicitly
            # wants to restart. For now, skip and let check_links verify.
            logger.info("app_links: %s has pending OAuth, skipping connect_link", label)
            continue
        try:
            links[label] = await connect_link(user_id, provider)
        except Exception as exc:
            logger.warning("app_links: composio link failed app=%s code=%s", label,
                           getattr(exc, "code", exc))

    logger.info("app_links: connected=%s unconnected=%s",
                sorted(connected_apps), sorted(links))
    try:
        await ctx.send(sender, link_buttons(links, connected=connected_apps))
    except Exception as exc:
        logger.error("app_links: ctx.send failed: %s", exc)
        text = "Connect your apps to unlock Margin:\n" + "\n".join(f"• {app}: {url}" for app, url in links.items())
        await send_text(ctx, sender, text)


async def _build_app_card_url(user_id: str, app_name: str) -> str:
    """Generate the OAuth/connect URL for an app. Returns '' on failure."""
    base = os.environ.get("PUBLIC_BASE_URL", "https://inappmargin.vip")
    if app_name == "Canvas":
        try:
            from web.connect_state import make_connect_url
            return make_connect_url(user_id)
        except Exception as exc:
            logger.warning("app_card: canvas url failed: %s", exc)
            return f"{base}/connect/canvas"
    _composio_id = {"Gmail": "gmail", "Google Calendar": "gcal", "Slack": "slack"}
    cid = _composio_id.get(app_name)
    if cid:
        try:
            from integrations.composio_tools import connect_link
            return await connect_link(user_id, cid)
        except Exception as exc:
            logger.warning("app_card: composio link failed app=%s: %s", app_name, exc)
    return ""


async def _send_next_app_card(ctx, sender: str, user_id: str, apps: list[str]) -> None:
    """Show a sequential connect card for the first app in the list."""
    await _send_next_app_card_with_context(ctx, sender, user_id, apps)


async def _send_next_app_card_with_context(
    ctx, sender: str, user_id: str, apps: list[str], prefix: str = ""
) -> None:
    """Send the next app card. prefix is embedded in the card narration (one send, no rate-limit)."""
    if not apps:
        await send_text(ctx, sender,
            "You're all set! Say 'plan my week' to block time for your assignments.")
        return

    app_name = apps[0]
    remaining = apps[1:]
    url = await _build_app_card_url(user_id, app_name)

    step_num = _ONBOARDING_ORDER.index(app_name) + 1 if app_name in _ONBOARDING_ORDER else 1
    total = step_num + len(remaining)
    logger.info("app_card: %s (%d/%d) url=%s", app_name, step_num, total, bool(url))

    desc = _APP_DESCRIPTIONS.get(app_name, "")
    narration = prefix or f"Step {step_num} of {total}: connect {app_name}."
    try:
        from core.cards import app_connection_card as _acc
        msg = _acc(app_name, desc, url, remaining)
        # Patch narration into card TextContent
        from uagents_core.contrib.protocols.chat import TextContent
        msg.content = [TextContent(type="text", text=narration)] + [
            c for c in msg.content if not isinstance(c, TextContent)
        ]
        await ctx.send(sender, msg)
    except Exception as exc:
        logger.error("app_card: ctx.send failed for %s: %s", app_name, exc)
        if url:
            await send_text(ctx, sender, f"{narration}\nConnect {app_name}: {url}")
        else:
            await send_text(ctx, sender,
                f"{narration}\nCouldn't generate the {app_name} link right now — "
                "try saying 'check my apps' to retry.")


async def chat_node(state: GraphState) -> dict:
    import re as _re
    ctx = state["ctx"]
    sender = state["sender"]
    user_id = state["user_id"]
    channel = state["channel"]
    text = (state["text"] or "").strip()
    lower = text.lower()
    intent = state.get("intent", "chat")
    sel = state.get("selection") or {}
    history = state.get("history") or []
    user_ctx = state.get("user_ctx") or {}
    logger.info("chat: intent=%s", intent)

    # User clicked a "Connect <App>" button
    if intent == "linked":
        app = sel.get("app", "")
        if app:
            await send_text(ctx, sender,
                f"Browser opened for {app} — complete the authorization there. "
                f"When you're done, come back and say \"connected\" or \"check apps\".")
        return {}

    # User tapped "Skip for now" on an app card
    if intent == "skip_app":
        app = sel.get("app", "")
        remaining = list(sel.get("remaining") or [])
        prefix = f"Skipped {app} — you can connect it anytime later." if app else ""
        await _send_next_app_card_with_context(ctx, sender, user_id, remaining, prefix=prefix)
        return {}

    # Greetings — resume from wherever the user left off in onboarding
    greetings = {"hi", "hello", "hey", "start", "help", "get started", "setup", "set up"}
    if lower in greetings or lower == "":
        if channel == "asi1":
            try:
                from db import repo
                user = await repo.get_user(user_id)
                if user:
                    remaining = await _get_onboarding_step(user_id)
                    if remaining:
                        await _send_next_app_card_with_context(ctx, sender, user_id, remaining,
                            prefix=f"Welcome back! Still need to connect {len(remaining)} app(s).")
                    else:
                        await send_text(ctx, sender,
                            "You're all set! Say 'plan my week' to get started.")
                    return {}
            except Exception as exc:
                logger.warning("chat: user lookup failed: %s", exc)
            await ctx.send(sender, welcome_card())
            await asyncio.sleep(2)
            await ctx.send(sender, connect_form())
        else:
            await send_text(ctx, sender,
                "Open Margin in ASI:One to complete the Connect form and link your phone.")
        return {}

    # Letter lookup — "what's B?", "tell me about C", "what is A"
    letter_match = _re.match(r"(?:what[\'']?s?|tell me about|what is|show me)\s+([a-eA-E])\b", lower)
    if letter_match:
        letter = letter_match.group(1).upper()
        try:
            from db import repo
            brief = await repo.get_today_brief(user_id)
            letters = (brief or {}).get("letters") or {}
            if letter in letters:
                item = await repo.get_item(user_id, letters[letter])
                if item:
                    reply = f"{letter} — {item['title']}"
                    if item.get("course"):
                        reply += f" ({item['course']})"
                    if item.get("due_at"):
                        try:
                            due = datetime.fromisoformat(item["due_at"].replace("Z", "+00:00"))
                            tz_name = user_ctx.get("tz", "America/Phoenix")
                            local_due = due.astimezone(ZoneInfo(tz_name))
                            reply += f"\nDue: {local_due.strftime('%a %-m/%-d at %-I:%M %p')}"
                        except Exception:
                            reply += f"\nDue: {item['due_at'][:10]}"
                    if item.get("quote"):
                        reply += f"\n{item['quote'][:200]}"
                    await send_text(ctx, sender, reply)
                    try:
                        await repo.save_message(user_id, "assistant", reply, intent="letter_lookup", channel=channel)
                    except Exception:
                        pass
                    return {}
        except Exception as exc:
            logger.warning("chat: letter lookup failed: %s", exc)

    # Budget query
    if intent == "budget":
        try:
            from db import repo
            amount = await repo.safe_to_spend(user_id)
            if amount > 0:
                reply = f"You have ${amount:.2f} safe to spend this week."
            elif amount < 0:
                reply = f"You're ${abs(amount):.2f} over budget this week. Consider cutting flexible spend."
            else:
                reply = "Budget isn't set up yet — complete the Connect form to add your income and bills."
            await send_text(ctx, sender, reply)
            try:
                await repo.save_message(user_id, "assistant", reply, intent="budget", channel=channel)
            except Exception:
                pass
            return {}
        except Exception as exc:
            logger.warning("chat: budget query failed: %s", exc)

    # Build rich system context
    tz = user_ctx.get("tz", "America/Phoenix")
    now_local = datetime.now(ZoneInfo(tz))

    connected_apps: list[str] = []
    pending_plan_info = ""
    budget_hint = ""
    try:
        from db import repo
        # Connected apps
        canvas_row = await repo.get_connection(user_id, "canvas")
        if canvas_row and canvas_row.get("status") == "active":
            connected_apps.append("Canvas")
        from integrations import composio_tools
        for provider, label in (("gmail", "Gmail"), ("gcal", "Google Calendar"), ("slack", "Slack")):
            try:
                st = await composio_tools.connection_status(user_id, provider)
                if st.status == "active":
                    connected_apps.append(label)
            except Exception:
                pass
        # Pending plan
        pending = await repo.pending_plan_for(user_id)
        if pending:
            pending_plan_info = " There is a pending plan awaiting approval."
        # Budget
        try:
            amount = await repo.safe_to_spend(user_id)
            if amount != 0:
                budget_hint = f" Safe to spend this week: ${amount:.2f}."
        except Exception:
            pass
    except Exception:
        pass

    apps_str = ", ".join(connected_apps) if connected_apps else "none connected yet"
    system_prompt = (
        f"You are Margin, a proactive AI chief-of-staff for an ASU student. "
        f"Today is {now_local.strftime('%A, %B %-d, %Y')}. "
        f"Connected apps: {apps_str}.{pending_plan_info}{budget_hint} "
        "You can: plan the week ('plan my week'), show daily brief ('summary'), "
        "connect apps, check budget, block calendar time. "
        "Hard rules: never do coursework, only create Gmail drafts (never send), never delete events. "
        "Keep replies under 4 sentences. Be specific and actionable."
    )

    # Build messages with full history
    messages = [{"role": "system", "content": system_prompt}]
    for turn in history[-6:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": state["text"]})

    try:
        reply = await fast(messages, timeout=15)
    except Exception:
        reply = None

    final_reply = reply or "I'm here! Try 'plan my week' to block time for your assignments."
    await send_text(ctx, sender, final_reply)

    try:
        from db import repo
        await repo.save_message(user_id, "assistant", final_reply, intent=intent, channel=channel)
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route(state: GraphState) -> str:
    routes: dict[str, str] = {
        "connect":     "connect",
        "connect_app": "connect",    # regenerate link for a specific app
        "check_links": "check_links",
        "check_next":  "check_links",  # verify one app then advance
        "linked":      "chat",
        "skip_app":    "chat",
        "plan_week":  "gather",
        "approve":    "execute",
        "cancel":     "cancel",
        "upgrade":    "upgrade",
        "open_task":  "priorities",
        "snooze":     "chat",
        "budget":     "chat",
        "summary":    "summary",
        "reset":      "reset",
        "reset_confirm": "reset",
        "back":       "chat",
        "chat":       "chat",
    }
    return routes.get(state.get("intent", "chat"), "chat")


# ---------------------------------------------------------------------------
# Build graph once at module level
# ---------------------------------------------------------------------------

def _build() -> Any:
    g = StateGraph(GraphState)
    for name, fn in [
        ("classify",    classify_node),
        ("connect",     connect_node),
        ("check_links", check_links_node),
        ("gather",      gather_node),
        ("plan_week",   plan_week_node),
        ("store_plan",  store_plan_node),
        ("execute",     execute_node),
        ("cancel",      cancel_node),
        ("summary",     summary_node),
        ("reset",       reset_node),
        ("priorities",  priorities_node),
        ("upgrade",     upgrade_node),
        ("chat",        chat_node),
    ]:
        g.add_node(name, fn)

    g.add_edge(START, "classify")
    g.add_conditional_edges(
        "classify", _route,
        {
            "connect":     "connect",
            "check_links": "check_links",
            "gather":      "gather",
            "execute":     "execute",
            "cancel":      "cancel",
            "upgrade":     "upgrade",
            "priorities":  "priorities",
            "summary":     "summary",
            "reset":       "reset",
            "chat":        "chat",
        },
    )
    g.add_edge("gather",     "plan_week")
    g.add_edge("plan_week",  "store_plan")
    for terminal in ("connect", "check_links", "store_plan", "execute", "cancel", "summary", "reset", "priorities", "upgrade", "chat"):
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
    import re as _re
    clean_text = _re.sub(r"@\w+", "", text or "").strip()

    # Load conversation history and user profile (best-effort)
    history: list[dict] = []
    user_ctx: dict = {}
    try:
        from db import repo
        history = await asyncio.wait_for(
            repo.get_recent_messages(user_id, limit=8), timeout=3
        )
        user_row = await asyncio.wait_for(repo.get_user(user_id), timeout=3)
        user_ctx = dict(user_row) if user_row else {}
    except Exception:
        pass

    # Persist the incoming user message (best-effort, non-blocking)
    if clean_text and not selection:
        try:
            from db import repo
            await asyncio.wait_for(
                repo.save_message(user_id, "user", clean_text, channel=channel),
                timeout=3,
            )
        except Exception:
            pass

    state: GraphState = {
        "ctx": ctx,
        "sender": sender,
        "user_id": user_id,
        "text": clean_text,
        "channel": channel,
        "selection": selection,
        "intent": "",
        "items": [],
        "free_slots": [],
        "plan_id": None,
        "actions": [],
        "gather_error": None,
        "history": history,
        "user_ctx": user_ctx,
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
