"""Smoke tests for core/cards.py card builders."""
import json

import pytest

from core.cards import (
    card_message,
    connect_form,
    done,
    error_card,
    events_carousel,
    link_buttons,
    priorities_carousel,
    review,
    task_detail,
    upgrade_card,
)
from uagents_core.contrib.protocols.chat import MetadataContent, TextContent

_NOW = "2026-10-18T14:00:00Z"
_ITEM = {
    "item_id": "i1",
    "title": "HW3 — Linear Algebra",
    "course": "MAT 343",
    "due_at": _NOW,
    "est_hours": 3.0,
    "source": "canvas",
    "quote": "Due Sunday",
}
_EVENT = {
    "item_id": "e1",
    "title": "ASU Club Dinner",
    "starts_at": _NOW,
    "location": "MU 202",
    "cost_usd": 15.0,
}


def _meta(msg) -> dict:
    for c in msg.content:
        if isinstance(c, MetadataContent):
            return c.metadata
    raise AssertionError("No MetadataContent found")


def _payload(msg) -> dict:
    return json.loads(_meta(msg)["card_payload"])


def test_metadata_values_are_strings():
    msg = connect_form()
    meta = _meta(msg)
    for k, v in meta.items():
        assert isinstance(v, str), f"metadata[{k!r}] is {type(v)}, expected str"


def test_payload_under_64kb():
    for builder, args in [
        (connect_form, []),
        (link_buttons, [{"Gmail": "https://example.com"}]),
        (priorities_carousel, [[_ITEM]]),
        (task_detail, [_ITEM, ["conflict"]]),
        (review, ["pid", [{"label": "l", "value": "v"}]]),
        (done, [[{"label": "Open", "link": "https://example.com"}]]),
        (events_carousel, [[_EVENT], 45.0]),
        (upgrade_card, []),
        (error_card, ["oops"]),
    ]:
        msg = builder(*args)
        size = len(_meta(msg)["card_payload"].encode())
        assert size < 65536, f"{builder.__name__} payload too large: {size}B"


def test_card_protocol_version():
    for builder, args in [
        (connect_form, []),
        (upgrade_card, []),
        (error_card, ["fail"]),
    ]:
        assert _meta(builder(*args))["card_protocol_version"] == "1"


def test_connect_form_fields():
    p = _payload(connect_form())
    assert p["title"] == "Set up Margin"
    names = [f["name"] for f in p["fields"]]
    assert "phone" in names
    assert "monthly_income" in names


def test_priorities_carousel_max_5():
    items = [dict(_ITEM, item_id=f"i{i}", title=f"Task {i}") for i in range(10)]
    p = _payload(priorities_carousel(items))
    assert len(p["items"]) == 5


def test_events_carousel_budget_badge():
    p = _payload(events_carousel([_EVENT], safe_to_spend=45.0))
    badges = p["items"][0]["badges"]
    assert "$15" in badges[0]
    assert "leaves" in badges[1]


def test_task_detail_structure():
    p = _payload(task_detail(_ITEM, ["CS lecture 3-4pm"]))
    assert p["title"] == _ITEM["title"]
    labels = [r["label"] for r in p["summary_rows"]]
    assert "Due" in labels
    assert "Conflicts" in labels


def test_review_card():
    rows = [{"label": "Block 2h", "value": "Mon 10-12"}]
    p = _payload(review("plan_abc", rows))
    assert p["approve_cta"]["selection"]["plan_id"] == "plan_abc"
    assert p["reject_cta"]["selection"]["action"] == "cancel"


def test_done_card_back_button():
    p = _payload(done([]))
    children = p["root"]["children"]
    back = [c for c in children if c.get("type") == "button" and "Back" in c.get("label", "")]
    assert back, "done() must have a Back button"


def test_narration_text_content():
    msg = connect_form()
    texts = [c.text for c in msg.content if isinstance(c, TextContent)]
    assert any("set up" in t.lower() for t in texts)


def test_card_message_requires_card_interaction():
    meta = _meta(card_message("form", {"x": 1}))
    assert meta["requires_card_interaction"] == "true"
