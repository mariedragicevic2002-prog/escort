"""Doubles MFF: escort-sourced vs client-sourced duration-tier pricing (Rates mff_supplied_* / couples_*)."""

from __future__ import annotations

from templates.confirmations import calculate_price


def test_doubles_mff_escort_sources_two_hours_uses_mff_supplied_hourly_defaults():
    bf = {
        "experience_type": "doubles_mff",
        "booking_type": "doubles_mff",
        "escort_supply_source": "escort",
    }
    # Defaults: mff_supplied_60=1600 → 2h = 3200
    assert calculate_price(120, "doubles_mff", "incall", bf) == 3200


def test_doubles_mff_escort_sources_display_label_with_space():
    bf = {
        "experience_type": "Doubles MFF",
        "booking_type": "doubles_mff",
        "escort_supply_source": "escort",
    }
    assert calculate_price(120, "Doubles MFF", "incall", bf) == 3200


def test_doubles_mff_client_brings_partner_two_hours_uses_couples_tier_defaults():
    bf = {
        "experience_type": "doubles_mff",
        "booking_type": "doubles_mff",
        "escort_supply_source": "client",
    }
    # Defaults: couples_mmf_60=800 → 2h = 1600
    assert calculate_price(120, "doubles_mff", "incall", bf) == 1600


def test_doubles_mff_booking_type_fallback_when_experience_string_odd():
    bf = {
        "experience_type": "",
        "booking_type": "doubles_mff",
        "escort_supply_source": "escort",
    }
    assert calculate_price(60, "gfe", "incall", bf) == 1600
