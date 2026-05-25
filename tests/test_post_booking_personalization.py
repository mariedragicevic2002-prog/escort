from __future__ import annotations

from datetime import datetime

from handlers import post_booking
from tests.fakes import FakeStateManager


def test_post_booking_greeting_recent_includes_name(monkeypatch):
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: datetime(2026, 6, 12, 22, 0))
    msg = post_booking._build_post_booking_greeting(  # noqa: SLF001
        {"client_name": "James", "date": "2026-06-12", "time": "21:00:00", "duration": 60}
    )
    assert "James" in msg
    assert "Hope you had a great time" in msg


def test_post_booking_greeting_later_window_changes_tone(monkeypatch):
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: datetime(2026, 6, 15, 22, 0))
    msg = post_booking._build_post_booking_greeting(  # noqa: SLF001
        {"client_name": "Sam", "date": "2026-06-12", "time": "21:00:00", "duration": 60}
    )
    assert "Great to hear from you again" in msg


def test_apply_smart_defaults_after_reset_does_not_override_extracted():
    phone = "+61400000123"
    sm = FakeStateManager(initial={phone: {"current_state": "POST_BOOKING"}})
    ctx = {
        "phone_number": phone,
        "state_manager": sm,
        "smart_defaults": {"duration": 60, "experience_type": "gfe", "incall_outcall": "incall"},
    }
    post_booking._apply_smart_defaults_after_reset(  # noqa: SLF001
        ctx,
        extracted={"duration": 120},
    )
    applied = {}
    for _p, update in sm.updates:
        applied.update(update)
    assert applied.get("duration") != 60
    assert applied.get("experience_type") == "gfe"
    assert applied.get("incall_outcall") == "incall"
