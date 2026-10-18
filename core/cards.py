"""Interactive card builders for ASI:One. Owner: Prem."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    MetadataContent,
    TextContent,
)


# ---------------------------------------------------------------------------
# Core helper (copied from tripmate pattern)
# ---------------------------------------------------------------------------

def card_message(kind: str, payload: dict, narration: str = "") -> ChatMessage:
    """Wrap a payload dict into a ChatMessage card. All metadata values are strings."""
    content: list[Any] = []
    if narration:
        content.append(TextContent(type="text", text=narration))
    content.append(
        MetadataContent(
            type="metadata",
            metadata={
                "card_protocol_version": "1",
                "requires_card_interaction": "true",
                "card_kind": kind,
                "card_payload": json.dumps(payload, default=str),
                "preferred_drawer_width_px": "540",
            },
        )
    )
    return ChatMessage(
        timestamp=datetime.now(timezone.utc),
        msg_id=uuid4(),
        content=content,
    )


def _local(iso_utc: str | None, tz: str) -> str:
    if not iso_utc:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
        local = dt.astimezone(ZoneInfo(tz))
        return local.strftime("%-m/%-d %-I:%M %p")
    except Exception:
        return iso_utc


# ---------------------------------------------------------------------------
# Card builders
# ---------------------------------------------------------------------------

def welcome_card() -> ChatMessage:
    """Welcome carousel shown on first greeting — explains what Margin does."""
    payload = {
        "title": "Welcome to Margin",
        "items": [
            {
                "id": "feat-brief",
                "title": "Daily 7 AM Brief",
                "subtitle": "Get lettered action items (A, B, C…) from Canvas, Gmail, and Slack — straight to your phone every morning.",
                "badges": ["SMS"],
                "primary_cta": {"label": "Get started", "selection": {"action": "connect"}},
            },
            {
                "id": "feat-planner",
                "title": "Week Planner",
                "subtitle": "Margin finds your free slots and blocks study time on Google Calendar — after you approve every event.",
                "badges": ["Calendar"],
                "primary_cta": {"label": "Get started", "selection": {"action": "connect"}},
            },
            {
                "id": "feat-inbox",
                "title": "Inbox Triage",
                "subtitle": "Surfaces action items from Gmail and Slack as interactive cards so nothing falls through the cracks.",
                "badges": ["Gmail", "Slack"],
                "primary_cta": {"label": "Get started", "selection": {"action": "connect"}},
            },
            {
                "id": "feat-budget",
                "title": "Budget Tracker",
                "subtitle": "One number: how much is safe to spend this week, updated from your Plaid transactions.",
                "badges": ["Plaid"],
                "primary_cta": {"label": "Get started", "selection": {"action": "connect"}},
            },
        ],
    }
    return card_message("carousel", payload, "Hi! I'm Margin — your AI chief of staff for college. Here's what I can do:")


def connect_form() -> ChatMessage:
    """Onboarding form: phone + budget fields."""
    payload = {
        "title": "Set up Margin",
        "fields": [
            {"name": "phone",          "kind": "text",   "label": "Phone number (E.164, e.g. +14805551234)", "required": True},
            {"name": "monthly_income", "kind": "number", "label": "Monthly income ($)",  "required": True},
            {"name": "weekly_cap",     "kind": "number", "label": "Weekly spending cap ($, optional)"},
            {"name": "savings_goal",   "kind": "number", "label": "Monthly savings goal ($, optional)"},
            {"name": "bills", "kind": "text", "label": "Monthly bills (optional): Name | Amount | Due day; e.g. Rent | 850 | 1; Utilities | 70 | 15"},
        ],
        "submit_cta": {"label": "Save and link phone", "selection": {"action": "connect"}},
    }
    return card_message("form", payload, "Let's set up your Margin account.")


def link_buttons(links: dict[str, str]) -> ChatMessage:
    """Custom card with one primary button per app + an 'I've connected' button.

    links = {"Gmail": "https://...", "Google Calendar": "https://...", ...}
    """
    buttons: list[dict] = []
    for app, url in links.items():
        buttons.append({
            "type": "button",
            "label": f"Connect {app}",
            "primary": True,
            "action": {
                "selection": {"action": "linked", "app": app},
                "redirect": url,
            },
        })
    buttons.append({
        "type": "button",
        "label": "I've connected everything",
        "action": {"selection": {"action": "check_links"}},
    })
    payload = {
        "root": {
            "type": "section",
            "title": "Connect your apps",
            "children": [
                {"type": "text", "value": "Connect each service so Margin can read your schedule.", "style": "muted"},
                *buttons,
            ],
        }
    }
    return card_message("custom", payload, "Connect your apps to get started.")


def priorities_carousel(items: list[dict], tz: str = "America/Phoenix") -> ChatMessage:
    """Carousel of up to 5 priority items."""
    cards = []
    for item in items[:5]:
        due = _local(item.get("due_at"), tz)
        subtitle_parts = []
        if item.get("course"):
            subtitle_parts.append(item["course"])
        subtitle_parts.append(f"due {due}")
        subtitle = " · ".join(subtitle_parts)

        badges = [item.get("source", "")]
        if item.get("est_hours"):
            badges.append(f"{item['est_hours']}h left")

        cards.append({
            "id": item["item_id"],
            "title": item["title"],
            "subtitle": subtitle,
            "badges": [b for b in badges if b],
            "secondary_text": item.get("quote", ""),
            "primary_cta": {
                "label": "Open",
                "selection": {"action": "open_task", "item_id": item["item_id"]},
            },
        })
    payload = {"title": "Your priorities", "items": cards}
    return card_message("carousel", payload, f"Here are your top {len(cards)} priorities.")


def task_detail(item: dict, conflicts: list[str], tz: str = "America/Phoenix") -> ChatMessage:
    """Detail card for a single task."""
    due = _local(item.get("due_at"), tz)
    payload = {
        "title": item["title"],
        "summary_rows": [
            {"label": "Due",       "value": due},
            {"label": "Estimated", "value": f"{item.get('est_hours', '?')}h"},
            {"label": "Source",    "value": item.get("source", "")},
            {"label": "Conflicts", "value": ", ".join(conflicts) if conflicts else "None"},
        ],
        "sub_options": {
            "name": "block",
            "kind": "radio",
            "label": "Block time",
            "choices": [
                {"value": "1h", "label": "1 hour"},
                {"value": "2h", "label": "2 hours"},
                {"value": "3h", "label": "3 hours"},
            ],
        },
        "ctas": [
            {"label": "Plan it",        "primary": True, "selection": {"action": "plan_task",  "item_id": item["item_id"]}},
            {"label": "Draft extension",               "selection": {"action": "draft_ext",  "item_id": item["item_id"]}},
            {"label": "Snooze",                        "selection": {"action": "snooze",     "item_id": item["item_id"]}},
        ],
    }
    return card_message("detail", payload)


def review(plan_id: str, rows: list[dict]) -> ChatMessage:
    """Review card: approve or cancel a plan."""
    payload = {
        "title": "Review plan",
        "summary_rows": rows,
        "approve_cta": {"label": "Approve", "primary": True, "selection": {"action": "approve", "plan_id": plan_id}},
        "reject_cta":  {"label": "Cancel",                   "selection": {"action": "cancel",  "plan_id": plan_id}},
    }
    return card_message("review", payload, "Here's what Margin will do:")


def done(results: list[dict]) -> ChatMessage:
    """Done card with result links and a Back button."""
    children: list[dict] = []
    for r in results:
        if r.get("link"):
            children.append({
                "type": "button",
                "label": r.get("label", "Open"),
                "primary": True,
                "action": {"selection": {"action": "open"}, "redirect": r["link"]},
            })
        elif r.get("error"):
            children.append({
                "type": "text",
                "value": f"Couldn't create {r.get('label', 'action')}: {r['error']}",
            })
    children.append({
        "type": "button",
        "label": "← Back",
        "action": {"selection": {"action": "back"}},
    })
    failed = any(result.get("error") for result in results)
    payload = {
        "root": {
            "type": "section",
            "title": "Plan complete" if failed else "Done!",
            "children": [
                {"type": "text", "value": "Your approved actions finished.", "style": "muted"},
                *children,
            ],
        }
    }
    return card_message("custom", payload, "Plan complete." if failed else "Done!")


def events_carousel(events: list[dict], safe_to_spend: float, tz: str = "America/Phoenix") -> ChatMessage:
    """Carousel of campus events with budget badge."""
    cards = []
    for ev in events[:5]:
        cost = ev.get("cost_usd") or 0.0
        remaining = safe_to_spend - cost
        cost_badge = f"${cost:.0f}" if cost else "Free"
        budget_badge = f"leaves ${remaining:.0f} this week"
        cards.append({
            "id": ev.get("item_id", ev.get("source_id", "")),
            "title": ev["title"],
            "subtitle": _local(ev.get("due_at") or ev.get("starts_at"), tz),
            "badges": [cost_badge, budget_badge],
            "secondary_text": ev.get("location", ""),
            "primary_cta": {
                "label": "Add to calendar",
                "selection": {"action": "add_event", "item_id": ev.get("item_id", ev.get("source_id", ""))},
            },
        })
    payload = {"title": "Events this week", "items": cards}
    return card_message("carousel", payload, "Here's what's happening:")


def upgrade_card() -> ChatMessage:
    """Upgrade options: Pro and Finals pack."""
    payload = {
        "root": {
            "type": "section",
            "title": "Upgrade Margin",
            "children": [
                {"type": "heading", "text": "Pro — $4.99 / 30 days"},
                {"type": "text",    "value": "Unlimited briefs, nudges and planning.", "style": "muted"},
                {"type": "button",  "label": "Get Pro", "primary": True,
                 "action": {"selection": {"action": "upgrade", "tier": "pro"}}},
                {"type": "divider"},
                {"type": "heading", "text": "Finals Pack — $1.99"},
                {"type": "text",    "value": "Two weeks of Pro during finals.", "style": "muted"},
                {"type": "button",  "label": "Get Finals Pack",
                 "action": {"selection": {"action": "upgrade", "tier": "finals"}}},
            ],
        }
    }
    return card_message("custom", payload, "Choose a plan:")


def app_connection_card(
    app_name: str,
    description: str,
    connect_url: str,
    remaining: list[str],
) -> ChatMessage:
    """Single-app connection card with a Skip option.

    remaining: list of app names still to be shown after this one.
    """
    connect_action: dict = {
        "selection": {"action": "linked", "app": app_name, "remaining": remaining},
    }
    if connect_url:
        connect_action["redirect"] = connect_url

    payload = {
        "root": {
            "type": "section",
            "title": f"Connect {app_name}",
            "children": [
                {"type": "text", "value": description, "style": "muted"},
                {
                    "type": "button",
                    "label": f"Connect {app_name}",
                    "primary": True,
                    "action": connect_action,
                },
                {
                    "type": "button",
                    "label": "Skip for now →",
                    "action": {
                        "selection": {"action": "skip_app", "app": app_name, "remaining": remaining},
                    },
                },
            ],
        }
    }
    return card_message("custom", payload)


def error_card(message: str, step: str = "") -> ChatMessage:
    """Error card with a Retry button."""
    payload = {
        "root": {
            "type": "section",
            "title": "Something went wrong",
            "children": [
                {"type": "text", "value": message, "style": "muted"},
                {"type": "button", "label": "Retry", "primary": True,
                 "action": {"selection": {"action": "retry", "step": step}}},
            ],
        }
    }
    return card_message("custom", payload)


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json as _json

    _now = "2026-10-18T14:00:00Z"
    _item = {
        "item_id": "i1", "title": "HW3 — Linear Algebra", "course": "MAT 343",
        "due_at": _now, "est_hours": 3.0, "source": "canvas", "quote": "Due Sunday",
    }
    _event = {
        "item_id": "e1", "title": "ASU Club Dinner", "starts_at": _now,
        "location": "MU 202", "cost_usd": 15.0,
    }

    samples = {
        "connect_form":         connect_form(),
        "link_buttons":         link_buttons({"Gmail": "https://example.com", "Google Calendar": "https://example.com"}),
        "priorities_carousel":  priorities_carousel([_item]),
        "task_detail":          task_detail(_item, ["CS 101 lecture 3-4pm"]),
        "review":               review("plan_abc", [{"label": "Block 2h for HW3", "value": "Mon 10-12am"}]),
        "done":                 done([{"label": "Open event", "link": "https://calendar.google.com"}]),
        "events_carousel":      events_carousel([_event], safe_to_spend=45.0),
        "upgrade_card":         upgrade_card(),
        "error_card":           error_card("Could not reach Canvas.", "gather"),
    }

    for name, msg in samples.items():
        for c in msg.content:
            if hasattr(c, "metadata"):
                payload = _json.loads(c.metadata["card_payload"])
                size = len(c.metadata["card_payload"])
                print(f"[{name}] kind={c.metadata['card_kind']} size={size}B payload_keys={list(payload.keys())}")
                break
        else:
            print(f"[{name}] text-only")
    print("\nAll samples generated OK.")
