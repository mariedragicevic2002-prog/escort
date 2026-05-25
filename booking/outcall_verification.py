# ruff: noqa: E402

"""
Outcall address verification using Google Maps API.
Verifies that outcall addresses are within 15km of the escort's current location.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re
import time
from math import atan2, cos, radians, sin, sqrt
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class GeocoderUnavailable(RuntimeError):
    """Every geocoding backend failed while verifying an outcall address."""

# Conditional import - googlemaps may not be installed
try:
    import googlemaps
    HAS_GOOGLEMAPS = True
except ImportError:
    googlemaps = None
    HAS_GOOGLEMAPS = False
    logger.warning("googlemaps package not installed - Outcall verification disabled")

from config import (
    get_base_url,
    get_current_incall_location,
    get_effective_booking_city,
    get_google_maps_server_api_key,
    get_opencage_api_key,
)

# Nominatim (OpenStreetMap) fallback geocoder \u2014 free, no API key required
NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/search"
def _nominatim_user_agent() -> str:
    """Build a neutral user-agent that avoids account-specific hardcoding."""
    try:
        host = urlparse(get_base_url()).netloc or "app.local"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        host = "app.local"
    return f"EscortChatbot/1.0 ({host})"
OPENCAGE_ENDPOINT = "https://api.opencagedata.com/geocode/v1/json"

# City-center coordinates used only as a fallback if the escort's address cannot be geocoded.
CBD_COORDINATES = {
    "adelaide": {"lat": -34.9285, "lng": 138.6007},
    "sydney": {"lat": -33.8688, "lng": 151.2093},
    "melbourne": {"lat": -37.8136, "lng": 144.9631},
    "brisbane": {"lat": -27.4698, "lng": 153.0251},
    "perth": {"lat": -31.9505, "lng": 115.8605},
    "darwin": {"lat": -12.4634, "lng": 130.8456},
    "hobart": {"lat": -42.8821, "lng": 147.3272},
    "canberra": {"lat": -35.2809, "lng": 149.1300},
    "gold coast": {"lat": -28.0167, "lng": 153.4000},
}


def _cbd_coords_for_city_name(city_name: str) -> dict:
    """
    Map a city name to CBD coordinates for distance checks.
    Uses :func:`config.get_effective_booking_city` before the internal default.
    """
    cl = (city_name or "").lower().strip()
    if cl in CBD_COORDINATES:
        return CBD_COORDINATES[cl]
    for k, v in CBD_COORDINATES.items():
        if k in cl or cl in k:
            return v
    try:
        ec = (get_effective_booking_city() or "").lower().strip()
        if ec in CBD_COORDINATES:
            return CBD_COORDINATES[ec]
        for k, v in CBD_COORDINATES.items():
            if k in ec or ec in k:
                return v
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return CBD_COORDINATES["adelaide"]


MAX_DISTANCE_KM = 15.0

# Known hotel chains (short/brand names that need city bias when geocoding)
_HOTEL_CHAIN_WORDS = frozenset({
    "hilton", "marriott", "hyatt", "intercontinental", "sheraton",
    "majestic", "stamford", "crown", "pullman", "novotel", "rydges", "sofitel",
    "ibis", "mercure", "adina", "mantra", "peppers", "vibe", "quest",
    "eos", "skycity",
    "westin", "four seasons", "voco", "doubletree", "holiday inn", "hampton",
    "radisson", "como", "langham", "renaissance",
})

# Google Maps client — created lazily from DB (see get_google_maps_server_api_key)
_gmaps_client = None
_gmaps_client_key: str | None = None

GOOGLE_MAPS_DISABLE_SECONDS = 1800  # 30 minutes
_google_maps_disabled_until = 0.0


def _get_gmaps_client():
    """Return a :class:`googlemaps.Client` using the current server key from admin_settings, or None."""
    global _gmaps_client, _gmaps_client_key
    if not HAS_GOOGLEMAPS or googlemaps is None:
        return None
    key = (get_google_maps_server_api_key() or "").strip()
    if not key:
        _gmaps_client = None
        _gmaps_client_key = None
        return None
    if _gmaps_client is not None and _gmaps_client_key == key:
        return _gmaps_client
    try:
        _gmaps_client = googlemaps.Client(key=key)
        _gmaps_client_key = key
        logger.info("Google Maps client initialized (lazy)")
    except Exception as e:
        logger.error("Google Maps client init failed: %s", e)
        _gmaps_client = None
        _gmaps_client_key = None
    return _gmaps_client


def _google_maps_auth_or_key_error(error: Exception) -> bool:
    """Return True when Google Maps error indicates key/auth config issue."""
    text = str(error or "").lower()
    markers = (
        "request_denied",
        "api key",
        "provided api key is expired",
        "api keys with referer restrictions",
        "this api project is not authorized",
        "invalid key",
        "forbidden",
        "permission_denied",
    )
    return any(m in text for m in markers)


def _can_use_google_maps() -> bool:
    """Gate Google Maps usage during temporary disable windows."""
    return bool(_get_gmaps_client()) and time.time() >= _google_maps_disabled_until


def _disable_google_maps_temporarily(error: Exception) -> None:
    """Temporarily disable Google Maps calls after key/auth failures."""
    global _google_maps_disabled_until
    _google_maps_disabled_until = time.time() + GOOGLE_MAPS_DISABLE_SECONDS
    logger.warning(
        "Temporarily disabling Google Maps geocoding for %ss due to error: %s",
        GOOGLE_MAPS_DISABLE_SECONDS,
        error,
    )


def _calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two coordinates using Haversine formula.

    Args:
        lat1, lon1: First coordinate
        lat2, lon2: Second coordinate

    Returns:
        Distance in kilometers
    """
    lat1_rad, lon1_rad = radians(lat1), radians(lon1)
    lat2_rad, lon2_rad = radians(lat2), radians(lon2)

    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad

    a = sin(dlat/2)**2 + cos(lat1_rad) * cos(lat2_rad) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    distance_km = 6371 * c  # Earth radius in km

    return distance_km


