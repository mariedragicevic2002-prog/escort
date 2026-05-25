"""Create, update, delete, and search booking rows in PostgreSQL."""

import logging
from datetime import date, datetime

import config
from config import ADELLA_CALENDAR_SOFT_HOLD_MARKER, COLOR_GRAPE, COLOR_LAVENDER

from booking.mmf_exploration import (
    decode_mmf_exploration_tags,
    format_mmf_exploration_calendar_line,
    humanize_mmf_exploration_tags,
    should_append_mmf_exploration_to_calendar,
)
from services.calendar.booking_window import (
    _format_duration_label,
    _parse_booking_window,
    parse_booking_time_hour_minute,
)
from services.calendar.client import HttpError  # backward compatibility
from services.calendar.travel_blocks import create_travel_time_blocks
from services.database_service import get_shared_db
from services.push_notification_service import send_new_booking_push_for_booking_id

logger = logging.getLogger(__name__)

try:
    from utils.circuit_breaker import circuit_breaker
    from utils.error_handler import retry_with_backoff
except ImportError:

    def circuit_breaker(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

    def retry_with_backoff(*args, **kwargs):
        def decorator(func):
            return func

        return decorator


def _is_doubles_booking(booking_details) -> bool:
    booking_type = str(booking_details.get("booking_type") or "").strip().lower()
    experience = str(booking_details.get("experience_type") or "").strip().lower()
    if booking_type in {"doubles_mff", "Doubles MMF"}:
        return True
    return any(token in experience for token in ("doubles mff", "doubles mmf", "doubles_mff", "Doubles MMF"))


def _normalize_organise_other_escort(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "yes" if value else "no"
    normalized = str(value).strip().lower()
    if normalized in {"yes", "y", "true", "1", "escort"}:
        return "yes"
    if normalized in {"no", "n", "false", "0", "client"}:
        return "no"
    return None


def _resolve_organise_other_escort(booking_details) -> str | None:
    explicit = _normalize_organise_other_escort(booking_details.get("organise_other_escort"))
    if explicit:
        return explicit

    supply_source = _normalize_organise_other_escort(booking_details.get("escort_supply_source"))
    if supply_source:
        return supply_source

    booking_status = str(booking_details.get("booking_status") or "").strip().lower()
    if booking_status == "doubles_supply_escort":
        return "yes"
    if booking_status == "doubles_supply_confirmed":
        return "no"

    notes = str(booking_details.get("special_requests") or "").strip().lower()
    if "asked provider to arrange" in notes:
        return "yes"

    return None


def _normalize_safety_screening_status(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "flagged watchlist match" if value else None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in {"flagged", "watchlist", "watchlist_match", "flagged watchlist match", "yes", "true", "1"}:
        return "flagged watchlist match"
    return None


def _resolve_safety_screening_status(phone_number: str, booking_details) -> str | None:
    explicit = _normalize_safety_screening_status(booking_details.get("safety_screening_status"))
    if explicit:
        return explicit
    explicit_flag = _normalize_safety_screening_status(booking_details.get("safety_screening_flagged"))
    if explicit_flag:
        return explicit_flag
    try:
        from services.safety_screening_service import lookup_flagged_number

        lookup = lookup_flagged_number(phone_number)
        if lookup.get("matched"):
            return "flagged watchlist match"
    except Exception as e:
        logger.warning("Safety screening lookup skipped for booking notes (%s): %s", phone_number, e)
    return None


def _get_db():
    return get_shared_db(config.DATABASE_URL)


def _send_new_booking_push_if_possible(event_id: str | None) -> None:
    primary_booking_id = ((event_id or "").split("|")[0]).strip()
    if not primary_booking_id:
        return
    try:
        db = _get_db()
        if not db:
            return
        sent = send_new_booking_push_for_booking_id(db, primary_booking_id)
        if sent > 0:
            logger.info("Sent %s push notification(s) for booking %s", sent, primary_booking_id)
    except Exception as push_err:
        logger.warning("New-booking push notification failed for %s: %s", primary_booking_id, push_err)


def _row_get(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, key):
        return getattr(row, key)
    try:
        return row[key]
    except Exception:
        return default


def _extract_returning_id(result) -> str | None:
    if not result:
        return None
    value = _row_get(result[0], "id")
    return str(value) if value is not None else None


def _coerce_int(value, default=0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


def _coerce_amount(value):
    if value in (None, ""):
        return None
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _split_event_ids(event_id) -> list[str]:
    if not event_id:
        return []
    return [part.strip() for part in str(event_id).split("|") if part and str(part).strip()]


def _is_dinner_booking(booking_details) -> bool:
    booking_type = str(booking_details.get("booking_type") or "").strip().lower()
    experience = str(booking_details.get("experience_type") or "").strip().lower()
    return booking_type == "dinner_date" or "dinner date" in experience or experience == "dinner_date"


def _is_outcall_booking(booking_details, is_outcall=None) -> bool:
    if is_outcall is not None:
        return bool(is_outcall)
    location_type = str(booking_details.get("incall_outcall") or booking_details.get("type") or "incall").strip().lower()
    return "outcall" in location_type or _is_dinner_booking(booking_details)


def _normalize_preferences(value) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, tuple):
        return [str(v).strip() for v in value if str(v).strip()]
    if value:
        return [str(value).strip()]
    return []


def _preferences_from_booking_details(booking_details) -> list[str]:
    pref_list = _normalize_preferences(booking_details.get("preferences"))
    mmf_tags = decode_mmf_exploration_tags(booking_details.get("mmf_exploration_tags"))
    if should_append_mmf_exploration_to_calendar(booking_details) and mmf_tags:
        human = humanize_mmf_exploration_tags(mmf_tags)
        if human:
            pref_list = [p.strip() for p in human.split(",") if p.strip()]
    seen = []
    for item in pref_list:
        if item not in seen:
            seen.append(item)
    return seen


def _build_notes(
    booking_details,
    phone_number,
    name,
    *,
    is_outcall_flag: bool,
    deposit_status: str,
    deposit_amount_value,
    payment_reference,
    total_booking_cost,
    remaining_amount,
    part_header: str | None = None,
    play_location: str | None = None,
) -> str:
    experience = booking_details.get("experience_type") or booking_details.get("experience") or "N/A"
    type_value = booking_details.get("incall_outcall") or ("outcall" if is_outcall_flag else "incall")
    description_lines = [
        f"Name: {name}",
        f"Phone: {phone_number}",
        f"Duration: {_format_duration_label(booking_details.get('duration', 'N/A'))}",
        f"Experience: {experience}",
        f"Type: {type_value}",
    ]

    safety_status = _resolve_safety_screening_status(phone_number, booking_details)
    if safety_status:
        description_lines.append(f"Safety screening: {safety_status}")

    if _is_doubles_booking(booking_details):
        organise_other_escort = _resolve_organise_other_escort(booking_details)
        description_lines.append(f"Organise other escort: {organise_other_escort or 'not specified'}")

    mmf_line = ""
    if should_append_mmf_exploration_to_calendar(booking_details):
        mmf_line = format_mmf_exploration_calendar_line(booking_details.get("mmf_exploration_tags"))
        if mmf_line:
            description_lines.append(mmf_line)

    pref_list = _preferences_from_booking_details(booking_details)
    if pref_list and not mmf_line:
        description_lines.append(f"Preferences: {', '.join(pref_list)}")

    dep_int = _coerce_int(deposit_amount_value, 0)
    if deposit_status == "paid" and dep_int:
        description_lines.append(f"Deposit Paid: ${dep_int}")
    elif deposit_status == "pending" and dep_int:
        description_lines.append(f"Deposit Due: ${dep_int}")

    ref = (payment_reference or booking_details.get("deposit_payment_reference") or booking_details.get("payment_reference") or "").strip()
    if ref:
        description_lines.append(f"Payment reference: {ref}")

    if total_booking_cost is not None:
        try:
            description_lines.append(f"Total booking cost: ${int(float(total_booking_cost))}")
        except (TypeError, ValueError):
            pass
    if remaining_amount is not None:
        try:
            description_lines.append(f"Remaining Balance: ${int(float(remaining_amount))}")
        except (TypeError, ValueError):
            pass

    if booking_details.get("outcall_address"):
        description_lines.append(f"Address: {booking_details['outcall_address']}")
    if play_location:
        description_lines.append(f"Play location: {play_location}")
    if booking_details.get("special_requests"):
        description_lines.append(f"Special requests: {booking_details['special_requests']}")

    description = "\n".join(description_lines)
    if deposit_status == "pending":
        description = description + "\n\n" + ADELLA_CALENDAR_SOFT_HOLD_MARKER
    if part_header:
        description = part_header + "\n\n" + description
    return description.strip()


def _booking_financials(is_confirmed, awaiting_deposit, deposit_amount, payment_reference, total_booking_cost, booking_details):
    if is_confirmed:
        status = "confirmed"
        deposit_status = "paid"
    elif awaiting_deposit:
        status = "pending-deposit"
        deposit_status = "pending"
    else:
        status = "reserved"
        deposit_status = "not_required"

    dep_amount = _coerce_amount(deposit_amount)
    if dep_amount is None and deposit_status in {"pending", "paid"}:
        dep_amount = _coerce_amount(booking_details.get("deposit_amount"))
    if dep_amount is None:
        dep_amount = 0

    total_amount = _coerce_amount(total_booking_cost if total_booking_cost is not None else booking_details.get("total_booking_cost"))
    remaining_amount = None
    if total_amount is not None:
        if is_confirmed:
            remaining_amount = max(0, round(total_amount - float(dep_amount or 0), 2))
        elif awaiting_deposit and dep_amount:
            remaining_amount = max(0, round(total_amount - float(dep_amount), 2))
        else:
            remaining_amount = total_amount

    deposit_reference = (payment_reference or booking_details.get("deposit_payment_reference") or booking_details.get("payment_reference") or "").strip()
    if deposit_status == "not_required":
        dep_amount = 0
        deposit_reference = ""

    return status, deposit_status, dep_amount, deposit_reference, total_amount, remaining_amount


def _insert_booking_row(
    start_dt,
    end_dt,
    *,
    name,
    phone_number,
    duration_label,
    booking_type,
    experience,
    preferences,
    deposit_status,
    deposit_amount,
    deposit_reference,
    status,
    special_requests,
    organise_other_escort,
    notes,
    price_total,
    remaining_amount,
    outcall_address,
) -> str | None:
    db = _get_db()
    if not db:
        logger.error("create_calendar_event: database unavailable")
        return None
    result = db.execute_query(
        """INSERT INTO bookings (
               start_time, end_time, client_name, phone, phone_number, duration, type, experience,
               preferences, deposit_status, deposit_amount, deposit_reference,
               status, special_requests, organise_other_escort, notes,
               price_total, remaining_amount, outcall_address, updated_at
           ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
           RETURNING id""",
        (
            start_dt,
            end_dt,
            name or "",
            phone_number or "",
            phone_number or "",
            duration_label,
            booking_type,
            experience,
            preferences,
            deposit_status,
            deposit_amount,
            deposit_reference,
            status,
            special_requests,
            organise_other_escort,
            notes,
            price_total,
            remaining_amount,
            outcall_address,
        ),
        fetch=True,
    )
    return _extract_returning_id(result)


@circuit_breaker(
    name="calendar_api",
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=(HttpError, ConnectionError, TimeoutError),
)
@retry_with_backoff(max_retries=2, initial_delay=1.0, exceptions=(HttpError, ConnectionError))
def create_calendar_event(
    booking_details,
    phone_number,
    is_confirmed=False,
    client_name=None,
    awaiting_deposit=False,
    return_travel_ids=False,
    deposit_amount=None,
    is_outcall=None,
    payment_reference=None,
    total_booking_cost=None,
):
    """Create one or more booking rows in the bookings table."""
    start_dt, end_dt = _parse_booking_window(booking_details)
    if not start_dt or not end_dt:
        logger.error(
            "Failed to parse booking window - date=%s time=%s",
            booking_details.get("date"),
            booking_details.get("time"),
        )
        return None

    name = client_name or booking_details.get("client_name") or "Client"
    is_outcall_flag = _is_outcall_booking(booking_details, is_outcall=is_outcall)
    is_dinner = _is_dinner_booking(booking_details)
    booking_type = "dinner_date" if is_dinner else ("outcall" if is_outcall_flag else "incall")
    experience = booking_details.get("experience_type") or booking_details.get("experience") or ""
    preferences = _preferences_from_booking_details(booking_details)
    organise_raw = _resolve_organise_other_escort(booking_details)
    organise_other_escort = True if organise_raw == "yes" else (False if organise_raw == "no" else None)
    status, deposit_status, deposit_amount_value, deposit_reference, price_total, remaining_amount = _booking_financials(
        is_confirmed,
        awaiting_deposit,
        deposit_amount,
        payment_reference,
        total_booking_cost,
        booking_details,
    )
    duration_label = _format_duration_label(booking_details.get("duration", "N/A"))
    special_requests = booking_details.get("special_requests") or None
    outcall_address = booking_details.get("outcall_address") or None

    try:
        from utils.dinner_date import compute_dinner_play_timeline

        dinner_timeline = compute_dinner_play_timeline(booking_details, start_dt) if is_dinner else None
        restaurant_to_play_minutes = None
        event_id = None

        if is_dinner and dinner_timeline:
            dinner_end, play_start, play_end, play_loc = dinner_timeline
            restaurant_to_play_minutes = max(1, int((play_start - dinner_end).total_seconds() // 60))
            dinner_notes = _build_notes(
                booking_details,
                phone_number,
                name,
                is_outcall_flag=True,
                deposit_status=deposit_status,
                deposit_amount_value=deposit_amount_value,
                payment_reference=deposit_reference,
                total_booking_cost=price_total,
                remaining_amount=remaining_amount,
                part_header="Part 1 of 2: Dinner (1 hour) at restaurant.",
            )
            play_notes = _build_notes(
                booking_details,
                phone_number,
                name,
                is_outcall_flag=True,
                deposit_status=deposit_status,
                deposit_amount_value=deposit_amount_value,
                payment_reference=deposit_reference,
                total_booking_cost=price_total,
                remaining_amount=remaining_amount,
                part_header="Part 2 of 2: Private time (1 hour).",
                play_location=play_loc,
            )
            # C4: wrap both inserts in a single DB transaction so a failure after
            # the first insert does not leave an orphaned dinner part in the DB.
            db = get_shared_db()
            with db.transaction():
                id1 = _insert_booking_row(
                    start_dt,
                    dinner_end,
                    name=name,
                    phone_number=phone_number,
                    duration_label=duration_label,
                    booking_type="dinner_date",
                    experience=experience,
                    preferences=preferences,
                    deposit_status=deposit_status,
                    deposit_amount=deposit_amount_value,
                    deposit_reference=deposit_reference,
                    status=status,
                    special_requests=special_requests,
                    organise_other_escort=organise_other_escort,
                    notes=dinner_notes,
                    price_total=price_total,
                    remaining_amount=remaining_amount,
                    outcall_address=outcall_address,
                )
                id2 = _insert_booking_row(
                    play_start,
                    play_end,
                    name=name,
                    phone_number=phone_number,
                    duration_label=duration_label,
                    booking_type="dinner_date",
                    experience=experience,
                    preferences=preferences,
                    deposit_status=deposit_status,
                    deposit_amount=deposit_amount_value,
                    deposit_reference=deposit_reference,
                    status=status,
                    special_requests=special_requests,
                    organise_other_escort=organise_other_escort,
                    notes=play_notes,
                    price_total=price_total,
                    remaining_amount=remaining_amount,
                    outcall_address=outcall_address,
                )
            event_id = "|".join(part for part in (id1, id2) if part)
            travel_block_end = play_end
        else:
            notes = _build_notes(
                booking_details,
                phone_number,
                name,
                is_outcall_flag=is_outcall_flag,
                deposit_status=deposit_status,
                deposit_amount_value=deposit_amount_value,
                payment_reference=deposit_reference,
                total_booking_cost=price_total,
                remaining_amount=remaining_amount,
            )
            event_id = _insert_booking_row(
                start_dt,
                end_dt,
                name=name,
                phone_number=phone_number,
                duration_label=duration_label,
                booking_type=booking_type,
                experience=experience,
                preferences=preferences,
                deposit_status=deposit_status,
                deposit_amount=deposit_amount_value,
                deposit_reference=deposit_reference,
                status=status,
                special_requests=special_requests,
                organise_other_escort=organise_other_escort,
                notes=notes,
                price_total=price_total,
                remaining_amount=remaining_amount,
                outcall_address=outcall_address,
            )
            travel_block_end = end_dt

        if not event_id:
            return None

        _send_new_booking_push_if_possible(event_id)

        travel_outbound_id = None
        travel_return_id = None
        if is_outcall_flag and booking_details.get("outcall_address") and (is_confirmed or awaiting_deposit):
            travel_color = COLOR_LAVENDER if awaiting_deposit else COLOR_GRAPE
            if is_dinner and dinner_timeline:
                _after = (booking_details.get("dinner_after_preference") or "").strip().lower()
                _skip_ret = _after in ("hotel", "escort_hotel", "my_hotel", "your_hotel")
                if booking_details.get("dinner_client_outside_15km"):
                    _skip_ret = True
                _client_home_raw = (booking_details.get("dinner_client_address") or "").strip()
                try:
                    from utils.dinner_date import extract_client_address_from_message as _extract_addr

                    _client_home = _extract_addr(_client_home_raw) if _client_home_raw else ""
                except Exception as e:
                    logger.warning("Dinner client address extract failed, using raw: %s", e)
                    _client_home = _client_home_raw
                travel_result = create_travel_time_blocks(
                    start_dt,
                    travel_block_end,
                    booking_details["outcall_address"],
                    name,
                    color_id=travel_color,
                    dinner_date_mode=True,
                    skip_return_travel=_skip_ret,
                    return_destination_address=_client_home if (_after == "client_place" and _client_home) else None,
                    experience_type=booking_details.get("experience_type"),
                    dinner_restaurant_to_play_minutes=restaurant_to_play_minutes,
                )
            else:
                travel_result = create_travel_time_blocks(
                    start_dt,
                    end_dt,
                    booking_details["outcall_address"],
                    name,
                    color_id=travel_color,
                    experience_type=booking_details.get("experience_type"),
                )
            if travel_result and travel_result[0]:
                travel_outbound_id, travel_return_id = travel_result

        if return_travel_ids:
            return {
                "event_id": event_id,
                "travel_outbound_id": travel_outbound_id,
                "travel_return_id": travel_return_id,
            }
        return event_id
    except Exception as e:
        logger.error("Booking create error: %s", e, exc_info=True)
        return None


def delete_calendar_event(event_id):
    """Delete one or more booking rows. ``event_id`` may be pipe-separated."""
    ids = _split_event_ids(event_id)
    if not ids:
        return False
    db = _get_db()
    if not db:
        logger.error("delete_calendar_event: database unavailable")
        return False
    try:
        deleted = db.execute_query(
            "DELETE FROM bookings WHERE id::text = ANY(%s) RETURNING id",
            (ids,),
            fetch=True,
        ) or []
        return bool(deleted)
    except Exception as e:
        logger.error("Booking delete error for %s: %s", event_id, e)
        return False


def _confirm_one_calendar_event(
    event_id: str,
    deposit_amount=None,
    client_name=None,
    is_outcall=False,
    experience_type=None,
    payment_reference: str | None = None,
    total_booking_cost=None,
) -> bool:
    db = _get_db()
    if not db:
        logger.error("confirm_calendar_event: database unavailable")
        return False
    try:
        dep_int = _coerce_int(deposit_amount, 0)
        remaining = 0
        price_total_val = None
        if total_booking_cost is not None:
            try:
                price_total_val = round(float(total_booking_cost), 2)
                remaining = max(0, int(price_total_val) - dep_int)
            except (TypeError, ValueError):
                remaining = 0
        db.execute_query(
            """UPDATE bookings SET
                   status='confirmed', deposit_status='paid',
                   deposit_amount=%s, deposit_reference=%s,
                   price_total=COALESCE(%s, price_total),
                   remaining_amount=%s, updated_at=NOW()
                   WHERE id = %s::uuid""",
            (dep_int or 0, (payment_reference or "").strip(), price_total_val, remaining or 0, event_id),
            fetch=False,
        )

        return True
    except Exception as e:
        logger.error("Booking confirm error for %s: %s", event_id, e)
        return False


def confirm_calendar_event(
    event_id,
    deposit_amount=None,
    client_name=None,
    is_outcall=False,
    experience_type=None,
    payment_reference=None,
    total_booking_cost=None,
):
    """Confirm one or more booking rows by setting status to confirmed."""
    ids = _split_event_ids(event_id)
    if not ids:
        return False
    ok = True
    for eid in ids:
        if not _confirm_one_calendar_event(
            eid,
            deposit_amount=deposit_amount,
            client_name=client_name,
            is_outcall=is_outcall,
            experience_type=experience_type,
            payment_reference=payment_reference,
            total_booking_cost=total_booking_cost,
        ):
            ok = False
    return ok


def _normalize_pending_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _normalize_pending_hour(pending_details):
    hm = parse_booking_time_hour_minute(pending_details.get("time"))
    return hm[0] if hm else None


def find_and_confirm_pending_event(phone_number, pending_details):
    """Find pending-deposit booking rows by phone/date and confirm them."""
    if not phone_number:
        logger.error("find_and_confirm_pending_event: No phone number provided")
        return False
    db = _get_db()
    if not db:
        logger.error("find_and_confirm_pending_event: database unavailable")
        return False

    try:
        pending_date = _normalize_pending_date(pending_details.get("date"))
        sql = (
            "SELECT id, start_time FROM bookings "
            "WHERE status='pending-deposit' AND phone=%s"
        )
        params = [phone_number]
        if pending_date:
            sql += " AND start_time::date = %s"
            params.append(pending_date)
        sql += " ORDER BY start_time ASC"
        rows = db.execute_query(sql, tuple(params), fetch=True) or []
        if not rows:
            logger.warning("[BOOKINGS] No pending-deposit booking found for %s", phone_number)
            return False

        pending_hour = _normalize_pending_hour(pending_details)
        if pending_hour is not None and len(rows) > 1:
            narrowed = []
            for row in rows:
                start_time = _row_get(row, "start_time")
                try:
                    if start_time is not None and int(start_time.hour) == pending_hour:
                        narrowed.append(row)
                except Exception:
                    continue
            if narrowed:
                rows = narrowed

        ids = [str(_row_get(row, "id")) for row in rows if _row_get(row, "id")]
        if not ids:
            return False

        return confirm_calendar_event(
            "|".join(ids),
            deposit_amount=pending_details.get("deposit_amount", 100),
            client_name=pending_details.get("client_name"),
            is_outcall="outcall" in str(pending_details.get("incall_outcall", "")).lower(),
            experience_type=pending_details.get("experience_type"),
            payment_reference=pending_details.get("payment_reference") or pending_details.get("payment_ref"),
            total_booking_cost=pending_details.get("total_booking_cost"),
        )
    except Exception as e:
        logger.error("[BOOKINGS] Error finding/confirming pending booking: %s", e, exc_info=True)
        return False
