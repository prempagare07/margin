"""Tests for core/approval.py in-memory path."""
import pytest
import pytest_asyncio

from core import approval


def _actions(n: int = 2) -> list[dict]:
    return [
        {
            "action_id": f"a{i}",
            "type": "create_event",
            "params": {"title": f"Task {i}", "start_iso": "2026-10-20T09:00:00+00:00",
                       "end_iso": "2026-10-20T11:00:00+00:00", "item_id": f"item{i}"},
        }
        for i in range(1, n + 1)
    ]


@pytest.mark.asyncio
async def test_create_and_pending():
    approval._mem_plans.clear()
    pid = await approval.create_plan("u1", _actions(2))
    p = await approval.pending_plan_for("u1")
    assert p is not None
    assert p["plan_id"] == pid
    assert p["status"] == "pending"


@pytest.mark.asyncio
async def test_approve_transitions_status():
    approval._mem_plans.clear()
    pid = await approval.create_plan("u2", _actions(1))
    ok = await approval.approve(pid, "u2")
    assert ok is True
    p = await approval.get_plan(pid, "u2")
    assert p["status"] == "approved"


@pytest.mark.asyncio
async def test_approve_idempotent():
    approval._mem_plans.clear()
    pid = await approval.create_plan("u3", _actions(1))
    await approval.approve(pid, "u3")
    ok2 = await approval.approve(pid, "u3")
    assert ok2 is False   # already approved — CAS rejects


@pytest.mark.asyncio
async def test_cancel():
    approval._mem_plans.clear()
    pid = await approval.create_plan("u4", _actions(1))
    ok = await approval.cancel(pid, "u4")
    assert ok is True
    p = await approval.get_plan(pid, "u4")
    assert p["status"] == "cancelled"


@pytest.mark.asyncio
async def test_full_execute_cycle():
    approval._mem_plans.clear()
    approval._mem_execs.clear()
    actions = _actions(3)
    pid = await approval.create_plan("u5", actions)
    await approval.approve(pid, "u5")

    for a in actions:
        claimed = await approval.claim_action(pid, a["action_id"], "u5")
        assert claimed is True
        await approval.complete_action(pid, a["action_id"], "u5", {"link": "https://cal.google.com"})

    await approval.mark_done(pid, "u5")
    p = await approval.get_plan(pid, "u5")
    assert p["status"] == "done"


@pytest.mark.asyncio
async def test_claim_not_double_claimable():
    approval._mem_plans.clear()
    approval._mem_execs.clear()
    pid = await approval.create_plan("u6", _actions(1))
    await approval.approve(pid, "u6")
    c1 = await approval.claim_action(pid, "a1", "u6")
    c2 = await approval.claim_action(pid, "a1", "u6")
    assert c1 is True
    assert c2 is False   # already running


@pytest.mark.asyncio
async def test_wrong_user_cannot_approve():
    approval._mem_plans.clear()
    pid = await approval.create_plan("u7", _actions(1))
    ok = await approval.approve(pid, "wrong-user")
    assert ok is False
    p = await approval.get_plan(pid, "u7")
    assert p["status"] == "pending"
