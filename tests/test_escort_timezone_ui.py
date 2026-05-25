"""
Escort-local time: config resolution, validation, and UI surfaces (booking, health, schedule).

Uses the same DB-free settings patch pattern as tests/conftest.py. Full-app tests import
``main_v2.application`` once (Flask app + blueprints); keep these tests read-only (GET).
"""

from __future__ import annotations

from datetime import datetime

import pytest

import config
import pytz


def _patch_get_setting(monkeypatch, mapping: dict):
    """Replace ``config.get_setting`` with a dict-backed fake."""

    def _fake(key: str, default=None):
        if key in mapping:
            v = mapping[key]
            return v if v is not None else default
        return default

    monkeypatch.setattr(config, "get_setting", _fake)


def test_get_effective_escort_timezone_prefers_saved_timezone(monkeypatch):
    _patch_get_setting(
        monkeypatch,
        {
            "timezone": "Australia/Darwin",
            "location_timezone": "Europe/London",
            "city": "Sydney",
        },
    )
    assert config.get_effective_escort_timezone() == "Australia/Darwin"


def test_get_effective_escort_timezone_falls_back_to_location_timezone(monkeypatch):
    _patch_get_setting(
        monkeypatch,
        {
            "timezone": "",
            "location_timezone": "Europe/London",
            "city": "Sydney",
        },
    )
    assert config.get_effective_escort_timezone() == "Europe/London"


def test_get_effective_escort_timezone_falls_back_to_city_map(monkeypatch):
    _patch_get_setting(
        monkeypatch,
        {
            "timezone": "",
            "location_timezone": "",
            "city": "Brisbane",
        },
    )
    assert config.get_effective_escort_timezone() == "Australia/Brisbane"


def test_get_timezone_for_city_subiaco_is_perth():
    assert config.get_timezone_for_city("Subiaco") == "Australia/Perth"


def test_get_timezone_for_city_perth_substring():
    assert config.get_timezone_for_city("East Perth") == "Australia/Perth"
    assert config.get_timezone_for_city("Hotel, Perth WA") == "Australia/Perth"


def test_get_effective_escort_timezone_default_when_empty(monkeypatch):
    _patch_get_setting(
        monkeypatch,
        {
            "timezone": "",
            "location_timezone": "",
            "city": "",
        },
    )
    assert config.get_effective_escort_timezone() == config.DEFAULT_TIMEZONE


def test_utils_get_current_datetime_is_in_escort_zone(monkeypatch):
    _patch_get_setting(
        monkeypatch,
        {
            "timezone": "Pacific/Auckland",
            "location_timezone": "",
            "city": "",
        },
    )
    from utils import timezone as tzmod

    dt = tzmod.get_current_datetime()
    assert dt.tzinfo is not None
    # pytz / zoneinfo name
    assert getattr(dt.tzinfo, "zone", None) == "Pacific/Auckland" or "Auckland" in str(
        dt.tzinfo
    )


def test_field_validator_rejects_yesterday_in_escort_local(monkeypatch):
    """Reject yesterday vs escort-local \"today\" via patched ``get_current_datetime``."""
    from datetime import date

    from booking.field_validator import FieldValidator

    perth = pytz.timezone("Australia/Perth")
    fixed_now = perth.localize(datetime(2026, 4, 10, 14, 30, 0))

    monkeypatch.setattr("utils.timezone.get_local_timezone", lambda: perth)
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: fixed_now)
    v = FieldValidator()
    ok, err = v.validate_date(date(2026, 4, 9))  # yesterday vs escort "today" 10 Apr
    assert ok is False
    assert "past" in err.lower()


@pytest.fixture(scope="module")
def flask_app():
    import main_v2.application as appmod

    return appmod.app


def test_booking_template_embeds_window_escort_iana(flask_app, monkeypatch):
    from flask import render_template
    from core.rates_from_config import get_default_pricing

    monkeypatch.setattr(
        config,
        "get_effective_escort_timezone",
        lambda: "Antarctica/Troll",  # distinctive
    )
    p = get_default_pricing()
    with flask_app.test_request_context("/"):
        html = render_template(
            "booking.html",
            location={"city": "", "hotel_name": "", "address": "", "display_name": ""},
            phone="+610400000000",
            token="tok",
            phone_locked=True,
            escort_name="Test",
            escort_timezone=config.get_effective_escort_timezone(),
            google_maps_api_key="",
            rates=p.get("incall", {}),
            outcall_rates=p.get("outcall", {}),
            surcharge=p.get("surcharge", 0),
            place_autocomplete_center=None,
        )
    s = html if isinstance(html, str) else html.decode("utf-8", errors="replace")
    assert "Antarctica/Troll" in s
    assert "window.ESCORT_IANA" in s or "ESCORT_IANA" in s


def test_health_dashboard_authenticated_shows_escort_iana_and_time_format(flask_app, monkeypatch):
    """ESCORT_IANA and escort-local time helpers live in ``extra_scripts`` (authenticated only)."""
    # `health` did `from config import get_effective_escort_timezone` — patch that binding.
    import admin.blueprints.health as health_mod

    monkeypatch.setattr(
        health_mod,
        "get_effective_escort_timezone",
        lambda: "Indian/Maldives",
    )
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["health_authenticated"] = True
    r = c.get("/health")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Indian/Maldives" in body
    assert "const ESCORT_IANA" in body
    assert "formatToEscortLocalTime" in body


def test_schedule_page_default_date_follows_local_clock(flask_app, monkeypatch):
    """Day-view date input defaults to escort-local YYYY-MM-DD (authenticated schedule page)."""
    import pytz
    from admin.blueprints.schedule import page_routes

    hnl = pytz.timezone("Pacific/Honolulu")
    fixed = hnl.localize(datetime(2026, 8, 15, 2, 0, 0))

    monkeypatch.setattr(page_routes, "_get_current_datetime", lambda: fixed)

    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess["schedule_authenticated"] = True
    r = c.get("/schedule")
    assert r.status_code == 200
    assert 'value="2026-08-15"' in r.get_data(as_text=True)


def test_infer_booking_time_uses_escort_localize(monkeypatch):
    """inferred datetimes for natural-language times are wall times in the escort zone."""
    _patch_get_setting(
        monkeypatch,
        {"timezone": "America/Toronto", "location_timezone": "", "city": ""},
    )
    from utils import timezone as tzmod
    from utils.time_parser import infer_requested_datetime_for_booking

    fixed = pytz.timezone("America/Toronto").localize(datetime(2026, 6, 2, 15, 0, 0))
    monkeypatch.setattr(tzmod, "get_current_datetime", lambda: fixed)
    out = infer_requested_datetime_for_booking("8pm tomorrow", now=None)
    assert out is not None
    z = getattr(out.tzinfo, "zone", None) or str(out.tzinfo)
    assert "Toronto" in z or "America" in str(z)
