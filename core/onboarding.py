"""Deterministic Connect validation and durable onboarding; no model/provider calls."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

try:
    from db import repo
except Exception:  # psycopg not installed in test env
    repo = None  # type: ignore[assignment]


class InvalidSubmission(ValueError):
    """A safe validation message that never includes submitted values."""


@dataclass(frozen=True)
class OnboardingResult:
    phone_verified: bool
    link_code: str | None = field(repr=False)


def phone_number(value: object) -> str:
    if not isinstance(value, str):
        raise InvalidSubmission("Enter a phone number with country code, such as +14805551234.")
    number = re.sub(r"[\s().-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{6,14}", number):
        raise InvalidSubmission("Enter a phone number with country code, such as +14805551234.")
    return number


def _money(value: object, label: str, default=None) -> Decimal | None:
    if isinstance(value, str):
        value = value.strip()
    if value is None or value == "":
        if default is not None:
            return default
        if label == "Weekly cap":
            return None
        raise InvalidSubmission(f"{label} is required.")
    try:
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise ValueError()
        amount = Decimal(str(value).strip())
        if not amount.is_finite() or not 0 <= amount <= Decimal("9999999999.99"):
            raise ValueError()
        if amount != amount.quantize(Decimal(".01")):
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation):
        raise InvalidSubmission(f"{label} must be a nonnegative dollar amount with at most two decimal places.") from None


def _bills(value: object) -> list[dict]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        if len(value) > 8000:
            raise InvalidSubmission("Enter no more than 30 monthly bills.")
        parsed = []
        for line in re.split(r"[;\n]", value):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3:
                raise InvalidSubmission("For each bill, enter Name | Amount | Due day, separated by semicolons.")
            parsed.append(dict(zip(("name", "amount", "due_day"), parts)))
        value = parsed
    if not isinstance(value, list) or len(value) > 30:
        raise InvalidSubmission("Enter no more than 30 monthly bills.")
    result, names = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise InvalidSubmission("Each bill needs a name, amount, and due day.")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise InvalidSubmission("Bill names must contain 1–100 characters.")
        name = name.strip()
        if name.casefold() in names:
            raise InvalidSubmission("Use a different name for each monthly bill.")
        names.add(name.casefold())
        day = raw.get("due_day")
        if isinstance(day, bool) or not re.fullmatch(r"(?:[1-9]|[12][0-9]|3[01])", str(day)):
            raise InvalidSubmission("Bill due days must be whole numbers from 1 to 31.")
        result.append({"name": name, "amount": _money(raw.get("amount"), "Bill amount"), "due_day": int(day)})
    return result


def submission_fields(selection: dict) -> dict:
    """Accept flat JSON and the form envelopes used by card clients."""
    fields = {key: value for key, value in selection.items() if key != "action"}
    for key in ("values", "form_data", "fields"):
        if key in selection:
            if not isinstance(selection[key], dict):
                raise InvalidSubmission("Please submit the Connect form again.")
            for name, value in selection[key].items():
                if name in fields and fields[name] != value:
                    raise InvalidSubmission("The form contains conflicting values. Please submit it again.")
                fields[name] = value
    return fields


def link_code_hash(code: str) -> str:
    return hashlib.sha256(("margin:phone-link:" + code.upper()).encode()).hexdigest()


async def save(user_id: str, selection: dict) -> OnboardingResult:
    fields = submission_fields(selection)
    if fields.get("_invalid"):
        raise InvalidSubmission("Please use the Connect form to submit your details.")
    phone = phone_number(fields.get("phone"))
    income = _money(fields.get("monthly_income"), "Monthly income")
    cap = _money(fields.get("weekly_cap"), "Weekly cap")
    savings = _money(fields.get("savings_goal"), "Savings goal", Decimal(0))
    bills = _bills(fields.get("bills"))
    import string as _string
    code = "".join(secrets.choice(_string.ascii_uppercase + _string.digits) for _ in range(6))
    verified = await repo.save_onboarding(
        user_id, phone=phone, monthly_income=income, weekly_cap=cap,
        savings_goal=savings, bills=bills, code_hash=link_code_hash(code),
        expires=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    return OnboardingResult(verified, None if verified else code)
