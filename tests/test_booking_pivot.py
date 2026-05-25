"""Tests for mid-conversation booking lane pivot helpers."""

from __future__ import annotations

from handlers.new_conv.booking_pivot import (
    STRUCTURED_ENQUIRY_INTENT_LANE,
    canonical_booking_lane,
    collecting_should_clear_for_structured_enquiry_switch,
)


def test_canonical_lane_dinner_from_booking_type():
    assert canonical_booking_lane({"booking_type": "dinner_date"}) == "dinner_date"


def test_canonical_lane_couples_from_experience():
    assert canonical_booking_lane({"experience_type": "couples_mff"}) == "couples_booking"


def test_collecting_same_lane_no_clear():
    state = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "booking_type": "dinner_date",
    }
    assert not collecting_should_clear_for_structured_enquiry_switch("dinner_date_enquiry", state)


def test_collecting_switch_lane_yes_clear():
    state = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "booking_type": "dinner_date",
    }
    assert collecting_should_clear_for_structured_enquiry_switch("couples_booking", state)


def test_generic_to_structured_clears():
    state = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "booking_type": None,
    }
    assert collecting_should_clear_for_structured_enquiry_switch("doubles_enquiry", state)


def test_non_structured_intent_never_auto_lane_clear():
    state = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "booking_type": "dinner_date",
    }
    assert "msog_enquiry" not in STRUCTURED_ENQUIRY_INTENT_LANE
    assert not collecting_should_clear_for_structured_enquiry_switch("msog_enquiry", state)
