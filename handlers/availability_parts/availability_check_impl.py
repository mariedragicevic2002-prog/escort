"""
handlers/availability_parts/availability_check_impl.py

The 3 public handler functions extracted from main_flow.py:
  handle_check_availability, handle_unknown_in_checking, handle_manual_review_pending
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT

import logging
from datetime import datetime, timedelta
from typing import Any

from core.booking_substates import DOUBLES_SUPPLY_ESCORT, MANUAL_REVIEW_PENDING as BOOKING_STATUS_MANUAL_REVIEW_PENDING
from config import get_base_url, get_escort_name, get_profile_url
from core.webform_security import get_webform_url
from utils.time_formatting import parse_booking_hour_minute
from templates.availability_messages import (
    CONFIRM_BOOKING_REMINDER,
    FORWARD_TO_ESCORT_NOTICE,
)

from handlers.availability_parts.locking import (
    _booking_lock_key,
    _finalization_booking_identity_key,
    _acquire_booking_lock,
    _release_booking_lock,
)
from handlers.availability_parts.time_rules import (
    _mark_followup_task_failure,
)

logger = logging.getLogger("handlers.availability_check")


_AWAITING_YES_TTL_HOURS = 12


def _parse_iso_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).strip())
    except Exception:
        return None


def _is_hedged_confirmation(message_lower: str) -> bool:
    """True when client response contains uncertainty despite YES-like tokens."""
    if not message_lower:
        return False
    hedges = (
        "maybe",
        "not sure",
        "i think",
        "probably",
        "might",
        "if possible",
    )
    return any(h in message_lower for h in hedges)


def _is_multi_slot_hold_request(message_lower: str) -> bool:
    if not message_lower:
        return False
    import re as _re

    if not _re.search(r"\b(hold|book|lock|reserve)\b", message_lower):
        return False
    if not _re.search(r"\b(both|two|2)\b", message_lower):
        return False
    _time_mentions = _re.findall(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", message_lower)
    return len(_time_mentions) >= 2


def _is_change_request_while_awaiting_yes(message_lower: str) -> bool:
    if not message_lower:
        return False
    import re as _re

    _change_terms = _re.search(
        r"\b(go with|instead|make it|change|move|reschedule|different|switch|use)\b",
        message_lower,
    )
    _time_or_day = _re.search(
        r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b(today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        message_lower,
    )
    return bool(_change_terms or _time_or_day)


def _is_change_or_cancel_choice_request(message_lower: str) -> bool:
    if not message_lower:
        return False
    return any(
        token in message_lower
        for token in (
            "change",
            "modify",
            "edit",
            "reschedule",
            "move it",
            "make a change",
            "cancel",
            "call it off",
            "stop it",
        )
    )


def _is_cancel_only_choice_request(message_lower: str) -> bool:
    if not message_lower:
        return False
    return any(
        token in message_lower
        for token in (
            "cancel",
            "call it off",
            "stop it",
            "delete it",
            "abort",
        )
    )


def _clear_awaiting_yes_flags(state_manager, phone_number: str, *, extra_updates: dict[str, Any] | None = None) -> bool:
    updates: dict[str, Any] = {
        "outcall_awaiting_yes": False,
        "incall_awaiting_yes": False,
        "awaiting_yes_set_at": None,
    }
    if extra_updates:
        updates.update(extra_updates)
    ok = bool(state_manager.update_fields(phone_number, updates))
    if not ok:
        logger.warning("Failed to clear awaiting-YES flags for %s", phone_number)
    return ok


def _set_awaiting_yes_flags(
    state_manager,
    phone_number: str,
    *,
    is_outcall: bool,
    extra_updates: dict[str, Any] | None = None,
) -> bool:
    if hasattr(state_manager, "set_awaiting_yes_flags"):
        return bool(
            state_manager.set_awaiting_yes_flags(
                phone_number,
                is_outcall=is_outcall,
                extra_updates=extra_updates,
            )
        )
    from utils.timezone import get_current_datetime

    updates: dict[str, Any] = {
        "outcall_awaiting_yes": bool(is_outcall),
        "incall_awaiting_yes": not bool(is_outcall),
        "awaiting_yes_set_at": get_current_datetime().isoformat(),
    }
    if extra_updates:
        updates.update(extra_updates)
    return bool(state_manager.update_fields(phone_number, updates))


def _claim_confirmation_token_status(state_manager, phone_number: str, token: str) -> str:
    if hasattr(state_manager, "claim_confirmation_token_status"):
        try:
            return str(state_manager.claim_confirmation_token_status(phone_number, token))
        except Exception as e:
            logger.warning("claim_confirmation_token_status failed for %s: %s", phone_number, e)
            return "error"
    try:
        return "claimed" if state_manager.claim_confirmation_token(phone_number, token) else "duplicate"
    except Exception as e:
        logger.warning("claim_confirmation_token failed for %s: %s", phone_number, e)
        return "error"


def handle_check_availability(context: dict[str, Any]) -> dict[str, Any]:
    """Check calendar availability and determine next state.

    Flow:
    1. Get booking fields from state
    2. Check if outcall → use check_outcall_conflict_with_travel()
    3. Else → use check_conflict()
    4. If conflict_type == "confirmed":
       - Find 3 alternative slots
       - Return alternatives message
       - Stay in CHECKING_AVAILABILITY
    5. If conflict_type == "peacock":
       - Treat as a hard block: offer alternatives if any, else suggest another time
       - Stay in CHECKING_AVAILABILITY
    6. If conflict_type == "none":
       - Calculate if deposit required
       - If deposit required → transition to DEPOSIT_REQUIRED
       - Else → create peacock event, transition to CONFIRMED
    7. If conflict_type == "unknown":
       - Calendar service error
       - Ask to try again or contact directly

    Args:
        context: Dict with phone_number, state_manager, state, message

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    state = context['state']
    _auto_confirm_without_experience = bool((state or {}).get('auto_confirm_without_experience'))

    # --- Correction detection ---
    # If the client sends a message while we're waiting for YES that looks like
    # a field correction ("I said 11am", "make it 3hrs", new address, etc.),
    # revert to COLLECTING so the field gets updated and a fresh confirmation is shown.
    _msg = (context.get('message') or '').strip().lower()
    _correction_patterns = [
        r'\bi\s+said\b',           # "I said 11am"
        r'\bactually\b',           # "actually make it 2 hours"
        r'\bchange\s+(the\s+)?(time|date|duration|address)\b',
        r'\bmake\s+it\b',          # "make it 3 hours"
        r'\bnot\s+\d',             # "not 7pm"
        r'\bwrong\b',              # "that time is wrong"
        r'\binstead\b',            # "3am instead"
        r'\bcorrect\b',            # "the correct address is..."
        r'\bscratch\s+that\b',     # "sorry scratch that ..."
        r'\bmy\s+address\s+is\b',  # client correcting the address
        r'\bthe\s+address\s+is\b',
    ]
    import re as _re
    # Do not hijack real cancellations ("yes but actually cancel") into COLLECTING — that path can throw.
    # Narrow "don't want" so service objections ("don't want doubles") don't wipe a pending booking.
    _cancel_or_reject_re = _re.compile(
        r'\b(?:cancel|cancellation|abort|never\s*mind|nevermind|forget\s+it)\b|'
        r"\bdon'?t\s+want\s+(?:that|this|it|the\s+slot|the\s+booking|to)\b"
    )
    _explicit_keep_booking_re = _re.compile(r"\b(?:don'?t\s+cancel|do\s+not\s+cancel)\b")

    def _wants_abort_booking(msg_lower: str) -> bool:
        if _explicit_keep_booking_re.search(msg_lower):
            return False
        return bool(_cancel_or_reject_re.search(msg_lower))

    if state.get("awaiting_booking_change_cancel_choice"):
        if _is_change_or_cancel_choice_request(_msg):
            _is_cancel = _is_cancel_only_choice_request(_msg)
            if _is_cancel:
                state_manager.clear_booking(phone_number)
                return {
                    "messages": ["No worries — no booking has been made. Goodbye."],
                    "new_state": "NEW",
                    "actions": [],
                }

            state_manager.clear_booking(phone_number)
            state_manager.update_fields(
                phone_number,
                {
                    "current_state": "COLLECTING",
                    "awaiting_booking_change_cancel_choice": False,
                    "first_contact_sent": False,
                },
            )
            return {
                "messages": [
                    "Sure — send your new date, time, duration, and whether you want incall or outcall, and I’ll start a fresh booking."
                ],
                "new_state": "COLLECTING",
                "actions": [],
            }

        return {
            "messages": [
                "Please reply with change or cancel so I know what you’d like to do."
            ],
            "new_state": "CHECKING_AVAILABILITY",
            "actions": [],
        }

    _abort = _wants_abort_booking(_msg)
    if any(_re.search(p, _msg) for p in _correction_patterns):
        if not _abort:
            logger.info(f"[CHECKING_AVAILABILITY] Detected correction in message, reverting to COLLECTING: {_msg!r}")
            # Drop back to COLLECTING — handle_provide_field will extract the new field value,
            # update state, and show a fresh confirmation summary.
            from handlers.booking_collection import handle_provide_field as _hpf
            context = dict(context)
            context['state'] = {**state, 'current_state': 'COLLECTING'}
            return _hpf(context)

    # Abort / drop-out beats YES-word parsing ("yes but cancel everything", "yes I don't want that slot").
    if _abort:
        from handlers.booking_coll._cancel_rates import handle_cancel_booking as _abort_collecting_cancel

        return _abort_collecting_cancel(context)

    # Import services
    from booking.deposit_handler import calculate_deposit_requirement
    from services.calendar_service import (
        check_conflict,
        check_outcall_conflict_with_travel,
        create_calendar_event,
        find_alternative_slots,
    )
    from templates.confirmations import (
        calculate_price,
        get_conflict_alternatives_message,
        get_deposit_request_message,
    )
    from templates.errors import get_error_message

    # Generate secure webform URL for this client (used in alternatives messages)
    _webform_url = get_webform_url(phone_number)

    # Get booking fields
    # For "available now" bookings, recalculate time based on current time + arrival
    is_available_now = state.get('available_now_requested', False)
    booking_time = state.get('time')
    booking_date = state.get('date')

    if is_available_now and booking_time is None:
        # Only recalculate when no time is already stored (i.e. the initial "available now" check).
        # If a booking time was already confirmed in the reconfirmation step, keep it — do NOT
        # overwrite with the current clock time when the client sends YES.
        from handlers.booking_collection import calculate_available_now_booking_datetime
        from utils.timezone import get_current_datetime
        now = get_current_datetime()
        arrival_mins = state.get('arrival_time_minutes')
        booking_datetime = calculate_available_now_booking_datetime(
            now,
            arrival_mins,
            is_outcall=state.get('incall_outcall') == 'outcall',
            outcall_address=state.get('outcall_address'),
        )

        booking_time = (booking_datetime.hour, booking_datetime.minute)
        booking_date = booking_datetime.date()
        # Update state with recalculated time
        state_manager.update_fields(phone_number, {
            'time': booking_time,
            'date': booking_date
        })

    booking_fields = {
        'date': booking_date,
        'time': booking_time,
        'duration': state.get('duration'),
        'experience_type': state.get('experience_type'),
        'incall_outcall': state.get('incall_outcall'),
        'outcall_address': state.get('outcall_address'),
        'client_name': state.get('client_name', 'Client'),
        'booking_type': state.get('booking_type'),
        'doubles_type': state.get('doubles_type'),
        'dinner_restaurant': state.get('dinner_restaurant'),
        'dinner_after_preference': state.get('dinner_after_preference'),
        'dinner_client_address': state.get('dinner_client_address'),
        'dinner_client_outside_15km': state.get('dinner_client_outside_15km'),
    }

    # Re-validate outcall address every time we enter CHECKING_AVAILABILITY. Prior validation
    # only ran when the address was first extracted; on revisit a previously accepted but
    # invalid address could persist through to confirmation.
    if (booking_fields.get('incall_outcall') or '').lower() == 'outcall' and booking_fields.get('outcall_address'):
        try:
            from booking.field_validator import FieldValidator
            from config import get_current_incall_location as _gcil
            _loc = _gcil() or {}
            _ok, _err = FieldValidator().validate_outcall_address(
                booking_fields['outcall_address'], 'outcall', city=_loc.get('city', ''),
            )
            if not _ok:
                logger.warning(
                    "Stored outcall_address failed re-validation for %s: %s — dropping back to COLLECTING",
                    phone_number, _err,
                )
                state_manager.update_fields(phone_number, {'outcall_address': None})
                return {
                    "messages": [f"❌ {_err}\n\nPlease provide a valid outcall address so I can continue your booking."],
                    "new_state": "COLLECTING",
                    "actions": [],
                }
        except Exception as _e:
            logger.error("Outcall address re-validation failed for %s: %s", phone_number, _e)
            try:
                state_manager.update_fields(phone_number, {'outcall_address': None})
            except Exception as _clear_err:
                logger.warning(LOG_SUPPRESSED_FMT, _clear_err, exc_info=False)
            return {
                "messages": [
                    "I couldn't verify that outcall address just now. "
                    "Please send the full address again so I can continue your booking."
                ],
                "new_state": "COLLECTING",
                "actions": [],
            }

    from utils.dinner_date import DINNER_DURATION_MINUTES, dinner_slot_fits_window

    if (state.get('booking_type') == 'dinner_date'
            or (state.get('experience_type') or '').strip().lower() in ('dinner date', 'dinner_date')):
        booking_fields['duration'] = DINNER_DURATION_MINUTES
        booking_fields['booking_type'] = booking_fields.get('booking_type') or 'dinner_date'
        try:
            import datetime as _dt
            from utils.timezone import get_local_timezone

            _bd = booking_fields.get('date')
            _bt = booking_fields.get('time')
            if _bd and _bt:
                if isinstance(_bt, _dt.time):
                    _h, _m = _bt.hour, _bt.minute
                elif isinstance(_bt, (tuple, list)) and len(_bt) >= 2:
                    _h, _m = int(_bt[0]), int(_bt[1])
                elif isinstance(_bt, int):
                    _h, _m = int(_bt), 0
                else:
                    _h, _m = None, None
                if _h is not None:
                    tz = get_local_timezone()
                    _start = tz.localize(_dt.datetime.combine(_bd, _dt.time(_h, _m)))
                    if not dinner_slot_fits_window(_start, DINNER_DURATION_MINUTES):
                        return {
                            "messages": [
                                "Dinner dates are a 2-hour booking: your start time must be between 5pm and 9pm "
                                "(not after 9pm). The booking may finish later than 9pm — for example 8:15pm is fine."
                            ],
                            "new_state": "COLLECTING",
                            "actions": [],
                        }
        except Exception as _e:
            logger.warning("Dinner window check skipped: %s", _e)

    logger.info(f"Checking availability for {phone_number}: {booking_fields} (available_now={is_available_now})")

    # Misclassified or stale routing (e.g. NEW + confirm_booking with no booking payload) cannot run
    # calendar/deposit logic safely — recover via the normal booking entry handler.
    _state_name = (state or {}).get("current_state")
    _missing_core_booking = (
        booking_fields.get("date") is None
        or booking_fields.get("time") is None
        or booking_fields.get("duration") is None
    )
    if _state_name == "NEW" and _missing_core_booking:
        from core.classifier import Classifier
        from handlers import new_conversation

        _recovered_intent = Classifier(ai_service=context.get("ai_service")).classify(
            context.get("message", ""),
            context.get("media_urls") or [],
            context,
        )
        if _recovered_intent == "cancel_booking":
            logger.info(
                "[CHECKING_AVAILABILITY] NEW without core fields — routing cancel intent to NEW cancel handler (%s)",
                phone_number,
            )
            return new_conversation.handle_cancel_booking_new(context)
        if _recovered_intent == "other":
            logger.info(
                "[CHECKING_AVAILABILITY] NEW without core fields — routing low-signal intent to NEW ambiguous handler (%s)",
                phone_number,
            )
            return new_conversation.handle_new_ambiguous(context)

        logger.info(
            "[CHECKING_AVAILABILITY] NEW without core booking fields — delegating to handle_book_appointment (%s)",
            phone_number,
        )
        return new_conversation.handle_book_appointment(context)

    # --- 10-minute cutoff rule ---
    # Reject any booking where the requested start time is within 10 minutes of now.
    _bk_date = booking_fields.get('date')
    _bk_time = booking_fields.get('time')
    if isinstance(_bk_date, datetime):
        _bk_date = _bk_date.date()
    elif isinstance(_bk_date, str):
        try:
            _bk_date = datetime.strptime(_bk_date[:10], "%Y-%m-%d").date()
        except ValueError:
            _bk_date = None
    if _bk_date and _bk_time and not is_available_now:
        try:
            import datetime as _dt

            from utils.timezone import get_current_datetime
            _now = get_current_datetime()
            _bk_hour, _bk_min = parse_booking_hour_minute(_bk_time)
            if _bk_hour is None:
                raise ValueError(f"Unsupported booking time value: {_bk_time!r}")
            _bk_dt = datetime.combine(_bk_date, _dt.time(_bk_hour, _bk_min))
            # Make timezone-aware if now is aware
            if _now.tzinfo and _bk_dt.tzinfo is None:
                try:
                    _bk_dt = _now.tzinfo.localize(_bk_dt)
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    _bk_dt = _bk_dt.replace(tzinfo=_now.tzinfo)
            _minutes_until = (_bk_dt - _now).total_seconds() / 60
            if _minutes_until < 0:
                logger.info(
                    "Booking rejected — requested start is %.1f min in the past (strict lead-time rule)",
                    -_minutes_until,
                )
                return {
                    "messages": [
                        "That start time looks like it's already passed on my side, so I can't slot you in "
                        "from then.\n\n"
                        "If you'd still like to book, send a future date and time (and duration if we "
                        "haven't settled it), or pick from the times I offered earlier."
                    ],
                    "new_state": "COLLECTING",
                    "actions": [],
                }
            if _minutes_until <= 10:
                logger.info(f"Booking rejected — requested time is only {_minutes_until:.1f} min away (cutoff: 10 min)")
                return {
                    "messages": [
                        "I need a little breathing room before the start - new bookings need at least "
                        "10 minutes' notice.\n\n"
                        "Could you pick a slightly later time, or tell me another slot you're considering "
                        "and I'll check?"
                    ],
                    "new_state": "COLLECTING",
                    "actions": []
                }
        except Exception as _e:
            logger.warning(f"10-min cutoff check failed: {_e}")

    # ── GOLDEN RULE: 4-hour notice for escort-supplied doubles ──────
    # When the escort must organise the other person for a doubles
    # booking, the requested start must be at least 4 hours from now.
    _escort_supplies_doubles = (
        state.get('escort_supply_source') == 'escort'
        or state.get('booking_status') == DOUBLES_SUPPLY_ESCORT
    )
    if (state.get('booking_type') in ('doubles_mff', 'Doubles MMF')
            and _escort_supplies_doubles
            and _bk_date and _bk_time and not is_available_now):
        try:
            import datetime as _dt

            from utils.timezone import get_current_datetime
            _now_4h = get_current_datetime()
            _d_hour, _d_min = parse_booking_hour_minute(_bk_time)
            if _d_hour is not None:
                _d_dt = datetime.combine(_bk_date, _dt.time(_d_hour, _d_min))
                if _now_4h.tzinfo and _d_dt.tzinfo is None:
                    try:
                        _d_dt = _now_4h.tzinfo.localize(_d_dt)
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                        _d_dt = _d_dt.replace(tzinfo=_now_4h.tzinfo)
                _hours_until = (_d_dt - _now_4h).total_seconds() / 3600
                if _hours_until < 4:
                    _earliest = _now_4h + timedelta(hours=4)
                    _earliest_str = _earliest.strftime("%I:%M %p").lstrip("0")
                    logger.info(
                        f"Doubles booking rejected — only {_hours_until:.1f}h notice "
                        f"(escort supplies, 4h minimum required)"
                    )
                    return {
                        "messages": [
                            f"When I need to organise the other escort, I require a minimum "
                            f"4 hours notice. The earliest I can offer is {_earliest_str}. "
                            f"Would you like to book for then, or choose a later time?"
                        ],
                        "new_state": "COLLECTING",
                        "actions": [],
                    }
        except Exception as _e4h:
            logger.warning(f"4-hour doubles notice check failed: {_e4h}")

    # Check availability
    is_outcall = booking_fields.get('incall_outcall') == 'outcall'

    # Scenario G: Outcall – once client responds YES, name, or experience type, create GRAPHITE (pending deposit) and send deposit message
    message_raw = (context.get('message') or '').strip()
    message_lower = message_raw.lower().strip()

    # If client switches to outcall mid-flow (was incall), restart collection for outcall address
    _outcall_switch_keywords = [
        'my place', 'my home', 'my hotel', 'my apartment', 'my airbnb', 'my room',
        'come to me', 'come to my', 'to my place', 'to my home', 'to my hotel',
        'come see me', 'come and see me', 'see me',
        'to me', 'at my', 'visit me', 'travel to', 'can you come',
    ]
    if not is_outcall and any(kw in message_lower for kw in _outcall_switch_keywords):
        _clear_awaiting_yes_flags(
            state_manager,
            phone_number,
            extra_updates={
                'incall_outcall': 'outcall',
                'outcall_address': None,
            },
        )
        context['state'] = state_manager.get_state(phone_number)
        from handlers import booking_collection
        return booking_collection.handle_provide_field(context)
    # Treat a few casual variants as YES so clients aren't forced to type it perfectly.
    yes_words = {'yes', 'yep', 'yeah', 'y', 'ya', 'yee', 'yss', 'yaa'}
    experience_words = {'gfe', 'pse', 'dgfe'}
    msg_words = set(message_lower.split())
    _has_yes_word = bool(msg_words & yes_words)
    _has_exp_word = bool(msg_words & experience_words)
    is_yes = _has_yes_word
    is_experience = _has_exp_word
    words = message_raw.split()
    # Reject obviously-non-name single words that a client might send casually
    # (e.g. "ok", "sure", "thanks") to avoid false-positive booking confirmations.
    _non_name_words = {
        'ok', 'okay', 'sure', 'hi', 'hello', 'thanks', 'thank', 'cancel', 'stop',
        'mate', 'cool', 'done', 'great', 'good', 'nice', 'please', 'noted', 'got',
        'right', 'sounds', 'fine', 'perfect', 'awesome', 'cheers', 'later', 'bye',
        'no', 'nope', 'nah', 'na', 'nada', 'negative',
    }
    from templates import greetings as _greetings_name
    is_name = (
        not _has_yes_word and not _has_exp_word
        and len(words) <= 2
        and all(w.isalpha() for w in words)
        and len(message_raw) >= 2
        and message_lower not in _non_name_words
        and _greetings_name.is_valid_client_name(message_raw)
    )
    _already_in_checking = state.get('current_state') == 'CHECKING_AVAILABILITY'

    # --- Unified awaiting-YES gate (outcall AND incall) ---
    # Outcall: persisted flag or fallback heuristic with outcall fields.
    _outcall_summary_ctx = (
        is_outcall
        and (is_available_now or _already_in_checking)
        and booking_fields.get('date')
        and booking_fields.get('time')
        and booking_fields.get('duration')
        and booking_fields.get('outcall_address')
        and state.get('deposit_required')
    )
    outcall_awaiting = is_outcall and (
        state.get('outcall_awaiting_yes')
        or (_outcall_summary_ctx and (is_yes or is_experience or is_name))
    )

    # Incall: persisted flag set when we sent the reconfirmation summary.
    _incall_summary_ctx = (
        not is_outcall
        and _already_in_checking
        and booking_fields.get('date')
        and booking_fields.get('time')
        and booking_fields.get('duration')
    )
    incall_awaiting = not is_outcall and (
        state.get('incall_awaiting_yes')
        or (_incall_summary_ctx and (is_yes or is_experience or is_name))
    )

    awaiting_yes = outcall_awaiting or incall_awaiting

    # Expire stale awaiting-YES windows so delayed confirmations re-enter collection cleanly.
    if awaiting_yes:
        from utils.timezone import get_current_datetime

        _awaiting_set_at = _parse_iso_dt(
            state.get('awaiting_yes_set_at_ts') or state.get('awaiting_yes_set_at')
        )
        _now_ttl = get_current_datetime()
        _age_hours = None
        if _awaiting_set_at is not None:
            try:
                _age_hours = (_now_ttl - _awaiting_set_at).total_seconds() / 3600
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                _age_hours = None
        if _age_hours is not None and _age_hours > _AWAITING_YES_TTL_HOURS:
            logger.info(
                "[CHECKING_AVAILABILITY] Expiring stale awaiting-YES window for %s (age=%.2fh)",
                phone_number,
                _age_hours,
            )
            _clear_awaiting_yes_flags(state_manager, phone_number)
            return {
                "messages": [
                    "That confirmation has expired. Please send your preferred date and time again and I'll re-check availability."
                ],
                "new_state": "COLLECTING",
                "actions": [],
            }

    # While awaiting YES, treat change/selection messages as edits (not repeated reconfirmation loops).
    if awaiting_yes and not (is_yes or is_experience or is_name or _auto_confirm_without_experience):
        # Explicit negation: client is cancelling / rejecting the booking summary
        _negation_words = {'no', 'nope', 'nah', 'na', 'nada', 'negative', 'cancel', 'stop', 'dont', "don't"}
        _msg_stripped = message_lower.strip().strip('.,!?')
        if (
            _msg_stripped in _negation_words
            or message_lower.startswith(("no ", "nope ", "nah "))
            or "dont want" in message_lower
            or "don't want" in message_lower
            or "not booking" in message_lower
            or "cancel booking" in message_lower
        ):
            _clear_awaiting_yes_flags(
                state_manager,
                phone_number,
                extra_updates={
                    "awaiting_booking_change_cancel_choice": True,
                },
            )
            state_manager.update_fields(
                phone_number,
                {
                    "current_state": "CHECKING_AVAILABILITY",
                    "awaiting_booking_change_cancel_choice": True,
                },
            )
            return {
                "messages": [
                    "No worries — would you like to make a change to your current booking, or cancel it completely?"
                ],
                "new_state": "CHECKING_AVAILABILITY",
                "actions": [],
            }
        if _is_multi_slot_hold_request(message_lower):
            _clear_awaiting_yes_flags(state_manager, phone_number)
            return {
                "messages": [
                    "I can only hold one time at once. Send the single time you'd like (for example: 8pm) and I'll check it immediately."
                ],
                "new_state": "COLLECTING",
                "actions": [],
            }
        if _is_change_request_while_awaiting_yes(message_lower):
            from handlers import booking_collection

            _clear_awaiting_yes_flags(state_manager, phone_number)
            context = dict(context)
            context['state'] = {**state, 'current_state': 'COLLECTING'}
            return booking_collection.handle_provide_field(context)
        _name = (state.get("client_name") or "").strip()
        _name_str = f" {_name}" if _name else ""
        return {
            "messages": [CONFIRM_BOOKING_REMINDER.format(name_str=_name_str)],
            "new_state": None,
            "actions": [],
        }

    if awaiting_yes and (is_yes or is_experience or is_name or _auto_confirm_without_experience):
        if _is_hedged_confirmation(message_lower):
            return {
                "messages": [
                    "Just to confirm before I proceed — please reply YES to lock this booking in, or send changes if you want to edit it."
                ],
                "new_state": None,
                "actions": [],
            }
        updates = {
            'auto_confirm_without_experience': False,
        }
        if is_experience and not booking_fields.get('experience_type'):
            _exp_match = (msg_words & experience_words).pop()
            updates['experience_type'] = _exp_match.upper()
        # Extract name from combo messages like "John PSE YES", "John YES".
        # Treat invalid placeholders (e.g. "midday") as missing.
        from templates import greetings
        _existing_name = (booking_fields.get('client_name') or '').strip()
        _has_valid_existing_name = greetings.is_valid_client_name(_existing_name)
        if not _has_valid_existing_name:
            _known_words = yes_words | experience_words | {'and', '&'}
            _name_parts = []
            for w in words:
                if w.lower() in _known_words:
                    continue
                if w.isalpha() and not greetings.is_likely_not_a_name(w):
                    _name_parts.append(w)
            if _name_parts and len(_name_parts) <= 2:
                _candidate_name = " ".join(p.capitalize() for p in _name_parts)
                if greetings.is_valid_client_name(_candidate_name):
                    updates['client_name'] = _candidate_name
        if updates:
            state_manager.update_fields(phone_number, updates)
        booking_fields = state_manager.get_booking_fields(phone_number) or booking_fields

        # Guard: required fields must be present before proceeding to deposit/confirmation.
        # Normally guaranteed by awaiting_yes only being set after full pre-confirm summary,
        # but this catches edge-case state corruption.
        _required = ['date', 'time', 'duration']
        if any(not booking_fields.get(f) for f in _required):
            logger.warning(
                "[CHECKING_AVAILABILITY] YES gate: missing required fields for %s — returning to COLLECTING",
                phone_number,
            )
            _clear_awaiting_yes_flags(state_manager, phone_number)
            return {
                "messages": ["Let me grab a few more details — what date and time were you thinking?"],
                "new_state": "COLLECTING",
                "actions": [],
            }

        # GOLDEN RULE: MMF + escort-sourced male — block YES until mmf_exploration_tags is set
        # (same policy as COLLECTING; SMS checklist: GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT).
        from booking.mmf_exploration import (
            decode_mmf_exploration_tags,
            escort_organises_male_for_mmf,
            mmf_exploration_sms_prompt,
        )
        _guard_merged = {**(state or {}), **booking_fields}
        if (
            awaiting_yes
            and escort_organises_male_for_mmf(_guard_merged)
            and not decode_mmf_exploration_tags(_guard_merged.get("mmf_exploration_tags"))
        ):
            state_manager.update_fields(
                phone_number,
                {
                    "outcall_awaiting_yes": False,
                    "incall_awaiting_yes": False,
                    "mmf_exploration_prompt_sent": True,
                    "awaiting_yes_set_at": None,
                },
            )
            return {
                "messages": [mmf_exploration_sms_prompt(_guard_merged)],
                "new_state": "COLLECTING",
                "actions": [],
            }

        # --- 30-minute cutoff: reject if booking time is now < 30 min away ---
        from utils.timezone import get_current_datetime
        _now = get_current_datetime()
        _bk_date = booking_fields.get('date')
        _bk_time = booking_fields.get('time')
        if _bk_date and _bk_time:
            import datetime as _dt
            if isinstance(_bk_date, str):
                try:
                    _bk_date = _dt.datetime.strptime(_bk_date[:10], '%Y-%m-%d').date()
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    _bk_date = None
            _bk_hour, _bk_min = parse_booking_hour_minute(_bk_time)
            if _bk_date and _bk_hour is not None:
                _bk_dt = _dt.datetime.combine(_bk_date, _dt.time(_bk_hour, _bk_min))
                try:
                    _bk_dt = _now.tzinfo.localize(_bk_dt) if hasattr(_now, 'tzinfo') and _now.tzinfo and not _bk_dt.tzinfo else _bk_dt
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                _bk_dt_naive = _bk_dt.replace(tzinfo=None) if _bk_dt.tzinfo else _bk_dt
                _now_naive = _now.replace(tzinfo=None) if _now.tzinfo else _now
                _mins_until = (_bk_dt_naive - _now_naive).total_seconds() / 60
                if _mins_until < 30:
                    logger.info(
                        "[CHECKING_AVAILABILITY] Booking time too close (%.1f min away) for %s, rejecting",
                        _mins_until, phone_number,
                    )
                    _clear_awaiting_yes_flags(state_manager, phone_number)
                    _wf_url = get_webform_url(phone_number)
                    _expired_msg = (
                        "Unfortunately your original booking time is no longer available "
                        "as it's too close to the start time.\n\n"
                        "Please use the booking webform to schedule a new date and time:\n"
                        f"{_wf_url}"
                    )
                    return {
                        "messages": [_expired_msg],
                        "new_state": "COLLECTING",
                        "actions": []
                    }

        deposit_amount = state.get('deposit_amount', 100)
        _reason_default = 'outcall' if is_outcall else 'incall'
        _reason_raw = state.get('deposit_reason')
        if isinstance(_reason_raw, str):
            reason = _reason_raw.strip() or _reason_default
        elif _reason_raw is None:
            reason = _reason_default
        else:
            reason = str(_reason_raw).strip() or _reason_default

        if is_outcall:
            from booking.field_validator import FieldValidator
            from config import get_current_incall_location
            from templates.errors import get_enhanced_validation_error

            validator = FieldValidator()
            location = get_current_incall_location() or {}
            city = location.get('city')
            if booking_fields.get('outcall_address'):
                valid, error = validator.validate_outcall_address(
                    booking_fields.get('outcall_address'),
                    'outcall',
                    city=city,
                )
                if not valid:
                    # If we're already at YES-confirmation from a previously built
                    # outcall summary, don't dead-end on re-validation jitter/API misses.
                    if state.get('outcall_awaiting_yes') or _outcall_summary_ctx:
                        logger.warning(
                            "[CHECKING_AVAILABILITY] Skipping blocking outcall re-validation for %s during YES gate: %s",
                            phone_number, error
                        )
                    else:
                        error_message = get_enhanced_validation_error([error], booking_fields)
                        return {"messages": [error_message], "new_state": None, "actions": []}

        deposit_needed = bool(state.get('deposit_required', False))

        # Re-check for group bookings that always require a mandatory deposit,
        # in case deposit_required wasn't pre-calculated (e.g. booking came via
        # the collecting flow which doesn't call calculate_deposit_requirement).
        if not deposit_needed:
            _exp_chk = (booking_fields.get('experience_type') or '').lower()
            _bt_chk = (booking_fields.get('booking_type') or state.get('booking_type') or '').lower()
            _dt_chk = (booking_fields.get('doubles_type') or state.get('doubles_type') or '').lower()
            _is_group = any(
                w in _exp_chk
                for w in ('double', 'threesome', 'couple', 'doubles', 'doubles_mff', 'Doubles MMF', 'mff', 'mmf')
            )
            if _is_group:
                try:
                    from core.rates_from_config import get_deposit_mff_pair
                    deposit_amount = get_deposit_mff_pair()
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    deposit_amount = 200
                deposit_needed = True
                if _bt_chk in ("doubles_mff", "Doubles MMF"):
                    reason = _bt_chk
                elif _dt_chk in ("mff", "mmf"):
                    reason = f"doubles_{_dt_chk}"
                elif "couple" in _exp_chk or "couples" in _bt_chk:
                    reason = "couples_mff"
                else:
                    reason = "doubles_mff"
                state_manager.update_fields(phone_number, {
                    'deposit_required': True,
                    'deposit_amount': deposit_amount,
                    'deposit_reason': reason,
                })

        # Final-confirmation safety net: never rely solely on the persisted
        # deposit_required flag. If state drift cleared it between the summary
        # and the YES reply, recompute from the actual booking fields so
        # mandatory-deposit bookings (especially outcalls) cannot fall through
        # to the no-deposit incall confirmation path.
        if not deposit_needed:
            recomputed_required, recomputed_amount, recomputed_reason = calculate_deposit_requirement(
                booking_fields,
                phone_number,
                state_manager,
            )
            if recomputed_required:
                deposit_needed = True
                deposit_amount = recomputed_amount
                if isinstance(recomputed_reason, str):
                    reason = recomputed_reason.strip() or reason
                elif recomputed_reason is not None:
                    reason = str(recomputed_reason).strip() or reason
                state_manager.update_fields(phone_number, {
                    'deposit_required': True,
                    'deposit_amount': deposit_amount,
                    'deposit_reason': reason,
                })
                logger.error(
                    "[CHECKING_AVAILABILITY] Recovered missing mandatory deposit state for %s "
                    "(incall_outcall=%r, reason=%r)",
                    phone_number,
                    booking_fields.get('incall_outcall'),
                    reason,
                )

        from services.calendar_service import create_calendar_event
        _lock_key = _booking_lock_key(booking_fields, is_outcall=is_outcall)
        _lock_handle = _acquire_booking_lock(_lock_key, max_wait_seconds=10.0)

        result: Any = None
        event_id: str | None = None
        travel_outbound_id = None
        travel_return_id = None

        if _lock_handle is None:
            # Another request may have already created the event (duplicate YES); if so, continue.
            _st_fallback = state_manager.get_state(phone_number) or {}
            _bfs_fb = state_manager.get_booking_fields(phone_number)
            _eid_fb = (
                str(_st_fallback.get("graphite_event_id") or _st_fallback.get("peacock_event_id") or "")
            ).strip()
            if _eid_fb and _finalization_booking_identity_key(
                _bfs_fb
            ) == _finalization_booking_identity_key(booking_fields):
                result = {
                    "event_id": _eid_fb,
                    "travel_outbound_id": _st_fallback.get("travel_outbound_event_id"),
                    "travel_return_id": _st_fallback.get("travel_return_event_id"),
                }
            else:
                return {
                    "messages": [
                        "Sorry, I could not finalise the booking just then (the system was busy). "
                        "Please reply YES again in a few seconds to confirm."
                    ],
                    "new_state": "CHECKING_AVAILABILITY",
                    "actions": [],
                }

        try:
            if _lock_handle is not None:
                # If a concurrent request already finished, reuse that event and skip a duplicate create.
                _st0 = state_manager.get_state(phone_number) or {}
                _bfs0 = state_manager.get_booking_fields(phone_number)
                _eid0 = (str(_st0.get("graphite_event_id") or _st0.get("peacock_event_id") or "")).strip()
                if _eid0 and _finalization_booking_identity_key(
                    _bfs0
                ) == _finalization_booking_identity_key(booking_fields):
                    result = {
                        "event_id": _eid0,
                        "travel_outbound_id": _st0.get("travel_outbound_event_id"),
                        "travel_return_id": _st0.get("travel_return_event_id"),
                    }
                else:
                    # Fresh check at final confirmation to avoid stale TOCTOU decisions.
                    if is_outcall:
                        _fresh_conflict, _ = check_outcall_conflict_with_travel(booking_fields)
                        _slot_available_now = _fresh_conflict == "none"
                    else:
                        _fresh_conflict, _ = check_conflict(booking_fields)
                        _slot_available_now = _fresh_conflict in ("none", "graphite")

                    if not _slot_available_now:
                        _clear_awaiting_yes_flags(state_manager, phone_number)
                        return {
                            "messages": [
                                "Sorry — that time was just taken while we were confirming. "
                                "Please choose another time and I'll check immediately."
                            ],
                            "new_state": "COLLECTING",
                            "actions": [],
                        }

                    if deposit_needed:
                        result = create_calendar_event(
                            booking_fields,
                            phone_number,
                            is_confirmed=False,
                            awaiting_deposit=True,
                            client_name=booking_fields.get('client_name', 'Client'),
                            return_travel_ids=True,
                            deposit_amount=deposit_amount,
                            is_outcall=is_outcall
                        )
                    else:
                        result = create_calendar_event(
                            booking_fields,
                            phone_number,
                            is_confirmed=False,
                            awaiting_deposit=False,
                            client_name=booking_fields.get('client_name', 'Client'),
                            return_travel_ids=False,
                            is_outcall=is_outcall
                        )

            if result is not None:
                if isinstance(result, dict):
                    event_id = result.get('event_id')
                    travel_outbound_id = result.get('travel_outbound_id')
                    travel_return_id = result.get('travel_return_id')
                else:
                    event_id = result
                    travel_outbound_id = None
                    travel_return_id = None
                if event_id:
                    # Persist event ids while still holding the lock so no second request
                    # can create a duplicate calendar event between release and state write.
                    state_updates = {
                        'graphite_event_id': event_id,
                        'peacock_event_id': event_id,
                    }
                    state_updates.update({
                        'outcall_awaiting_yes': False,
                        'incall_awaiting_yes': False,
                        'awaiting_yes_set_at': None,
                    })
                    if travel_outbound_id:
                        state_updates['travel_outbound_event_id'] = travel_outbound_id
                    if travel_return_id:
                        state_updates['travel_return_event_id'] = travel_return_id
                    saved = state_manager.update_fields(phone_number, state_updates)
                    if not saved:
                        _st_after = state_manager.get_state(phone_number) or {}
                        _saved_eid = str(
                            _st_after.get("graphite_event_id") or _st_after.get("peacock_event_id") or ""
                        ).strip()
                        if _saved_eid != str(event_id).strip():
                            logger.error(
                                "[CHECKING_AVAILABILITY] Failed to persist event ids for %s; aborting to avoid inconsistent confirmation.",
                                phone_number,
                            )
                            return {
                                "messages": [
                                    "Sorry, I couldn't finalize that booking safely just now. "
                                    "Please reply YES again in a few seconds."
                                ],
                                "new_state": "CHECKING_AVAILABILITY",
                                "actions": [],
                            }
        finally:
            if _lock_handle is not None:
                _release_booking_lock(_lock_handle)

        # Fail-closed: never confirm or request deposit if calendar event creation failed.
        if not event_id:
            logger.error(
                "[CHECKING_AVAILABILITY] Calendar event creation failed for %s (deposit_needed=%s). "
                "Blocking confirmation/deposit flow to avoid untracked bookings.",
                phone_number,
                deposit_needed,
            )
            _clear_awaiting_yes_flags(state_manager, phone_number)
            _wf_url = get_webform_url(phone_number)
            return {
                "messages": [
                    "Sorry, I couldn't lock that booking into the calendar right now. "
                    f"Please try again in a moment, or use the booking webform: {_wf_url}"
                ],
                "new_state": "CHECKING_AVAILABILITY",
                "actions": [],
            }

        if deposit_needed:
            upload_url = None
            payment_reference = None
            try:
                from core.deposit_upload_tokens import resolve_deposit_upload_and_reference

                upload_url, payment_reference = resolve_deposit_upload_and_reference(
                    phone_number, deposit_amount
                )
            except Exception as e:
                logger.warning("Deposit: failed to generate upload URL for %s: %s", phone_number, e)
            _deposit_total = calculate_price(
                int(booking_fields.get("duration") or 60),
                experience_type=booking_fields.get("experience_type"),
                incall_outcall=booking_fields.get("incall_outcall", "incall"),
                booking_fields=booking_fields,
            )
            state_manager.update_fields(
                phone_number,
                {
                    "deposit_payment_reference": payment_reference,
                    "total_booking_cost": _deposit_total,
                },
            )
            outcall_address = (booking_fields.get('outcall_address') or '').strip() or None
            deposit_message = get_deposit_request_message(
                deposit_amount,
                reason,
                phone_number=phone_number,
                upload_url=upload_url,
                outcall_address=outcall_address if is_outcall else None,
                client_name=(booking_fields.get("client_name") or "").strip() or None,
                booking_fields=booking_fields,
                payment_reference=payment_reference,
            )
            return {
                "messages": [deposit_message],
                "new_state": "DEPOSIT_REQUIRED",
                "actions": ["create_pending_event"]
            }
        else:
            # No deposit — incall YES confirmation creates PEACOCK and confirms
            from utils.timezone import get_current_datetime
            now = get_current_datetime()

            # Idempotency guard — derive a stable token from the booking identity
            _token = (
                f"{phone_number}:"
                f"{booking_fields.get('date', '')}:"
                f"{booking_fields.get('time', '')}:"
                f"{booking_fields.get('duration', '')}"
            )
            _claim_status = _claim_confirmation_token_status(state_manager, phone_number, _token)
            if _claim_status == "error":
                logger.error(
                    "Incall confirmation token claim failed for %s (token=%s)",
                    phone_number,
                    _token,
                )
                return {
                    "messages": [
                        "Sorry, I couldn't safely finalise that confirmation just now. "
                        "Please reply YES again in a few seconds."
                    ],
                    "new_state": "CHECKING_AVAILABILITY",
                    "actions": [],
                }
            if _claim_status != "claimed":
                logger.warning(
                    "Duplicate incall confirmation suppressed for %s (token=%s)",
                    phone_number,
                    _token,
                )
                from templates.confirmations import get_incall_confirmed_message
                _dup_fields = {**booking_fields, 'phone_number': phone_number}
                _dup_price = calculate_price(
                    int(booking_fields.get('duration') or 60),
                    experience_type=booking_fields.get('experience_type'),
                    incall_outcall=booking_fields.get('incall_outcall', 'incall'),
                    booking_fields=booking_fields,
                )
                try:
                    from core.feature_flags import optional_deposit_enabled as _ode_dup

                    if _ode_dup():
                        state_manager.update_fields(phone_number, {"optional_deposit_requested": True})
                except Exception as e:
                    logger.warning("optional_deposit_requested (dup path): %s", e)
                return {
                    "messages": [get_incall_confirmed_message(_dup_fields, _dup_price)],
                    "new_state": "CONFIRMED",
                    "actions": [],
                }

            if event_id:
                state_manager.update_fields(phone_number, {
                    'peacock_event_id': event_id,
                    'peacock_created_at': now.isoformat(),
                    'confirmed_event_id': event_id,
                    'confirmed_at': now,
                    'feedback_request_sent': False,
                })
                # Durable booking history record (no-deposit path, idempotent)
                try:
                    state_manager.append_booking_history(
                        phone_number, booking_fields,
                        confirmed_at=now,
                        deposit_paid=False,
                        total_cost=None,
                    )
                except Exception as _bh_err:
                    logger.warning("append_booking_history (no-deposit path) failed: %s", _bh_err)

            price = calculate_price(
                int(booking_fields.get('duration') or 60),
                experience_type=booking_fields.get('experience_type'),
                incall_outcall=booking_fields.get('incall_outcall', 'incall'),
                booking_fields=booking_fields,
            )
            _cost_updates = {'total_booking_cost': price}
            try:
                from core.feature_flags import optional_deposit_enabled as _ode_ok

                if _ode_ok():
                    _cost_updates['optional_deposit_requested'] = True
            except Exception as e:
                logger.warning("optional_deposit_requested (confirm path): %s", e)
            state_manager.update_fields(phone_number, _cost_updates)

            from templates.confirmations import get_incall_confirmed_message
            booking_fields_with_phone = {**booking_fields, 'phone_number': phone_number}
            confirmation_message = get_incall_confirmed_message(booking_fields_with_phone, price)

            try:
                from services.reminder_service import schedule_booking_reminders
                schedule_booking_reminders(booking_fields, phone_number, state_manager)
            except Exception as e:
                _mark_followup_task_failure(state_manager, phone_number, "schedule_booking_reminders", e)
                logger.warning("Failed to schedule reminders: %s", type(e).__name__)

            if not is_outcall:
                try:
                    from services.reminder_service import schedule_confirmation_30min_followup
                    schedule_confirmation_30min_followup(booking_fields, phone_number, state_manager)
                except Exception as e:
                    _mark_followup_task_failure(state_manager, phone_number, "schedule_confirmation_30min_followup", e)
                    logger.warning("Failed to schedule 30-min confirmation follow-up: %s", type(e).__name__)
                try:
                    from services.room_detail_service import schedule_room_detail_reminder
                    schedule_room_detail_reminder(booking_fields, phone_number, state_manager)
                except Exception as e:
                    _mark_followup_task_failure(state_manager, phone_number, "schedule_room_detail_reminder", e)
                    logger.warning("Failed to schedule room detail reminder: %s", type(e).__name__)

            return {
                "messages": [confirmation_message],
                "new_state": "CONFIRMED",
                "actions": ["create_peacock_event"]
            }

    # Use performance timing for calendar checks
    from utils.performance_timing import PerformanceTimer
    from utils.structured_logging import get_logger

    structured_logger = get_logger("escort_chatbot.availability")

    with PerformanceTimer("calendar_availability_check"):
        if is_outcall:
            conflict_type, events = check_outcall_conflict_with_travel(booking_fields)
        else:
            conflict_type, events = check_conflict(booking_fields)

    structured_logger.info(
        "availability_check_completed",
        phone_number=phone_number,
        conflict_type=conflict_type,
        event_count=len(events or []),
        is_outcall=is_outcall
    )

    # Handle conflicts
    if conflict_type == "confirmed":
        # Overnight / FMTY / dirty weekend: webform + manual handling — not the unified ❌+3 slots UX
        from utils.golden_booking_rules import is_exempt_from_unified_golden_booking_flow

        if is_exempt_from_unified_golden_booking_flow(state, context.get("message") or ""):
            _wf = _webform_url or f"{get_base_url()}/booking"
            return {
                "messages": [
                    f"❌ That time isn't available. "
                    f"For this type of experience please use the booking webform so we can arrange it:\n{_wf}"
                ],
                "new_state": "COLLECTING",
                "actions": [],
            }

        # Hard conflict — same ❌ + up to three alternatives as other booking flows
        from templates import greetings as _greetings

        _req_time = booking_fields.get('time')
        _time_display = ""
        try:
            if isinstance(_req_time, (tuple, list)) and len(_req_time) >= 2:
                _rh = int(_req_time[0])
                _rm = int(_req_time[1])
                _rp = "pm" if _rh >= 12 else "am"
                _rh12 = _rh % 12 or 12
                _time_display = f"{_rh12}:{_rm:02d}{_rp}" if _rm else f"{_rh12}{_rp}"
            elif isinstance(_req_time, int):
                _time_display = _greetings.format_time_simple(_req_time, 0)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        _loc_ac = get_current_incall_location()
        _purl_ac = (get_profile_url() or "").strip()
        _client_nm = (booking_fields.get("client_name") or "").strip()
        _unavail_msg, _ = _greetings.build_booking_time_unavailable_message(
            booking_fields,
            _time_display if _time_display else "that time",
            city=(_loc_ac.get("city") or ""),
            hotel_name=(_loc_ac.get("hotel_name") or ""),
            address=(_loc_ac.get("address") or ""),
            client_name=_client_nm,
            is_outcall=is_outcall,
            escort_name=get_escort_name(),
            webform_url=_webform_url or "",
            profile_url=_purl_ac,
        )
        return {
            "messages": [_unavail_msg],
            "new_state": "COLLECTING",
            "actions": []
        }

    elif conflict_type == "peacock":
        # PEACOCK conflict — treat as a hard block. Offer alternative slots if any,
        # otherwise tell the client the time isn't available and ask for another.
        alternatives = find_alternative_slots(booking_fields, max_results=3)
        if alternatives:
            alt_message = get_conflict_alternatives_message(
                alternatives, booking_fields, webform_url=_webform_url
            )
        else:
            alt_message = (
                "❌ Sorry, that time slot isn't available. "
                "Could you suggest another date/time that works for you?"
            )

        return {
            "messages": [alt_message],
            "new_state": None,  # Stay in CHECKING_AVAILABILITY
            "actions": []
        }

    elif conflict_type == "none":
        # Available! Check if deposit required
        deposit_required, deposit_amount, reason = calculate_deposit_requirement(
            booking_fields,
            phone_number,
            state_manager
        )
        if isinstance(reason, str):
            reason = reason.strip()
        elif reason is None:
            reason = ""
        else:
            reason = str(reason).strip()

        # If client already confirmed with YES + valid name and declined experience,
        # auto-complete confirmation without prompting for experience again.
        # Safety gate (C4): only legitimate no-experience path is dinner_date. For any
        # other booking_type, refuse to auto-confirm with experience_type=None (would
        # corrupt the calendar event + SMS template) and prompt for it instead.
        # Also defensively clear the flag so the second pass never re-enters this branch.
        if _auto_confirm_without_experience and not booking_fields.get('experience_type'):
            _booking_type_for_autoconfirm = (state.get('booking_type') or '').strip().lower()
            if _booking_type_for_autoconfirm == 'dinner_date':
                # GOLDEN RULE: name must NEVER block confirmation. We auto-confirm
                # the dinner_date YES path regardless of whether we have a parsed
                # client_name; downstream templates fall back to a generic
                # placeholder if the name is missing.
                state_manager.mark_awaiting_confirmation(
                    phone_number,
                    is_outcall=bool(is_outcall),
                    deposit_required=bool(deposit_required),
                    deposit_amount=deposit_amount,
                    deposit_reason=reason,
                    extra={"auto_confirm_without_experience": False},
                )
                _next_ctx = dict(context)
                _next_ctx['state'] = state_manager.get_state(phone_number) or {}
                return handle_check_availability(_next_ctx)
            else:
                logger.info(
                    "[CHECKING_AVAILABILITY] Refusing auto-confirm without experience_type for %s "
                    "(booking_type=%r); prompting client and returning to COLLECTING",
                    phone_number, _booking_type_for_autoconfirm,
                )
                state_manager.update_fields(phone_number, {
                    'auto_confirm_without_experience': False,
                    'outcall_awaiting_yes': False,
                    'incall_awaiting_yes': False,
                })
                return {
                    "messages": [
                        "One more thing before I can confirm — which experience would you like? "
                        "(e.g. GFE, PSE, BSE, MSOG)"
                    ],
                    "new_state": "COLLECTING",
                    "actions": [],
                }

        # Manual review gate still depends on overnight classification.
        booking_type = state.get('booking_type', '')
        try:
            _dur_min = int(booking_fields.get("duration") or 0)
        except (TypeError, ValueError):
            _dur_min = 0
        is_overnight = booking_type == 'overnight' or _dur_min >= 240  # 4+ hours

        # Check if booking requires manual review (overnight, fly-me-to-you)
        client_message = context.get('message', '').lower()
        is_fly_me = 'fly' in client_message if client_message else False

        # Check for fly-me-to-you keywords in booking fields
        experience_lower = (booking_fields.get('experience_type', '') or '').lower()
        is_fly_me = is_fly_me or 'fly me' in experience_lower or 'fly-me' in experience_lower or 'fmty' in experience_lower

        if is_overnight or is_fly_me:
            # Forward to escort for manual review
            from config import get_escort_phone_number
            from services.sms_service import send_escort_sms

            booking_type_label = "Overnight" if is_overnight else "Fly-Me-To-You"
            _escort_phone = get_escort_phone_number()

            # Send notification to escort
            if _escort_phone:
                notification_msg = (
                    f"\U0001F514 {booking_type_label} Booking Request - Manual Review Required\n\n"
                    f"Client: {booking_fields.get('client_name', 'Client')}\n"
                    f"Phone: {phone_number}\n"
                    f"Date: {booking_fields.get('date', 'TBA')}\n"
                    f"Time: {booking_fields.get('time', 'TBA')}\n"
                    f"Duration: {booking_fields.get('duration', 0)} minutes\n"
                    f"Experience: {booking_fields.get('experience_type', 'Not specified')}\n"
                    f"Location: {booking_fields.get('incall_outcall', 'incall')}\n\n"
                    f"Please review and confirm manually."
                )
                send_escort_sms(_escort_phone, notification_msg, category='special_bookings')

            # Inform client (use escort name from config)
            client_message = FORWARD_TO_ESCORT_NOTICE.format(
                booking_type_label=booking_type_label.lower(),
                escort_name=get_escort_name(),
            )
            state_manager.update_fields(phone_number, {
                "manual_review_required": True,
                "booking_status": BOOKING_STATUS_MANUAL_REVIEW_PENDING,
            })

            return {
                "messages": [client_message],
                "new_state": "MANUAL_REVIEW_PENDING",
                "actions": ["forward_to_escort"]
            }

        if deposit_required:
            # Need deposit before confirming — send reconfirmation first, create
            # calendar event only after client responds YES (both outcall AND incall).
            state_manager.update_fields(phone_number, {
                'deposit_required': True,
                'deposit_amount': deposit_amount,
                'deposit_reason': reason,
                'deposit_payment_reference': None,
            })

            from templates.booking_reconfirmation import (
                build_available_now_outcall_reconfirmation,
                build_booking_reconfirmation,
                build_incall_preconfirm_summary,
            )
            booking_fields_with_phone = {**booking_fields, 'phone_number': phone_number}

            if is_outcall:
                _set_awaiting_yes_flags(state_manager, phone_number, is_outcall=True)
                if is_available_now:
                    reconfirmation_msg = build_available_now_outcall_reconfirmation(
                        booking_fields_with_phone, _webform_url
                    )
                    if state.get('_outcall_verification_skipped_available_now'):
                        reconfirmation_msg += "\n\nI wasn't able to fully verify that hotel, but I'm happy to come – please have your full address and room number ready when I'm on my way."
                        state_manager.update_fields(phone_number, {'_outcall_verification_skipped_available_now': False})
                else:
                    reconfirmation_msg = build_booking_reconfirmation(booking_fields_with_phone)
            else:
                # Incall with deposit — also wait for YES before creating calendar event
                _set_awaiting_yes_flags(state_manager, phone_number, is_outcall=False)
                reconfirmation_msg = build_incall_preconfirm_summary(booking_fields_with_phone, webform_url=_webform_url)

            return {
                "messages": [reconfirmation_msg],
                "new_state": "CHECKING_AVAILABILITY",
                "actions": []
            }
        else:
            # No deposit needed — send reconfirmation summary, wait for YES
            from templates.booking_reconfirmation import build_incall_preconfirm_summary
            _set_awaiting_yes_flags(state_manager, phone_number, is_outcall=False)
            booking_fields_with_phone = {**booking_fields, 'phone_number': phone_number}
            reconfirmation_msg = build_incall_preconfirm_summary(booking_fields_with_phone, webform_url=_webform_url)

            return {
                "messages": [reconfirmation_msg],
                "new_state": "CHECKING_AVAILABILITY",
                "actions": []
            }

    else:  # "unknown"
        return {
            "messages": [get_error_message('calendar_unavailable')],
            "new_state": "COLLECTING",
            "actions": []
        }


