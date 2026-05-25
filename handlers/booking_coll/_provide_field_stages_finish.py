"""Pipeline stage helpers for provide_field (extract / slot / validate / finish)."""
from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re
from typing import Any

from config import get_base_url, get_current_incall_location, get_escort_name, get_profile_url
from templates import field_prompts
from templates.booking_collection_messages import (
    ASK_TIME_AND_DURATION_NUDGE,
    append_outcall_duration_minimum_if_needed,
    build_requested_time_followup_prompt,
    build_time_available_prompt,
    experience_already_set_for_gfe_prompt,
    message_looks_like_duration_attempt,
    pick_outcall_venue_display_name,
)
from utils.confirmation_tokens import NAME_SCAN_SKIP_WORDS, is_confirmation_token
from utils.dinner_date import is_dinner_date_booking

from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._shared import (
    _build_outcall_address_confirmed_msg,
    _build_three_slot_available_now_response,
    _get_outcall_policy_amounts,
    _incall_duration_prompt_with_calendar_probe,
    _webform_url_for_phone,
    check_and_format_outside_hours,
)

logger = logging.getLogger("adella_chatbot.handlers.collecting")


def _build_outcall_verified_address_message(
    *,
    updated_fields: dict[str, Any],
    state: dict[str, Any],
    message: str | None,
    verified_info: dict[str, Any],
) -> str:
    client_name = (updated_fields.get("client_name") or state.get("client_name") or "").strip()
    outcall_address = updated_fields.get("outcall_address", "")
    distance_km = verified_info.get("distance_km") or updated_fields.get("_verified_distance_km", 0)
    verified_address = verified_info.get("verified_address") or updated_fields.get(
        "_verified_address", outcall_address
    )
    escort_location = get_current_incall_location() or {}
    escort_address = (
        escort_location.get("address")
        or escort_location.get("hotel_name")
        or escort_location.get("display_name")
        or "my current location"
    )
    _city_ack = (verified_info.get("city") or escort_location.get("city") or "").strip()
    _venue_ack = pick_outcall_venue_display_name(
        verified_info, outcall_address, booking_city=_city_ack
    )
    _merged_prompt = {**(state or {}), **updated_fields}
    _gfe_complete = experience_already_set_for_gfe_prompt(_merged_prompt)
    _ack_dur = message_looks_like_duration_attempt(message or "") and not updated_fields.get("duration")
    return _build_outcall_address_confirmed_msg(
        client_name,
        verified_address,
        distance_km,
        escort_address,
        ask_experience=not _gfe_complete,
        acknowledge_unparsed_duration=_ack_dur,
        city=_city_ack,
        venue_name=_venue_ack,
    )


def _try_mmf_escort_sourced_exploration_gate(
    *,
    phone_number: str,
    message: str | None,
    state: dict[str, Any],
    updated_fields: dict[str, Any],
    state_manager: Any,
) -> dict[str, Any] | None:
    """Before reconfirmation / YES: MMF + escort sources male requires exploration tags.

    Golden rule (SMS wording): ``utils.golden_booking_rules.GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT``.
    Client cannot confirm until ``mmf_exploration_tags`` is populated (parsed SMS or webform).
    """
    from booking.mmf_exploration import (
        decode_mmf_exploration_tags,
        encode_mmf_exploration_tags,
        escort_organises_male_for_mmf,
        mmf_exploration_followup_prompt,
        mmf_exploration_sms_prompt,
        parse_mmf_exploration_reply,
    )

    merged = {**(state or {}), **(updated_fields or {})}
    if not escort_organises_male_for_mmf(merged):
        return None

    existing = decode_mmf_exploration_tags(merged.get("mmf_exploration_tags"))
    if existing:
        return None

    parsed = parse_mmf_exploration_reply(message or "")
    if parsed:
        state_manager.update_fields(
            phone_number,
            {
                "mmf_exploration_tags": encode_mmf_exploration_tags(parsed),
                "mmf_exploration_prompt_sent": True,
            },
        )
        return None

    # Mandatory full checklist must go out before we accept bare YES / confirm paths.
    if not bool(merged.get("mmf_exploration_prompt_sent")):
        state_manager.update_fields(phone_number, {"mmf_exploration_prompt_sent": True})
        return {"messages": [mmf_exploration_sms_prompt(merged)], "new_state": None, "actions": []}

    if is_confirmation_token(message):
        return {"messages": [mmf_exploration_followup_prompt()], "new_state": None, "actions": []}

    return {"messages": [mmf_exploration_followup_prompt()], "new_state": None, "actions": []}


# ---------------------------------------------------------------------------
# Stage 17 — apply extracted updates + name
# ---------------------------------------------------------------------------

