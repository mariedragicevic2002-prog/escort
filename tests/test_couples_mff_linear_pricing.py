"""Couples MFF: incall.mff is hourly rate, pro-rated by booking duration."""

from __future__ import annotations

from templates.confirmations import calculate_price


def test_couples_mff_one_hour_default_hourly():
    assert calculate_price(60, "couples_mff", "incall") == 1000


def test_couples_mff_ninety_minutes_linear():
    assert calculate_price(90, "Couples MFF", "incall") == 1500


def test_couples_mff_two_hours_linear():
    assert calculate_price(120, "couples_mff", "incall") == 2000


def test_couples_mff_thirty_minutes_half_hourly():
    assert calculate_price(30, "couples_mff", "incall") == 500


def test_couples_mff_outcall_adds_travel_surcharge_once():
    # Default surcharge 100 on top of linear hourly base
    assert calculate_price(60, "couples_mff", "outcall") == 1100
    assert calculate_price(120, "couples_mff", "outcall") == 2100
