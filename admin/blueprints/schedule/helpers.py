"""Shared helpers for schedule management (no route handlers)."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import re
from datetime import datetime, timedelta
from typing import Any

from flask import request, session

import config
from services.calendar_service import get_calendar_service
from services.database_service import get_shared_db

from .log import logger

def _format_reschedule_datetime(dt, comma_after_weekday=False, space_before_am_pm=False):
    """Format for reschedule SMS: 'Thursday 26th Feb 09:00PM' or 'Sunday, 29th March 5:00 pm'."""
    weekday = dt.strftime("%A")
    day = dt.day
    suffix = "th" if 10 <= day % 100 <= 20 else {"1": "st", "2": "nd", "3": "rd"}.get(str(day % 10), "th")
    day_ord = f"{day}{suffix}"
    month = dt.strftime("%b")
    if space_before_am_pm:
        h = int(dt.strftime("%I").lstrip("0") or "12")
        time_str = f"{h}:{dt.strftime('%M')} {dt.strftime('%p').lower()}"
    else:
        time_str = dt.strftime("%I:%M%p")
    prefix = f"{weekday}, " if comma_after_weekday else f"{weekday} "
    return f"{prefix}{day_ord} {month} {time_str}"

# Import color constants from config to ensure consistency with conflict detection
COLOR_BASIL = config.COLOR_BASIL          # Green - Confirmed with deposit
COLOR_PEACOCK = config.COLOR_PEACOCK      # Turquoise - Reserved (no deposit)
COLOR_GRAPHITE = config.COLOR_GRAPHITE    # Grey - Pending deposit
COLOR_GRAPE = config.COLOR_GRAPE          # Purple - Confirmed travel time
COLOR_LAVENDER = config.COLOR_LAVENDER    # Light purple - Pending travel time
COLOR_BANANA = config.COLOR_BANANA        # Yellow - Maintenance
COLOR_TOMATO = config.COLOR_TOMATO        # Red - Social events


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _is_travel_block_summary(summary: str) -> bool:
    """True for bot-created travel blocks (incl. dinner date 'Travel time —' summaries)."""
    s = (summary or "").strip()
    if not s:
        return False
    sl = s.lower()
    return (
        sl.startswith("travel there")
        or sl.startswith("travel back")
        or sl.startswith("client travel back")
        or sl.startswith("travel time")
        or s.startswith("TRAVEL TIME")
    )


def _get_status_from_color(color_id, summary=""):
    """Get status class and label from Google Calendar color ID and optional summary."""
    color_id = str(color_id)
    summary = (summary or "").strip()

    if "PENDING RESCHEDULE" in summary:
        return "pending-reschedule", "Pending Reschedule"
    if "RESCHEDULE CONFIRMED" in summary:
        return "reschedule-confirmed", "Reschedule Confirmed"
    # Travel blocks: LAVENDER = pending travel (awaiting deposit), GRAPE = confirmed travel.
    # Dinner dates use summaries like "Travel time — {escort name} → restaurant · …" (not "Travel there").
    if _is_travel_block_summary(summary):
        if color_id == "1":  # Lavender = pending travel (awaiting deposit)
            return "pending-travel", "Travel Time (Pending)"
        if color_id == "3":  # GRAPE = confirmed travel (after deposit)
            return "travel", "Travel Time (Confirmed)"
        return "travel", "Travel Time"
    if color_id in ["2", "10"]:  # Sage or Basil = Confirmed
        return "confirmed", "Confirmed (Deposit Paid)"
    elif color_id == "8":  # Graphite = Pending Deposit
        return "pending-deposit", "Pending Deposit"
    elif color_id == "7":  # Peacock = Reserved
        return "reserved", "Reserved (No Deposit)"
    elif color_id == "3":  # Grape = travel (no travel-shaped summary, still show confirmed)
        return "travel", "Travel Time (Confirmed)"
    elif color_id in ["5", "6"]:  # Banana or Tangerine = Admin
        return "admin", "Admin/Maintenance"
    elif color_id in ["11", "4"]:  # Tomato or Flamingo = Social
        return "social", "Social/Personal"
    else:
        return "manual", "Manual Entry"


def _event_start_end_local(event, tz):
    """
    Convert Google Calendar event start/end into timezone-aware local datetimes.
    Supports both timed events ("dateTime") and all-day events ("date").
    """
    start_raw = event.get("start", {}) or {}
    end_raw = event.get("end", {}) or {}

    start_dt_iso = start_raw.get("dateTime")
    end_dt_iso = end_raw.get("dateTime")
    if start_dt_iso and end_dt_iso:
        start_dt = datetime.fromisoformat(start_dt_iso.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_dt_iso.replace("Z", "+00:00"))
        if start_dt.tzinfo is None:
            start_dt = tz.localize(start_dt)
        if end_dt.tzinfo is None:
            end_dt = tz.localize(end_dt)
        return start_dt.astimezone(tz), end_dt.astimezone(tz)

    start_date = start_raw.get("date")
    end_date = end_raw.get("date")
    if start_date and end_date:
        start_naive = datetime.strptime(start_date, "%Y-%m-%d")
        # Google all-day end date is exclusive; subtract one minute for display range.
        end_naive = datetime.strptime(end_date, "%Y-%m-%d") - timedelta(minutes=1)
        return tz.localize(start_naive), tz.localize(end_naive)

    raise ValueError("Event missing supported start/end fields")


def _event_overlaps_local_date(event, tz, target_date):
    """
    True when event overlaps target local date.
    This is robust for events crossing midnight and API timezone quirks.
    """
    start_local, end_local = _event_start_end_local(event, tz)
    target_start = tz.localize(datetime.combine(target_date, datetime.min.time()))
    target_end = target_start + timedelta(days=1)
    return start_local < target_end and end_local >= target_start


def _event_matches_selected_date(event, tz, target_date):
    """
    Robust date membership:
    - Primary: timezone-aware overlap check
    - Fallback: raw ISO date prefix match on start/end fields
    """
    try:
        if _event_overlaps_local_date(event, tz, target_date):
            return True
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)

    target_str = target_date.strftime("%Y-%m-%d")
    start = event.get("start", {}) or {}
    end = event.get("end", {}) or {}
    for value in (
        start.get("dateTime"),
        start.get("date"),
        end.get("dateTime"),
        end.get("date"),
    ):
        if isinstance(value, str) and value.startswith(target_str):
            return True
    return False


def _merge_events_with_db_event_ids(service, events, target_date, tz):
    """
    Supplement calendar list results with direct event lookups from conversation state.
    This guards against occasional list/query inconsistencies for known booking IDs.
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return events
        rows = db.execute_query(
            """
            SELECT peacock_event_id, graphite_event_id, confirmed_event_id
            FROM conversation_states
            WHERE date = %s
              AND (
                    peacock_event_id IS NOT NULL
                    OR graphite_event_id IS NOT NULL
                    OR confirmed_event_id IS NOT NULL
                  )
            """,
            (target_date.strftime("%Y-%m-%d"),),
            fetch=True
        ) or []
    except Exception as e:
        logger.warning("DB event-id merge skipped (query failed): %s", e)
        return events

    existing_ids = {e.get("id") for e in events if e.get("id")}
    candidate_ids = set()
    for row in rows:
        peacock_id = (row.get("peacock_event_id") or "").strip()
        graphite_id = (row.get("graphite_event_id") or "").strip()
        confirmed_id = (row.get("confirmed_event_id") or "").strip()
        if peacock_id:
            candidate_ids.add(peacock_id)
        if graphite_id:
            candidate_ids.add(graphite_id)
        if confirmed_id:
            candidate_ids.add(confirmed_id)

    added = 0
    for event_id in sorted(candidate_ids):
        if not event_id or event_id in existing_ids:
            continue
        try:
            event = service.events().get(
                calendarId=config.get_google_calendar_id(),
                eventId=event_id
            ).execute()
            status = (event.get("status") or "").strip().lower()
            if status == "cancelled":
                continue
            if _event_matches_selected_date(event, tz, target_date):
                events.append(event)
                existing_ids.add(event_id)
                added += 1
        except Exception as e:
            logger.warning("Skipping DB-linked event %s: %s", event_id, e)

    if added:
        logger.info("Schedule list augmented with %s DB-linked event(s)", added)
    return events