def _stage_apply_extracted_updates_and_name(ctx: CollectingCtx) -> None:
    """Stage 17: merge extracted fields + name heuristics; reload booking snapshot into ctx."""
    from templates import greetings

    extracted = ctx.extracted
    phone_number = ctx.phone_number
    message = ctx.message
    state_manager = ctx.state_manager
    field_collector = ctx.field_collector
    field_validator = ctx.field_validator

    updates: dict[str, Any] = {}
    for field, value in extracted.items():
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        if field == "client_name":
            candidate = str(value).strip()
            if not greetings.is_valid_client_name(candidate):
                continue
            updates[field] = candidate
            continue
        updates[field] = value

    # Never overwrite an already-set client_name. Day-of-week tokens etc. would otherwise
    # clobber a real saved name (e.g. "Wed 1am" → bot greeting "Hi Wed").
    _existing_name = (
        (ctx.state or {}).get("client_name")
        or ctx.current_fields.get("client_name")
        or ""
    ).strip()

    if _existing_name:
        name_from_message = ""
    else:
        name_from_message = greetings.extract_client_name(message)

    if name_from_message and greetings.is_valid_client_name(name_from_message):
        updates["client_name"] = name_from_message

    if updates:
        state_manager.update_fields(phone_number, updates)

    verified_info = getattr(field_validator, "_last_verified_hotel_info", None)
    if verified_info:
        verified_updates = {}
        if verified_info.get("distance_km") is not None:
            verified_updates["_verified_distance_km"] = verified_info["distance_km"]
        if verified_info.get("verified_address"):
            verified_updates["_verified_address"] = verified_info["verified_address"]
        if verified_updates:
            state_manager.update_fields(phone_number, verified_updates)

    ctx.verified_info = getattr(field_validator, "_last_verified_hotel_info", None)
    ctx.updated_fields = state_manager.get_booking_fields(phone_number)
    ctx.missing = field_collector.get_missing_fields(ctx.updated_fields)


# ---------------------------------------------------------------------------
# Stage 18 — outcall policy after validate
# ---------------------------------------------------------------------------

