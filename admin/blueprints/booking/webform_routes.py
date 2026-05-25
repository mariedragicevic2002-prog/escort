"""Main booking webform GET/POST handlers."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import hashlib
from datetime import datetime

from flask import render_template, request

import config
from config import (
    get_account_name,
    get_available_hours,
    get_current_incall_location,
    get_effective_escort_timezone,
    get_escort_name,
    get_payid,
)
from core.webform_security import (
    get_phone_number_from_token,
    mark_token_as_used,
    validate_webform_token,
)
from services.database_service import get_shared_db

from .blueprint import booking_bp
from .helpers import (
    WEBFORM_GROUP_EXPERIENCES,
    _append_group_escort_notes,
    _append_mmf_exploration_special_requests_line,
    _duration_to_minutes,
    _format_booking_date,
    _format_booking_time,
    _get_google_maps_browser_key,
    _validate_group_escort_notice,
    adjust_webform_date_str_for_overnight_time,
    get_booking_place_autocomplete_center,
    webform_dinner_start_time_ok,
)
from .log import logger
from .tokens_and_bookings import (
    _get_active_booking_for_phone,
    _get_token_record,
    _render_already_booked_page,
)


@booking_bp.route("/booking", methods=["GET", "POST"])
def booking_form():
    """Secure web booking form with token validation."""
    token = request.args.get("token", "")
    location = get_current_incall_location()

    if request.method == "GET":
        return _handle_booking_get(token, location)
    else:
        return _handle_booking_post(token, location)


def _handle_booking_get(token, location):
    """Handle GET request for booking form."""
    if not token:
        return render_template(
            "booking_error.html",
            error_title="Invalid Booking Link",
            error_message="This booking link is missing required security token. Please request a new booking link via SMS."
        ), 400
    if not get_shared_db(config.DATABASE_URL):
        return render_template(
            "booking_error.html",
            error_title="Service Temporarily Unavailable",
            error_message="Booking links are temporarily unavailable right now. Please try again shortly.",
        ), 503

    # Check if token is a hash (64 chars) - from short code link
    is_token_hash = len(token) == 64 and all(c in '0123456789abcdef' for c in token.lower())

    # Resolve phone number from token record first (works even after token is marked used).
    token_record = _get_token_record(token=token)
    phone_number = token_record.get("phone_number") if isinstance(token_record, dict) else None
    if not phone_number:
        # Fallback to existing helper path.
        phone_number = get_phone_number_from_token(token)

    # Hard-stop duplicate booking attempts: once booked, all older links should show booking details.
    existing_booking = _get_active_booking_for_phone(phone_number)
    if existing_booking:
        return _render_already_booked_page(phone_number, existing_booking)

    if not phone_number:
        # Try to get more specific error message
        try:
            db = get_shared_db(config.DATABASE_URL)
            if is_token_hash:
                result = db.execute_query("""
                    SELECT phone_number, expires_at, used, COALESCE(use_count, 0) as use_count
                    FROM webform_tokens
                    WHERE token_hash = %s
                """, (token.lower(),), fetch=True)
            else:
                token_hash = hashlib.sha256(token.encode()).hexdigest()
                result = db.execute_query("""
                    SELECT phone_number, expires_at, used, COALESCE(use_count, 0) as use_count
                    FROM webform_tokens
                    WHERE token_hash = %s
                """, (token_hash,), fetch=True)
            
            if result:
                token_data = result[0]
                from datetime import datetime

                import pytz
                now = datetime.now(pytz.UTC)
                expires_at = token_data['expires_at']
                if expires_at.tzinfo is None:
                    expires_at = pytz.UTC.localize(expires_at)
                else:
                    expires_at = expires_at.astimezone(pytz.UTC)
                
                if now > expires_at:
                    hours_expired = (now - expires_at).total_seconds() / 3600
                    error_msg = f"This booking link expired {int(hours_expired)} hours ago. Please request a new link."
                elif token_data.get('used') or token_data.get('use_count', 0) >= 1:
                    error_msg = "This booking link has already been used. Please request a new link."
                else:
                    error_msg = "This booking link is invalid. Please request a new link."
            else:
                error_msg = "This booking link was not found. Please request a new link."
        except Exception as e:
            logger.error(f"Error checking token details: {e}")
            error_msg = f"This booking link has expired or is invalid. Please text {get_escort_name()} to request a new booking link."
        
        return render_template(
            "booking_error.html",
            error_title="Link Expired or Invalid",
            error_message=error_msg
        ), 400

    from core.rates_from_config import get_incall_pricing, get_outcall_pricing
    from core.rates_from_config import get_surcharge as _get_surcharge
    from core.rates_from_config import get_surcharge_doubles_escort_supplied_outcall as _get_surcharge_doubles_pair

    return render_template(
        "booking.html",
        location=location,
        phone=phone_number,
        token=token,
        phone_locked=True,
        escort_name=get_escort_name(),
        escort_timezone=get_effective_escort_timezone(),
        google_maps_api_key=_get_google_maps_browser_key(),
        rates=get_incall_pricing(),
        outcall_rates=get_outcall_pricing(),
        surcharge=_get_surcharge(),
        surcharge_doubles_pair=_get_surcharge_doubles_pair(),
        place_autocomplete_center=get_booking_place_autocomplete_center(location),
    )


def _handle_booking_post(token, location):
    """Handle POST request for booking form submission."""
    phone_number = request.form.get("phone")
    submitted_token = request.form.get("token", "")

    # Check if this is a token_hash (from short URL) or regular token
    is_token_hash = len(submitted_token) == 64

    # Validate token
    is_valid, error_msg = validate_webform_token(submitted_token, phone_number, is_token_hash=is_token_hash)

    if not is_valid:
        status_code = 503 if "temporarily unavailable" in (error_msg or "").lower() else 403
        return render_template(
            "booking_error.html",
            error_title="Security Validation Failed",
            error_message=error_msg
        ), status_code

    # Once a booking exists, previously sent webform links must not create another booking.
    existing_booking = _get_active_booking_for_phone(phone_number)
    if existing_booking:
        return _render_already_booked_page(phone_number, existing_booking)

    # Extract form data
    date_str = request.form.get("date")
    time_str = request.form.get("time")
    duration = request.form.get("duration")
    experience = request.form.get("experience")
    hours_str = get_available_hours()
    resolved_date_str = adjust_webform_date_str_for_overnight_time(
        date_str, time_str, hours_str, experience=(experience or "").strip()
    ) or date_str
    incall_outcall = (request.form.get("incall_outcall") or "").strip()
    incall_outcall_lower = incall_outcall.lower()
    is_outcall = incall_outcall_lower == "outcall"
    outcall_address = (request.form.get("outcall_address", "") or "").strip()
    total_price = request.form.get("total_price", "")
    client_name = request.form.get("client_name", "").strip()
    special_requests = request.form.get("special_requests", "").strip()
    # self_provider_female=1 means ESCORT arranges the other female (tick "please source for me").
    # needs_provider_female = True when checkbox IS ticked for Doubles MFF.
    needs_provider_female = request.form.get("self_provider_female") == "1" and (experience or "").strip() == "Doubles MFF"
    needs_provider_male = request.form.get("self_provider_male") == "1" and (experience or "").strip() == "Doubles MMF"
    organise_other_escort = None
    booking_status = None
    if (experience or "").strip() == "Doubles MFF":
        organise_other_escort = "yes" if needs_provider_female else "no"
    elif (experience or "").strip() == "Doubles MMF":
        organise_other_escort = "yes" if needs_provider_male else "no"
    if organise_other_escort == "yes":
        booking_status = "doubles_supply_escort"
    elif organise_other_escort == "no":
        booking_status = "doubles_supply_confirmed"

    mmf_exploration_tags_json = None
    mmf_tags_list: list[str] = []
    if needs_provider_male:
        from booking.mmf_exploration import MMF_EXPLORATION_SLUGS, encode_mmf_exploration_tags

        for slug in MMF_EXPLORATION_SLUGS:
            if request.form.get(f"mmf_exploration_{slug}") == "1":
                mmf_tags_list.append(slug)
        if not mmf_tags_list:
            return render_template(
                "booking_error.html",
                error_title="MMF Exploration required",
                error_message=(
                    "When you'd like me to arrange the other male for Doubles MMF, "
                    "please tick at least one exploration option (Humiliation, Voyeurism, Bisexual, Heterosexual) "
                    "so I can source the right provider."
                ),
            ), 400
        mmf_exploration_tags_json = encode_mmf_exploration_tags(mmf_tags_list)

    ok_notice, notice_err = _validate_group_escort_notice(
        resolved_date_str,
        time_str,
        (experience or "").strip(),
        needs_provider_female,
        needs_provider_male,
        (location.get("city") if isinstance(location, dict) else "") or "",
    )
    if not ok_notice:
        return render_template(
            "booking_error.html",
            error_title="Notice period required",
            error_message=notice_err or "Invalid booking time for this request.",
        ), 400

    _exp_strip = (experience or "").strip()
    if _exp_strip == "Dinner Date":
        if not webform_dinner_start_time_ok(time_str):
            return render_template(
                "booking_error.html",
                error_title="Invalid dinner start time",
                    error_message=(
                        "Dinner Date start times must be between 5:00 PM and 9:00 PM "
                        "(escort local time)."
                    ),
            ), 400
    else:
        try:
            from core.settings_manager import get_setting
            from handlers.booking_coll._shared import check_within_available_hours_and_days

            _booking_day = datetime.strptime(resolved_date_str, "%Y-%m-%d").date()
            _tparts = str(time_str or "").strip().split(":")
            if len(_tparts) < 2:
                return render_template(
                    "booking_error.html",
                    error_title="Invalid time",
                    error_message="Please select a valid start time.",
                ), 400
            _bh, _bm = int(_tparts[0]), int(_tparts[1])
            _ok_hours, _reason = check_within_available_hours_and_days(
                _booking_day,
                (_bh, _bm),
                hours_str,
                get_setting("available_days", "7 days a week") or "7 days a week",
            )
            if not _ok_hours:
                return render_template(
                    "booking_error.html",
                    error_title="Outside available hours",
                    error_message=(
                        "That start time is outside working hours "
                        f"(including the 30-minute buffer before the end of your shift). "
                        f"Configured hours: {hours_str}."
                    ),
                ), 400
        except ValueError:
            return render_template(
                "booking_error.html",
                error_title="Invalid date or time",
                error_message="Please check your booking date and time and try again.",
            ), 400

    special_requests = _append_group_escort_notes(
        special_requests,
        (experience or "").strip(),
        needs_provider_female,
        needs_provider_male,
    )
    if mmf_tags_list:
        special_requests = _append_mmf_exploration_special_requests_line(special_requests, mmf_tags_list)

    # Outcall minimum 1 hour; 15 mins is incall only
    if is_outcall and duration in ("15 minutes", "30 minutes"):
        return render_template(
            "booking_error.html",
            error_title="Minimum duration for outcall",
            error_message="Outcall bookings have a minimum duration of 1 hour. Please choose 1 hour or longer, or select Incall for shorter sessions."
        ), 400

    # Server-side address validation for outcall
    if is_outcall and not outcall_address:
        return render_template(
            "booking_error.html",
            error_title="Address required for outcall",
            error_message="Please provide your hotel or address for outcall bookings."
        ), 400

    _bt_web = None
    _es_w = (experience or "").strip()
    if _es_w == "Doubles MMF":
        _bt_web = "Doubles MMF"
    elif _es_w == "Doubles MFF":
        _bt_web = "doubles_mff"

    _escort_src_web = None
    if organise_other_escort == "yes":
        _escort_src_web = "escort"
    elif organise_other_escort == "no":
        _escort_src_web = "client"

    booking_details = {
        "date": resolved_date_str,
        "time": time_str,
        "duration": duration,
        "experience_type": experience,
        "incall_outcall": incall_outcall_lower or "incall",
        "outcall_address": outcall_address if is_outcall else None,
        "city": location.get("city", ""),
        "price": total_price,
        "client_name": client_name,
        "special_requests": special_requests if special_requests else None,
        "organise_other_escort": organise_other_escort,
        "booking_status": booking_status,
        "booking_type": _bt_web,
        "escort_supply_source": _escort_src_web,
        "mmf_exploration_tags": mmf_exploration_tags_json,
    }

    db = get_shared_db(config.DATABASE_URL)
    if not db:
        return render_template(
            "booking_error.html",
            error_title="Database unavailable",
            error_message="The booking system cannot save your request right now. Please try again shortly.",
        ), 503

    try:
        event_id = None
        travel_outbound_id = None
        travel_return_id = None

        # Deposit requirement must be known before any calendar patch/create (GRAPHITE vs BASIL).
        duration_minutes = _duration_to_minutes(duration)
        awaiting_deposit = False
        deposit_amount = 0
        upload_url = None
        payment_reference = None
        deposit_reason = ""

        if is_outcall:
            try:
                from booking.deposit_handler import calculate_deposit_requirement
                deposit_required, deposit_amount, deposit_reason = calculate_deposit_requirement(
                    {
                        'incall_outcall': 'outcall',
                        'experience_type': (experience or '').lower(),
                        'duration': duration_minutes or 60,
                    },
                    phone_number
                )
                if deposit_required and deposit_amount > 0:
                    awaiting_deposit = True
                    deposit_reason = str(deposit_reason or "outcall")
                    # Generate upload token for deposit screenshot
                    try:
                        from core.deposit_upload_tokens import generate_deposit_upload_token
                        token_result = generate_deposit_upload_token(phone_number, deposit_amount)
                        if token_result:
                            upload_url = token_result.get('upload_url')
                            payment_reference = token_result.get('payment_reference')
                    except Exception as e:
                        logger.error(f"Failed to generate deposit upload token: {e}")
            except Exception as e:
                logger.error(f"Deposit check failed: {e}")
                # Default to requiring deposit for outcalls
                awaiting_deposit = True
                deposit_amount = 100
                deposit_reason = "outcall"

        # Group experiences (Doubles MFF/MMF, Couples MFF) always require mandatory deposit.
        if experience in WEBFORM_GROUP_EXPERIENCES:
            try:
                from core.rates_from_config import get_deposit_mff_pair
                deposit_amount = get_deposit_mff_pair()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                deposit_amount = 200
            awaiting_deposit = True
            _exp_l = (experience or "").strip().lower()
            if "mmf" in _exp_l:
                deposit_reason = "Doubles MMF"
            elif "couples" in _exp_l:
                deposit_reason = "couples_mff"
            else:
                deposit_reason = "doubles_mff"
            try:
                from core.deposit_upload_tokens import generate_deposit_upload_token
                token_result = generate_deposit_upload_token(phone_number, deposit_amount)
                if token_result:
                    upload_url = token_result.get('upload_url')
                    payment_reference = token_result.get('payment_reference')
            except Exception as e:
                logger.error(f"Failed to generate deposit upload token for group experience: {e}")

        # If client has a pending reschedule, apply the new time directly to the bookings row.
        from handlers.reschedule_response import get_pending_reschedule
        from services.calendar_service import _parse_booking_window

        pending = get_pending_reschedule(phone_number, db)
        pending_event_applied = False
        if pending:
            try:
                start_dt, end_dt = _parse_booking_window(booking_details)
                if start_dt and end_dt:
                    event_id = pending["event_id"]
                    new_status = "pending-deposit" if awaiting_deposit else "reschedule-confirmed"
                    db.execute_query(
                        "UPDATE bookings SET start_time = %s, end_time = %s, status = %s, updated_at = NOW() WHERE id = %s",
                        (start_dt.isoformat(), end_dt.isoformat(), new_status, event_id),
                        fetch=False,
                    )
                    db.execute_query(
                        "UPDATE pending_reschedules SET confirmed = TRUE, confirmed_at = NOW() WHERE id = %s",
                        (pending["id"],),
                        fetch=False,
                    )
                    pending_event_applied = bool(event_id)
                    logger.info(
                        "Reschedule via webform for %s booking %s (awaiting_deposit=%s, status=%s)",
                        phone_number,
                        event_id,
                        awaiting_deposit,
                        new_status,
                    )
            except Exception as e:
                logger.exception("Failed to update pending reschedule from webform: %s", e)

        if not pending_event_applied:
            from services.calendar_service import check_conflict, check_outcall_conflict_with_travel

            conflict_type = "none"
            try:
                if is_outcall:
                    conflict_type, _ = check_outcall_conflict_with_travel(booking_details)
                else:
                    conflict_type, _ = check_conflict(booking_details)
            except Exception as e:
                logger.warning(
                    "Calendar conflict check raised exception: %s — treating as conflict (fail-closed)",
                    e,
                )
                return _handle_booking_conflict(resolved_date_str, time_str, duration)

            if conflict_type != "none":
                return _handle_booking_conflict(resolved_date_str, time_str, duration)

            # Create calendar event with correct color (GRAPHITE if awaiting deposit, PEACOCK otherwise)
            try:
                from services.calendar_service import create_calendar_event
                event_result = create_calendar_event(
                    booking_details,
                    phone_number,
                    awaiting_deposit=awaiting_deposit,
                    client_name=client_name or None,
                    return_travel_ids=True,
                    deposit_amount=deposit_amount if awaiting_deposit else None,
                    is_outcall=is_outcall,
                )
                if isinstance(event_result, dict):
                    event_id = event_result.get('event_id')
                    travel_outbound_id = event_result.get('travel_outbound_id')
                    travel_return_id = event_result.get('travel_return_id')
                else:
                    event_id = event_result
                logger.info(
                    f"Calendar event created: {event_id} for {phone_number} "
                    f"(awaiting_deposit={awaiting_deposit}, outcall={is_outcall}, "
                    f"travel_outbound={travel_outbound_id}, travel_return={travel_return_id})"
                )
            except Exception as e:
                logger.error(f"Failed to create calendar event: {e}")
                event_id = None

            if not event_id:
                return render_template(
                    "booking_error.html",
                    error_title="Calendar sync failed",
                    error_message=(
                        "Your booking could not be added to the calendar right now. "
                        f"Please text {get_escort_name()} so we can confirm manually."
                    ),
                ), 502

        # Mark token + persist booking state in one DB transaction (SMS is sent after commit).
        incall_outcall_lower = incall_outcall_lower if incall_outcall_lower else "incall"
        if incall_outcall_lower not in ("incall", "outcall"):
            incall_outcall_lower = "incall" if (incall_outcall or "").strip().lower().startswith("in") else "outcall"
        state_name = "DEPOSIT_REQUIRED" if awaiting_deposit else "CONFIRMED"
        graphite_event_id = event_id if awaiting_deposit else None
        confirmed_event_id = None if awaiting_deposit else event_id
        _escort_supply_confirmed_web = organise_other_escort is not None
        _insert_params = (
            phone_number, state_name, resolved_date_str, time_str, duration_minutes or 60, experience,
            incall_outcall_lower, (outcall_address if is_outcall else None),
            int(total_price) if (total_price and str(total_price).strip().isdigit()) else 0,
            client_name, special_requests or "",
            booking_status,
            _bt_web,
            _escort_src_web,
            _escort_supply_confirmed_web,
            mmf_exploration_tags_json,
            bool(awaiting_deposit),
            int(deposit_amount) if awaiting_deposit else None,
            event_id, graphite_event_id, confirmed_event_id,
            travel_outbound_id, travel_return_id
        )
        _insert_sql = """
            INSERT INTO conversation_states
            (phone_number, current_state, date, time, duration, experience_type,
             incall_outcall, outcall_address, price, client_name, special_requests,
             booking_status,
             booking_type, escort_supply_source, escort_supply_confirmed, mmf_exploration_tags,
             deposit_required, deposit_amount, deposit_paid,
             peacock_event_id, graphite_event_id, confirmed_event_id,
             travel_outbound_event_id, travel_return_event_id, last_message_at)
            VALUES (%s, %s, %s::date, %s::time, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (phone_number)
            DO UPDATE SET
                current_state = EXCLUDED.current_state,
                date = EXCLUDED.date,
                time = EXCLUDED.time,
                duration = EXCLUDED.duration,
                experience_type = EXCLUDED.experience_type,
                incall_outcall = EXCLUDED.incall_outcall,
                outcall_address = EXCLUDED.outcall_address,
                price = EXCLUDED.price,
                client_name = EXCLUDED.client_name,
                special_requests = EXCLUDED.special_requests,
                booking_status = EXCLUDED.booking_status,
                booking_type = EXCLUDED.booking_type,
                escort_supply_source = EXCLUDED.escort_supply_source,
                escort_supply_confirmed = EXCLUDED.escort_supply_confirmed,
                mmf_exploration_tags = EXCLUDED.mmf_exploration_tags,
                deposit_required = EXCLUDED.deposit_required,
                deposit_amount = EXCLUDED.deposit_amount,
                deposit_paid = EXCLUDED.deposit_paid,
                peacock_event_id = EXCLUDED.peacock_event_id,
                graphite_event_id = EXCLUDED.graphite_event_id,
                confirmed_event_id = EXCLUDED.confirmed_event_id,
                travel_outbound_event_id = EXCLUDED.travel_outbound_event_id,
                travel_return_event_id = EXCLUDED.travel_return_event_id,
                last_message_at = NOW()
        """
        try:
            with db.transaction() as conn:
                if not mark_token_as_used(submitted_token, is_token_hash=is_token_hash, conn=conn):
                    raise RuntimeError("mark_token_as_used failed")
                db.execute_query(_insert_sql, _insert_params, fetch=False, conn=conn)
                if awaiting_deposit:
                    db.execute_query(
                        """
                        UPDATE conversation_states
                        SET deposit_payment_reference = %s
                        WHERE phone_number = %s
                        """,
                        (payment_reference, phone_number),
                        fetch=False,
                        conn=conn,
                    )
        except Exception as e:
            logger.exception("Webform booking DB transaction failed: %s", e)
            return render_template(
                "booking_error.html",
                error_title="Could not save booking",
                error_message=(
                    "Your booking could not be saved right now. "
                    f"Please text {get_escort_name()} so we can confirm manually."
                ),
            ), 503

        # Send confirmation/deposit SMS
        try:
            from services.sms_service import send_sms
            if awaiting_deposit:
                _dep_safe_name = (client_name or "").strip()
                try:
                    from templates.greetings import is_valid_client_name
                    if not is_valid_client_name(_dep_safe_name):
                        _dep_safe_name = ""
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e)
                from templates.confirmations import get_deposit_request_message

                reason_for_deposit = (deposit_reason or ("outcall" if is_outcall else "booking")).strip()
                booking_fields_for_deposit = {
                    "date": resolved_date_str,
                    "time": time_str,
                    "duration": duration_minutes or 60,
                    "experience_type": experience or "GFE",
                    "incall_outcall": incall_outcall_lower or ("outcall" if is_outcall else "incall"),
                    "outcall_address": (outcall_address if is_outcall else None),
                    "client_name": _dep_safe_name,
                    "phone_number": phone_number,
                    "booking_type": booking_details.get("booking_type"),
                    "escort_supply_source": booking_details.get("escort_supply_source"),
                    "booking_status": booking_details.get("booking_status"),
                }
                deposit_msg = get_deposit_request_message(
                    amount=deposit_amount,
                    reason=reason_for_deposit,
                    phone_number=phone_number,
                    upload_url=upload_url,
                    mandatory=True,
                    client_name=_dep_safe_name or None,
                    outcall_address=(outcall_address if is_outcall else None),
                    booking_fields=booking_fields_for_deposit,
                    payment_reference=payment_reference,
                )
                send_sms(phone_number, deposit_msg)
            else:
                # Incall webform confirmation template (client requested format)
                if (incall_outcall_lower or "incall") == "incall":
                    safe_name = (client_name or "").strip()
                    try:
                        from templates.greetings import is_valid_client_name
                        if not is_valid_client_name(safe_name):
                            safe_name = ""
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)
                    name_part = safe_name if safe_name else ""

                    # Format date line: "Tuesday, 31 March 2026"
                    date_line = resolved_date_str
                    try:
                        _dt = datetime.strptime(str(resolved_date_str), "%Y-%m-%d")
                        try:
                            date_line = _dt.strftime("%A, %-d %B %Y")
                        except Exception as e:
                            logger.warning(LOG_SUPPRESSED_FMT, e)
                            date_line = f"{_dt.strftime('%A')}, {_dt.day} {_dt.strftime('%B %Y')}"
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)

                    # Format time line: "12pm" / "12:30pm"
                    time_line = time_str
                    try:
                        _parts = str(time_str).split(":")
                        _h = int(_parts[0])
                        _m = int(_parts[1]) if len(_parts) > 1 else 0
                        _ampm = "pm" if _h >= 12 else "am"
                        _h12 = _h % 12 or 12
                        time_line = f"{_h12}{_ampm}" if _m == 0 else f"{_h12}:{_m:02d}{_ampm}"
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)

                    # Format duration line: "1h", "1h 30m", "30m"
                    duration_line = duration or "1h"
                    try:
                        _mins_total = int(duration_minutes or 60)
                        if _mins_total >= 60:
                            _hh, _mm = divmod(_mins_total, 60)
                            duration_line = f"{_hh}h" if _mm == 0 else f"{_hh}h {_mm}m"
                        else:
                            duration_line = f"{_mins_total}m"
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)

                    # Location line: prefer full address, then hotel+city (no duplicate city/suburb).
                    city = (location.get("city") or "").strip()
                    addr = (location.get("address") or "").strip()
                    hotel = (location.get("hotel_name") or "").strip()
                    try:
                        from templates.booking_reconfirmation import (
                            _compose_incall_location,
                            dedupe_incall_address_line,
                        )
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)
                        _compose_incall_location = None
                        dedupe_incall_address_line = None
                    if addr:
                        location_line = (
                            dedupe_incall_address_line(addr, city)
                            if dedupe_incall_address_line
                            else addr
                        )
                    elif hotel and city and _compose_incall_location:
                        location_line = _compose_incall_location(city, hotel)
                    elif hotel and city:
                        location_line = f"{hotel}, {city}"
                    else:
                        location_line = city or hotel or "Incall location"

                    total_line = f"${total_price}" if total_price and str(total_price).strip() else "As quoted"

                    optional_deposit_block = ""
                    try:
                        from templates.deposit_templates import get_non_mandatory_deposit_template
                        optional_deposit_block = get_non_mandatory_deposit_template(phone_number=phone_number)
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e)

                    confirmation_msg = (
                        f"\u2705 Thanks {name_part} your booking has been reserved for:\n\n"
                        f"\U0001F4C5 Date: {date_line}\n"
                        f"\u23F0 Time: {time_line}\n"
                        f"\u23F1\uFE0F Duration: {duration_line}\n"
                        f"\U0001F3AD Experience: {experience or 'GFE'}\n"
                        f"\U0001F4CD Incall @ Location: {location_line}\n"
                        f"\U0001F4B0 Total: {total_line}\n\n"
                        f"{optional_deposit_block}".strip()
                    )
                    send_sms(phone_number, confirmation_msg)
                else:
                    confirmation_msg = f"\u2705 Booking confirmed!\n\n\U0001F4C5 {resolved_date_str} at {time_str}\n\u23F1\uFE0F {duration}\n\U0001F4AB {experience}\n\nLooking forward to seeing you! - {get_escort_name()}"
                    send_sms(phone_number, confirmation_msg)
        except Exception as e:
            logger.error(f"Failed to send confirmation SMS: {e}")

        logger.info(f"Booking created for {phone_number}: {resolved_date_str} {time_str} (deposit={'yes' if awaiting_deposit else 'no'})")

        # Format display values for success/deposit page
        formatted_date = _format_booking_date(resolved_date_str)
        formatted_time = _format_booking_time(time_str)
        price_display = f"${total_price}" if total_price and str(total_price).strip() else "As quoted"

        if awaiting_deposit:
            # Outcall: render dedicated deposit required page
            try:
                total_int = int(total_price) if total_price and str(total_price).strip().isdigit() else 0
                remaining = total_int - deposit_amount if total_int > deposit_amount else 0
                remaining_display = f"${remaining}" if remaining > 0 else price_display
            except (ValueError, TypeError):
                remaining_display = price_display

            return render_template(
                "deposit_required.html",
                booking_date=formatted_date,
                booking_time=formatted_time,
                booking_duration=duration or "As requested",
                booking_experience=experience or "GFE",
                booking_type=incall_outcall or "Outcall",
                total_price=price_display,
                deposit_amount=deposit_amount,
                remaining_balance=remaining_display,
                upload_url=upload_url,
                payid=get_payid() or "[PayID not configured]",
                account_name=get_account_name() or "",
                payment_reference=payment_reference,
                client_name=client_name,
                escort_name=get_escort_name(),
            )
        else:
            # Incall: render booking confirmed page
            try:
                from core.rates_from_config import get_deposit_incall, get_deposit_outcall
                deposit_incall = get_deposit_incall()
                deposit_outcall = get_deposit_outcall()
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
                from core.rates_from_config import get_default_pricing
                _defaults = get_default_pricing()
                deposit_incall = int(_defaults.get("deposit_incall", 50))
                deposit_outcall = int(_defaults.get("deposit_outcall", 100))

            upload_url = None
            optional_payment_reference = None
            try:
                from core.deposit_upload_tokens import generate_deposit_upload_token
                token_data = generate_deposit_upload_token(phone_number, deposit_incall)
                if token_data:
                    upload_url = token_data.get('upload_url')
                    optional_payment_reference = token_data.get('payment_reference')
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)

            return render_template(
                "booking_success.html",
                booking_date=formatted_date,
                booking_time=formatted_time,
                booking_duration=duration or "As requested",
                booking_experience=experience,
                booking_type=incall_outcall or "Incall",
                booking_price=price_display,
                # Raw values for calendar ICS
                booking_date_raw=formatted_date,
                booking_time_raw=formatted_time,
                booking_duration_minutes=str(duration_minutes or 60),
                # Client and location
                client_name=client_name,
                city=location.get("city", ""),
                hotel=location.get("hotel_name") or location.get("address", "my location"),
                escort_name=get_escort_name(),
                payid=get_payid() or "[PayID not configured]",
                account_name=get_account_name() or "",
                location=location,
                deposit_incall=deposit_incall,
                deposit_outcall=deposit_outcall,
                upload_url=upload_url,
                payment_reference=optional_payment_reference,
            )

    except Exception as e:
        logger.error(f"Booking submission error: {e}")
        return render_template(
            "booking_error.html",
            error_title="Booking Error",
            error_message=f"An error occurred while processing your booking. Please contact {get_escort_name()} via SMS."
        ), 500


def _handle_booking_conflict(date_str, time_str, duration):
    """Handle booking time conflict."""
    return render_template(
        "booking_error.html",
        error_title="Time Slot Unavailable",
        error_message=f"Unfortunately, {date_str} at {time_str} ({duration}) is no longer available. Please text {get_escort_name()} to find an alternative time."
    ), 409
