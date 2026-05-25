from services.analytics_service import AnalyticsService


class _FakeDB:
    def execute_query(self, query, params=None, fetch=False, **_kwargs):
        q = str(query)
        if "FROM conversation_events" in q:
            return [
                {"to_state": "NEW", "count": 10, "unique_clients": 8},
                {"to_state": "COLLECTING", "count": 7, "unique_clients": 6},
                {"to_state": "CHECKING_AVAILABILITY", "count": 5, "unique_clients": 4},
                {"to_state": "DEPOSIT_REQUIRED", "count": 2, "unique_clients": 2},
                {"to_state": "CONFIRMED", "count": 4, "unique_clients": 3},
            ]
        if "FROM conversation_states" in q:
            return []
        return [] if fetch else None


def test_funnel_prefers_event_sourced_data():
    svc = AnalyticsService(_FakeDB())  # type: ignore[arg-type]
    data = svc.get_booking_funnel_analytics(days=30)
    assert data["snapshot_based"] is False
    assert data["funnel"]["COLLECTING"] == 7
