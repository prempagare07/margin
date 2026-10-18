"""Scheduling data must survive the specialist-to-orchestrator wire format."""

import json

import pytest

from shared.protocol import GatherResult


@pytest.mark.parametrize("ends_at", ["2026-10-20T01:30:00Z", "2026-10-20T02:30:00Z"])
def test_gather_result_preserves_event_details(ends_at):
    event = {
        "item_id": "demo-review",
        "user_id": "demo-student",
        "kind": "event",
        "title": "CSE 310 midterm review",
        "source": "gcal",
        "source_id": "demo-calendar:review",
        "starts_at": "2026-10-20T01:30:00Z",
        "ends_at": ends_at,
        "location": "BYAC 110",
    }
    outgoing = GatherResult(
        request_id="gather-demo", agent="margin-planner", items=[event]
    )

    # Use the same serialization methods as uAgents, including nested Item parsing.
    wire = outgoing.model_dump_json()
    incoming = GatherResult.model_validate_json(wire)
    received = incoming.items[0].model_dump()

    for field, value in event.items():
        assert json.loads(wire)["items"][0][field] == value
        assert received[field] == value
    assert incoming.items[0].due_at is None


def test_legacy_assignment_payload_needs_no_event_fields():
    wire = json.dumps({
        "request_id": "gather-legacy",
        "agent": "margin-academic",
        "items": [{
            "item_id": "demo-homework",
            "user_id": "demo-student",
            "kind": "assignment",
            "title": "Homework 4",
            "source": "canvas_ical",
            "source_id": "assignment:4",
            "due_at": "2026-10-21T06:59:00Z",
        }],
    })

    assignment = GatherResult.model_validate_json(wire).items[0]

    assert assignment.due_at == "2026-10-21T06:59:00Z"
    assert assignment.starts_at is None
    assert assignment.ends_at is None
    assert assignment.location is None
