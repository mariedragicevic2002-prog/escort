"""Shared booking-status substate constants and helpers."""

from __future__ import annotations

DOUBLES_SUPPLY_GATE = "doubles_supply_gate"
DOUBLES_SUPPLY_CONFIRMED = "doubles_supply_confirmed"
DOUBLES_SUPPLY_ESCORT = "doubles_supply_escort"
MANUAL_REVIEW_PENDING = "manual_review_pending"

DOUBLES_SUPPLY_STATUSES = frozenset(
    {
        DOUBLES_SUPPLY_GATE,
        DOUBLES_SUPPLY_CONFIRMED,
        DOUBLES_SUPPLY_ESCORT,
    }
)


def is_doubles_supply_status(value: str | None) -> bool:
    return (value or "").strip().lower() in DOUBLES_SUPPLY_STATUSES


def is_doubles_supply_escort(value: str | None) -> bool:
    return (value or "").strip().lower() == DOUBLES_SUPPLY_ESCORT