def _is_description_section_header(nxt: str) -> bool:
    """True when a line is a label/section start (not a continuation of an address line)."""
    nl = nxt.lower().strip()
    if not nl:
        return True
    return (
        nl.startswith("current address")
        or nl.startswith("destination address")
        or nl.startswith("travel time")
        or nl.startswith("experience type")
        or nl.startswith("phone:")
        or nl.startswith("client (booking name)")
    )


def _current_or_destination_value(i: int, lines: list) -> tuple[str | None, int]:
    """
    One header line: ``Current / Destination address (...): [optional same-line value]`` with the
    value on the same line, or the next non-empty line (standard outcall LAVENDER/GRAPE body).
    """
    line = lines[i] if 0 <= i < len(lines) else ""
    s = line.strip()
    low = s.lower()
    is_cur = low.startswith("current address")
    is_dest = low.startswith("destination address")
    if not (is_cur or is_dest) or ":" not in s:
        return None, i
    after = s.rsplit(":", 1)[-1].strip()
    if after:
        return after, i
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    if j < len(lines) and not _is_description_section_header(lines[j]):
        return lines[j].strip(), j
    return None, i


def _parse_travel_leg_client_from_summary(summary: str) -> str | None:
    """
    First / second name in a ``(A – B)`` pair from a travel leg calendar title, e.g.
    ``Travel there (escort – client)`` or ``Client travel back (client – escort)``.
    """
    s = (summary or "").strip()
    if not s:
        return None
    m = re.match(
        r"^travel there\s*\(\s*(?P<escort>[^)]+?)\s*[–—\-]\s*(?P<client>[^)]+?)\s*\)\s*$",
        s,
        re.I,
    )
    if m:
        return m.group("client").strip()
    m = re.match(
        r"^client travel back\s*\(\s*(?P<client>[^)]+?)\s*[–—\-]\s*(?P<escort>[^)]+?)\s*\)\s*$",
        s,
        re.I,
    )
    if m:
        return m.group("client").strip()
    m = re.match(
        r"^travel back\s*\(\s*(?P<a>[^)]+?)\s*[–—\-]\s*(?P<b>[^)]+?)\s*\)\s*$",
        s,
        re.I,
    )
    if m:
        return m.group("a").strip()
    return None


