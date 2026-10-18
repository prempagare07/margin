"""Tests for channels/scheduler.py (in-memory path, no live Photon calls)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from channels.scheduler import (
    _users, _briefs, _nudges,
    register_user,
    send_morning_brief,
    send_evening_nudge,
    run_morning_briefs,
    run_evening_nudges,
    start_scheduler,
    stop_scheduler,
    _brief_sent_today,
    _nudge_already_sent,
    _safe_to_spend,
)


def _clear():
    _users.clear()
    _briefs.clear()
    _nudges.clear()


def _user(**kw) -> dict:
    base = {
        "user_id": "u-test",
        "phone": "+16027513236",
        "tz": "America/Phoenix",
        "monthly_income": 2000.0,
        "weekly_cap": None,
        "savings_goal": 200.0,
    }
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_register_user():
    _clear()
    register_user("u1", "+16027510001")
    assert "u1" in _users
    assert _users["u1"]["phone"] == "+16027510001"


@pytest.mark.asyncio
async def test_brief_not_sent_initially():
    _clear()
    register_user("u2", "+16027510002")
    sent = await _brief_sent_today("u2", "America/Phoenix")
    assert sent is False


@pytest.mark.asyncio
async def test_morning_brief_sends_and_dedupes(monkeypatch):
    _clear()
    user = _user(user_id="u3", phone="+16027510003")
    _users["u3"] = user

    sent_calls = []

    async def fake_send(to, text, idempotency_key=None):
        sent_calls.append((to, text))
        return {"ok": True}

    monkeypatch.setattr("channels.scheduler.send_message", fake_send)

    # First call sends
    await send_morning_brief(user)
    assert len(sent_calls) == 1
    assert "+16027510003" in sent_calls[0][0]
    text = sent_calls[0][1]
    assert "A." in text
    assert "Safe to spend" in text

    # Second call is deduplicated
    await send_morning_brief(user)
    assert len(sent_calls) == 1   # still 1


@pytest.mark.asyncio
async def test_morning_brief_letters_recorded(monkeypatch):
    _clear()
    user = _user(user_id="u4", phone="+16027510004")
    _users["u4"] = user

    monkeypatch.setattr("channels.scheduler.send_message", AsyncMock(return_value={"ok": True}))
    await send_morning_brief(user)

    assert len(_briefs) == 1
    brief = list(_briefs.values())[0]
    assert brief["user_id"] == "u4"
    assert "A" in brief["letters"]


@pytest.mark.asyncio
async def test_evening_nudge_sends_and_dedupes(monkeypatch):
    _clear()
    user = _user(user_id="u5", phone="+16027510005")
    _users["u5"] = user

    sent = []
    monkeypatch.setattr("channels.scheduler.send_message", AsyncMock(side_effect=lambda to, text, **kw: sent.append(text) or {"ok": True}))

    await send_evening_nudge(user)
    assert len(sent) == 1
    assert "1." in sent[0]
    assert "left to spend" in sent[0]

    # Deduplication
    await send_evening_nudge(user)
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_run_morning_briefs_no_users(monkeypatch):
    _clear()
    monkeypatch.setattr("channels.scheduler.send_message", AsyncMock())
    # Should not raise even with zero users
    await run_morning_briefs()


@pytest.mark.asyncio
async def test_run_evening_nudges_multiple_users(monkeypatch):
    _clear()
    for i in range(3):
        uid = f"u-multi-{i}"
        _users[uid] = _user(user_id=uid, phone=f"+1602751000{i}")

    sent = []
    monkeypatch.setattr(
        "channels.scheduler.send_message",
        AsyncMock(side_effect=lambda to, text, **kw: sent.append(to) or {"ok": True}),
    )
    await run_evening_nudges()
    assert len(sent) == 3


@pytest.mark.asyncio
async def test_safe_to_spend_no_db():
    user = _user(monthly_income=2000, savings_goal=200, weekly_cap=None)
    safe = await _safe_to_spend(user)
    # 2000 - 0 bills - 200 savings - 0 spent, divided by ~weeks_left
    assert safe > 0


@pytest.mark.asyncio
async def test_safe_to_spend_respects_weekly_cap():
    user = _user(monthly_income=10000, savings_goal=0, weekly_cap=100.0)
    safe = await _safe_to_spend(user)
    assert safe <= 100.0


@pytest.mark.asyncio
async def test_scheduler_start_stop():
    s = start_scheduler()
    assert s.running
    # Starting again returns same running scheduler
    s2 = start_scheduler()
    assert s is s2
    # stop should not raise; scheduler module resets internal ref
    stop_scheduler()
    # After stop, start_scheduler() creates a fresh instance
    import channels.scheduler as _sched_mod
    assert _sched_mod._scheduler is None or not _sched_mod._scheduler.running
