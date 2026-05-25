"""Outcall travel time: Google Distance Matrix, OSRM, Nominatim fallbacks."""

import logging
import math
import re
import time
from urllib.parse import urlparse

from config import get_google_maps_server_api_key, get_base_url

from services.calendar import client as calendar_client

logger = logging.getLogger(__name__)

GOOGLE_MAPS_DISABLE_SECONDS = 1800  # 30 minutes
_google_maps_disabled_until = 0.0


def _google_maps_auth_or_key_error(error: Exception) -> bool:
    """Return True for Google Maps key/auth errors worth cooldown disabling."""
    text = str(error or "").lower()
    markers = (
        "request_denied",
        "api key",
        "provided api key is expired",
        "this api project is not authorized",
        "invalid key",
        "permission_denied",
        "forbidden",
    )
    return any(m in text for m in markers)


def _can_use_google_maps_distance_matrix() -> bool:
    """True when server key can call Distance Matrix, Geocoding, and related Maps web services."""
    return bool(
        get_google_maps_server_api_key()
        and calendar_client.HAS_GOOGLEMAPS
        and calendar_client.googlemaps
        and time.time() >= _google_maps_disabled_until
    )


def _disable_google_maps_distance_matrix_temporarily(error: Exception) -> None:
    global _google_maps_disabled_until
    _google_maps_disabled_until = time.time() + GOOGLE_MAPS_DISABLE_SECONDS
    logger.warning(
        "Temporarily disabling Google Distance Matrix for %ss due to error: %s",
        GOOGLE_MAPS_DISABLE_SECONDS,
        error,
    )