def _parse_travel_leg_client_from_line(line: str) -> str | None:
    s = (line or "").strip()
    m = re.search(
        r"^Travel there\s*\(\s*(?P<escort>[^)]+?)\s*[–—\-]\s*(?P<client>[^)]+?)\s*\)\s*",
        s,
        re.I,
    )
    if m:
        return m.group("client").strip()
    m = re.search(
        r"^Client travel back\s*\(\s*(?P<client>[^)]+?)\s*[–—\-]\s*(?P<escort>[^)]+?)\s*\)\s*",
        s,
        re.I,
    )
    if m:
        return m.group("client").strip()
    m = re.search(
        r"^Travel back\s*\(\s*(?P<a>[^)]+?)\s*[–—\-]\s*(?P<b>[^)]+?)\s*\)\s*",
        s,
        re.I,
    )
    if m:
        return m.group("a").strip()
    return None


def _enrich_travel_block_fields(details, summary) -> None:
    """
    For standard outcall LAVENDER/GRAPE legs: set legacy *address* and default *duration* when
    the body did not list travel minutes; do not clobber *experience* from ``Experience type:``.

    Dinner-date summaries (``Travel time — …``) already carry minutes in the description; only fill
    missing address/location fields and never replace a parsed duration.
    """
    s = (summary or "").strip()
    if not s:
        return
    sl = s.lower()
    # Dinner date purple summaries: "Travel time — escort → restaurant · …" etc.
    if sl.startswith("travel time —") or sl.startswith("travel time –") or re.match(
        r"(?i)^travel time\s+[—–\-]",
        s,
    ):
        if not (details.get("location_type") or "").strip():
            details["location_type"] = "Travel"
        o = (details.get("origin_address") or "").strip()
        d = (details.get("destination_address") or "").strip()
        if o and not (details.get("from_address") or "").strip():
            details["from_address"] = o
        if d and not (details.get("to_address") or "").strip():
            details["to_address"] = d
        if not (details.get("address") or "").strip():
            details["address"] = details.get("to_address") or d or None
        return
    is_there = sl.startswith("travel there")
    is_back = sl.startswith("travel back") or sl.startswith("client travel back")
    if not (is_there or is_back):
        return
    o = (details.get("origin_address") or "").strip()
    d = (details.get("destination_address") or "").strip()
    if is_there:
        if o and not (details.get("from_address") or "").strip():
            details["from_address"] = o
        if d and not (details.get("to_address") or "").strip():
            details["to_address"] = d
        if not (details.get("address") or "").strip():
            details["address"] = details.get("to_address") or d or None
    else:
        if o and not (details.get("from_address") or "").strip():
            details["from_address"] = o
        if d and not (details.get("to_address") or "").strip():
            details["to_address"] = d
        if not (details.get("address") or "").strip():
            details["address"] = details.get("from_address") or o or None
    if not (details.get("location_type") or "").strip():
        details["location_type"] = "Travel"
    if not (details.get("duration") or "").strip():
        if is_there:
            details["duration"] = "Travel time going there"
        else:
            details["duration"] = "Travel time coming back"


