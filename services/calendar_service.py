"""
Google Calendar integration for booking management.

Backward-compatible facade: implementation lives in services.calendar.* submodules.
"""

# Re-exports for tests and legacy patches (e.g. @patch('services.calendar_service.build')).
try:
    from googleapiclient.discovery import build
except ImportError:
    build = None  # type: ignore[misc, assignment]

try:
    import requests
except ImportError:
    requests = None  # type: ignore[misc, assignment]

from services.calendar.alternative_slots import find_alternative_slots
from services.calendar.booking_window import (
    _format_duration_label,
    _parse_booking_window,
    is_datetime_in_past,
    parse_booking_time_hour_minute,
)
from services.calendar.list_events import _event_color
from services.calendar.client import HAS_CALENDAR, calendar_api_key, get_calendar_service
from services.calendar.conflicts import check_conflict, check_outcall_conflict_with_travel
from services.calendar.event_crud import (
    confirm_calendar_event,
    create_calendar_event,
    delete_calendar_event,
    find_and_confirm_pending_event,
)
from services.calendar.travel_blocks import (
    confirm_travel_time_blocks,
    create_travel_time_blocks,
    delete_travel_time_blocks,
)
from services.calendar.travel_routing import get_outcall_one_way_travel_minutes, get_travel_minutes_between

__all__ = [
    "calendar_api_key",
    "HAS_CALENDAR",
    "get_calendar_service",
    "_event_color",
    "_parse_booking_window",
    "parse_booking_time_hour_minute",
    "_format_duration_label",
    "is_datetime_in_past",
    "check_conflict",
    "check_outcall_conflict_with_travel",
    "get_outcall_one_way_travel_minutes",
    "get_travel_minutes_between",
    "create_calendar_event",
    "delete_calendar_event",
    "confirm_calendar_event",
    "find_and_confirm_pending_event",
    "find_alternative_slots",
    "create_travel_time_blocks",
    "confirm_travel_time_blocks",
    "delete_travel_time_blocks",
]
