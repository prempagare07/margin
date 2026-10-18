# Shared message contract. Owners: Alex and Prem.
# shared/protocol.py
from typing import Literal, Optional
from uagents import Model

class Item(Model):
    item_id: str
    user_id: str # ASI:One sender address
    kind: Literal["assignment", "announcement", "email_action",
    "slack_action", "event", "bill"]
    title: str
    course: Optional[str] = None
    due_at: Optional[str] = None # ISO 8601, UTC
    est_hours: Optional[float] = None
    source: Literal["canvas", "canvas_ical", "gmail", "slack", "gcal",
    "sundevilsync", "plaid"]
    source_id: str # assignment id, message id, event uid
    source_url: Optional[str] = None
    quote: Optional[str] = None # short evidence snippet from the source
    status: Literal["open", "submitted", "done", "snoozed"] = "open"
    cost_usd: Optional[float] = None
class GatherRequest(Model):
    request_id: str
    user_id: str
    window_start: str
    window_end: str
class GatherResult(Model):
    request_id: str
    agent: str
    items: list[Item] = []
    free_slots: list[list[str]] = [] # Planner only: [start, end]
    stale_since: Optional[str] = None # set when served from cache
    error: Optional[str] = None
class ActionRequest(Model):
    action_id: str
    plan_id: str
    user_id: str
    type: Literal["create_event", "create_draft", "add_reminder",
    "fetch_source"]
    params: dict
class ActionResult(Model):
    action_id: str