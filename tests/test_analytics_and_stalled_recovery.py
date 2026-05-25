from __future__ import annotations

from services.analytics_service import AnalyticsService
from services.stalled_recovery_service import check_and_send_stalled_nudges
from tests.fakes import FakeDB, FakeStateManager


def test_funnel_dropoffs_are_never_negative():
    db = FakeDB()
    db.enqueue_result(
        [
            {"current_state": "NEW", "count": 5, "unique_clients": 5},
            {"current_state": "COLLECTING", "count": 7, "unique_clients": 7},
            {"current_state": "CHECKING_AVAILABILITY", "count": 3, "unique_clients": 3},
            {"current_state": "DEPOSIT_REQUIRED", "count": 4, "unique_clients": 4},
            {"current_state": "CONFIRMED", "count": 6, "unique_clients": 6},
        ]
    )
    data = AnalyticsService(db).get_booking_funnel_analytics(30)  # type: ignore[arg-type]
    assert data["snapshot_based"] is True
    assert all(v >= 0 for v in data["drop_offs"].values())


def test_stalled_recovery_handles_extended_enquiry_message(monkeypatch):
    db = FakeDB()
    db.set_handler(
        "FROM conversation_states",
        lambda _q, _p: [
            {"phone_number": "+61400000331", "current_state": "EXTENDED_ENQUIRY"},
            {"phone_number": "+61400000332", "current_state": "COLLECTING"},
        ],
    )
    monkeypatch.setattr("services.stalled_recovery_service.send_sms", lambda *_a, **_k: True)

    sm = FakeStateManager()
    sent = check_and_send_stalled_nudges(sm, db)
    outbound_messages = [m[2] for m in sm.messages]

    assert sent == 2
    assert any("ask another question" in msg.lower() for msg in outbound_messages)
    assert any("continue your booking" in msg.lower() for msg in outbound_messages)
