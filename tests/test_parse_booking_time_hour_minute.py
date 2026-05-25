"""Regression: HH:MM string times must format deposit SMS and calendar parsing."""

from __future__ import annotations

import datetime as dt

import pytest

from services.calendar.booking_window import _parse_booking_window, parse_booking_time_hour_minute


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("03:30", (3, 30)),
        ("3:30", (3, 30)),
        ("18:00", (18, 0)),
        ("23:45:00", (23, 45)),
        ((14, 0), (14, 0)),
        (dt.time(3, 30), (3, 30)),
    ],
)
def test_parse_booking_time_hour_minute(raw, expected):
    assert parse_booking_time_hour_minute(raw) == expected


def test_mandatory_deposit_acknowledgement_line_uses_webform_time_string(monkeypatch):
    """Webform posts ``time`` as ``HH:MM``; previously defaulted to 6pm."""
    from templates.deposit_templates import _format_mandatory_deposit_acknowledgement_line

    monkeypatch.setattr(
        "templates.booking_reconfirmation._format_experience",
        lambda x: str(x),
        raising=False,
    )

    line = _format_mandatory_deposit_acknowledgement_line(
        {
            "date": "2026-05-08",
            "time": "03:30",
            "experience_type": "Doubles MMF",
            "client_name": "",
        },
        client_name=None,
    )
    assert "6pm" not in line.lower()
    assert "3:30am" in line.lower()


def test_parse_booking_window_accepts_hh_mm_ss_string(monkeypatch):
    import pytz

    perth = pytz.timezone("Australia/Perth")
    monkeypatch.setattr("utils.timezone.get_local_timezone", lambda: perth)

    start, end = _parse_booking_window(
        {"date": "2026-05-08", "time": "03:30:00", "duration": 60}
    )
    assert start is not None and end is not None
    assert start.hour == 3 and start.minute == 30
