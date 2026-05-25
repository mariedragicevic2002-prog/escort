"""Messaging when tonight's constrained window has no slots but forward slots exist."""

from __future__ import annotations

from datetime import datetime

from templates.greetings import get_available_now_3slot_message


def test_available_now_3slot_prepends_fully_booked_tonight_notice():
    slots = [(datetime(2026, 5, 5, 16, 0), "Tue 5th May 4:00pm")]
    msg = get_available_now_3slot_message(
        slots,
        city="Melbourne",
        hotel_name="",
        client_name="Sam",
        fully_booked_tonight=True,
    )
    low = msg.lower()
    assert "fully booked for the rest of tonight" in low
    assert "4:00pm" in low


def test_available_now_3slot_no_notice_when_flag_false():
    slots = [(datetime(2026, 5, 5, 16, 0), "Tue 5th May 4:00pm")]
    msg = get_available_now_3slot_message(
        slots,
        city="Melbourne",
        hotel_name="",
        client_name="Sam",
        fully_booked_tonight=False,
    )
    assert "fully booked for the rest of tonight" not in msg.lower()