def _stage_outcall_policy_after_validate(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Post-validation: outcall with no address → policy message (differs from stage 14 gating)."""
    updated_fields = ctx.updated_fields
    verified_info = ctx.verified_info
    phone_number = ctx.phone_number

    is_outcall_no_address = (
        (updated_fields.get("incall_outcall") or "").lower() == "outcall"
        and not updated_fields.get("outcall_address")
        and not verified_info
    )
    if not is_outcall_no_address:
        return None

    # Dinner dates go to a restaurant — never send the hotel/apartment policy.
    # Use the dinner-specific restaurant prompt instead.
    if is_dinner_date_booking(updated_fields) or is_dinner_date_booking(ctx.state):
        from templates.special_bookings import get_dinner_restaurant_prompt
        return {"messages": [get_dinner_restaurant_prompt()], "new_state": None, "actions": []}

    try:
        from config import get_current_incall_location
        from core.rates_from_config import get_deposit_outcall, get_outcall_travel_surcharge_for_booking
        from templates.greetings import build_outcall_policy_message

        location = get_current_incall_location() or {}
        city = location.get("city") or "the city"
        try:
            _pol_bf = {**updated_fields}
            _pol_bf.setdefault("incall_outcall", "outcall")
            surcharge = get_outcall_travel_surcharge_for_booking(_pol_bf)
            deposit_outcall = get_deposit_outcall()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            surcharge, deposit_outcall = _get_outcall_policy_amounts()

        _webform_url = _webform_url_for_phone(phone_number)
        has_duration = bool(updated_fields.get("duration"))
        policy_msg = build_outcall_policy_message(
            city=city,
            surcharge=surcharge,
            deposit_outcall=deposit_outcall,
            webform_url=_webform_url,
            has_duration=has_duration,
        )
        # If client just corrected their time (e.g. "actually make it 3pm"),
        # acknowledge the update before restating the outcall policy.
        _msg_lower = (ctx.message or "").lower()
        _time_correction = bool((ctx.extracted or {}).get("time")) and bool(
            re.search(
                r"\b(actually|change|reschedul(?:e|ed|ing)|move|instead|make it)\b",
                _msg_lower,
            )
        )
        if _time_correction:
            try:
                from templates.greetings import format_time_simple

                _tv = ctx.extracted.get("time")
                if isinstance(_tv, (tuple, list)) and len(_tv) >= 2:
                    _hh, _mm = int(_tv[0]), int(_tv[1])
                elif isinstance(_tv, int):
                    _hh, _mm = int(_tv), 0
                else:
                    _hh = _mm = None
                if _hh is not None:
                    _ack = format_time_simple(_hh, _mm)
                    policy_msg = f"No worries - I've updated your time to {_ack}.\n\n{policy_msg}"
            except Exception as _e:
                logger.warning("time-correction acknowledgment skipped: %s", _e)
        return {"messages": [policy_msg], "new_state": None, "actions": []}
    except Exception as e:
        logger.warning("Failed to build outcall policy message: %s", e)
    return None


# ---------------------------------------------------------------------------
# Stage 19 — outcall address confirmed after validate
# ---------------------------------------------------------------------------

def _stage_outcall_address_confirmed_after_validate(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Address verified this request and duration still missing → address-confirmed template."""
    updated_fields = ctx.updated_fields
    verified_info = ctx.verified_info
    state = ctx.state
    missing = ctx.missing

    is_outcall_with_address = (
        (updated_fields.get("incall_outcall") or "").lower() == "outcall"
        and updated_fields.get("outcall_address")
    )
    if not (verified_info and is_outcall_with_address and "duration" in missing):
        return None

    try:
        msg = _build_outcall_verified_address_message(
            updated_fields=updated_fields,
            state=state,
            message=ctx.message,
            verified_info=verified_info,
        )
        return {"messages": [msg], "new_state": None, "actions": []}
    except Exception as e:
        logger.warning("Failed to build outcall address confirmed message: %s", e)
    return None


# ---------------------------------------------------------------------------
# Stage 20 — time known, no duration
# ---------------------------------------------------------------------------

def _stage_time_known_no_duration(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Date+time known but no duration → duration prompts, outside-hours, available-now, incall calendar probe."""
    import datetime as _dt

    updated_fields = ctx.updated_fields
    phone_number = ctx.phone_number
    state = ctx.state
    state_manager = ctx.state_manager
    field_validator = ctx.field_validator
    is_available_now = ctx.is_available_now

    if not (updated_fields.get("date") and updated_fields.get("time") and not updated_fields.get("duration")):
        return None

    _booking_is_outcall = str(updated_fields.get("incall_outcall") or "").lower() == "outcall"

    is_outcall_with_address = (
        (updated_fields.get("incall_outcall") or "").lower() == "outcall"
        and updated_fields.get("outcall_address")
    )
    address_was_verified = (
        updated_fields.get("_verified_distance_km") is not None
        or getattr(field_validator, "_last_verified_hotel_info", None) is not None
    )

    if is_outcall_with_address and address_was_verified:
        try:
            verified_info = getattr(field_validator, "_last_verified_hotel_info", None) or {}
            msg = _build_outcall_verified_address_message(
                updated_fields=updated_fields,
                state=state,
                message=ctx.message,
                verified_info=verified_info,
            )
            return {"messages": [msg], "new_state": None, "actions": []}
        except Exception as e:
            logger.warning("Failed to build outcall verified address message: %s", e)

    within_hours, _outside_msg_early, avail_hours, avail_days = check_and_format_outside_hours(
        updated_fields,
        phone_number=phone_number,
        state_manager=state_manager,
        hours_setting_default="",
    )

    if not within_hours:
        state_manager.update_fields(phone_number, {"date": None, "time": None})
        return {"messages": [_outside_msg_early], "new_state": None, "actions": []}

    _merged_prompt = {**(state or {}), **updated_fields}
    _exp_already_set = experience_already_set_for_gfe_prompt(_merged_prompt)

    if is_available_now:
        time_val = updated_fields.get("time")
        if isinstance(time_val, _dt.time):
            hour, minute = time_val.hour, time_val.minute
        elif isinstance(time_val, (tuple, list)) and len(time_val) == 2:
            hour, minute = int(time_val[0]), int(time_val[1])
        else:
            hour, minute = None, None
        if hour is not None:
            period = "pm" if hour >= 12 else "am"
            disp = (hour if hour <= 12 else hour - 12) or 12
            time_str = f"{disp}:{minute:02d}{period}" if minute else f"{disp}{period}"
            client_name = (state.get("client_name") or "").strip() if state else ""
            ask_duration_msg = build_time_available_prompt(
                time_str=time_str,
                client_name=client_name,
                experience_already_set=_exp_already_set,
                is_outcall=_booking_is_outcall,
            )
        else:
            ask_duration_msg = field_prompts.get_duration_only_prompt(
                experience_already_set=_exp_already_set,
                is_outcall=_booking_is_outcall,
            )
    else:
        if (updated_fields.get("incall_outcall") or "incall").lower() == "incall":
            ask_duration_msg = _incall_duration_prompt_with_calendar_probe(updated_fields, state, _exp_already_set)
        else:
            ask_duration_msg = field_prompts.get_duration_only_prompt(
                experience_already_set=_exp_already_set,
                is_outcall=_booking_is_outcall,
            )

    return {"messages": [ask_duration_msg], "new_state": None, "actions": []}


# ---------------------------------------------------------------------------
# Stage 21 — available-now no datetime slots
# ---------------------------------------------------------------------------

def _stage_available_now_no_datetime_slots(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Available-now request but no concrete date/time yet → outside-hours or 3-slot template."""
    from utils.timezone import get_current_datetime

    updated_fields = ctx.updated_fields
    phone_number = ctx.phone_number
    state = ctx.state

    is_available_now = state.get("available_now_requested", False)
    has_date_and_time = updated_fields.get("date") and updated_fields.get("time")

    if not (is_available_now and not has_date_and_time):
        return None

    _now_dt = get_current_datetime()
    _bf_now = {
        **updated_fields,
        "date": _now_dt.date(),
        "time": (_now_dt.hour, _now_dt.minute),
    }
    _now_within, _outside_now_msg, _, _ = check_and_format_outside_hours(
        _bf_now,
        phone_number=phone_number,
        state_manager=ctx.state_manager,
        hours_setting_default="",
        suppress_time_specific_opener=True,
    )

    if not _now_within:
        return {"messages": [_outside_now_msg], "new_state": None, "actions": []}

    is_outcall = updated_fields.get("incall_outcall") == "outcall"
    return _build_three_slot_available_now_response(
        phone_number,
        state,
        updated_fields,
        is_outcall=is_outcall,
        state_manager=ctx.state_manager,
    )


# ---------------------------------------------------------------------------
# Stage 22 helpers — no-experience branch + YES delegation
# ---------------------------------------------------------------------------

def _no_experience_branch_response(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Handle the date+time+duration present but experience_type missing sub-case."""
    from services.calendar_service import check_conflict, check_outcall_conflict_with_travel
    from templates import greetings

    updated_fields = ctx.updated_fields
    phone_number = ctx.phone_number
    message = ctx.message
    state = ctx.state
    state_manager = ctx.state_manager

    if state.get("calendar_yes_degraded") and is_confirmation_token(message):
        try:
            from core.webform_security import get_webform_url

            wf = get_webform_url(phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            wf = f"{get_base_url()}/booking"
        state_manager.update_fields(phone_number, {"calendar_yes_degraded": False})
        return {
            "messages": [
                "I'm having trouble confirming with the calendar right now. "
                f"Please complete your booking here: {wf}"
            ],
            "new_state": "COLLECTING",
            "actions": [],
        }

    has_yes = is_confirmation_token(message)
    effective_name = (updated_fields.get("client_name") or state.get("client_name") or "").strip()
    if not greetings.is_valid_client_name(effective_name):
        fallback = greetings.extract_client_name(message or "")
        if greetings.is_valid_client_name(fallback):
            effective_name = fallback
    # Booking confirmation format: "James YES", "YES James", "James GFE YES".
    # extract_client_name doesn't recognise bare names next to YES, so when has_yes is True
    # and we still have no name, scan each word and take the first valid-looking name.
    if has_yes and not greetings.is_valid_client_name(effective_name):
        _skip = NAME_SCAN_SKIP_WORDS
        for _word in (message or "").split():
            _candidate = _word.strip(".,!?\"'").capitalize()
            if _candidate.lower() not in _skip and greetings.is_valid_client_name(_candidate):
                effective_name = _candidate
                break
    if has_yes:
        # GOLDEN RULE: plain YES must always confirm. Name is NOT a gate. Persist
        # deposit + awaiting flags, move to CHECKING_AVAILABILITY, and run the real
        # availability handler. Returning messages=[] + check_calendar is unsafe:
        # application.py only logs actions and then substitutes a generic "what
        # time?" prompt when messages are empty.
        from booking.deposit_handler import calculate_deposit_requirement
        from handlers import availability_check

        _bf = {**updated_fields, "phone_number": phone_number}
        deposit_required, deposit_amount, dep_reason = calculate_deposit_requirement(
            _bf, phone_number, state_manager
        )
        is_outcall = (_bf.get("incall_outcall") or "").lower() == "outcall"
        _reason = (dep_reason or "").strip() or ("outcall" if is_outcall else "incall")
        extra: dict = {"auto_confirm_without_experience": True}
        # If we managed to extract a sensible name from the YES message, persist
        # it. Otherwise leave client_name unset and let downstream templates use
        # a generic "there" / "Client" placeholder — the booking still confirms.
        if not updated_fields.get("client_name") and greetings.is_valid_client_name(effective_name):
            extra["client_name"] = effective_name
        state_manager.mark_awaiting_confirmation(
            phone_number,
            is_outcall=is_outcall,
            deposit_required=bool(deposit_required),
            deposit_amount=deposit_amount,
            deposit_reason=_reason,
            extra=extra,
        )
        # State transition to CHECKING_AVAILABILITY is handled by the router
        # via the new_state returned by handle_check_availability below.
        context = dict(ctx.raw_context)
        context["state"] = state_manager.get_state(phone_number)
        # Golden rule: plain YES must always confirm. Try once, retry once on
        # transient failure (calendar/AI blip), and on a second failure park the
        # client in CHECKING_AVAILABILITY with a recoverable message — never reply
        # "system error" to a YES. State is already marked awaiting_confirmation
        # above, so any next inbound message re-enters this same path.
        for _attempt in range(2):
            try:
                return availability_check.handle_check_availability(context)
            except Exception as e:
                logger.exception(
                    "handle_check_availability failed after name+YES "
                    "(no-experience branch, attempt=%d) for %s: %s",
                    _attempt + 1, phone_number, e,
                )
        try:
            state_manager.update_fields(phone_number, {"calendar_yes_degraded": True})
        except Exception as _cdf_err:
            logger.warning("calendar_yes_degraded flag update failed for %s: %s", phone_number, _cdf_err)
        return {
            "messages": [
                "Got your YES — just double-checking the calendar now and "
                "I'll lock it in shortly. If you don't hear back in a couple of "
                "minutes, please send YES again."
            ],
            "new_state": "CHECKING_AVAILABILITY",
            "actions": [],
        }

    _bd_date = updated_fields.get("date")
    _bd_time = updated_fields.get("time")
    _bd_dur = updated_fields.get("duration")
    if not (_bd_date and _bd_time and isinstance(_bd_dur, int) and _bd_dur > 0):
        logger.warning(
            "_no_experience_branch_response: incomplete date/time/duration for %s — falling through",
            phone_number,
        )
        return None

    booking_details = {
        "date": _bd_date,
        "time": _bd_time,
        "duration": _bd_dur,
        "incall_outcall": updated_fields.get("incall_outcall", "incall"),
        "outcall_address": updated_fields.get("outcall_address"),
    }
    is_outcall = booking_details["incall_outcall"] == "outcall"
    try:
        if is_outcall:
            conflict_type, _ = check_outcall_conflict_with_travel(booking_details)
        else:
            conflict_type, _ = check_conflict(booking_details)
    except Exception as e:
        logger.exception(
            "Calendar conflict check failed in _no_experience_branch_response for %s: %s",
            phone_number,
            e,
        )
        return None

    if conflict_type == "none":
        _exp_url = "https://www.adella-allure.com.au/experience"
        try:
            from templates.field_prompts import _get_experience_url
            _exp_url = _get_experience_url()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        client_name = (updated_fields.get("client_name") or state.get("client_name") or "").strip()
        name_part = f" {client_name}" if client_name else ""
        t = updated_fields.get("time")
        if isinstance(t, (tuple, list)) and len(t) >= 2:
            th, tm = int(t[0]), int(t[1])
        elif isinstance(t, int):
            th, tm = int(t), 0
        else:
            th, tm = None, None

        if th is not None:
            time_str = greetings.format_time_simple(th, tm)
            d = updated_fields.get("date")
            if d is not None and hasattr(d, "strftime"):
                avail_line = f"✅ Great{name_part}. Your requested time of {time_str} on {d.strftime('%A')} is available.\n\n"
            else:
                avail_line = f"✅ Great{name_part}. Your requested time of {time_str} is available.\n\n"
        else:
            avail_line = f"✅ Great{name_part}. Your requested time is available.\n\n"

        if updated_fields.get("duration"):
            wf_url = ""
            try:
                from core.webform_security import get_webform_url
                wf_url = get_webform_url(phone_number)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                wf_url = f"{get_base_url()}/booking"
            if is_outcall:
                from templates.booking_reconfirmation import build_available_now_outcall_reconfirmation
                summary = build_available_now_outcall_reconfirmation(updated_fields, webform_url=wf_url)
            else:
                from templates.booking_reconfirmation import build_incall_preconfirm_summary
                summary = build_incall_preconfirm_summary(updated_fields, webform_url=wf_url)
            # Staying in COLLECTING breaks the next "Name YES" / YES reply — the handler
            # returned empty messages with a check_calendar action that the app never runs.
            from booking.deposit_handler import calculate_deposit_requirement

            _bf_pre = {**updated_fields, "phone_number": phone_number}
            dep_req, dep_amt, dep_rsn = calculate_deposit_requirement(_bf_pre, phone_number, state_manager)
            _rsn = (dep_rsn or "").strip() or ("outcall" if is_outcall else "incall")
            state_manager.update_fields(
                phone_number,
                {
                    "deposit_required": bool(dep_req),
                    "deposit_amount": dep_amt,
                    "deposit_reason": _rsn,
                    "outcall_awaiting_yes": is_outcall,
                    "incall_awaiting_yes": not is_outcall,
                },
            )
            # State transition to CHECKING_AVAILABILITY is handled by the router
            # via new_state in the returned dict — no direct call needed.
            return {"messages": [avail_line + summary], "new_state": "CHECKING_AVAILABILITY", "actions": []}
        else:
            _merged_follow = {**(state or {}), **updated_fields}
            _exp_already = experience_already_set_for_gfe_prompt(_merged_follow)
            msg = build_requested_time_followup_prompt(
                available_line=avail_line,
                is_outcall=is_outcall,
                experience_already_set=_exp_already,
            )
            return {"messages": [msg], "new_state": None, "actions": []}

    if not (updated_fields.get("date") and updated_fields.get("time")):
        return _build_three_slot_available_now_response(
            phone_number,
            state,
            updated_fields,
            is_outcall=is_outcall,
            state_manager=state_manager,
        )

    req_time = updated_fields.get("time")
    _time_display = ""
    if isinstance(req_time, (tuple, list)) and len(req_time) >= 2:
        _th = int(req_time[0])
        _tm = int(req_time[1])
        _tp = "pm" if _th >= 12 else "am"
        _th12 = _th % 12 or 12
        _time_display = f"{_th12}:{_tm:02d}{_tp}" if _tm else f"{_th12}{_tp}"
    elif isinstance(req_time, int):
        _time_display = greetings.format_time_simple(req_time, 0)
    _wf_pe = ""
    try:
        from core.webform_security import get_webform_url
        _wf_pe = get_webform_url(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        _wf_pe = f"{get_base_url()}/booking"
    _loc_pe = get_current_incall_location()
    _purl_pe = (get_profile_url() or "").strip()
    _nm_pe = (updated_fields.get("client_name") or state.get("client_name") or "").strip()
    unavail_msg, _ = greetings.build_booking_time_unavailable_message(
        booking_details,
        _time_display if _time_display else "that time",
        city=(_loc_pe.get("city") or ""),
        hotel_name=(_loc_pe.get("hotel_name") or ""),
        address=(_loc_pe.get("address") or ""),
        client_name=_nm_pe,
        is_outcall=is_outcall,
        escort_name=get_escort_name(),
        webform_url=_wf_pe,
        profile_url=_purl_pe,
    )
    return {"messages": [unavail_msg], "new_state": "COLLECTING", "actions": []}


def _yes_check_availability(ctx: CollectingCtx) -> dict[str, Any] | None:
    """If message contains YES, tag state and delegate to availability_check. Returns None to fall through."""
    from handlers import availability_check

    updated_fields = ctx.updated_fields
    phone_number = ctx.phone_number
    message = ctx.message
    state_manager = ctx.state_manager
    context = ctx.raw_context

    if not is_confirmation_token(message):
        return None
    is_outcall = (updated_fields.get("incall_outcall") or "").lower() == "outcall"
    from utils.timezone import get_current_datetime

    _awaiting_at = get_current_datetime().isoformat()
    state_manager.update_fields(
        phone_number,
        {"outcall_awaiting_yes": True, "awaiting_yes_set_at": _awaiting_at}
        if is_outcall
        else {"incall_awaiting_yes": True, "awaiting_yes_set_at": _awaiting_at},
    )
    context["state"] = state_manager.get_state(phone_number)
    return availability_check.handle_check_availability(context)


def _coerce_duration_minutes_positive(raw: Any) -> int | None:
    """Normalize duration for mandatory-field gate (DB/API sometimes yield str)."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, float):
        if raw <= 0:
            return None
        ir = int(raw)
        return ir if ir == raw else None
    if isinstance(raw, str):
        s = raw.strip()
        if s.isdigit():
            n = int(s)
            return n if n > 0 else None
    return None


# ---------------------------------------------------------------------------
# Stage 22 — mandatory date+time+duration
# ---------------------------------------------------------------------------

def _stage_mandatory_date_time_duration(ctx: CollectingCtx) -> dict[str, Any] | None:
    """Date + time + duration present → hours guard, experience branch, YES delegation, booking summary."""
    from booking.deposit_handler import check_unsafe_service, count_profanity_words
    from core.settings_manager import get_setting as _gs
    from handlers import availability_check

    updated_fields = ctx.updated_fields
    phone_number = ctx.phone_number
    message = ctx.message
    state = ctx.state
    state_manager = ctx.state_manager
    context = ctx.raw_context
    avail_now_requested = state.get("available_now_requested", False)

    _raw_dur = updated_fields.get("duration")
    _dur = _coerce_duration_minutes_positive(_raw_dur)
    if not (updated_fields.get("date") and updated_fields.get("time") and _dur):
        return None
    if _raw_dur != _dur:
        state_manager.update_fields(phone_number, {"duration": _dur})
        updated_fields["duration"] = _dur

    scan_text = " ".join(filter(None, [state.get("last_message", ""), message]))
    if check_unsafe_service(scan_text):
        _already_flagged = bool(state.get("unsafe_service_requested"))
        state_manager.update_fields(phone_number, {"unsafe_service_requested": True})
        if not _already_flagged:
            # First detection — alert the escort immediately so they can intervene before deposit/booking.
            try:
                from services.sms_service import send_escort_sms
                from config import get_escort_phone_number
                _escort = get_escort_phone_number()
                if _escort:
                    _alert = (
                        f"\u26A0\uFE0F UNSAFE SERVICE flag: client {phone_number} requested something "
                        f"flagged unsafe. Message: {(message or '')[:180]!r}"
                    )
                    send_escort_sms(_escort, _alert, category='safety_screening')
            except Exception as e:
                import logging as _lg
                _lg.getLogger("adella_chatbot.booking_coll").warning(
                    "Failed to send unsafe-service escort alert for %s: %s", phone_number, e,
                )
    if (_gs("profanity_deposit_enabled") or "true").lower() in ("true", "1", "yes"):
        try:
            _state_now = state_manager.get_state(phone_number) or {}
            if int(_state_now.get("profanity_count") or 0) >= 3:
                state_manager.update_fields(phone_number, {"profanity_detected": True})
        except Exception:
            if count_profanity_words(message) >= 3:
                state_manager.update_fields(phone_number, {"profanity_detected": True})

    is_within_hours, outside_hours_msg, _, _ = check_and_format_outside_hours(
        updated_fields,
        phone_number=phone_number,
        state_manager=state_manager,
    )
    if not is_within_hours:
        state_manager.update_fields(phone_number, {"date": None, "time": None})
        return {"messages": [outside_hours_msg], "new_state": None, "actions": []}

    if not updated_fields.get("experience_type"):
        return _no_experience_branch_response(ctx)

    # Hard assertion: experience_type must be set (or booking is a dinner date) before ANY
    # transition to CHECKING_AVAILABILITY. The earlier guard only catches the first pass;
    # a looped-back revisit could otherwise reach calendar check without experience_type.
    if not (updated_fields.get("experience_type") or is_dinner_date_booking(updated_fields) or is_dinner_date_booking(state)):
        return _no_experience_branch_response(ctx)

    _mmf_gate = _try_mmf_escort_sourced_exploration_gate(
        phone_number=phone_number,
        message=message,
        state=state,
        updated_fields=updated_fields,
        state_manager=state_manager,
    )
    if _mmf_gate is not None:
        return _mmf_gate

    _has_yes = is_confirmation_token(message)

    if avail_now_requested and _has_yes:
        is_outcall_an = (updated_fields.get("incall_outcall") or "").lower() == "outcall"
        from utils.timezone import get_current_datetime

        _awaiting_at = get_current_datetime().isoformat()
        state_manager.update_fields(
            phone_number,
            {"outcall_awaiting_yes": True, "awaiting_yes_set_at": _awaiting_at}
            if is_outcall_an
            else {"incall_awaiting_yes": True, "awaiting_yes_set_at": _awaiting_at},
        )
        context["state"] = state_manager.get_state(phone_number)
        return availability_check.handle_check_availability(context)

    result = _yes_check_availability(ctx)
    if result is not None:
        return result

    # No explicit YES yet — send reconfirmation so the client must confirm before we check availability / take a deposit.
    _is_outcall_preconfirm = (updated_fields.get("incall_outcall") or "").lower() == "outcall"
    if hasattr(state_manager, "set_awaiting_yes_flags"):
        try:
            state_manager.set_awaiting_yes_flags(phone_number, is_outcall=_is_outcall_preconfirm)
        except Exception as e:
            logger.warning("set_awaiting_yes_flags failed for %s: %s", phone_number, e)
    else:
        from utils.timezone import get_current_datetime
        _awaiting_at = get_current_datetime().isoformat()
        state_manager.update_fields(
            phone_number,
            {
                "outcall_awaiting_yes": _is_outcall_preconfirm,
                "incall_awaiting_yes": not _is_outcall_preconfirm,
                "awaiting_yes_set_at": _awaiting_at,
            },
        )
    from templates.booking_reconfirmation import build_booking_reconfirmation
    booking_fields_with_phone = {**updated_fields, "phone_number": phone_number}
    summary_message = build_booking_reconfirmation(booking_fields_with_phone)
    return {"messages": [summary_message], "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}


# ---------------------------------------------------------------------------
# Stage 23 — missing fields or transition
# ---------------------------------------------------------------------------

def _stage_missing_fields_or_transition(ctx: CollectingCtx) -> dict[str, Any]:
    """Prompt for missing fields or send reconfirmation → CHECKING_AVAILABILITY."""
    from templates.booking_reconfirmation import build_booking_reconfirmation

    updated_fields = ctx.updated_fields
    missing = ctx.missing
    phone_number = ctx.phone_number
    message = ctx.message

    if missing:
        _merged_miss = {**(ctx.state or {}), **updated_fields}
        _exp_already = experience_already_set_for_gfe_prompt(_merged_miss)
        _is_oc = str(updated_fields.get("incall_outcall") or "").lower() == "outcall"
        try:
            from handlers.booking_collection import get_targeted_questions_for_fields

            prompt = get_targeted_questions_for_fields(list(missing or []))
        except Exception as e:
            logger.warning("targeted missing-field prompt unavailable for %s: %s", phone_number, e)
            prompt = None
        if not prompt:
            prompt = field_prompts.build_missing_fields_message(
                missing,
                context_message=message,
                experience_already_set=_exp_already,
                is_outcall=_is_oc,
            )
        if prompt:
            # Conversational correction UX: when the client changes time in COLLECTING
            # (e.g. "actually make it 3pm"), acknowledge the updated time before
            # continuing to ask for remaining fields (typically outcall address).
            _is_time_correction = (
                bool((ctx.extracted or {}).get("time"))
                and _is_oc
                and "outcall_address" in set(missing)
                and bool(
                    re.search(
                        r"\b(actually|change|reschedul(?:e|ed|ing)|move|instead|make it)\b",
                        (message or "").lower(),
                    )
                )
            )
            if _is_time_correction:
                try:
                    from templates import greetings as _greetings

                    _tv = updated_fields.get("time")
                    if isinstance(_tv, (tuple, list)) and len(_tv) >= 2:
                        _hh, _mm = int(_tv[0]), int(_tv[1])
                    elif isinstance(_tv, int):
                        _hh, _mm = int(_tv), 0
                    else:
                        _hh = _mm = None
                    if _hh is not None:
                        _ack_time = _greetings.format_time_simple(_hh, _mm)
                        prompt = f"No worries - I've updated your time to {_ack_time}.\n\n{prompt}"
                except Exception as e:
                    logger.warning("time-correction acknowledgment skipped for %s: %s", phone_number, e)
            return {"messages": [prompt], "new_state": None, "actions": []}
        _core_only = {"date", "time", "duration"}
        if set(missing).issubset(_core_only):
            prompt = field_prompts.get_prompt_for_missing_core_fields(
                missing, experience_already_set=_exp_already, is_outcall=_is_oc
            )
            return {"messages": [prompt], "new_state": None, "actions": []}
        _webform_url = _webform_url_for_phone(phone_number)
        _nudge = append_outcall_duration_minimum_if_needed(
            ASK_TIME_AND_DURATION_NUDGE.format(webform_url=_webform_url),
            _is_oc,
        )
        return {
            "messages": [_nudge],
            "new_state": None,
            "actions": [],
        }

    _mmf_gate23 = _try_mmf_escort_sourced_exploration_gate(
        phone_number=phone_number,
        message=message,
        state=ctx.state,
        updated_fields=updated_fields,
        state_manager=ctx.state_manager,
    )
    if _mmf_gate23 is not None:
        return _mmf_gate23

    _is_outcall_preconfirm = (updated_fields.get("incall_outcall") or "").lower() == "outcall"
    if hasattr(ctx.state_manager, "set_awaiting_yes_flags"):
        try:
            ctx.state_manager.set_awaiting_yes_flags(phone_number, is_outcall=_is_outcall_preconfirm)
        except Exception as e:
            logger.warning("set_awaiting_yes_flags failed for %s: %s", phone_number, e)
    else:
        from utils.timezone import get_current_datetime
        _awaiting_at = get_current_datetime().isoformat()
        ctx.state_manager.update_fields(
            phone_number,
            {
                "outcall_awaiting_yes": _is_outcall_preconfirm,
                "incall_awaiting_yes": not _is_outcall_preconfirm,
                "awaiting_yes_set_at": _awaiting_at,
            },
        )

    # Double-booking guard: before sending the booking summary, verify the slot
    # is still actually available. Without this, a duration change or a stale
    # time field can produce a summary for a slot that conflicts with an
    # existing booking, which the client could then confirm with YES.
    _bd_date = updated_fields.get("date")
    _bd_time = updated_fields.get("time")
    _bd_dur = updated_fields.get("duration")
    if _bd_date and _bd_time and _bd_dur:
        from services.calendar_service import check_conflict, check_outcall_conflict_with_travel
        _conflict_booking_details = {
            "date": _bd_date,
            "time": _bd_time,
            "duration": _bd_dur,
            "incall_outcall": updated_fields.get("incall_outcall", "incall"),
            "outcall_address": updated_fields.get("outcall_address"),
        }
        _conflict_is_outcall = _conflict_booking_details["incall_outcall"] == "outcall"
        try:
            if _conflict_is_outcall:
                _conflict_type, _ = check_outcall_conflict_with_travel(_conflict_booking_details)
            else:
                _conflict_type, _ = check_conflict(_conflict_booking_details)
        except Exception as _conf_err:
            logger.exception(
                "Conflict pre-summary check failed for %s: %s", phone_number, _conf_err
            )
            _conflict_type = "none"
        if _conflict_type != "none":
            _t = _bd_time
            if isinstance(_t, (tuple, list)) and len(_t) >= 2:
                _th, _tm = int(_t[0]), int(_t[1])
                _tp = "pm" if _th >= 12 else "am"
                _th12 = _th % 12 or 12
                _time_display = f"{_th12}:{_tm:02d}{_tp}" if _tm else f"{_th12}{_tp}"
            elif isinstance(_t, int):
                from templates import greetings  # noqa: PLC0415
                _time_display = greetings.format_time_simple(_t, 0)
            else:
                _time_display = "that time"
            try:
                from core.webform_security import get_webform_url
                _wf_pre = get_webform_url(phone_number)
            except Exception:
                _wf_pre = f"{get_base_url()}/booking"
            _loc_pre = get_current_incall_location()
            _client_nm = (
                updated_fields.get("client_name")
                or (ctx.state or {}).get("client_name")
                or ""
            ).strip()
            from templates import greetings  # noqa: PLC0415
            unavail_msg, _ = greetings.build_booking_time_unavailable_message(
                _conflict_booking_details,
                _time_display,
                city=(_loc_pre.get("city") or ""),
                hotel_name=(_loc_pre.get("hotel_name") or ""),
                address=(_loc_pre.get("address") or ""),
                client_name=_client_nm,
                is_outcall=_conflict_is_outcall,
                escort_name=get_escort_name(),
                webform_url=_wf_pre,
                profile_url=(get_profile_url() or "").strip(),
            )
            try:
                ctx.state_manager.update_fields(phone_number, {"date": None, "time": None})
            except Exception as _clr_err:
                logger.warning("clear-bad-time failed for %s: %s", phone_number, _clr_err)
            return {"messages": [unavail_msg], "new_state": "COLLECTING", "actions": []}

    booking_fields_with_phone = updated_fields.copy()
    booking_fields_with_phone["phone_number"] = phone_number
    summary_message = build_booking_reconfirmation(booking_fields_with_phone)
    return {"messages": [summary_message], "new_state": "CHECKING_AVAILABILITY", "actions": ["check_calendar"]}

