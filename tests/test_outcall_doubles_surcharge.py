"""Doubles MMF/MFF outcall: pair travel surcharge when escort sources second provider."""

from __future__ import annotations

from templates.confirmations import calculate_price


def test_is_doubles_escort_supplies_second_provider():
    from core.rates_from_config import is_doubles_escort_supplies_second_provider

    assert is_doubles_escort_supplies_second_provider(
        {"booking_type": "doubles_mff", "escort_supply_source": "escort"}
    )
    assert not is_doubles_escort_supplies_second_provider(
        {"booking_type": "doubles_mff", "escort_supply_source": "client"}
    )
    assert not is_doubles_escort_supplies_second_provider({"experience_type": "couples_mff"})


def test_get_outcall_travel_surcharge_pair_vs_standard():
    from core.rates_from_config import get_outcall_travel_surcharge_for_booking

    assert (
        get_outcall_travel_surcharge_for_booking(
            {"booking_type": "doubles_mff", "escort_supply_source": "escort"}
        )
        == 200
    )
    assert (
        get_outcall_travel_surcharge_for_booking(
            {"booking_type": "doubles_mff", "escort_supply_source": "client"}
        )
        == 100
    )


def test_calculate_doubles_mff_escort_outcall_uses_pair_surcharge_in_tier():
    bf = {"booking_type": "doubles_mff", "escort_supply_source": "escort"}
    assert calculate_price(90, "doubles_mff", "outcall", bf) == 1800


def test_calculate_doubles_mmf_escort_outcall_uses_pair_surcharge_in_tier():
    bf = {"booking_type": "Doubles MMF", "escort_supply_source": "escort"}
    assert calculate_price(90, "Doubles MMF", "outcall", bf) == 1700


def test_couples_mff_outcall_linear_still_standard_surcharge():
    assert calculate_price(60, "couples_mff", "outcall") == 1100


def test_format_doubles_escort_arranges_second_outcall_notice_lists_current_amounts():
    from core.rates_from_config import (
        format_doubles_escort_arranges_second_outcall_travel_notice,
        get_surcharge,
        get_surcharge_doubles_escort_supplied_outcall,
    )

    msg = format_doubles_escort_arranges_second_outcall_travel_notice()
    assert str(get_surcharge()) in msg
    assert str(get_surcharge_doubles_escort_supplied_outcall()) in msg


def test_format_doubles_escort_sourcing_waits_for_verified_deposit_notice_mmf():
    from core.rates_from_config import format_doubles_escort_sourcing_waits_for_verified_deposit_notice

    bf = {"booking_type": "Doubles MMF", "escort_supply_source": "escort"}
    msg = format_doubles_escort_sourcing_waits_for_verified_deposit_notice(bf)
    assert "other male escort" in msg.lower()
    assert "deposit" in msg.lower() and "verified" in msg.lower()


def test_format_doubles_escort_sourcing_waits_for_verified_deposit_notice_mff():
    from core.rates_from_config import format_doubles_escort_sourcing_waits_for_verified_deposit_notice

    bf = {"booking_type": "doubles_mff", "escort_supply_source": "escort"}
    msg = format_doubles_escort_sourcing_waits_for_verified_deposit_notice(bf)
    assert "other female escort" in msg.lower()


def test_format_doubles_escort_sourcing_waits_for_verified_deposit_notice_suppressed_when_client_sources():
    from core.rates_from_config import format_doubles_escort_sourcing_waits_for_verified_deposit_notice

    assert (
        format_doubles_escort_sourcing_waits_for_verified_deposit_notice(
            {"booking_type": "doubles_mmf", "escort_supply_source": "client"}
        )
        == ""
    )


def test_format_doubles_escort_sourcing_waits_for_verified_deposit_notice_empty_without_fields():
    from core.rates_from_config import format_doubles_escort_sourcing_waits_for_verified_deposit_notice

    assert format_doubles_escort_sourcing_waits_for_verified_deposit_notice(None) == ""
    assert format_doubles_escort_sourcing_waits_for_verified_deposit_notice({}) == ""