def _sanitize_routing_address(address: str) -> str:
    """Strip trailing punctuation (e.g. '?') and collapse whitespace so geocoders don't mis-resolve."""
    if not address:
        return ""
    s = address.strip()
    s = re.sub(r"[?!.,;:]+$", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km (for sanity checks on routed driving time)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    c = 2 * math.asin(min(1.0, math.sqrt(a)))
    return r * c


def _adjust_travel_minutes_for_sanity(origin: str, dest: str, minutes: int) -> int:
    """
    When OSRM/Google mis-geocode (e.g. 'hotel?' → wrong country), routed minutes can be absurd.
    Use straight-line Nominatim distance to cap short urban hops.
    """
    if minutes is None or minutes <= 0:
        return minutes
    o = _sanitize_routing_address(origin)
    d = _sanitize_routing_address(dest)
    oc = _nominatim_geocode_coords(o)
    dc = _nominatim_geocode_coords(d)
    if not oc or not dc:
        return minutes
    km = _haversine_km(oc[0], oc[1], dc[0], dc[1])
    logger.info(
        "Travel sanity: routed=%s min, straight-line ~%.1f km",
        minutes,
        km,
    )
    # Short local trips (e.g. CBD to nearby hotel): driving should be minutes, not hours
    if km <= 15.0:
        reasonable_max = max(5, min(60, int(10 + km * 3)))
        if minutes > reasonable_max + 2:
            logger.warning(
                "Routed travel %s min implausible for ~%.1f km — using %s min instead",
                minutes,
                km,
                reasonable_max,
            )
            return reasonable_max
    if km <= 100.0 and minutes > 180:
        alt = max(30, min(120, int(25 + km * 0.8)))
        logger.warning(
            "Capping excessive routed travel %s min to %s min for ~%.1f km",
            minutes,
            alt,
            km,
        )
        return alt
    return minutes


def _nominatim_user_agent() -> str:
    """Build a neutral user-agent without account-specific hardcoding."""
    try:
        host = urlparse(get_base_url()).netloc or "app.local"
    except Exception as e:
        logger.warning("get_base_url for Nominatim user-agent failed: %s", e)
        host = "app.local"
    return f"AdellaChatbot/1.0 ({host})"


def _nominatim_geocode_coords(address: str):
    """Geocode an address via Nominatim and return (lat, lng) or None."""
    try:
        import requests

        params = {"q": address, "format": "json", "limit": 1, "countrycodes": "au"}
        headers = {"User-Agent": _nominatim_user_agent()}
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        logger.warning(f"Nominatim geocode failed for '{address}': {e}")
    return None


def _google_geocode_lat_lng(address: str) -> tuple[float, float] | None:
    """Resolve an address to (lat, lng) using Google Geocoding API (region AU)."""
    if not address or not _can_use_google_maps_distance_matrix():
        return None
    try:
        gmaps = calendar_client.googlemaps.Client(key=get_google_maps_server_api_key())
        results = gmaps.geocode(address, region="au")
        if not results:
            logger.warning(
                "Google Geocoding returned no results for '%s'", address[:120]
            )
            return None
        loc = results[0].get("geometry", {}).get("location", {})
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            return None
        return float(lat), float(lng)
    except Exception as e:
        if _google_maps_auth_or_key_error(e):
            _disable_google_maps_distance_matrix_temporarily(e)
        logger.warning("Google Geocoding failed for '%s': %s", (address or "")[:120], e)
        return None


def _distance_matrix_with_google_client(
    gmaps,
    origins: list,
    destinations: list,
) -> dict | None:
    """Run Distance Matrix; returns parsed result dict or None on hard failure."""
    try:
        result = gmaps.distance_matrix(
            origins=origins,
            destinations=destinations,
            mode="driving",
            units="metric",
            region="au",
        )
        return result
    except Exception as e:
        if _google_maps_auth_or_key_error(e):
            _disable_google_maps_distance_matrix_temporarily(e)
        logger.warning("Google Distance Matrix failed (%s) - %s", e, origins)
        return None


def _minutes_from_distance_matrix_result(
    result: dict, origin: str, dest: str
) -> int | None:
    """Extract drive minutes from a Distance Matrix API response, or None."""
    if not result or result.get("status") != "OK":
        return None
    element = (result.get("rows") or [{}])[0].get("elements", [{}])[0]
    if element.get("status") != "OK":
        return None
    seconds = element.get("duration", {}).get("value", 0)
    if seconds <= 0:
        return None
    minutes = max(1, seconds // 60)
    logger.info("Google Distance Matrix: %s min", minutes)
    try:
        return _adjust_travel_minutes_for_sanity(origin, dest, minutes)
    except Exception as e:
        logger.warning(
            "Travel minutes sanity adjustment failed, using raw minutes: %s", e
        )
        return minutes


def _osrm_drive_minutes(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float
):
    """Return driving minutes between two coordinates using the free OSRM public API."""
    try:
        import requests

        url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{origin_lng},{origin_lat};{dest_lng},{dest_lat}"
            f"?overview=false"
        )
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok" and data.get("routes"):
            seconds = data["routes"][0]["duration"]
            raw_minutes = max(1, seconds / 60)
            # Apply 1.5x buffer for traffic lights, slow roads, parking etc.
            # then round up to nearest 5 minutes
            buffered = raw_minutes * 1.5
            minutes = int(5 * -(-buffered // 5))  # ceiling to nearest 5
            logger.info(
                f"OSRM travel time: {raw_minutes:.1f} min raw → {minutes} min (1.5x buffer, rounded to 5)"
            )
            return minutes
    except Exception as e:
        logger.warning(f"OSRM routing failed: {e}")
    return None


def _distance_matrix_minutes(origin: str, dest: str, _round_trip=False):
    """Return drive minutes between origin and dest.

    1. Google Distance Matrix (address strings)
    2. If element not OK: Distance Matrix again using lat,lng from Google Geocoding
    3. OSRM with Google Geocoding coordinates when possible, else Nominatim

    Args:
        origin: Starting address string
        dest: Destination address string

    Returns:
        int: Travel time in minutes (None if all methods fail)
    """
    origin = _sanitize_routing_address(origin or "")
    dest = _sanitize_routing_address(dest or "")

    # --- 1–2: Google Distance Matrix (text, then geocoded coordinates) ---
    if _can_use_google_maps_distance_matrix():
        gmaps = calendar_client.googlemaps.Client(key=get_google_maps_server_api_key())
        result = _distance_matrix_with_google_client(gmaps, [origin], [dest])
        if result:
            api_status = result.get("status")
            if api_status != "OK":
                logger.warning(
                    "Google Distance Matrix API status=%s — trying geocoded retry / OSRM. error_message=%s",
                    api_status,
                    result.get("error_message", ""),
                )
            else:
                element = (result.get("rows") or [{}])[0].get("elements", [{}])[0]
                el_status = element.get("status")
                if el_status == "OK":
                    parsed = _minutes_from_distance_matrix_result(result, origin, dest)
                    if parsed is not None:
                        return parsed
                else:
                    logger.warning(
                        "Google Distance Matrix element status=%s — retry with Geocoding coords. element=%s",
                        el_status,
                        element,
                    )
                    o_ll = _google_geocode_lat_lng(origin)
                    d_ll = _google_geocode_lat_lng(dest)
                    if o_ll and d_ll and _can_use_google_maps_distance_matrix():
                        o_str = f"{o_ll[0]},{o_ll[1]}"
                        d_str = f"{d_ll[0]},{d_ll[1]}"
                        result2 = _distance_matrix_with_google_client(
                            gmaps, [o_str], [d_str]
                        )
                        if result2:
                            parsed2 = _minutes_from_distance_matrix_result(
                                result2, origin, dest
                            )
                            if parsed2 is not None:
                                logger.info(
                                    "Google Distance Matrix succeeded using Geocoding lat/lng: %s -> %s",
                                    o_str[:40],
                                    d_str[:40],
                                )
                                return parsed2

    # --- 3: OSRM — prefer Google Geocoding for coordinates, then Nominatim ---
    logger.info(
        "Using OSRM for travel time (after Google Maps): '%s' -> '%s'",
        origin[:80],
        dest[:80],
    )
    origin_coords = None
    dest_coords = None
    if _can_use_google_maps_distance_matrix():
        origin_coords = _google_geocode_lat_lng(origin)
        dest_coords = _google_geocode_lat_lng(dest)
        if origin_coords and dest_coords:
            logger.info("OSRM legs use Google Geocoding coordinates")
    if not origin_coords:
        origin_coords = _nominatim_geocode_coords(origin)
    if not dest_coords:
        dest_coords = _nominatim_geocode_coords(dest)
    if origin_coords and dest_coords:
        minutes = _osrm_drive_minutes(
            origin_coords[0], origin_coords[1], dest_coords[0], dest_coords[1]
        )
        if minutes is not None:
            try:
                return _adjust_travel_minutes_for_sanity(origin, dest, minutes)
            except Exception as e:
                logger.warning("OSRM travel minutes sanity adjustment failed: %s", e)
                return minutes
        logger.warning("OSRM returned no result")
    else:
        logger.warning(
            "Could not geocode addresses for OSRM: origin=%s, dest=%s",
            origin_coords,
            dest_coords,
        )

    return None


def _apply_au_local_context(address: str) -> str:
    """
    Append escort city + Australia when missing so Nominatim/OSRM match leg-1 behaviour.

    Point-to-point calls used to pass only _sanitize_routing_address(); leg 1 uses
    _build_outcall_route_addresses() which adds context — without it, later dinner legs
    often failed geocoding and fell back to the default 30 minutes.
    """
    from config import get_current_incall_location

    destination = _sanitize_routing_address(address or "")
    if not destination:
        return ""
    location = get_current_incall_location() or {}
    city = (location.get("city") or "").strip()
    if city and city.lower() not in destination.lower():
        destination = f"{destination}, {city}"
    if destination and "australia" not in destination.lower():
        destination = f"{destination}, Australia"
    return destination


def get_escort_base_address_for_travel() -> str:
    """
    Escort base for routing and calendar travel blocks: prefer street address, then hotel name.

    If admin ``address`` is blank, use ``hotel_name`` / ``display_name`` so copy shows the
    accommodation (e.g. Oaks Embassy) instead of only the city. If ``address`` is set to the
    city name alone (common mis-entry), treat it as missing and fall back to hotel name.
    """
    from config import get_current_incall_location

    loc = get_current_incall_location() or {}
    city = (loc.get("city") or "").strip()
    addr = (loc.get("address") or "").strip()
    hotel = (loc.get("hotel_name") or loc.get("display_name") or "").strip()

    if addr and city and addr.lower() == city.lower():
        addr = ""
    if addr:
        base = addr
    elif hotel:
        base = hotel
    else:
        base = ""

    if not base:
        return f"{city}, SA, Australia" if city else ""

    if "australia" in base.lower():
        return base

    if city and city.lower() not in base.lower():
        return f"{base}, {city}, Australia"
    return f"{base}, Australia"


def _build_outcall_route_addresses(client_address: str):
    """Build normalized origin/destination addresses for outcall travel lookups."""
    origin = get_escort_base_address_for_travel()
    destination = _apply_au_local_context(client_address or "")
    return origin, destination


def get_outcall_one_way_travel_minutes(
    client_address: str, default_minutes: int = 30
) -> int:
    """Return one-way travel minutes for an outcall, with shared fallback behavior."""
    origin, destination = _build_outcall_route_addresses(client_address)
    travel_minutes = _distance_matrix_minutes(origin, destination)
    if travel_minutes is None:
        logger.warning(
            "Distance Matrix API failed - using %s min default", default_minutes
        )
        return default_minutes
    return travel_minutes


def get_outcall_return_travel_minutes(
    client_address: str, default_minutes: int = 30
) -> int:
    """Drive time from client back to escort base (reverse leg; can differ from outbound)."""
    origin, destination = _build_outcall_route_addresses(client_address)
    return get_travel_minutes_between(
        destination, origin, default_minutes=default_minutes
    )


def get_travel_minutes_between(
    origin_address: str, dest_address: str, default_minutes: int = 30
) -> int:
    """One-way drive time between two arbitrary addresses (e.g. restaurant → client's home)."""
    o = _apply_au_local_context(_sanitize_routing_address(origin_address or ""))
    d = _apply_au_local_context(_sanitize_routing_address(dest_address or ""))
    if not o or not d:
        return default_minutes
    travel_minutes = _distance_matrix_minutes(o, d)
    if travel_minutes is None:
        logger.warning(
            "Distance Matrix failed for point-to-point route — using %s min default",
            default_minutes,
        )
        return default_minutes
    return travel_minutes