def handle_unknown_in_checking(context: dict[str, Any]) -> dict[str, Any]:
    """
    State-level fallback for CHECKING_AVAILABILITY.
    Fires when the client sends something unrecognized while waiting to confirm.
    Uses the template reminder to reply YES. AI is not used here — the client
    is mid-booking and needs to confirm, not get distracted.
    """
    state = context.get('state') or {}
    # Safety net: if classifier misses a valid YES/name/experience combo while
    # awaiting confirmation, route it through normal confirmation handler.
    try:
        msg = (context.get("message") or "").strip().lower()
        words = [w for w in msg.split() if w]
        awaiting = bool(state.get("outcall_awaiting_yes") or state.get("incall_awaiting_yes"))
        yes_words = {"yes", "yep", "yeah", "y", "ya", "yee", "yss", "yaa", "confirm"}
        exp_words = {"gfe", "pse", "dgfe"}
        non_name_words = {
            "ok", "okay", "sure", "hi", "hello", "thanks", "thank", "cancel", "stop",
            "mate", "cool", "done", "great", "good", "nice", "please", "noted", "got",
            "right", "sounds", "fine", "perfect", "awesome", "cheers", "later", "bye",
        }
        from templates import greetings as _greetings_name
        is_name_like = (
            len(words) <= 2
            and all(w.isalpha() for w in words)
            and msg not in non_name_words
            and _greetings_name.is_valid_client_name(msg)
        )
        is_combo = (
            awaiting
            and (
                any(w in yes_words for w in words)
                or any(w in exp_words for w in words)
                or is_name_like
            )
        )
        if is_combo:
            return handle_check_availability(context)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)

    # Hybrid loop-break detector (ambiguity-only): this state fallback only runs when
    # normal classifier/routing did not identify a stronger intent.
    try:
        awaiting = bool(state.get("outcall_awaiting_yes") or state.get("incall_awaiting_yes"))
        if awaiting:
            from services.hybrid_nlp_detector import HybridNLPDetector

            detector = HybridNLPDetector(ai_service=context.get("ai_service"))
            flow_hint = detector.detect_flow_shift(
                message=context.get("message", ""),
                state=state,
                history=context.get("message_history"),
            )
            if flow_hint.accepted and flow_hint.hint is not None:
                label = (flow_hint.hint.shift_label or "").strip().lower()
                if label in ("cancel", "modify", "confirm"):
                    return handle_check_availability(context)

            deposit_hint = detector.detect_deposit_intent(
                message=context.get("message", ""),
                state=state,
                history=context.get("message_history"),
            )
            if deposit_hint.accepted and deposit_hint.hint is not None:
                deposit_label = (deposit_hint.hint.intent or "").strip().lower()
                if deposit_label in ("resistance", "question"):
                    if bool(state.get("deposit_required")):
                        msg = (
                            "I understand. A deposit is required for this booking before I can lock it in. "
                            "If you'd rather cancel, just say cancel booking."
                        )
                    else:
                        msg = (
                            "No deposit is being requested at this step. "
                            "Reply YES to confirm this slot, or send a change."
                        )
                    return {"messages": [msg], "new_state": None, "actions": []}

            legacy_hint = detector.detect_loop_break(
                message=context.get("message", ""),
                state=state,
                history=context.get("message_history"),
            )
            if legacy_hint.accepted and legacy_hint.hint is not None:
                legacy_label = (legacy_hint.hint.shift_label or "").strip().lower()
                if legacy_label in ("cancel", "modify"):
                    return handle_check_availability(context)
                if legacy_label == "deposit_resistance":
                    if bool(state.get("deposit_required")):
                        msg = (
                            "I understand. A deposit is required for this booking before I can lock it in. "
                            "If you'd rather cancel, just say cancel booking."
                        )
                    else:
                        msg = (
                            "No deposit is being requested at this step. "
                            "Reply YES to confirm this slot, or send a change."
                        )
                    return {"messages": [msg], "new_state": None, "actions": []}
                if legacy_label == "frustration":
                    try:
                        from main_v2.conversation_guards import check_frustration

                        frustration_result = check_frustration(
                            context.get("message", ""),
                            context.get("phone_number", ""),
                            state,
                            context.get("state_manager"),
                        )
                        if frustration_result is not None:
                            return frustration_result
                    except Exception as e:
                        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    except Exception as e:
        logger.warning("Hybrid loop-break (CHECKING) failed: %s", e, exc_info=False)

    client_name = (state.get('client_name') or '').strip()
    name_str = f" {client_name}" if client_name else ""
    return {
        "messages": [CONFIRM_BOOKING_REMINDER.format(name_str=name_str)],
        "new_state": None,
        "actions": []
    }


def handle_manual_review_pending(context: dict[str, Any]) -> dict[str, Any]:
    """State-level fallback while booking is waiting for escort manual review."""
    return {
        "messages": [
            "I've forwarded your booking request for manual review. "
            "I'll message you as soon as it's approved or if I need anything else."
        ],
        "new_state": None,
        "actions": [],
    }