def _set_schedule_display_client_name(details, summary, status_class, status_label, color_id) -> None:
    """
    Coloured non-booking blocks use the event title; travel legs prefer a parsed client when the
    title uses ``(Escort – Client)`` so LAVENDEL/GRAPE match apart from title/colour.
    """
    s = (summary or "").strip()
    cid = str(color_id)
    status_class = status_class or ""
    if status_class in ("travel", "pending-travel"):
        p = _parse_travel_leg_client_from_summary(s)
        if p:
            details["client_name"] = p
        elif (details.get("client_name") or "").strip() not in ("", "Client"):
            pass
        else:
            details["client_name"] = (details.get("client_name") or "") or s or status_label
        return
    if status_class in ("admin", "social"):
        details["client_name"] = s or status_class.title()
        return
    if cid in (str(COLOR_GRAPE), str(COLOR_LAVENDER), str(COLOR_BANANA), str(COLOR_TOMATO)):
        details["client_name"] = s or status_label
    return


def _parse_event_description(description):
    """Parse event description to extract booking details."""
    details: dict[str, Any] = {
        'phone_number': None,
        'client_name': None,
        'duration': None,
        'experience': None,
        'organise_other_escort': None,
        'safety_screening_status': None,
        'location_type': None,
        'address': None,
        'from_address': None,
        'to_address': None,
        'origin_address': None,
        'destination_address': None,
        'price': None,
        'deposit_paid': None,
        'deposit_due': None,
        'remaining_balance': None,
        'special_requests': None,
        'preferences': None,
        'notes': None,
        'payment_reference': None,
    }

    if not description:
        return details

    # Multi-line LAVENDER/GRAPE / standard outcall bodies, plus drive-time line and title in description.
    lines = [ln.rstrip() for ln in description.split("\n")]
    i = 0
    while i < len(lines):
        st = lines[i].strip()
        stl = st.lower()
        if stl.startswith("current address"):
            val, ni = _current_or_destination_value(i, lines)
            if val:
                details['origin_address'] = val
            i = ni + 1
            continue
        if stl.startswith("destination address"):
            val, ni = _current_or_destination_value(i, lines)
            if val:
                details['destination_address'] = val
            i = ni + 1
            continue
        if re.match(r"(?i)^Travel time:\s*", st):
            details['duration'] = re.sub(r"(?i)^Travel time:\s*", "", st).strip() or "Travel"
        elif re.match(r"(?i)^estimated drive:\s*", st):
            ed = re.sub(r"(?i)^estimated drive:\s*", "", st).strip()
            if ed:
                details["duration"] = ed
        elif (not (details.get("client_name") or "").strip()) and _parse_travel_leg_client_from_line(st):
            details['client_name'] = _parse_travel_leg_client_from_line(st)
        i += 1

    for line in description.split("\n"):
        if "Phone:" in line:
            details['phone_number'] = line.split("Phone:")[-1].strip()
        elif "Client (booking name):" in line:
            details['client_name'] = line.split("Client (booking name):")[-1].strip()
        elif line.strip().startswith("Experience type:"):
            details['experience'] = line.split("Experience type:", 1)[-1].strip()
        elif "Name:" in line:
            details['client_name'] = line.split("Name:")[-1].strip()
        elif "Duration:" in line:
            details['duration'] = line.split("Duration:")[-1].strip()
        elif "Experience:" in line:
            details['experience'] = line.split("Experience:")[-1].strip()
        elif line.strip().startswith("Organise other escort:"):
            details['organise_other_escort'] = line.split("Organise other escort:", 1)[-1].strip()
        elif line.strip().startswith("Organize other escort:"):
            details['organise_other_escort'] = line.split("Organize other escort:", 1)[-1].strip()
        elif re.match(r"(?i)^mmf exploration:\s*", line.strip()):
            details["preferences"] = line.split(":", 1)[-1].strip()
        elif re.match(r"(?i)^preferences:\s*", line.strip()):
            if not (details.get("preferences") or "").strip():
                details["preferences"] = line.split(":", 1)[-1].strip()
        elif line.strip().startswith("Safety screening:"):
            details['safety_screening_status'] = line.split("Safety screening:", 1)[-1].strip()
        elif line.strip().startswith("Safety Screening:"):
            details['safety_screening_status'] = line.split("Safety Screening:", 1)[-1].strip()
        elif "Type:" in line:
            details['location_type'] = line.split("Type:")[-1].strip()
        elif "Address:" in line:
            details['address'] = line.split("Address:")[-1].strip()
        elif "Client:" in line:
            details['client_name'] = line.split("Client:")[-1].strip()
        elif line.strip().startswith("From:"):
            details['from_address'] = line.split("From:")[-1].strip()
        elif line.strip().startswith("To:"):
            details['to_address'] = line.split("To:")[-1].strip()
        elif "Price:" in line:
            details['price'] = line.split("Price:")[-1].strip()
        elif "Deposit Paid:" in line:
            details['deposit_paid'] = line.split("Deposit Paid:")[-1].strip()
        elif "Deposit Due:" in line:
            details['deposit_due'] = line.split("Deposit Due:")[-1].strip()
        elif "Deposit:" in line:
            # Handle legacy/alternate format from confirm_calendar_event:
            # "💰 Deposit: $100 PAID"
            deposit_value = line.split("Deposit:")[-1].strip()
            lowered = deposit_value.lower()
            if "paid" in lowered:
                details['deposit_paid'] = deposit_value
            elif "pending" in lowered:
                details['deposit_due'] = deposit_value
            else:
                # Fallback: if amount present, treat as paid-style display value.
                if _parse_amount(deposit_value) is not None:
                    details['deposit_paid'] = deposit_value
        elif "Payment reference:" in line:
            details['payment_reference'] = line.split("Payment reference:", 1)[-1].strip()
        elif re.match(r"(?i)^estimated drive:\s*", line.strip()):
            ed = line.split(":", 1)[-1].strip()
            if ed:
                details["duration"] = ed
        elif re.match(r"(?i)^Travel time:\s*", line.strip()):
            tt = line.split(":", 1)[-1].strip()
            if tt:
                details["duration"] = tt
        elif re.match(r"(?i)^Special requests:\s*", line.strip()):
            details['special_requests'] = line.split(":", 1)[-1].strip()
        elif "Notes:" in line:
            notes_val = line.split("Notes:")[-1].strip()
            details['notes'] = notes_val
            if not (details.get("special_requests") or "").strip():
                details['special_requests'] = notes_val

    return details


