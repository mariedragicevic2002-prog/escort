"""Tests for Phase 3 schema migration items:
  - booking_history appending (idempotent, both confirmation paths)
  - awaiting_yes_set_at dual-write (typed column written alongside text)
  - conversation_context reads booking_history as primary booking list source
"""
from __future__ import annotations

from datetime import datetime, timezone


from tests.fakes import FakeDB


# ---------------------------------------------------------------------------
# booking_history append helper
# ---------------------------------------------------------------------------

class _CapturingDB:
    """Records all execute_query calls for assertion."""
    def __init__(self):
        self.inserts: list[tuple[str, tuple]] = []

    def execute_query(self, query, params=(), fetch=None, conn=None, **_kw):
        if "booking_history" in str(query):
            self.inserts.append((str(query), tuple(params) if params else ()))
        return []


def _make_sm(db):
    from core.state_manager import StateManager
    sm = StateManager.__new__(StateManager)
    sm.db = db
    return sm


def test_append_booking_history_writes_correct_row():
    db = _CapturingDB()
    sm = _make_sm(db)
    confirmed_at = datetime(2026, 8, 1, 14, 0, 0, tzinfo=timezone.utc)
    booking_fields = {
        "date": "2026-08-01",
        "time": "14:00:00",
        "duration": 60,
        "experience_type": "gfe",
        "incall_outcall": "incall",
        "booking_type": "incall",
        "deposit_amount": None,
    }
    result = sm.append_booking_history(
        "+61400000001", booking_fields,
        confirmed_at=confirmed_at, deposit_paid=False, total_cost=350,
    )
    assert result is True
    assert len(db.inserts) == 1
    _query, _params = db.inserts[0]
    assert "booking_history" in _query
    assert "ON CONFLICT" in _query  # idempotency guard
    assert _params[0] == "+61400000001"  # phone_number
    assert _params[1] == confirmed_at


def test_append_booking_history_is_idempotent():
    """Two calls with identical phone + confirmed_at should both succeed without error."""
    calls = []
    class _DB:
        def execute_query(self, query, params=(), fetch=None, conn=None, **_kw):
            calls.append(1)
            return []

    sm = _make_sm(_DB())
    confirmed_at = datetime(2026, 8, 2, 10, 0, 0, tzinfo=timezone.utc)
    bf = {"date": "2026-08-02", "time": "10:00", "duration": 60}
    sm.append_booking_history("+61400000002", bf, confirmed_at=confirmed_at)
    sm.append_booking_history("+61400000002", bf, confirmed_at=confirmed_at)
    assert len(calls) == 2  # both calls issued; DB handles ON CONFLICT


def test_append_booking_history_tolerates_db_error():
    class _BadDB:
        def execute_query(self, *a, **kw):
            raise RuntimeError("connection lost")

    sm = _make_sm(_BadDB())
    result = sm.append_booking_history("+61400000003", {})
    assert result is False  # should not propagate exception


# ---------------------------------------------------------------------------
# awaiting_yes_set_at dual-write
# ---------------------------------------------------------------------------

def test_mark_awaiting_confirmation_dual_writes_timestamp():
    """mark_awaiting_confirmation must set both awaiting_yes_set_at (str) and awaiting_yes_set_at_ts (datetime)."""
    from core.state_manager import StateManager

    sm = StateManager.__new__(StateManager)
    written = {}

    def _fake_update_fields(phone_number, updates, conn=None):
        written.update(updates)
        return True

    sm.update_fields = _fake_update_fields

    sm.mark_awaiting_confirmation(
        "+61400000004",
        is_outcall=False,
        deposit_required=True,
        deposit_amount=100,
        deposit_reason="overnight",
    )

    assert "awaiting_yes_set_at" in written
    assert isinstance(written["awaiting_yes_set_at"], str)
    assert "awaiting_yes_set_at_ts" in written
    assert isinstance(written["awaiting_yes_set_at_ts"], datetime)


def test_set_awaiting_yes_flags_dual_writes_timestamp():
    from core.state_manager import StateManager

    sm = StateManager.__new__(StateManager)
    written = {}

    def _fake_update_fields(phone_number, updates, conn=None):
        written.update(updates)
        return True

    sm.update_fields = _fake_update_fields
    sm.set_awaiting_yes_flags("+61400000005", is_outcall=True)

    assert isinstance(written.get("awaiting_yes_set_at"), str)
    assert isinstance(written.get("awaiting_yes_set_at_ts"), datetime)


def test_awaiting_yes_set_at_ts_in_allowed_fields():
    from core.state_manager import ALLOWED_STATE_UPDATE_FIELDS
    assert "awaiting_yes_set_at_ts" in ALLOWED_STATE_UPDATE_FIELDS


# ---------------------------------------------------------------------------
# conversation_context reads booking_history as primary booking list
# ---------------------------------------------------------------------------

def test_conversation_context_prefers_booking_history():
    """booking_history rows should populate bookings before analytics/state fallback."""
    db = FakeDB()
    db.set_handler("FROM client_preferences", lambda _q, _p: [
        {"preferred_duration": 60, "preferred_experience": "gfe",
         "preferred_location": "incall", "total_bookings": 2, "last_booking_date": "2026-08-01"}
    ])
    db.set_handler("FROM booking_history", lambda _q, _p: [
        {"date": "2026-08-01", "duration": 60, "experience_type": "gfe",
         "incall_outcall": "incall", "confirmed_at": "2026-08-01T14:00:00+00:00"},
        {"date": "2026-07-01", "duration": 90, "experience_type": "pse",
         "incall_outcall": "incall", "confirmed_at": "2026-07-01T10:00:00+00:00"},
    ])
    # These should NOT be called when booking_history returns rows.
    analytics_called = []
    db.set_handler("FROM booking_analytics", lambda _q, _p: analytics_called.append(1) or [])
    db.set_handler("FROM conversation_states", lambda _q, _p: [])

    from core.conversation_context import ConversationContext
    ctx = ConversationContext(db).get_client_context("+61400000006")

    assert len(ctx["booking_history"]) == 2
    assert analytics_called == [], "booking_analytics should not be queried when booking_history has data"


def test_conversation_context_falls_back_to_analytics_when_history_empty():
    """When booking_history is empty, analytics rows should still populate bookings."""
    db = FakeDB()
    db.set_handler("FROM client_preferences", lambda _q, _p: [
        {"preferred_duration": None, "preferred_experience": None,
         "preferred_location": None, "total_bookings": 0, "last_booking_date": None}
    ])
    db.set_handler("FROM booking_history", lambda _q, _p: [])
    db.set_handler("FROM booking_analytics", lambda _q, _p: [
        {"booking_fields": '{"date":"2026-06-01","duration":60,"experience_type":"gfe","incall_outcall":"incall"}',
         "created_at": "2026-06-01"}
    ])
    db.set_handler("FROM conversation_states", lambda _q, _p: [])

    from core.conversation_context import ConversationContext
    ctx = ConversationContext(db).get_client_context("+61400000007")

    assert len(ctx["booking_history"]) == 1
    assert ctx["booking_history"][0]["experience_type"] == "gfe"