def normalize_outcall_address_for_verification(address: str, city: str = None) -> str:
    """For ultra-short brand-only addresses (e.g. 'Hilton'), return 'Brand City' to improve geocoding. Otherwise return address unchanged."""
    base = (address or "").strip()
    city_stripped = (city or "").strip()
    if not base:
        return base
    # Expand common Australian suburb abbreviations that hurt geocoding.
    base = re.sub(r"\bsth\b", "south", base, flags=re.IGNORECASE)
    base = re.sub(r"\bnth\b", "north", base, flags=re.IGNORECASE)
    base = re.sub(r"\beast\b", "east", base, flags=re.IGNORECASE)
    base = re.sub(r"\bwest\b", "west", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip()
    # Common local alias normalization for Adelaide venues/hotels.
    # Always apply these regardless of whether city is provided.
    alias_key = re.sub(r"[^a-z0-9]+", " ", base.lower()).strip()
    if alias_key in {"eos star city", "eos skycity", "eos starcity", "star city eos", "eos adelaide casino", "eos casino"}:
        base = "Eos by SkyCity Adelaide"
    # Bare venue names can geocode overseas; lock to local restaurant (King William Rd, Goodwood).
    if alias_key in {"le pas sage", "le pas sage restaurant"}:
        base = "Le Pas Sage restaurant Goodwood South Australia"
    if not city_stripped:
        return base
    base_lower = base.lower()
    _brand_key = base_lower.removeprefix("the ").strip()
    if base_lower in _HOTEL_CHAIN_WORDS or _brand_key in _HOTEL_CHAIN_WORDS:
        return f"{base.strip().title()} {city_stripped}"
    return base


def _is_short_or_brand_address(address: str) -> bool:
    """True if address is a single word or known hotel brand (needs city bias for geocoding)."""
    base = (address or "").strip()
    if not base:
        return False
    base_lower = base.lower()
    words = base_lower.split()
    if len(words) == 1 and len(base) <= 30:
        return True
    if base_lower in _HOTEL_CHAIN_WORDS:
        return True
    if any(brand in base_lower for brand in _HOTEL_CHAIN_WORDS) and len(words) <= 2:
        return True
    return False


def _looks_like_venue_name_no_street(address: str) -> bool:
    """True when the string looks like a venue name, not a street address (helps geocoding)."""
    if not (address or "").strip():
        return False
    a = address.strip().lower()
    if re.search(r"^\d+\s+", a):
        return False
    if re.search(
        r"\b(st|street|rd|road|ave|avenue|dr|drive|court|ct|pl|place|terrace|tce|way|highway|hwy|boulevard|blvd|lane|ln)\b",
        a,
    ):
        return False
    return True


def _build_geocode_queries(address: str, city: str = None) -> list[str]:
    """Build geocode queries, preferring city-specific lookups for short/generic hotel names."""
    queries = []
    base = (address or "").strip()
    if not base:
        return queries

    base_lower = base.lower()
    city_stripped = (city or "").strip()
    has_city = bool(city_stripped)
    city_in_address = city_stripped and city_stripped.lower() in base_lower

    # Restaurant/venue names without street numbers: bare names often resolve to the wrong country.
    if has_city and _looks_like_venue_name_no_street(base) and not city_in_address:
        state_names = _CITY_TO_STATE.get(city_stripped.lower(), [])
        if state_names:
            queries.append(f"{base} restaurant {city_stripped}, {state_names[0].title()}, Australia")
        queries.append(f"{base} restaurant {city_stripped}")
        queries.append(f"{base} {city_stripped} Australia")

    # For short or brand-like addresses (e.g. "Hilton"), put city-specific queries first
    if has_city and _is_short_or_brand_address(base) and not city_in_address:
        queries.append(f"{base} {city_stripped} hotel")
        queries.append(f"{base} {city_stripped}")
        queries.append(f"{base}, {city_stripped}")
        if "australia" not in base_lower:
            queries.append(f"{base}, {city_stripped}, Australia")

    queries.append(base)
    if has_city and city_stripped.lower() not in base_lower:
        queries.append(f"{base}, {city_stripped}")
    if has_city and "australia" not in base_lower:
        queries.append(f"{base}, {city_stripped}, Australia")
    elif not has_city and "australia" not in base_lower:
        queries.append(f"{base}, Australia")

    # De-duplicate while preserving order
    seen = set()
    deduped = []
    for query in queries:
        key = query.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped


# Coarse radius (km): if best result is beyond this, treat as wrong-city/ambiguous rather than "too far"
WRONG_CITY_THRESHOLD_KM = 80.0

# Map city names to their state/territory so suburbs (e.g. South Plympton for Adelaide) are recognised.
_CITY_TO_STATE = {
    "adelaide": ["south australia", "sa"],
    "sydney": ["new south wales", "nsw"],
    "melbourne": ["victoria", "vic"],
    "brisbane": ["queensland", "qld"],
    "perth": ["western australia", "wa"],
    "darwin": ["northern territory", "nt"],
    "hobart": ["tasmania", "tas"],
    "canberra": ["australian capital territory", "act"],
    "gold coast": ["queensland", "qld"],
}


def _result_in_target_city(result: dict, city: str) -> bool:
    """True if the geocode result appears to be in the target city or its metro area (same state)."""
    city_lower = (city or "").lower().strip()
    if not city_lower:
        return False
    formatted = (result.get("formatted_address") or "").lower()
    if city_lower in formatted:
        return True
    for comp in result.get("address_components") or []:
        comp_types = comp.get("types") or []
        long_name_lower = (comp.get("long_name") or "").lower()
        short_name_lower = (comp.get("short_name") or "").lower()
        # Direct city/locality match
        if "locality" in comp_types or "administrative_area_level_2" in comp_types:
            if city_lower in long_name_lower or city_lower in short_name_lower:
                return True
        # State-level match: suburb is in the same state as the target city
        if "administrative_area_level_1" in comp_types:
            state_aliases = _CITY_TO_STATE.get(city_lower, [])
            if any(alias in long_name_lower or alias == short_name_lower for alias in state_aliases):
                return True
    return False


def _select_best_geocode_result(geocode_results: list, city: str, reference_coords: dict) -> dict:
    """Prefer results in the target city and closest to the reference coordinates."""
    if not geocode_results:
        return {}

    city_lower = (city or "").lower().strip()

    def candidate_score(result: dict):
        formatted_address = (result.get("formatted_address") or "").lower()
        location = (result.get("geometry") or {}).get("location") or {}
        lat = location.get("lat")
        lng = location.get("lng")
        if lat is None or lng is None:
            distance_km = float("inf")
        else:
            distance_km = _calculate_distance(
                reference_coords["lat"], reference_coords["lng"], lat, lng
            )

        types = result.get("types") or []
        is_city_match = bool(city_lower and city_lower in formatted_address)
        looks_like_hotel = any(t in {"lodging", "premise", "establishment"} for t in types)

        return (
            0 if is_city_match else 1,
            0 if looks_like_hotel else 1,
            distance_km,
        )

    # Prefer results that are in the target city; if any exist, choose best among them
    in_city = [r for r in geocode_results if _result_in_target_city(r, city)]
    candidates = in_city if in_city else geocode_results
    return min(candidates, key=candidate_score)


def _get_reference_location(city: str) -> tuple[dict, str]:
    """Resolve the escort's current location to coordinates for distance checks."""
    escort_location = get_current_incall_location() or {}
    escort_address = (
        escort_location.get("address")
        or escort_location.get("hotel_name")
        or escort_location.get("display_name")
        or ""
    ).strip()

    gmaps = _get_gmaps_client() if _can_use_google_maps() else None
    if escort_address and gmaps is not None:
        geocode_results = []
        for geocode_query in _build_geocode_queries(escort_address, city):
            logger.info(f"Geocoding escort location: '{geocode_query}' (city={city})")
            try:
                query_results = gmaps.geocode(geocode_query) or []
            except Exception as e:
                logger.warning(f"gmaps.geocode failed for '{geocode_query}': {e}")
                if _google_maps_auth_or_key_error(e):
                    _disable_google_maps_temporarily(e)
                    break
                query_results = []
            geocode_results.extend(query_results)

        if geocode_results:
            fallback_coords = _cbd_coords_for_city_name(city)
            best_result = _select_best_geocode_result(geocode_results, city, fallback_coords)
            location = (best_result.get("geometry") or {}).get("location") or {}
            if location.get("lat") is not None and location.get("lng") is not None:
                return location, best_result.get("formatted_address") or escort_address

    city_lower = (city or "").lower().strip()
    fallback_coords = CBD_COORDINATES.get(city_lower)
    if not fallback_coords:
        for city_key, coords in CBD_COORDINATES.items():
            if city_key in city_lower or city_lower in city_key:
                fallback_coords = coords
                break
    if not fallback_coords:
        fallback_coords = _cbd_coords_for_city_name(city)
        if not city:
            city = get_effective_booking_city() or "Adelaide"

    return fallback_coords, escort_address or f"{city} CBD"


def get_escort_reference_coords_for_ui() -> dict[str, float] | None:
    """
    Lat/lng for the booking webform Places map bias and 15 km circle.

    Uses the same reference as :func:`_get_reference_location` (geocoded
    admin Location address / hotel, else city CBD) so the browser matches
    server-side :func:`verify_hotel_in_cbd` policy.
    """
    try:
        city = (get_effective_booking_city() or "").strip() or "Adelaide"
        ref, _ = _get_reference_location(city)
        if not ref:
            return None
        lat, lng = ref.get("lat"), ref.get("lng")
        if lat is None or lng is None:
            return None
        return {"lat": float(lat), "lng": float(lng)}
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return None


def _nominatim_geocode(query: str) -> dict | None:
    """Geocode an address using Nominatim (OpenStreetMap). Returns dict with lat/lng/formatted_address or None."""
    try:
        import requests
        params = {"q": query, "format": "json", "limit": 5, "countrycodes": "au", "addressdetails": 0}
        headers = {"User-Agent": _nominatim_user_agent()}
        resp = requests.get(NOMINATIM_ENDPOINT, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if results:
            best = results[0]
            return {
                "lat": float(best["lat"]),
                "lng": float(best["lon"]),
                "formatted_address": best.get("display_name", query),
            }
    except Exception as e:
        if "maximum recursion depth exceeded" in str(e).lower():
            raise RuntimeError("Geocoding runtime failure") from e
        logger.warning(f"Nominatim geocode failed for '{query}': {e}")
    return None


def _opencage_geocode(query: str, city: str = None) -> dict | None:
    """Geocode using OpenCage. Returns dict with lat/lng/formatted_address or None."""
    opencage_key = (get_opencage_api_key() or "").strip()
    if not opencage_key:
        return None
    try:
        import requests
        params = {
            "q": query,
            "key": opencage_key,
            "countrycode": "au",
            "limit": 5,
            "no_annotations": 1,
            "language": "en",
        }
        resp = requests.get(OPENCAGE_ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
        results = (resp.json().get("results") or [])
        if not results:
            return None
        # Prefer results in the target city
        city_lower = (city or "").lower().strip()
        if city_lower:
            city_matches = [r for r in results if city_lower in r.get("formatted", "").lower()]
            if city_matches:
                results = city_matches
        best = results[0]
        geom = best.get("geometry") or {}
        lat, lng = geom.get("lat"), geom.get("lng")
        if lat is None or lng is None:
            return None
        return {"lat": float(lat), "lng": float(lng), "formatted_address": best.get("formatted", query)}
    except Exception as e:
        if "maximum recursion depth exceeded" in str(e).lower():
            raise RuntimeError("Geocoding runtime failure") from e
        logger.warning(f"OpenCage geocode failed for '{query}': {e}")
    return None


def _verify_with_nominatim(address: str, city: str, reference_coords: dict, reference_address: str) -> tuple[bool, str, dict]:
    """Fallback address verification using Nominatim when Google Maps and OpenCage are unavailable."""
    logger.info(f"Falling back to Nominatim for address verification: '{address}' (city={city})")

    # Strip leading "the " for hotel names
    clean = address
    if clean.lower().startswith("the "):
        clean = clean[4:].strip()

    # Try progressively broader queries, with hotel-specific variants first for hotel names
    queries = []
    if _is_short_or_brand_address(address) or not any(c.isdigit() for c in address):
        queries += [
            f"{clean} restaurant, {city}, Australia",
            f"{clean} hotel, {city}, Australia",
            f"{clean}, {city}, Australia",
            f"{clean} {city} Australia",
        ]
    queries += [
        f"{address}, {city}, Australia",
        f"{address}, Australia",
        address,
    ]
    client_result = None
    for q in queries:
        client_result = _nominatim_geocode(q)
        if client_result:
            break

    if not client_result:
        # Try suburb-only fallback: extract the last part of the address (suburb/postcode) and verify it's within range
        parts = [p.strip() for p in address.split(',') if p.strip()]
        suburb_result = None
        for part in reversed(parts[1:]):  # Try second-to-last, third-to-last etc (skipping street number+name)
            if len(part) >= 3:
                suburb_result = _nominatim_geocode(f"{part}, {city}, Australia") or _nominatim_geocode(f"{part}, Australia")
                if suburb_result:
                    client_result = suburb_result
                    logger.info(f"Nominatim: street not found, verified via suburb '{part}'")
                    break

    if not client_result:
        return False, "Address not found. Please provide a valid street address or hotel name.", {}

    distance_km = _calculate_distance(
        reference_coords["lat"], reference_coords["lng"],
        client_result["lat"], client_result["lng"],
    )

    formatted_address = client_result["formatted_address"]

    if distance_km > MAX_DISTANCE_KM:
        if distance_km > WRONG_CITY_THRESHOLD_KM:
            message = (
                f"I can't find that address near my current location in {city}. "
                "Please send the full address including suburb and city."
            )
        else:
            message = (
                f"Unfortunately your location is {distance_km:.1f}km from my current location "
                f"at {reference_address} (max {MAX_DISTANCE_KM}km)."
            )
        return False, message, {"distance_km": round(distance_km, 1), "city": city, "reference_address": reference_address}

    hotel_name = formatted_address.split(",")[0]
    hotel_info = {
        "original_address": address,
        "verified_hotel_name": hotel_name,
        "verified_address": formatted_address,
        "distance_km": round(distance_km, 1),
        "city": city,
        "reference_address": reference_address,
    }
    logger.info(f"Nominatim: address verified - {distance_km:.1f}km from escort location")
    return True, f"Location verified - {distance_km:.1f}km from escort location", hotel_info


def _looks_like_venue_name(address: str) -> bool:
    """Return True when address has no leading street number — i.e. it looks like a venue name."""
    return not bool(re.match(r'^\d+\s', address.strip()))


def _gmaps_places_search(address: str, city: str, gmaps_client) -> dict | None:
    """Use Google Places text search to resolve a hotel name to a street address.

    More reliable than geocoding for bare business names like 'The Westin'.
    Returns {"lat", "lng", "formatted_address", "name"} or None on any failure.
    """
    try:
        query = f"{address} hotel {city} Australia"
        response = gmaps_client.places(query=query, type="lodging")
        results = (response or {}).get("results") or []
        if not results:
            return None
        top = results[0]
        loc = (top.get("geometry") or {}).get("location") or {}
        lat, lng = loc.get("lat"), loc.get("lng")
        if lat is None or lng is None:
            return None
        formatted = top.get("formatted_address") or top.get("vicinity") or ""
        name = top.get("name") or address
        return {"lat": lat, "lng": lng, "formatted_address": formatted, "name": name}
    except Exception as e:
        logger.debug("Places text search failed for '%s': %s", address, e)
        return None


def verify_hotel_in_cbd(address: str, city: str = None) -> tuple[bool, str, dict]:
    """Verify address is within 15km of the escort's current location.

    Args:
        address: Hotel address to verify
        city: City name (if None, uses current location from config)

    Returns:
        Tuple of (is_valid, message, hotel_info)

        hotel_info includes:
            - original_address: Input address
            - verified_hotel_name: Extracted hotel name
            - verified_address: Formatted address from Google Maps
            - distance_km: Distance from the escort's current location
            - city: City used for verification
    """
    gmaps = _get_gmaps_client()
    if not gmaps:
        logger.warning("Google Maps API not configured - will use Nominatim fallback")

    # Get current city if not provided (tour city or home base from admin)
    if not city:
        try:
            city = get_effective_booking_city()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            city = ""
    if not (city or "").strip():
        try:
            location_info = get_current_incall_location()
            city = (location_info.get("city") or "").strip()
        except Exception as e:
            logger.warning(f"Failed to get current location: {e}")
            city = ""
    if not (city or "").strip():
        try:
            from core.settings_manager import get_setting

            city = (get_setting("city", "") or "").strip()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            city = ""

    # Normalize city name
    city_lower = city.lower().strip()

    if not city_lower:
        city = get_effective_booking_city() or "Adelaide"
        city_lower = city.lower().strip()

    # Get escort's reference location \u2014 fall back to CBD coords if Google fails
    try:
        reference_coords, reference_address = _get_reference_location(city)
    except Exception as e:
        logger.warning(f"Google Maps failed to geocode escort location ({e}) - using CBD fallback")
        city_lower = city.lower().strip()
        reference_coords = _cbd_coords_for_city_name(city)
        reference_address = f"{city} CBD"

    # --- Places API: first-priority for bare hotel names (no street number) ---
    if _can_use_google_maps() and gmaps is not None and _looks_like_venue_name(address):
        places_result = _gmaps_places_search(address, city, gmaps)
        if places_result:
            try:
                distance_km = _calculate_distance(
                    reference_coords['lat'], reference_coords['lng'],
                    places_result['lat'], places_result['lng'],
                )
                formatted_address = places_result['formatted_address']
                if distance_km > MAX_DISTANCE_KM:
                    in_target_city = city.lower() in formatted_address.lower() if city else False
                    if distance_km > WRONG_CITY_THRESHOLD_KM or not in_target_city:
                        message = (
                            f"I can't find that address near my current location in {city}. "
                            "Please send the full address including suburb and city."
                        )
                    else:
                        message = (
                            f"Unfortunately your location is {distance_km:.1f}km from my current location "
                            f"at {reference_address} (max {MAX_DISTANCE_KM}km)."
                        )
                    return False, message, {
                        'distance_km': round(distance_km, 1),
                        'city': city,
                        'reference_address': reference_address,
                    }
                hotel_info = {
                    'original_address': address,
                    'verified_hotel_name': places_result['name'],
                    'verified_address': formatted_address,
                    'distance_km': round(distance_km, 1),
                    'city': city,
                    'reference_address': reference_address,
                }
                logger.info("Outcall location verified (Places): %s → %s - %.1fkm", address, formatted_address, distance_km)
                return True, f"Location verified - {distance_km:.1f}km from escort location", hotel_info
            except Exception as e:
                logger.debug("Places result processing failed, falling through to geocode: %s", e)

    # --- Try Google Maps first ---
    if _can_use_google_maps() and gmaps is not None:
        try:
            geocode_results = []
            for geocode_query in _build_geocode_queries(address, city):
                logger.info(f"Geocoding outcall address (Google): '{geocode_query}' (city={city})")
                query_results = gmaps.geocode(geocode_query) or []
                geocode_results.extend(query_results)

            if not geocode_results:
                logger.info(f"Google Maps returned no results for '{address}' - falling back to Nominatim")
                # Fall through to Nominatim below (residential addresses often not in Google Maps)
            else:
                best_result = _select_best_geocode_result(geocode_results, city, reference_coords)
                location = best_result['geometry']['location']
                formatted_address = best_result['formatted_address']

                distance_km = _calculate_distance(
                    reference_coords['lat'], reference_coords['lng'],
                    location['lat'], location['lng']
                )

                if distance_km > MAX_DISTANCE_KM:
                    in_target_city = _result_in_target_city(best_result, city)
                    if distance_km > WRONG_CITY_THRESHOLD_KM or not in_target_city:
                        message = (
                            f"I can't find that address near my current location in {city}. "
                            "Please send the full address including suburb and city."
                        )
                    else:
                        message = (
                            f"Unfortunately your location is {distance_km:.1f}km from my current location "
                            f"at {reference_address} (max {MAX_DISTANCE_KM}km)."
                        )
                    return False, message, {
                        'distance_km': round(distance_km, 1),
                        'city': city,
                        'reference_address': reference_address,
                    }

                hotel_name = formatted_address.split(',')[0]
                hotel_info = {
                    'original_address': address,
                    'verified_hotel_name': hotel_name,
                    'verified_address': formatted_address,
                    'distance_km': round(distance_km, 1),
                    'city': city,
                    'reference_address': reference_address,
                }
                logger.info(f"Outcall location verified (Google): {address} - {distance_km:.1f}km")
                return True, f"Location verified - {distance_km:.1f}km from escort location", hotel_info

        except Exception as e:
            if _google_maps_auth_or_key_error(e):
                _disable_google_maps_temporarily(e)
            if "maximum recursion depth exceeded" in str(e).lower():
                raise RuntimeError("Geocoding runtime failure") from e
            logger.warning(f"Google Maps geocoding failed ({e}) - falling back to Nominatim")

    # --- OpenCage fallback ---
    if (get_opencage_api_key() or "").strip():
        try:
            queries = _build_geocode_queries(address, city)
            for q in queries:
                result = _opencage_geocode(q, city=city)
                if result:
                    distance_km = _calculate_distance(
                        reference_coords["lat"], reference_coords["lng"],
                        result["lat"], result["lng"],
                    )
                    logger.info(f"OpenCage verified '{address}' - {distance_km:.1f}km")
                    if distance_km > MAX_DISTANCE_KM:
                        msg = (
                            f"I can't find that address near my current location in {city}. "
                            "Please send the full address including suburb."
                            if distance_km > WRONG_CITY_THRESHOLD_KM else
                            f"Unfortunately your location is {distance_km:.1f}km from my current location "
                            f"at {reference_address} (max {MAX_DISTANCE_KM}km)."
                        )
                        return False, msg, {"distance_km": round(distance_km, 1), "city": city, "reference_address": reference_address}
                    hotel_name = result["formatted_address"].split(",")[0]
                    return True, f"Location verified - {distance_km:.1f}km from escort location", {
                        "original_address": address,
                        "verified_hotel_name": hotel_name,
                        "verified_address": result["formatted_address"],
                        "distance_km": round(distance_km, 1),
                        "city": city,
                        "reference_address": reference_address,
                    }
        except Exception as e:
            logger.warning(f"OpenCage fallback failed: {e}")

    # --- Nominatim fallback ---
    try:
        return _verify_with_nominatim(address, city, reference_coords, reference_address)
    except Exception as e:
        logger.error(f"Nominatim fallback also failed: {e}")
        raise GeocoderUnavailable("Verification failed. Please try again shortly.") from e