def _parse_amount(value):
    """Parse dollar-like text into int dollars."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)", s.replace(",", ""))
    if not m or m.lastindex is None or m.lastindex < 1:
        return None
    try:
        return int(round(float(m.group(1))))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return None


def _format_amount(amount):
    """Format int amount for schedule display."""
    if amount is None:
        return None
    return f"${int(amount)}"


from utils.row_utils import row_get

def _build_financial_result(row):
    """Build normalized financial display values from a conversation_states row.
    Accept either dict-like rows (RealDictCursor) or tuple-like rows and try sensible
    positional fallbacks for legacy schemas."""
    if not row:
        return {}

    def _pos_val(r, *indices):
        for i in indices:
            v = row_get(r, i, None)
            if v is not None:
                return v
        return None

    deposit_paid_flag = bool(
        row_get(row, 'deposit_paid', _pos_val(row, 0))
        or row_get(row, 'optional_deposit_paid', _pos_val(row, 1))
    )

    # optional_deposit_amount may appear at index 3 (full schema) or 2 (fallback schema)
    deposit_amount = _parse_amount(
        row_get(row, 'optional_deposit_amount', _pos_val(row, 3, 2))
        or row_get(row, 'deposit_amount', _pos_val(row, 2, 1))
    )

    # total_booking_cost / price may be at index 4/5 or 3/4 depending on schema
    total_cost = _parse_amount(
        row_get(row, 'total_booking_cost', _pos_val(row, 4, 3, 2))
        or row_get(row, 'price', _pos_val(row, 5, 4, 3))
    )

    result = {}
    if deposit_amount is not None:
        result["_deposit_amount"] = deposit_amount
    ref = (row_get(row, "deposit_payment_reference", None) or "").strip()
    # Always attach reference key when deposit is paid so UI can show "—" if unknown.
    if deposit_paid_flag:
        result["deposit_reference"] = ref
    # Only show "Deposit Paid" when the booking is actually marked paid.
    if deposit_paid_flag and deposit_amount is not None:
        result["deposit_paid"] = _format_amount(deposit_amount)
        if total_cost is not None:
            result["remaining_balance"] = _format_amount(max(total_cost - deposit_amount, 0))

    raw_mmf = row_get(row, "mmf_exploration_tags", None)
    if raw_mmf:
        try:
            from booking.mmf_exploration import decode_mmf_exploration_tags, humanize_mmf_exploration_tags

            mmf_labels = humanize_mmf_exploration_tags(decode_mmf_exploration_tags(raw_mmf))
            if mmf_labels:
                result["_preferences_from_state"] = mmf_labels
        except Exception:
            pass
    return result


def _normalized_phone_candidates(phone_number):
    """Return common normalized phone variants for DB matching."""
    digits = re.sub(r"\D", "", str(phone_number or ""))
    if not digits:
        return []
    candidates = {digits}
    if digits.startswith("61") and len(digits) >= 11:
        candidates.add("0" + digits[2:])
    if digits.startswith("0") and len(digits) >= 10:
        candidates.add("61" + digits[1:])
    return [c for c in candidates if c]


def _get_state_financials_by_phone(phone_number, cache):
    """Read deposit/total values from conversation state with per-request cache."""
    if not phone_number or phone_number == "Not provided":
        return {}
    if phone_number in cache:
        return cache[phone_number]

    db = get_shared_db(config.DATABASE_URL)
    if db is None:
        return {}
    try:
        candidates = _normalized_phone_candidates(phone_number)
        if candidates:
            try:
                rows = db.execute_query(
                    """
                    SELECT deposit_paid, optional_deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                    FROM conversation_states
                    WHERE regexp_replace(phone_number, '[^0-9]', '', 'g') IN %s
                    LIMIT 1
                    """,
                    (tuple(candidates),),
                    fetch=True
                ) or []
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                rows = db.execute_query(
                    """
                    SELECT deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                    FROM conversation_states
                    WHERE regexp_replace(phone_number, '[^0-9]', '', 'g') IN %s
                    LIMIT 1
                    """,
                    (tuple(candidates),),
                    fetch=True
                ) or []
        else:
            try:
                rows = db.execute_query(
                    """
                    SELECT deposit_paid, optional_deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                    FROM conversation_states
                    WHERE phone_number = %s
                    LIMIT 1
                    """,
                    (phone_number,),
                    fetch=True
                ) or []
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                rows = db.execute_query(
                    """
                    SELECT deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                    FROM conversation_states
                    WHERE phone_number = %s
                    LIMIT 1
                    """,
                    (phone_number,),
                    fetch=True
                ) or []
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        rows = []

    if not rows:
        cache[phone_number] = {}
        return {}

    row = rows[0]
    result = _build_financial_result(row)
    cache[phone_number] = result
    return result


def _get_state_financials_by_event_id(event_id, cache):
    """Read deposit/total values from conversation state by linked calendar event ID."""
    if not event_id:
        return {}
    if event_id in cache:
        return cache[event_id]

    db = get_shared_db(config.DATABASE_URL)
    if db is None:
        return {}
    try:
        try:
            rows = db.execute_query(
                """
                SELECT deposit_paid, optional_deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                FROM conversation_states
                WHERE ('|' || COALESCE(peacock_event_id, '') || '|') LIKE '%|' || %s || '|%'
                   OR ('|' || COALESCE(graphite_event_id, '') || '|') LIKE '%|' || %s || '|%'
                   OR ('|' || COALESCE(confirmed_event_id, '') || '|') LIKE '%|' || %s || '|%'
                LIMIT 1
                """,
                (event_id, event_id, event_id),
                fetch=True
            ) or []
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            rows = db.execute_query(
                """
                SELECT deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                FROM conversation_states
                WHERE ('|' || COALESCE(peacock_event_id, '') || '|') LIKE '%|' || %s || '|%'
                   OR ('|' || COALESCE(graphite_event_id, '') || '|') LIKE '%|' || %s || '|%'
                   OR ('|' || COALESCE(confirmed_event_id, '') || '|') LIKE '%|' || %s || '|%'
                LIMIT 1
                """,
                (event_id, event_id, event_id),
                fetch=True
            ) or []
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        rows = []

    if not rows:
        cache[event_id] = {}
        return {}

    result = _build_financial_result(rows[0])
    cache[event_id] = result
    return result


def _get_state_financials_by_signature(selected_date, start_local, client_name, cache):
    """Fallback financial lookup by booking date/time/client when phone/event IDs are missing."""
    try:
        client_key = (client_name or "").strip().lower()
        time_key = start_local.strftime("%H:%M") if start_local else ""
        cache_key = f"{selected_date}|{time_key}|{client_key}"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return {}

    if not selected_date or not start_local or not client_key or cache_key in cache:
        return cache.get(cache_key, {})

    db = get_shared_db(config.DATABASE_URL)
    if db is None:
        return {}
    try:
        try:
            rows = db.execute_query(
                """
                SELECT deposit_paid, optional_deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                FROM conversation_states
                WHERE date = %s
                  AND lower(coalesce(client_name, '')) = %s
                ORDER BY last_message_at DESC NULLS LAST
                LIMIT 3
                """,
                (selected_date, client_key),
                fetch=True
            ) or []
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            rows = db.execute_query(
                """
                SELECT deposit_paid, deposit_amount, optional_deposit_amount, total_booking_cost, price, deposit_payment_reference, mmf_exploration_tags
                FROM conversation_states
                WHERE date = %s
                  AND lower(coalesce(client_name, '')) = %s
                ORDER BY last_message_at DESC NULLS LAST
                LIMIT 3
                """,
                (selected_date, client_key),
                fetch=True
            ) or []
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        rows = []

    if not rows:
        cache[cache_key] = {}
        return {}

    # Prefer row that has a paid deposit indication.
    chosen = rows[0]
    for row in rows:
        built = _build_financial_result(row)
        if built.get("deposit_paid"):
            chosen = row
            break

    result = _build_financial_result(chosen)
    cache[cache_key] = result
    return result


def _apply_financial_fallback(details, phone_number, event_id, status_class, selected_date, start_local, financial_cache, event_financial_cache, signature_financial_cache):
    """Fill missing financial fields from conversation state when available."""
    state_financials = _get_state_financials_by_phone(phone_number, financial_cache)
    if not state_financials:
        state_financials = _get_state_financials_by_event_id(event_id, event_financial_cache)
    if not state_financials:
        state_financials = _get_state_financials_by_signature(
            selected_date,
            start_local,
            details.get("client_name"),
            signature_financial_cache,
        )
    if not details.get("deposit_paid") and state_financials.get("deposit_paid"):
        details["deposit_paid"] = state_financials["deposit_paid"]
    # Some legacy rows miss deposit_paid flag updates; infer paid deposit for confirmed/basil bookings.
    if (
        not details.get("deposit_paid")
        and status_class == "confirmed"
        and state_financials.get("_deposit_amount") is not None
    ):
        details["deposit_paid"] = _format_amount(state_financials.get("_deposit_amount"))
    if not details.get("remaining_balance") and state_financials.get("remaining_balance"):
        details["remaining_balance"] = state_financials["remaining_balance"]
    if not details.get("deposit_reference") and state_financials.get("deposit_reference"):
        details["deposit_reference"] = state_financials["deposit_reference"]
    if not details.get("deposit_reference") and details.get("payment_reference"):
        details["deposit_reference"] = details["payment_reference"]
    from booking.mmf_exploration import schedule_should_show_mmf_preferences, scrub_schedule_mmf_preferences

    if not (details.get("preferences") or "").strip() and state_financials.get("_preferences_from_state"):
        if schedule_should_show_mmf_preferences(details):
            details["preferences"] = state_financials["_preferences_from_state"]

    scrub_schedule_mmf_preferences(details)
    return details


def _parse_summary_fallback(summary):
    """
    Extract fallback booking details from summary lines like:
    - RESERVED INCALL - Henry - DGFE
    - PENDING DEPOSIT (Henry) - GFE - OUTCALL @ Address
    """
    result: dict[str, Any] = {
        "client_name": None,
        "experience": None,
        "location_type": None,
    }
    text = (summary or "").strip()
    if not text:
        return result

    upper = text.upper()
    if "INCALL" in upper:
        result["location_type"] = "incall"
    elif "OUTCALL" in upper:
        result["location_type"] = "outcall"

    # Common format: "... - Name - Experience"
    parts = [p.strip() for p in text.split(" - ") if p.strip()]
    if len(parts) >= 3:
        potential_name = parts[1]
        if potential_name:
            result["client_name"] = potential_name.title()
        potential_exp = parts[2].split(" @ ")[0].strip()
        if potential_exp:
            result["experience"] = potential_exp.upper()
        return result

    # Alternate format: "PENDING DEPOSIT (Name) - GFE - OUTCALL ..."
    m = re.search(r"\(([^)]+)\)", text)
    if m:
        maybe_name = m.group(1).strip()
        if maybe_name:
            result["client_name"] = maybe_name.title()

    exp_match = re.search(r"\b(GFE|DGFE|PSE)\b", upper)
    if exp_match:
        result["experience"] = exp_match.group(1)

    return result


def _apply_summary_fallback(details, summary):
    """Fill missing booking fields from summary text when description is sparse."""
    parsed = _parse_summary_fallback(summary)

    current_name = (details.get("client_name") or "").strip().lower()
    if not current_name or current_name == "client":
        if parsed.get("client_name"):
            details["client_name"] = parsed["client_name"]

    if not (details.get("experience") or "").strip() and parsed.get("experience"):
        details["experience"] = parsed["experience"]

    if not (details.get("location_type") or "").strip() and parsed.get("location_type"):
        details["location_type"] = parsed["location_type"]

    return details


def _get_local_timezone():
    """Escort-local ``pytz`` zone (admin Location) — same as :func:`utils.timezone.get_local_timezone`."""
    get_shared_db(config.DATABASE_URL)
    from utils.timezone import get_local_timezone

    return get_local_timezone()


def _format_friendly_date(date_obj):
    """Format date as friendly string (e.g., 'Monday 15 January 2024')."""
    return date_obj.strftime("%A %d %B %Y")


def _get_current_datetime():
    """Get current datetime in local timezone."""
    tz = _get_local_timezone()
    return datetime.now(tz)


def _fetch_raw_event_debug_for_date(selected_date):
    """Fetch lightweight raw event debug data for a date."""
    try:
        service = get_calendar_service()
        if not service:
            return {
                "error": "Calendar service unavailable",
                "raw_count": 0,
                "active_count": 0,
                "cancelled_count": 0,
                "items": [],
            }
        cal_id = (config.get_google_calendar_id() or "").strip()
        if not cal_id:
            return {
                "error": "Calendar ID is not configured. Open Config, set Google Calendar ID, save, then reload.",
                "raw_count": 0,
                "active_count": 0,
                "cancelled_count": 0,
                "items": [],
            }
        tz = _get_local_timezone()
        date_obj = datetime.strptime(selected_date, "%Y-%m-%d").date()
        day_start = tz.localize(datetime.combine(date_obj, datetime.min.time()))
        # Query a wider range, then filter by local overlap ourselves.
        time_min = (day_start - timedelta(hours=12)).isoformat()
        time_max = (day_start + timedelta(days=1, hours=12)).isoformat()
        events_result = service.events().list(
            calendarId=cal_id,
            timeMin=time_min,
            timeMax=time_max,
            timeZone=str(tz),
            maxResults=2500,
            showDeleted=True,
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = [e for e in events_result.get('items', []) if _event_matches_selected_date(e, tz, date_obj)]
        events = _merge_events_with_db_event_ids(service, events, date_obj, tz)
        active_count = 0
        cancelled_count = 0
        items = []
        for event in events:
            start = event.get("start", {}) or {}
            status = (event.get("status") or "").strip().lower() or "unknown"
            if status == "cancelled":
                cancelled_count += 1
            else:
                active_count += 1
            items.append({
                "id": event.get("id"),
                "summary": event.get("summary", ""),
                "colorId": str(event.get("colorId", "")),
                "status": status,
                "startDateTime": start.get("dateTime", ""),
                "startDate": start.get("date", ""),
            })
        return {
            "error": None,
            "raw_count": len(events),
            "active_count": active_count,
            "cancelled_count": cancelled_count,
            "items": items,
        }
    except Exception as e:
        return {
            "error": str(e),
            "raw_count": 0,
            "active_count": 0,
            "cancelled_count": 0,
            "items": [],
        }


def _is_schedule_authenticated():
    """Check if user is authenticated for schedule — accepts session cookie OR API key header."""
    if session.get("schedule_authenticated") or session.get("admin_authenticated"):
        return True
    # Accept API key via Authorization: Bearer <key> or X-API-Key: <key>
    try:
        from core.settings_manager import get_setting
        stored_key = (get_setting("schedule_api_key") or "").strip()
        if stored_key:
            auth_header = (request.headers.get("Authorization") or "").strip()
            if auth_header.startswith("Bearer "):
                provided = auth_header[7:].strip()
            else:
                provided = (request.headers.get("X-API-Key") or "").strip()
            if provided and provided == stored_key:
                return True
    except Exception as e:
        logger.warning("API key auth check failed: %s", e)
    return False
