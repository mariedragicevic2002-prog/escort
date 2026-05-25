"""Find alternative time slots near a requested booking time."""

import logging
from datetime import timedelta

import config

from services.calendar.booking_window import _parse_booking_window
from services.database_service import get_shared_db

logger = logging.getLogger(__name__)


def find_alternative_slots(
    details,
    max_results=3,
    same_day_only=False,
    max_hours_from_requested: float | None = None,
):
    """Find alternative available time slots CLOSEST to the requested time.

    Returns slots in ascending chronological order.
    When same_day_only=True, only slots on the same calendar day as the requested
    date are returned (3 closest to requested time on that day, ascending).
    Otherwise searches before/after and prefers same date.

    When max_hours_from_requested is set (e.g. 2.0), only candidates within that
    many hours before/after the requested start are considered (golden rule for
    "requested time not available" replies).
    """
    start_dt, _ = _parse_booking_window(details)
    if not start_dt:
        return []

    try:
        from datetime import datetime

        from utils.timezone import get_current_datetime

        now = get_current_datetime()
        if start_dt.tzinfo is not None and getattr(now, 'tzinfo', None) is None:
            try:
                _tz = start_dt.tzinfo
                if hasattr(_tz, 'localize'):
                    now = _tz.localize(datetime.combine(now.date(), now.time()))
                else:
                    now = datetime.combine(now.date(), now.time()).replace(tzinfo=_tz)
            except Exception as e:
                logger.warning('Timezone alignment for alternative slots failed: %s', e)

        duration_minutes = details.get('duration', 60)
        booking_duration = timedelta(minutes=duration_minutes)

        requested_date = start_dt.date()
        if same_day_only:
            search_start = max(now, start_dt.replace(hour=0, minute=0, second=0, microsecond=0))
            search_end = start_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            search_start = max(now, start_dt - timedelta(days=2))
            search_end = start_dt + timedelta(days=5)

        blocking_statuses = ['confirmed', 'reschedule-confirmed', 'reserved', 'travel', 'admin', 'social']
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.error('Alternative slots: database unavailable')
            return []
        try:
            db_rows = db.execute_query(
                """SELECT start_time, end_time FROM bookings
                   WHERE status = ANY(%s) AND start_time < %s AND end_time > %s""",
                (blocking_statuses, search_end, search_start),
                fetch=True,
            ) or []
        except Exception as e:
            logger.error('Alternative slots DB query failed: %s', e)
            return []

        busy_periods = []
        for row in db_rows:
            try:
                ev_start = row['start_time'] if isinstance(row, dict) else row.start_time
                ev_end = row['end_time'] if isinstance(row, dict) else row.end_time
                if ev_start is None or ev_end is None:
                    continue
                if hasattr(ev_start, 'tzinfo') and ev_start.tzinfo is None:
                    from utils.timezone import get_local_timezone
                    ev_start = get_local_timezone().localize(ev_start)
                if hasattr(ev_end, 'tzinfo') and ev_end.tzinfo is None:
                    from utils.timezone import get_local_timezone
                    ev_end = get_local_timezone().localize(ev_end)
                busy_periods.append((ev_start, ev_end))
            except Exception as e:
                logger.warning('Skipping DB booking row when building busy periods: %s', e)
                continue

        def is_slot_available(slot_start):
            slot_end = slot_start + booking_duration
            for busy_start, busy_end in busy_periods:
                if slot_start < busy_end and slot_end > busy_start:
                    return False
            return True

        def is_valid_time(slot):
            if slot <= now:
                return False
            if slot.hour >= 3 and slot.hour < 15:
                return False
            slot_end = slot + booking_duration
            if slot_end.hour >= 3 and slot_end.hour < 15:
                return False
            return True

        candidate_slots = []
        _max_range_minutes = (
            int(max_hours_from_requested * 60)
            if max_hours_from_requested is not None
            else (60 * 24)
        )

        if same_day_only:
            start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            for offset_minutes in range(0, _max_range_minutes + 1, 15):
                after_slot = start_dt + timedelta(minutes=offset_minutes)
                if after_slot.date() != requested_date:
                    continue
                if is_valid_time(after_slot) and is_slot_available(after_slot):
                    time_distance = abs((after_slot - start_dt).total_seconds())
                    candidate_slots.append((time_distance, after_slot))
                if offset_minutes > 0:
                    before_slot = start_dt - timedelta(minutes=offset_minutes)
                    if (
                        before_slot.date() == requested_date
                        and is_valid_time(before_slot)
                        and is_slot_available(before_slot)
                    ):
                        time_distance = abs((before_slot - start_dt).total_seconds())
                        candidate_slots.append((time_distance, before_slot))
            candidate_slots.sort(key=lambda x: (x[0], x[1]))
            seen = set()
            result = []
            for _time_distance, slot in candidate_slots:
                slot_key = slot.strftime('%Y-%m-%d %H:%M')
                if slot_key not in seen:
                    if any(abs((slot - kept).total_seconds()) < 3600 for kept in result):
                        continue
                    seen.add(slot_key)
                    result.append(slot)
                    if len(result) >= max_results:
                        break
            result.sort()
            logger.info(f'Find alternatives result (same_day_only): requested={start_dt.isoformat()}, found={len(result)}')
            return result

        _max_range_minutes_wide = (
            int(max_hours_from_requested * 60)
            if max_hours_from_requested is not None
            else (60 * 24 * 5)
        )

        next_day_date = (start_dt + timedelta(days=1)).date()

        def get_date_priority(slot):
            slot_date = slot.date()
            if slot_date == requested_date:
                return 0
            elif slot_date == next_day_date and slot.hour < 3:
                return 0
            else:
                days_away = abs((slot_date - requested_date).days)
                return days_away

        for offset_minutes in range(0, _max_range_minutes_wide + 1, 15):
            after_slot = start_dt + timedelta(minutes=offset_minutes)
            if is_valid_time(after_slot) and is_slot_available(after_slot):
                date_priority = get_date_priority(after_slot)
                time_distance = abs((after_slot - start_dt).total_seconds())
                candidate_slots.append((date_priority, time_distance, after_slot))
            if offset_minutes > 0:
                before_slot = start_dt - timedelta(minutes=offset_minutes)
                if is_valid_time(before_slot) and is_slot_available(before_slot):
                    date_priority = get_date_priority(before_slot)
                    time_distance = abs((before_slot - start_dt).total_seconds())
                    candidate_slots.append((date_priority, time_distance, before_slot))

        candidate_slots.sort(key=lambda x: (x[0], x[1]))
        seen = set()
        result = []
        for _date_priority, _time_distance, slot in candidate_slots:
            slot_key = slot.strftime('%Y-%m-%d %H:%M')
            if slot_key not in seen:
                if any(abs((slot - kept).total_seconds()) < 3600 for kept in result):
                    continue
                seen.add(slot_key)
                result.append(slot)
                if len(result) >= max_results:
                    break
        result.sort()

        logger.info(f'Find alternatives result: requested={start_dt.isoformat()}, found={len(result)}')
        return result

    except Exception as e:
        logger.error(f'Find alternatives error: {e}')
        return []
