"""
Phase 2b — travel routing unit tests.

Covers the fallback chain in services/calendar/travel_routing.py:
  Google Distance Matrix → OSRM → default minutes
and the pure helper functions (_haversine_km, _sanitize_routing_address, OSRM buffer math).

Nothing here makes real network calls: all HTTP is mocked via unittest.mock.patch.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.calendar.travel_routing import (
    _haversine_km,
    _osrm_drive_minutes,
    _sanitize_routing_address,
    get_outcall_one_way_travel_minutes,
    get_outcall_return_travel_minutes,
    get_escort_base_address_for_travel,
)


# ---------------------------------------------------------------------------
# Pure functions — no mocks needed
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_km(0, 0, 0, 0) == pytest.approx(0.0, abs=1e-6)

    def test_known_distance_adelaide_sydney(self):
        # Adelaide (-34.929, 138.601) → Sydney (-33.869, 151.209) ≈ 1166 km
        km = _haversine_km(-34.929, 138.601, -33.869, 151.209)
        assert 1100 < km < 1250, f"Expected ~1166 km, got {km:.1f}"

    def test_short_local_hop(self):
        # Two points 1 km apart in Adelaide CBD (approx)
        km = _haversine_km(-34.929, 138.601, -34.929, 138.615)
        assert km < 2.0, f"Expected < 2 km for short hop, got {km:.2f}"

    def test_antipodal_points_about_20000km(self):
        km = _haversine_km(0, 0, 0, 180)
        assert 19900 < km < 20100


class TestSanitizeRoutingAddress:
    def test_strips_trailing_question_mark(self):
        assert _sanitize_routing_address("Hilton Hotel?") == "Hilton Hotel"

    def test_strips_multiple_trailing_punctuation(self):
        assert _sanitize_routing_address("Grand Hyatt...") == "Grand Hyatt"

    def test_collapses_internal_whitespace(self):
        assert _sanitize_routing_address("  Rydges  Hotel  ") == "Rydges Hotel"

    def test_empty_string(self):
        assert _sanitize_routing_address("") == ""

    def test_none_returns_empty(self):
        assert _sanitize_routing_address(None) == ""  # type: ignore[arg-type]

    def test_preserves_comma_in_address(self):
        result = _sanitize_routing_address("123 Main St, Adelaide")
        assert result == "123 Main St, Adelaide"


# ---------------------------------------------------------------------------
# OSRM buffer math
# ---------------------------------------------------------------------------

class TestOsrmBuffer:
    """Verify the 1.5× buffer + round-up-to-5 logic without hitting the network."""

    def _fake_osrm_response(self, duration_seconds: float) -> dict:
        return {
            "code": "Ok",
            "routes": [{"duration": duration_seconds}],
        }

    def test_10_min_raw_becomes_15(self):
        # 600 s → 10 min raw → 1.5x = 15 → ceil/5 = 15
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = self._fake_osrm_response(600)
        with patch("requests.get", return_value=resp):
            result = _osrm_drive_minutes(0, 0, 0, 0)
        assert result == 15

    def test_11_min_raw_rounds_up_to_20(self):
        # 660 s → 11 min → 1.5x = 16.5 → ceil/5 = 20
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = self._fake_osrm_response(660)
        with patch("requests.get", return_value=resp):
            result = _osrm_drive_minutes(0, 0, 0, 0)
        assert result == 20

    def test_1_min_raw_becomes_5(self):
        # 60 s → 1 min → 1.5x = 1.5 → ceil/5 = 5
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = self._fake_osrm_response(60)
        with patch("requests.get", return_value=resp):
            result = _osrm_drive_minutes(0, 0, 0, 0)
        assert result == 5

    def test_network_failure_returns_none(self):
        with patch("requests.get", side_effect=ConnectionError("timeout")):
            result = _osrm_drive_minutes(0, 0, 0, 0)
        assert result is None

    def test_bad_osrm_status_returns_none(self):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"code": "Error", "routes": []}
        with patch("requests.get", return_value=resp):
            result = _osrm_drive_minutes(0, 0, 0, 0)
        assert result is None


# ---------------------------------------------------------------------------
# Full fallback chain
# ---------------------------------------------------------------------------

class TestDistanceMatrixFallbackChain:
    """get_outcall_one_way_travel_minutes returns default_minutes when everything fails."""

    def _patch_all_network_down(self):
        """Context manager: Google unavailable (no key), OSRM fails, Nominatim fails."""
        return patch.multiple(
            "services.calendar.travel_routing",
            _can_use_google_maps_distance_matrix=MagicMock(return_value=False),
            _nominatim_geocode_coords=MagicMock(return_value=None),
            _osrm_drive_minutes=MagicMock(return_value=None),
        )

    def test_all_fail_returns_default_30(self):
        with self._patch_all_network_down():
            result = get_outcall_one_way_travel_minutes("Irrelevant", default_minutes=30)
        assert result == 30

    def test_all_fail_custom_default(self):
        with self._patch_all_network_down():
            result = get_outcall_one_way_travel_minutes("Irrelevant", default_minutes=45)
        assert result == 45

    def test_osrm_succeeds_returns_osrm_value(self):
        with patch.multiple(
            "services.calendar.travel_routing",
            _can_use_google_maps_distance_matrix=MagicMock(return_value=False),
            _nominatim_geocode_coords=MagicMock(return_value=(-34.929, 138.601)),
            _osrm_drive_minutes=MagicMock(return_value=20),
            _adjust_travel_minutes_for_sanity=MagicMock(side_effect=lambda o, d, m: m),
        ):
            result = get_outcall_one_way_travel_minutes("Some Hotel", default_minutes=30)
        assert result == 20


class TestGetOutcallReturnTravelMinutes:
    """Return leg must use client→escort matrix, not reuse outbound minutes."""

    def test_delegates_to_get_travel_minutes_between_reversed(self):
        with patch(
            "services.calendar.travel_routing.get_travel_minutes_between",
            return_value=22,
        ) as mock_between:
            with patch(
                "services.calendar.travel_routing._build_outcall_route_addresses",
                return_value=("escort base str", "client dest str"),
            ):
                result = get_outcall_return_travel_minutes("123 St")
        assert result == 22
        mock_between.assert_called_once_with(
            "client dest str", "escort base str", default_minutes=30
        )


class TestGetEscortBaseAddress:
    """get_escort_base_address_for_travel builds the right base string."""

    def _fake_location(self, address="", hotel_name="", city="Adelaide"):
        loc = {"address": address, "hotel_name": hotel_name, "city": city}
        return loc

    def test_uses_address_when_set(self):
        loc = self._fake_location(address="99 Frome St", city="Adelaide")
        with patch("config.get_current_incall_location", return_value=loc):
            result = get_escort_base_address_for_travel()
        assert "99 Frome St" in result
        assert "Australia" in result

    def test_falls_back_to_hotel_name(self):
        loc = self._fake_location(address="", hotel_name="Oaks Embassy", city="Adelaide")
        with patch("config.get_current_incall_location", return_value=loc):
            result = get_escort_base_address_for_travel()
        assert "Oaks Embassy" in result

    def test_city_only_fallback(self):
        loc = self._fake_location(address="", hotel_name="", city="Adelaide")
        with patch("config.get_current_incall_location", return_value=loc):
            result = get_escort_base_address_for_travel()
        assert "Adelaide" in result
        assert "Australia" in result

    def test_address_equal_to_city_is_ignored(self):
        loc = self._fake_location(address="Adelaide", hotel_name="Grand Hyatt", city="Adelaide")
        with patch("config.get_current_incall_location", return_value=loc):
            result = get_escort_base_address_for_travel()
        assert "Grand Hyatt" in result
