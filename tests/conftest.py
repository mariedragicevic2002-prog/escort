"""
Shared pytest fixtures for the booking test suite.

Ground rules:
  - No network, no real Postgres, no real Google APIs, no real Anthropic/Gemini calls.
  - Fixtures lean on tests/fakes.py for the dependencies above.
  - The project isn't installed as a package, so we put the repo root on sys.path here
    and tests can just `from core...` / `from services...` as the app does.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Must run before any test module imports ``main_v2.application`` (validate_config otherwise
# treats DEBUG=True + PYTHONANYWHERE_* as a fatal misconfiguration).
os.environ["PYTEST_RUNNING"] = "1"

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Set required env vars before any project import that reads config at import time.
# DEBUG=True so production-only fail-fast guards (H8 DB init, SECRET_KEY validation)
# stay dormant under unit tests — we mock the DB rather than connect to one.
os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-calendar@group.calendar.google.com")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")

# Config uses admin_settings only for operational keys; tests have no real DB — feed calendar_id from env.
import core.settings_manager as _sm

_orig_get_setting = _sm.get_setting


def _get_setting_patched(key, default=None):
    if key == "calendar_id":
        cal = os.environ.get("GOOGLE_CALENDAR_ID", "").strip()
        if cal:
            return cal
    return _orig_get_setting(key, default)


_sm.get_setting = _get_setting_patched  # type: ignore[method-assign]

from tests.fakes import (  # noqa: E402 — sys.path set up above
    FakeCalendarService,
    FakeDB,
    FakeStateManager,
    make_calendar_event,
)


@pytest.fixture
def fake_db() -> FakeDB:
    return FakeDB()


@pytest.fixture
def fake_state_manager() -> FakeStateManager:
    return FakeStateManager()


@pytest.fixture
def fake_calendar_service() -> FakeCalendarService:
    return FakeCalendarService()


@pytest.fixture
def event_factory():
    """Returns the make_calendar_event helper for readable test construction."""
    return make_calendar_event


@pytest.fixture
def booking_details():
    """Canonical booking-window dict shape used by check_conflict / _parse_booking_window."""
    return {
        "date": "2026-05-01",
        "time": "14:00",
        "duration": 60,
        "incall_outcall": "incall",
    }

# --- Mobile sync API test fixtures ---
import pytest
from main_v2.application import app

@pytest.fixture
def client():
    with app.test_client() as client:
        yield client

@pytest.fixture
def schedule_auth_headers():
    # Replace with actual auth header logic as needed
    return {"Authorization": "Bearer testtoken"}
