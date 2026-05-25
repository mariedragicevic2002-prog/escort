"""
Google Calendar API client — STUB.

Google Calendar has been removed from the live booking flow; the `bookings`
table is now the single source of truth. This stub keeps the public surface
(`get_calendar_service`, `HAS_CALENDAR`, `calendar_api_key`) so existing
imports still resolve, but `get_calendar_service()` always returns ``None``.

All callers already guard with ``if not service: ...`` so this safely
no-ops every remaining Google Calendar code path.
"""

import logging

logger = logging.getLogger(__name__)

try:
    from googleapiclient.errors import HttpError  # type: ignore[import]
except ImportError:
    class HttpError(Exception):  # type: ignore[no-redef]
        """Stub for googleapiclient.errors.HttpError when Google API client is not installed."""

HAS_CALENDAR = False
calendar_api_key = ""

try:
    import googlemaps as googlemaps  # type: ignore[import]
    HAS_GOOGLEMAPS = True
except ImportError:
    googlemaps = None  # type: ignore[assignment]
    HAS_GOOGLEMAPS = False


def get_calendar_service():
    """Always returns None — Google Calendar integration is disabled."""
    return None
