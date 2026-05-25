from __future__ import annotations

from datetime import datetime

import pytz

from admin.blueprints.schedule.helpers import _parse_event_description
from core.state_manager import StateManager
from services.calendar import event_crud
from services import safety_screening_service
from tests.fakes import FakeDB

_NOTES_PARAM_INDEX = 15  # position of 'notes' in the _insert_booking_row params tuple


class _FakeInsertDB:
    """Captures INSERT params and returns a fake id for RETURNING id."""

    def __init__(self):
        self.calls: list[tuple] = []
        self._count = 0

    def execute_query(self, sql, params=None, fetch=False):
        self._count += 1
        if params is not None:
            self.calls.append(params)
        return [{"id": f"evt-{self._count}"}]


def test_create_calendar_event_writes_doubles_supply_line_from_booking_status(monkeypatch):
    db = _FakeInsertDB()
    monkeypatch.setattr(event_crud, "_get_db", lambda: db)

    tz = pytz.timezone("Australia/Adelaide")
    start = tz.localize(datetime(2026, 5, 1, 10, 0))
    end = tz.localize(datetime(2026, 5, 1, 11, 0))
    monkeypatch.setattr(event_crud, "_parse_booking_window", lambda _details: (start, end))

    event_id = event_crud.create_calendar_event(
        {
            "date": "2026-05-01",
            "time": "10:00",
            "duration": 60,
            "experience_type": "doubles_mff",
            "incall_outcall": "incall",
            "booking_status": "doubles_supply_escort",
            "client_name": "Client",
        },
        "+61400000001",
    )

    assert event_id == "evt-1"
    assert len(db.calls) >= 1
    notes = db.calls[0][_NOTES_PARAM_INDEX]
    assert "Organise other escort: yes" in notes


def test_create_calendar_event_prefers_explicit_organise_flag(monkeypatch):
    db = _FakeInsertDB()
    monkeypatch.setattr(event_crud, "_get_db", lambda: db)

    tz = pytz.timezone("Australia/Adelaide")
    start = tz.localize(datetime(2026, 5, 1, 12, 0))
    end = tz.localize(datetime(2026, 5, 1, 13, 0))
    monkeypatch.setattr(event_crud, "_parse_booking_window", lambda _details: (start, end))

    event_id = event_crud.create_calendar_event(
        {
            "date": "2026-05-01",
            "time": "12:00",
            "duration": 60,
            "experience_type": "Doubles MMF",
            "incall_outcall": "incall",
            "booking_status": "doubles_supply_escort",
            "organise_other_escort": "no",
            "client_name": "Client",
        },
        "+61400000002",
    )

    assert event_id == "evt-1"
    assert len(db.calls) >= 1
    notes = db.calls[0][_NOTES_PARAM_INDEX]
    assert "Organise other escort: no" in notes


def test_get_booking_fields_includes_doubles_supply_metadata():
    db = FakeDB()
    db.set_handler(
        "SELECT * FROM conversation_states",
        lambda _query, params: [
            {
                "phone_number": params[0],
                "date": "2026-05-01",
                "time": "10:00:00",
                "duration": 60,
                "experience_type": "doubles_mff",
                "incall_outcall": "incall",
                "outcall_address": None,
                "client_name": "Client",
                "booking_type": "doubles_mff",
                "booking_status": "doubles_supply_confirmed",
                "escort_supply_source": "client",
                "dinner_restaurant": None,
                "dinner_after_preference": None,
                "dinner_client_address": None,
                "dinner_client_outside_15km": False,
                "version": 1,
                "current_state": "COLLECTING",
            }
        ],
    )
    sm = StateManager(db_service=db)

    fields = sm.get_booking_fields("+61400000003")

    assert fields["booking_status"] == "doubles_supply_confirmed"
    assert fields["escort_supply_source"] == "client"


def test_create_calendar_event_writes_safety_screening_line_when_watchlist_match(monkeypatch):
    db = _FakeInsertDB()
    monkeypatch.setattr(event_crud, "_get_db", lambda: db)

    tz = pytz.timezone("Australia/Adelaide")
    start = tz.localize(datetime(2026, 5, 1, 14, 0))
    end = tz.localize(datetime(2026, 5, 1, 15, 0))
    monkeypatch.setattr(event_crud, "_parse_booking_window", lambda _details: (start, end))
    monkeypatch.setattr(
        safety_screening_service,
        "lookup_flagged_number",
        lambda _phone: {"matched": True, "normalized_phone": "+61400000004"},
    )

    event_id = event_crud.create_calendar_event(
        {
            "date": "2026-05-01",
            "time": "14:00",
            "duration": 60,
            "experience_type": "GFE",
            "incall_outcall": "incall",
            "client_name": "Client",
        },
        "+61400000004",
    )

    assert event_id == "evt-1"
    assert len(db.calls) >= 1
    notes = db.calls[0][_NOTES_PARAM_INDEX]
    assert "Safety screening: flagged watchlist match" in notes


def test_parse_event_description_extracts_safety_screening_status():
    description = "\n".join(
        [
            "Name: Client",
            "Phone: +61400000005",
            "Safety screening: flagged watchlist match",
            "Duration: 1 hour",
            "Experience: GFE",
            "Type: Incall",
        ]
    )

    parsed = _parse_event_description(description)

    assert parsed["safety_screening_status"] == "flagged watchlist match"


def test_parse_event_description_special_requests_and_legacy_notes():
    d1 = _parse_event_description("Special requests: Please dim lights\nType: Incall")
    assert d1["special_requests"] == "Please dim lights"

    d2 = _parse_event_description("Notes: Old format note")
    assert d2["notes"] == "Old format note"
    assert d2["special_requests"] == "Old format note"


def test_parse_event_description_extracts_mmf_preferences_with_core_fields():
    description = "\n".join(
        [
            "Name: Joe",
            "Phone: +61400000999",
            "Experience: Doubles MMF",
            "Type: incall",
            "Organise other escort: yes",
            "MMF Exploration: Humiliation, Bisexual",
        ]
    )

    parsed = _parse_event_description(description)

    assert parsed["phone_number"] == "+61400000999"
    assert parsed["location_type"] == "incall"
    assert parsed["organise_other_escort"] == "yes"
    assert parsed["preferences"] == "Humiliation, Bisexual"
