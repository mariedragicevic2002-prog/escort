"""Calendar conflict checks — DB-backed."""

import logging
from datetime import timedelta

from services.calendar.booking_window import _parse_booking_window
from services.calendar.travel_routing import (
    get_outcall_one_way_travel_minutes,
    get_outcall_return_travel_minutes,
)

logger = logging.getLogger(__name__)

_BLOCKING_STATUSES = ['confirmed', 'reschedule-confirmed', 'reserved', 'travel', 'admin', 'social']


def _query_blocking_bookings(start_dt, end_dt) -> list:
    import config
    from services.database_service import get_shared_db

    db = get_shared_db(config.DATABASE_URL)
    if not db:
        logger.error("check_conflict: database unavailable")
        return []
    rows = db.execute_query(
        """SELECT id, client_name, status, start_time, end_time, type
           FROM bookings
           WHERE status = ANY(%s) AND start_time < %s AND end_time > %s""",
        (_BLOCKING_STATUSES, end_dt, start_dt),
        fetch=True,
    ) or []
    return [dict(r) if not isinstance(r, dict) else r for r in rows]


def _classify_db_rows(rows: list) -> tuple:
    if not rows:
        return "none", []
    reserved = [
        r for r in rows
        if (r.get('status') if isinstance(r, dict) else getattr(r, 'status', None)) == 'reserved'
    ]
    other = [
        r for r in rows
        if (r.get('status') if isinstance(r, dict) else getattr(r, 'status', None)) != 'reserved'
    ]
    if other:
        logger.warning("CONFLICT FOUND: %d confirmed/blocking bookings", len(other))
        return "confirmed", rows
    if reserved:
        logger.warning("CONFLICT FOUND: %d reserved bookings", len(reserved))
        return "peacock", rows
    return "none", []


def _make_aware(dt):
    """Ensure dt is timezone-aware using local timezone if naive."""
    if dt is None:
        return dt
    if hasattr(dt, 'tzinfo') and dt.tzinfo is None:
        from utils.timezone import get_local_timezone
        return get_local_timezone().localize(dt)
    return dt


def _rows_overlapping_window(items: list, win_start, win_end) -> list:
    in_window = []
    for item in items:
        if isinstance(item, dict):
            item_start = _make_aware(item.get('start_time'))
            item_end = _make_aware(item.get('end_time'))
        else:
            item_start = _make_aware(getattr(item, 'start_time', None))
            item_end = _make_aware(getattr(item, 'end_time', None))
        if item_start is None or item_end is None:
            continue
        if item_start < win_end and item_end > win_start:
            in_window.append(item if isinstance(item, dict) else dict(item))
    return in_window


def check_conflict(details):
    start_dt, end_dt = _parse_booking_window(details)
    if not start_dt or not end_dt:
        logger.error("check_conflict: could not parse booking window")
        return "unknown", []
    rows = _query_blocking_bookings(start_dt, end_dt)
    result = _classify_db_rows(rows)
    logger.info("check_conflict: %s → %s (%d rows)", details.get('date'), result[0], len(rows))
    return result


def check_outcall_conflict_with_travel(details):
    start_dt, end_dt = _parse_booking_window(details)
    if not start_dt or not end_dt:
        logger.error("check_outcall_conflict_with_travel: could not parse booking window")
        return "unknown", []
    out_addr = details.get("outcall_address")
    to_client_mins = get_outcall_one_way_travel_minutes(out_addr)
    from_client_mins = get_outcall_return_travel_minutes(out_addr)
    extended_start = start_dt - timedelta(minutes=to_client_mins)
    extended_end = end_dt + timedelta(minutes=from_client_mins)
    rows = _query_blocking_bookings(extended_start, extended_end)
    if not rows:
        return "none", []
    logger.warning("check_outcall_conflict_with_travel: %d blocking rows in travel window", len(rows))
    return "confirmed", rows


def check_conflict_from_cached_events(cached_items: list, details: dict) -> tuple:
    """Same as check_conflict but uses pre-fetched DB row dicts."""
    if not cached_items:
        return "none", []
    start_dt, end_dt = _parse_booking_window(details)
    if not start_dt or not end_dt:
        return "unknown", []
    in_window = _rows_overlapping_window(cached_items, start_dt, end_dt)
    blocking = [r for r in in_window if r.get('status') in _BLOCKING_STATUSES]
    return _classify_db_rows(blocking)


def check_outcall_conflict_from_cached_events(cached_items: list, details: dict) -> tuple:
    """Same as check_outcall_conflict_with_travel but uses pre-fetched DB row dicts."""
    if not cached_items:
        return "none", []
    start_dt, end_dt = _parse_booking_window(details)
    if not start_dt or not end_dt:
        return "unknown", []
    out_addr = details.get("outcall_address")
    to_client_mins = get_outcall_one_way_travel_minutes(out_addr)
    from_client_mins = get_outcall_return_travel_minutes(out_addr)
    extended_start = start_dt - timedelta(minutes=to_client_mins)
    extended_end = end_dt + timedelta(minutes=from_client_mins)
    in_window = _rows_overlapping_window(cached_items, extended_start, extended_end)
    blocking = [r for r in in_window if r.get('status') in _BLOCKING_STATUSES]
    if not blocking:
        return "none", []
    return "confirmed", blocking
