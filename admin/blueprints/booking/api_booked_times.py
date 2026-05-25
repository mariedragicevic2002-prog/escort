"""JSON API for booked time slots (calendar overlay for webform).

Incall: :func:`services.calendar.conflicts.check_conflict_from_cached_events` (in-person window).

Outcall / Dinner: :func:`services.calendar.conflicts.check_outcall_conflict_from_cached_events`
matches :func:`services.calendar.conflicts.check_outcall_conflict_with_travel` (extended travel window).

Verification: POST /api/booked-times with JSON body, e.g.::

    curl -s -X POST https://www.example.com/api/booked-times \\
      -H 'Content-Type: application/json' \\
      -d '{\"date\":\"2026-04-13\",\"duration\":\"1 hour\",\"experience\":\"\",\"incall_outcall\":\"outcall\"}' | jq .
"""

import hashlib
import json as _json
from datetime import datetime, time as dt_time, timedelta

from flask import jsonify, make_response, request

import config

from handlers.booking_coll._shared import OPERATING_HOURS_END_BUFFER_MINUTES

from .blueprint import booking_bp
from .helpers import _parse_available_hours, _parse_duration_minutes
from .overnight_calendar_date import calendar_date_for_overnight_slot
from .log import logger


def _time_candidates_for_experience(
    is_dinner: bool,
) -> list[tuple[int, int]]:
    if is_dinner:
        # Dinner window ends 21:00 — no operating-hours end buffer (unlike standard bookings).
        return [(h, m) for tmin in range(17 * 60, 21 * 60 + 1, 15) for h, m in [divmod(tmin, 60)]]
    return [
        (actual // 60, actual % 60)
        for base_mins in range(0, 1440 + 180 + 1, 15)
        for actual in (base_mins % 1440,)
    ]


def _get_booked_times_for_date(
    date_obj,
    duration_minutes: int,
    experience: str,
    is_outcall: bool,
    outcall_address: str | None,
    avail_start_hhmm: str,
    avail_end_hhmm: str,
) -> list[str]:
    """
    Return sorted HH:MM strings to disable in the webform. Raises on hard calendar I/O errors.
    """
    import pytz

    from services.calendar.conflicts import (
        check_conflict_from_cached_events,
        check_outcall_conflict_from_cached_events,
    )
    from services.calendar.travel_routing import (
        get_outcall_one_way_travel_minutes,
        get_outcall_return_travel_minutes,
    )
    from services.database_service import get_shared_db
    from utils.dinner_date import DINNER_DURATION_MINUTES


    from config import get_effective_escort_timezone

    tz = pytz.timezone(get_effective_escort_timezone())
    day_start = tz.localize(datetime.combine(date_obj, datetime.min.time()))
    day_end = tz.localize(datetime.combine(date_obj + timedelta(days=1), datetime.min.time()))

    is_dinner = (experience or "").strip() == "Dinner Date"
    booking_type = "dinner_date" if is_dinner else ""
    slot_dur = DINNER_DURATION_MINUTES if is_dinner else int(duration_minutes)
    use_travel = is_dinner or is_outcall
    addr = (outcall_address or "").strip() or None

    # List window must cover the latest possible booking end on this session day (overnight tail +
    # long durations + outcall travel). Previously day_end+6h missed weekend/overnight overlaps so
    # calendar bookings did not grey out in the webform.
    dur_h = max(1, (int(slot_dur) + 59) // 60)
    travel_h = 0
    if use_travel:
        if addr:
            travel_h = (
                get_outcall_one_way_travel_minutes(addr)
                + get_outcall_return_travel_minutes(addr)
                + 119
            ) // 60
        else:
            travel_h = 4
    pre_h = 2
    if use_travel and addr:
        pre_h = max(2, (get_outcall_one_way_travel_minutes(addr) + 59) // 60)
    tail_h = max(12, 3 + dur_h + travel_h)
    tail_h = min(tail_h, 96)
    window_start = day_start - timedelta(hours=pre_h)
    window_end = day_end + timedelta(hours=tail_h)
    logger.info(
        "[BOOKED_TIMES] date=%s window=%s to %s duration=%dmin outcall=%s experience=%r",
        date_obj,
        window_start.isoformat(),
        window_end.isoformat(),
        duration_minutes,
        is_outcall,
        experience,
    )
    _blocking_statuses = ['confirmed', 'reschedule-confirmed', 'reserved', 'travel', 'admin', 'social']
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        raise RuntimeError("Database unavailable")
    db_rows = db.execute_query(
        """SELECT id, client_name, status, start_time, end_time, type
           FROM bookings
           WHERE status = ANY(%s) AND start_time < %s AND end_time > %s""",
        (_blocking_statuses, window_end, window_start),
        fetch=True,
    ) or []
    all_events: list = [dict(r) if not isinstance(r, dict) else r for r in db_rows]
    logger.info("[BOOKED_TIMES] Found %d DB bookings for %s", len(all_events), date_obj)

    booked: set = set()
    time_candidates = _time_candidates_for_experience(is_dinner)

    for h, mm in time_candidates:
        slot_cal_date = calendar_date_for_overnight_slot(
            date_obj, h, mm, avail_start_hhmm, avail_end_hhmm
        )
        try:
            _ = tz.localize(datetime.combine(slot_cal_date, dt_time(h, mm)))
        except (ValueError, pytz.AmbiguousTimeError, pytz.NonExistentTimeError) as e:
            logger.debug("[BOOKED_TIMES] skip invalid local time %02d:%02d: %s", h, mm, e)
            continue
        # Use session anchor date in details for dinner; overnight post-midnight uses next civil day
        # so conflict geometry matches calendar events (same as webform POST normalization).
        details = {
            "date": slot_cal_date,
            "time": (h, mm),
            "duration": int(slot_dur),
            "incall_outcall": "outcall" if use_travel else "incall",
            "booking_type": booking_type,
        }
        if use_travel:
            details["outcall_address"] = addr
        # In-person / geometry: BASIL, PEACOCK, GRAPE, TOMATO/BANANA/unknown, etc. (all schedule types).
        # Outcall: also run travel-padded check so buffer rules match `check_outcall_conflict_with_travel` on submit.
        # Block if *either* reports a conflict (do not require both — avoids a single path missing overlap).
        c_geom, _ = check_conflict_from_cached_events(all_events, details)
        if use_travel:
            c_travel, _ = check_outcall_conflict_from_cached_events(all_events, details)
            blocked = (c_geom not in ("none",)) or (c_travel not in ("none",))
        else:
            blocked = c_geom not in ("none",)
        if blocked:
            booked.add(f"{h:02d}:{mm:02d}")

    return sorted(booked)


def _get_booked_times_for_date_with_fallback(
    date_obj,
    duration_minutes: int,
    experience: str,
    is_outcall: bool,
    outcall_address: str | None,
    avail_start_hhmm: str,
    avail_end_hhmm: str,
) -> tuple[list[str], str | None]:
    """Returns (booked_times, error). On hard failure, fail-closed: all standard slots + error set."""
    is_dinner = (experience or "").strip() == "Dinner Date"
    all_keys: set = set()
    for h, mm in _time_candidates_for_experience(is_dinner):
        all_keys.add(f"{h:02d}:{mm:02d}")
    try:
        return _get_booked_times_for_date(
            date_obj,
            duration_minutes,
            experience,
            is_outcall,
            outcall_address,
            avail_start_hhmm,
            avail_end_hhmm,
        ), None
    except Exception as e:
        logger.error("Booked times calendar error (fail-closed): %s", e)
        return sorted(all_keys), "Calendar unavailable; all times blocked until it loads. Try again."


def _body_is_outcall(incall_raw: str) -> bool:
    s = (incall_raw or "").strip().lower()
    if not s:
        return False
    return s in ("outcall", "o", "out") or s.startswith("out")


def _jsonify_with_etag(payload, status=200):
    """Return JSON response with ETag; 304 if client already has it."""
    raw = _json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.md5(raw.encode()).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return make_response("", 304)
    resp = make_response(raw, status)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["ETag"] = etag
    return resp


@booking_bp.route("/api/booked-times", methods=["POST"])
def api_booked_times():
    """Return HH:MM strings to grey out, aligned with incall or outcall travel rules."""
    try:
        data = request.get_json() or {}
        date_str = data.get("date", "")
        duration = data.get("duration", "")
        experience = (data.get("experience") or "").strip()
        incall_raw = data.get("incall_outcall", "")
        outcall_address = (data.get("outcall_address") or "").strip() or None

        from config import get_available_hours

        hours_str = get_available_hours()
        available_start, available_end = _parse_available_hours(hours_str)

        if experience == "Dinner Date":
            available_start, available_end = "17:00", "21:00"

        if not date_str:
            return _jsonify_with_etag(
                {
                    "booked_times": [],
                    "available_start": available_start,
                    "available_end": available_end,
                    "operating_hours_end_buffer_minutes": OPERATING_HOURS_END_BUFFER_MINUTES,
                    "error": None,
                }
            )

        try:
            date_obj = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return (
                jsonify(
                    {
                        "booked_times": [],
                        "available_start": available_start,
                        "available_end": available_end,
                        "operating_hours_end_buffer_minutes": OPERATING_HOURS_END_BUFFER_MINUTES,
                        "error": "Invalid date format",
                    }
                ),
                400,
            )

        duration_minutes = _parse_duration_minutes(duration)
        is_dinner = experience == "Dinner Date"
        is_outcall = is_dinner or _body_is_outcall(incall_raw)

        booked_list, err = _get_booked_times_for_date_with_fallback(
            date_obj,
            duration_minutes,
            experience,
            is_outcall,
            outcall_address,
            available_start,
            available_end,
        )
        return _jsonify_with_etag(
            {
                "booked_times": booked_list,
                "available_start": available_start,
                "available_end": available_end,
                "operating_hours_end_buffer_minutes": OPERATING_HOURS_END_BUFFER_MINUTES,
                "error": err,
            }
        )

    except Exception as e:
        logger.error("Booked times API error: %s", e, exc_info=True)
        return (
            jsonify(
                {
                    "booked_times": [],
                    "available_start": "00:00",
                    "available_end": "23:45",
                    "operating_hours_end_buffer_minutes": OPERATING_HOURS_END_BUFFER_MINUTES,
                    "error": "Internal error",
                }
            ),
            500,
        )
