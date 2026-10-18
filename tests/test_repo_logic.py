"""Pure validation and budget arithmetic, with no database or credentials."""

from datetime import date
from decimal import Decimal

import pytest

from db import repo


@pytest.mark.parametrize("today,cap,expected", [
    (date(2026, 2, 1), None, 175.0),  # 28 inclusive days / 7 = four weeks
    (date(2026, 2, 15), None, 350.0),  # fractional weeks: 14 days / 7
    (date(2026, 2, 22), None, 700.0),
    (date(2026, 2, 28), None, 700.0),  # day-31 bill clamps to February 28
    (date(2024, 2, 29), None, 700.0),
    (date(2026, 2, 1), 0, 0.0),
    (date(2026, 2, 1), 100, 100.0),
])
def test_safe_to_spend_month_boundaries_and_zero_cap(today, cap, expected):
    budget = {"monthly_income": 1000, "savings_goal": 100, "weekly_cap": cap}
    bills = [{"amount": 100, "due_day": 31}]
    assert repo._safe_to_spend(budget, bills, Decimal(100), today) == expected


def test_budget_uses_fractional_weeks_and_ignores_past_due_days():
    budget = {"monthly_income": 700, "savings_goal": 0, "weekly_cap": None}
    assert repo._safe_to_spend(
        budget, [{"amount": 600, "due_day": 1}], Decimal(0), date(2026, 2, 17)
    ) == 408.33  # 700 / (12 / 7), not rounded up to two weeks


def test_budget_retains_deficit():
    assert repo._safe_to_spend(
        {"monthly_income": 100, "savings_goal": 100, "weekly_cap": None},
        [], Decimal(30), date(2026, 2, 28),
    ) == -30


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf")])
def test_invalid_money_is_rejected(amount):
    with pytest.raises(ValueError):
        repo._money(amount)


@pytest.mark.parametrize("value", ["2026-10-18", "2026-10-18T12:00:00"])
def test_naive_times_are_rejected(value):
    with pytest.raises(ValueError, match="timezone"):
        repo._datetime(value)


@pytest.mark.parametrize("actions", [
    [{"action_id": "a1", "type": "send_email", "params": {}}],
    [{"action_id": "a1", "type": "create_draft", "params": []}],
    [{"action_id": "", "type": "create_event", "params": {}}],
    [{"action_id": "a1", "type": "create_event", "params": {}}] * 2,
    [{"action_id": "a1", "type": "create_event", "params": {}, "user_id": "other"}],
])
def test_invalid_plan_actions_are_rejected(actions):
    with pytest.raises(ValueError):
        repo._actions("plan", "student", actions)
