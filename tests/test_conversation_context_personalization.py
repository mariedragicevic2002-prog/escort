from __future__ import annotations

from core.conversation_context import ConversationContext
from tests.fakes import FakeDB


def test_client_context_uses_client_preferences_as_primary_source():
    db = FakeDB()
    db.set_handler(
        "FROM client_preferences",
        lambda _q, _p: [
            {
                "preferred_duration": 60,
                "preferred_experience": "gfe",
                "preferred_location": "incall",
                "total_bookings": 4,
                "last_booking_date": "2026-06-01",
            }
        ],
    )
    db.set_handler("FROM booking_analytics", lambda _q, _p: [])
    db.set_handler("FROM conversation_states", lambda _q, _p: [])

    context = ConversationContext(db).get_client_context("+61400000222")

    assert context["total_bookings"] == 4
    assert context["preferred_duration"] == 60
    assert context["preferred_experience"] == "gfe"
    assert context["preferred_location"] == "incall"
    assert context["last_booking_date"] == "2026-06-01"


def test_smart_defaults_map_from_client_context():
    db = FakeDB()
    db.set_handler(
        "FROM client_preferences",
        lambda _q, _p: [
            {
                "preferred_duration": 90,
                "preferred_experience": "pse",
                "preferred_location": "outcall",
                "total_bookings": 2,
                "last_booking_date": "2026-07-01",
            }
        ],
    )
    db.set_handler("FROM booking_analytics", lambda _q, _p: [])
    db.set_handler("FROM conversation_states", lambda _q, _p: [])

    defaults = ConversationContext(db).get_smart_defaults("+61400000223")

    assert defaults == {
        "duration": 90,
        "experience_type": "pse",
        "incall_outcall": "outcall",
    }
