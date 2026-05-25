"""Pipeline stage helpers for provide_field (extract / slot / validate / finish)."""
from __future__ import annotations

from typing import Any
from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
import re

from config import get_base_url
from core.webform_security import get_webform_url
from utils.dinner_date import is_dinner_date_booking

from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._shared import (
    _get_outcall_policy_amounts,
    _min_hour_error_response,
    _too_far_error_response,
)

logger = logging.getLogger("adella_chatbot.handlers.collecting")


# ---------------------------------------------------------------------------
# Stage 14 — outcall no address short-circuit
# ---------------------------------------------------------------------------

def _stage_outcall_no_address_shortcircuit(ctx: CollectingCtx) -> dict | None:
    """Stage 14: outcall detected but no address yet — show policy message immediately."""
    ftv = ctx.fields_to_validate
    if not ((ftv.get('incall_outcall') or '').lower() == 'outcall' and not ftv.get('outcall_address')):
        return None
    if is_dinner_date_booking(ctx.state):
        return None
    try:
        from config import get_current_incall_location
        from core.rates_from_config import get_deposit_outcall, get_surcharge
        from templates.greetings import build_outcall_policy_message

        _persist: dict[str, Any] = {'incall_outcall': 'outcall'}
        if ftv.get('duration'):
            _persist['duration'] = ftv['duration']

        _loc = get_current_incall_location() or {}
        _city = _loc.get('city') or 'the city'
        try:
            _surcharge = get_surcharge()
            _deposit = get_deposit_outcall()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            _surcharge, _deposit = _get_outcall_policy_amounts()
        try:
            _wf_url = get_webform_url(ctx.phone_number)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            _wf_url = f"{get_base_url()}/booking"

        _ftv_duration_ok = int(ftv.get('duration') or 0) >= 60
        _policy = build_outcall_policy_message(
            city=_city,
            surcharge=_surcharge,
            deposit_outcall=_deposit,
            webform_url=_wf_url,
            has_duration=_ftv_duration_ok,
        )

        # If the client corrected their time (e.g. "actually make it 3pm"), check
        # availability of the new slot before acknowledging or rejecting.
        _msg_lower = (ctx.message or "").lower()
        _is_time_correction = bool((ctx.extracted or {}).get("time")) and bool(
            re.search(
                r"\b(actually|change|reschedul(?:e|ed|ing)|move|instead|make\s+it)\b",
                _msg_lower,
            )
        )
        if _is_time_correction:
            try:
                from templates.greetings import format_time_simple, format_slot_today_dd_month_at_time
                from templates.booking_collection_messages import build_outcall_policy_line
                from services.calendar_service import check_conflict as _cc, find_alternative_slots

                _tv = ctx.extracted.get("time")
                if isinstance(_tv, (tuple, list)) and len(_tv) >= 2:
                    _hh, _mm = int(_tv[0]), int(_tv[1])
                elif isinstance(_tv, int):
                    _hh, _mm = int(_tv), 0
                else:
                    _hh = _mm = None

                if _hh is not None:
                    _ack_time_str = format_time_simple(_hh, _mm)
                    _date = (ctx.current_fields or {}).get("date") or (ctx.state or {}).get("date")
                    _duration = (
                        ftv.get("duration")
                        or (ctx.current_fields or {}).get("duration")
                        or (ctx.state or {}).get("duration")
                        or 60
                    )
                    _conflict_type = "unknown"
                    _check_details = {
                        "date": _date,
                        "time": (_hh, _mm),
                        "duration": _duration,
                        "incall_outcall": "outcall",
                    }
                    if _date:
                        try:
                            _conflict_type, _ = _cc(_check_details)
                        except Exception as _ce:
                            logger.warning("Stage14 conflict check failed: %s", _ce)

                    _client_name = (
                        (ctx.current_fields or {}).get("client_name")
                        or (ctx.state or {}).get("client_name")
                        or ""
                    ).strip()
                    _hi = f"Hi {_client_name} " if _client_name else "Hi "
                    _policy_line = build_outcall_policy_line(
                        surcharge=_surcharge, deposit_outcall=_deposit, city=_city
                    )

                    if _conflict_type == "none":
                        # Slot is free — save corrected time and confirm ✅
                        _persist["time"] = (_hh, _mm)
                        ctx.state_manager.update_fields(ctx.phone_number, _persist)
                        if _ftv_duration_ok:
                            _ask = "What's your address?"
                            _hint = ""
                        else:
                            _ask = (
                                "What's your address and how long would you like to book for? "
                                "(Minimum 1 hour for outcalls)"
                            )
                            _hint = '\n\nPlease reply with both — e.g. "Hilton Adelaide 1 hr"'
                        _avail_msg = (
                            f"{_hi}✅ I've updated your time to {_ack_time_str}\n\n"
                            f"{_policy_line}\n\n"
                            f"{_ask}{_hint}\n\n"
                            f"To book a different time fill in my booking webform: {_wf_url}"
                        )
                        return {"messages": [_avail_msg], "new_state": None, "actions": []}

                    elif _conflict_type in ("confirmed", "peacock"):
                        # Slot is busy — return ❌ + closest 3 alternatives
                        ctx.state_manager.update_fields(ctx.phone_number, _persist)
                        try:
                            raw_alts = find_alternative_slots(_check_details, max_results=3)
                            if raw_alts:
                                slots_text = "\n".join(
                                    f"• {format_slot_today_dd_month_at_time(dt)}" for dt in raw_alts
                                )
                                _unavail_msg = (
                                    f"{_hi}❌ Unfortunately {_ack_time_str} isn't available.\n\n"
                                    f"Here are my closest available times:\n\n"
                                    f"{slots_text}\n\n"
                                    f"{_policy_line}\n\n"
                                    f"What time suits you and what's your address?\n\n"
                                    f"To request a different time fill in the booking webform {_wf_url}"
                                )
                            else:
                                _unavail_msg = (
                                    f"{_hi}❌ Unfortunately {_ack_time_str} isn't available. "
                                    f"Please choose another time or fill out the webform: {_wf_url}"
                                )
                            return {"messages": [_unavail_msg], "new_state": None, "actions": []}
                        except Exception as _ua_e:
                            logger.warning("Stage14 unavail msg build failed: %s", _ua_e)
                            _fallback = (
                                f"{_hi}❌ Sorry, {_ack_time_str} isn't available. "
                                f"Please choose another time or fill out the webform: {_wf_url}"
                            )
                            return {"messages": [_fallback], "new_state": None, "actions": []}
                    # else "unknown" (calendar error) — fall through without acknowledgment
            except Exception as _ack_e:
                logger.warning("time-correction ack skipped in stage14: %s", _ack_e)

        ctx.state_manager.update_fields(ctx.phone_number, _persist)
        return {"messages": [_policy], "new_state": None, "actions": []}
    except Exception as _e:
        logger.warning("Outcall short-circuit policy message failed: %s", _e)
        return None


