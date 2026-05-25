"""Outside-hours SMS template: greeting order, hours line, incall location."""

from __future__ import annotations

from templates.utility_templates import (
    _format_incall_location_line,
    get_outside_available_hours_message,
)


def test_incall_location_line_venue_street_city():
    assert _format_incall_location_line(
        "Park Hyatt",
        "176 Cumberland Street, The Rocks",
        "Sydney",
    ) == (
        "I'm located at Park Hyatt 176 Cumberland Street, The Rocks (Sydney)"
    )


def test_incall_location_skips_duplicate_venue_in_street():
    line = _format_incall_location_line(
        "Park Hyatt",
        "Park Hyatt 176 Cumberland Street, The Rocks",
        "Sydney",
    )
    assert line == "I'm located at Park Hyatt 176 Cumberland Street, The Rocks (Sydney)"
    assert line.count("Park Hyatt") == 1


def test_incall_location_skips_parenthetical_city_when_already_in_core():
    line = _format_incall_location_line(
        "Shangri-La Sydney",
        "176 Cumberland Street, The Rocks Sydney",
        "Sydney",
    )
    assert "(Sydney)" not in line
    assert "Shangri-La Sydney" in line


def test_outside_hours_message_opener_hours_and_location():
    msg = get_outside_available_hours_message(
        city="Sydney",
        address="176 Cumberland Street, The Rocks",
        venue_name="Park Hyatt",
        available_hours="1pm-4am, Thursday-Sunday",
        available_days="7 days a week",
        profile_url="https://example.com/profile",
        webform_url="https://example.com/b",
        client_name="Brad",
        requested_booking_time=(3, 30),
        time_slots=[("", "Thu 7th May 12:00am"), ("", "Thu 7th May 1:00am")],
        is_outcall=False,
    )
    assert msg.startswith("Hi Brad ")
    assert "\u274c" in msg.split("\n")[0]
    assert "Unfortunately 3:30am isn't available" in msg
    assert "My available hours are 1pm-4am, Thursday-Sunday" in msg
    assert (
        "I'm located at Park Hyatt 176 Cumberland Street, The Rocks (Sydney)"
        in msg
    )
    assert "same as on my booking page" not in msg.lower()
    assert msg.index("Here are my next available times") < msg.index("https://example.com/profile")
    assert msg.index("https://example.com/profile") < msg.index(
        "I'm located at Park Hyatt"
    )


def test_outside_hours_message_generic_opener_when_suppress_true():
    msg = get_outside_available_hours_message(
        city="Sydney",
        address="176 Cumberland Street, The Rocks",
        venue_name="Park Hyatt",
        available_hours="1pm-4am, Thursday-Sunday",
        available_days="7 days a week",
        profile_url="https://example.com/profile",
        webform_url="https://example.com/b",
        client_name="David",
        requested_booking_time=(4, 13),
        time_slots=[("", "Thu 7th May 1:00pm")],
        is_outcall=True,
        suppress_time_specific_opener=True,
    )
    assert msg.startswith("Hi David ")
    assert "\u274c" in msg.split("\n")[0]
    assert "Unfortunately I'm currently not available." in msg
    assert "4:13am" not in msg
    assert "isn't available" not in msg
    assert "My available hours are 1pm-4am, Thursday-Sunday" in msg