# ---------------------------------------------------------------------------
# Stages 15+16 — validate fields
# ---------------------------------------------------------------------------

def _stage_validate_fields(ctx: CollectingCtx) -> dict | None:
    """Stage 15+16: run validate_all on fields_to_validate, handle all validation-error early returns."""
    from booking.field_validator import OUTCALL_MINIMUM_1_HOUR, is_outcall_too_far_error
    from templates import greetings

    ftv = ctx.fields_to_validate
    phone_number = ctx.phone_number
    message = ctx.message
    state = ctx.state
    state_manager = ctx.state_manager
    field_validator = ctx.field_validator
    extracted = ctx.extracted
    current_fields = ctx.current_fields
    is_available_now = ctx.is_available_now

    if (ftv.get("incall_outcall") or "").lower() == "outcall" and ftv.get("outcall_address"):
        logger.info(
            "Outcall address before validation: extracted=%r final=%r",
            extracted.get("outcall_address"),
            ftv.get("outcall_address"),
        )

    if is_available_now and (ftv.get("incall_outcall") or "").lower() == "outcall":
        short_min_match = re.search(r"(\d+)\s*(?:mins?|minutes?)", message.lower())
        if short_min_match:
            try:
                mins_val = int(short_min_match.group(1))
            except (TypeError, ValueError):
                mins_val = None
            if mins_val is not None and mins_val < 60:
                ftv["duration"] = mins_val

    valid, errors = field_validator.validate_all(ftv)

    if not valid and (ftv.get("incall_outcall") or "").lower() == "outcall" and is_available_now:
        has_too_far = any(is_outcall_too_far_error(e) for e in (errors or []))

        def _is_outcall_verification_error(e):
            if not e or is_outcall_too_far_error(e):
                return False
            low = e.lower()
            return any(x in low for x in ("address", "hotel", "verification", "found", "location", "outcall"))

        outcall_verification_errors = [e for e in (errors or []) if _is_outcall_verification_error(e)]
        other_errors = [e for e in (errors or []) if e not in outcall_verification_errors]
        if not has_too_far and outcall_verification_errors and not other_errors:
            valid = True
            errors = []
            state_manager.update_fields(phone_number, {"_outcall_verification_skipped_available_now": True})
            logger.info("Available-now outcall: allowing booking despite verification failure (lenient), phone=%s", phone_number)

    ctx.valid = valid
    ctx.errors = errors

    if not valid:
        logger.info(
            "Validation failed for %s: incall_outcall=%r duration=%r errors=%r is_available_now=%r",
            phone_number,
            (ftv.get("incall_outcall") or "").lower(),
            ftv.get("duration"),
            errors,
            is_available_now,
        )

        is_outcall = (ftv.get("incall_outcall") or "").lower() == "outcall"
        too_far_error = any(
            e and ("15km" in e.lower() or ("current location" in e.lower() and "max" in e.lower()))
            for e in (errors or [])
        )
        if is_outcall and too_far_error:
            result = _too_far_error_response(phone_number, state, ftv, errors)
            if result is not None:
                return result

        if OUTCALL_MINIMUM_1_HOUR in (errors or []):
            result = _min_hour_error_response(phone_number, state_manager, extracted, ftv, current_fields, greetings)
            if result is not None:
                return result

        from templates.errors import get_enhanced_validation_error
        error_message = get_enhanced_validation_error(errors, ftv)
        return {"messages": [error_message], "new_state": None, "actions": []}

    return None
