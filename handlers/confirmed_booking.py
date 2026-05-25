"""

CONFIRMED state handler - Post-confirmation interactions.
Handles modifications, cancellations, and general queries after booking is confirmed.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
from datetime import datetime, timedelta
from typing import Any

from core.feature_flags import optional_deposit_enabled
from templates.confirmed_booking_messages import (
    GREAT_DEPOSIT_PREFIX,
    IMAGE_DOWNLOAD_FAILED,
    OPTIONAL_DEPOSIT_SCREENSHOT_PROMPT,
    YOURE_WELCOME_SEE_YOU_SOON,
)

logger = logging.getLogger("adella_chatbot.handlers.confirmed_booking")


def handle_greeting(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle greeting in CONFIRMED state.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    booking_fields = context['state_manager'].get_booking_fields(context['phone_number'])

    # Show booking reminder
    from templates.confirmations import format_booking_summary

    summary = format_booking_summary(booking_fields)
    name_part = f" {booking_fields.get('client_name')}" if booking_fields.get('client_name') else ""
    message = f"Hi{name_part}! Your booking is confirmed:\n\n{summary}\n\nLooking forward to seeing you!"

    return {
        "messages": [message],
        "new_state": None,  # Stay in CONFIRMED
        "actions": []
    }


def handle_modify_booking(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle booking modification request.

    Flow:
    1. Ask what they want to change
    2. Transition to COLLECTING
    3. Keep existing peacock/confirmed event until new booking confirmed

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']

    logger.info(f"Client {phone_number} requested modification")

    try:
        from templates.confirmations import format_booking_summary

        booking_fields = state_manager.get_booking_fields(phone_number) or {}
        summary = format_booking_summary(booking_fields).strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        summary = ""

    if summary:
        message = (
            "Your current booking is:\n\n"
            f"{summary}\n\n"
            "What would you like to change? (date, time, duration, etc.)\n\n"
            "Or just tell me your new preferred date and time."
        )
    else:
        message = "What would you like to change? (date, time, duration, etc.)\n\n"
        message += "Or just tell me your new preferred date and time."

    return {
        "messages": [message],
        "new_state": "COLLECTING",
        "actions": []
    }


_CONFIRMED_RESCHEDULE_RE = re.compile(
    r"\b(reschedule|rescheduled|change\s+(it\s+)?to|move\s+(it\s+)?to|"
    r"different\s+(time|day|date))\b",
    re.IGNORECASE,
)

def handle_reschedule(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle conversational reschedule intent in CONFIRMED state.
    Prompts user for new date/time and transitions to COLLECTING.
    """
    logger.info("confirmed_booking: reschedule intent detected, routing to COLLECTING")
    return {
        "messages": ["Of course! What date and time works better for you?"],
        "new_state": "COLLECTING",
        "actions": [],
    }

def handle_cancel_booking(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle booking cancellation.

    Flow:
    1. Delete calendar events (confirmed + travel if outcall)
    2. Clear booking state
    3. Send cancellation confirmation
    4. Transition to NEW

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    state = context['state']
    state_manager = context['state_manager']
    message = context.get('message', '')

    logger.info(f"Client {phone_number} cancelled confirmed booking")

    # Get event IDs
    confirmed_event_id = state.get('confirmed_event_id') or state.get('peacock_event_id')
    travel_outbound_id = state.get('travel_outbound_event_id')
    travel_return_id = state.get('travel_return_event_id')

    # Delete calendar events
    from services.calendar_service import delete_calendar_event

    events_deleted = []

    if confirmed_event_id:
        if delete_calendar_event(confirmed_event_id):
            events_deleted.append("booking")

    if travel_outbound_id:
        if delete_calendar_event(travel_outbound_id):
            events_deleted.append("travel_outbound")

    if travel_return_id:
        from services.calendar.travel_blocks import split_travel_return_event_ids

        for tid in split_travel_return_event_ids(travel_return_id):
            if delete_calendar_event(tid):
                events_deleted.append("travel_return")

    logger.info(f"Deleted events for {phone_number}: {events_deleted}")

    # Clear booking
    state_manager.clear_booking(phone_number)

    # Use template for cancellation message
    from templates.utility_templates import get_cancellation_confirmed_message, get_cancellation_with_credit_message
    
    # Check if deposit was paid
    if state.get('deposit_paid'):
        deposit_amount = state.get('deposit_amount', 0)
        message = get_cancellation_with_credit_message(deposit_amount)
    else:
        message = get_cancellation_confirmed_message()

    return {
        "messages": [message],
        "new_state": "NEW",
        "actions": ["delete_calendar_events"]
    }


def handle_ask_rates(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle rate inquiry while booking confirmed.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    from core.rates_from_config import format_extended_rates_message, format_rates_message
    rates_message = format_rates_message(include_extended=False) + "\n\n" + format_extended_rates_message() + "\n\nYour current booking is already confirmed. Let me know if you want to change the duration!"

    return {
        "messages": [rates_message],
        "new_state": None,  # Stay in CONFIRMED
        "actions": []
    }


def handle_service_inquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle service-related questions.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    from templates.service_descriptions import get_all_experiences_description
    
    message = get_all_experiences_description(include_profile_link=True)
    message += "\n\nYour booking is confirmed! Looking forward to our time together."

    return {
        "messages": [message],
        "new_state": None,
        "actions": []
    }


def handle_doubles_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle doubles inquiry after booking confirmed.

    Args:
        context: Context dict

    Returns:
        Dict with messages, new_state, actions
    """
    intro = """I do offer doubles and threesomes!

However, you already have a confirmed booking. Would you like to:
1. Change your current booking to a doubles session?
2. Book a separate doubles session?

Let me know and I'll help you arrange it!"""

    from core.rates_from_config import (
        format_doubles_couples_group_rates_message,
        format_extended_rates_message,
        format_rates_message,
    )

    rates_bundle = (
        "\n\n"
        + format_rates_message(include_extended=True)
        + "\n\n"
        + format_extended_rates_message()
        + "\n\n"
        + format_doubles_couples_group_rates_message()
    )

    return {
        "messages": [intro + rates_bundle],
        "new_state": None,
        "actions": []
    }


def handle_provide_field(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle generic messages in CONFIRMED state.

    Args:
        context: Handler context

    Returns:
        Dict with messages, new_state, actions
    """
    raw_msg = context.get("message") or ""
    message = raw_msg.lower()
    state = context['state']
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    stripped = raw_msg.strip().lower()

    if stripped in {"k", "kk", "ok", "okay"} or (stripped and re.fullmatch(r"[^\w]+", stripped)):
        return {
            "messages": [
                "Got you — your booking is still confirmed. If you want any change, send the new date/time and duration."
            ],
            "new_state": None,
            "actions": [],
        }

    if re.search(r"\b(are you real|is this real|are you a bot|are you human)\b", message):
        return {
            "messages": [
                "Yep — this number is actively monitored. Your booking is confirmed, and I can help with any changes if needed."
            ],
            "new_state": None,
            "actions": [],
        }

    if re.search(r"[\u0400-\u052f\u0600-\u06ff\u0900-\u097f\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]", raw_msg):
        return {
            "messages": [
                "I can help fastest in English. If you want to change your confirmed booking, send your preferred date, time, and duration."
            ],
            "new_state": None,
            "actions": [],
        }

    # Block unrealistic shortening attempts on already-confirmed bookings (novel QA NE025).
    import config as cfg
    from booking.field_collector import FieldCollector

    fc = FieldCollector(cfg, ai_service=context.get("ai_service"))
    parsed_short_dur = fc._parse_duration(raw_msg)
    if parsed_short_dur is not None and parsed_short_dur < 60 and re.search(
        r"\b(change|changing|shorten|length|duration|booking|appointment)\b", message
    ) and re.search(r"\b(minute|min|hour|hr)s?\b", message):
        return {
            "messages": [
                "I can't change a confirmed booking to under 1 hour — that's below my usual minimum session length. "
                "Your current booking stays as it is unless you tell me a different change you'd like."
            ],
            "new_state": None,
            "actions": [],
        }

    # Check if this is a response to optional deposit request
    optional_deposit_requested = state.get('optional_deposit_requested', False)
    optional_deposit_paid = state.get('optional_deposit_paid', False)

    # Handle optional deposit responses
    if (
        optional_deposit_enabled()
        and optional_deposit_requested
        and not optional_deposit_paid
    ):
        # Check for decline responses
        decline_keywords = [
            'no thanks', 'no thank you', 'skip', 'decline', 'not interested',
            'no deposit', "don't want", "don't need",
            'cash', 'pay cash', 'prefer cash', 'prefer to pay', 'paying cash',
            'in cash', 'rather pay', 'will pay cash', 'would prefer', 'prefer not',
            "won't be", "wont be", 'not paying', 'no need',
            "don't wish", "dont wish", "won't pay", "wont pay", "not paying deposit",
            "don't pay", "dont pay", "wont pay deposit", "won't pay deposit",
        ]
        if any(keyword in message for keyword in decline_keywords):
            state_manager.update_fields(phone_number, {
                'optional_deposit_requested': False,
            })
            from templates.optional_deposit import get_optional_deposit_declined_message
            return {
                "messages": [get_optional_deposit_declined_message()],
                "new_state": None,
                "actions": []
            }

        # Check for accept/payment responses (avoid bare "ok"/"yes" — they match innocent phrases like "ok I'll pay cash")
        strong_accept_keywords = [
            'will pay', 'paying', 'paid', 'sending', 'deposit', 'screenshot',
            'payment', 'transfer', 'payid', 'sent ', 'just sent', 'bank',
        ]
        short_accept_re = re.compile(
            r'^\s*(yes|yep|yeah|ok|okay|sure)\s*[!?.]?\s*$',
            re.IGNORECASE,
        )
        wants_pay = bool(short_accept_re.match((context.get('message') or '').strip())) or any(
            kw in message for kw in strong_accept_keywords
        )

        if wants_pay:
            # Check if they're sending a screenshot
            media_urls = context.get('media_urls', [])
            if media_urls:
                # They're sending a screenshot - process it
                return handle_optional_deposit_screenshot(context)
            else:
                # They said yes but no screenshot yet - remind them using non-mandatory deposit template
                from templates.deposit_templates import build_deposit_message
                deposit_message = build_deposit_message(
                    mandatory=False,
                    followup=False,
                    phone_number=phone_number
                )
                return {
                    "messages": [GREAT_DEPOSIT_PREFIX + deposit_message],
                    "new_state": None,
                    "actions": []
                }

    # Check for common intents
    if re.search(r"\b(cheaper|discount|negotiate|lower(?:\s+the)?\s+price|better\s+rate)\b", message):
        return handle_rate_negotiation(context)

    if re.search(r"\b(book\s+again|another\s+booking|new\s+booking|book\s+another)\b", message):
        return {
            "messages": [
                "Absolutely — happy to set up another booking. Send your preferred date, time, and duration."
            ],
            "new_state": "COLLECTING",
            "actions": [],
        }

    if any(word in message for word in ['thanks', 'thank you', 'perfect', 'great', 'awesome']):
        return {
            "messages": [YOURE_WELCOME_SEE_YOU_SOON],
            "new_state": None,
            "actions": []
        }

    elif any(word in message for word in ['when', 'time', 'date', 'reminder']):
        # Show booking details
        return handle_greeting(context)

    else:
        _OUTCALL_KWS = (
            'outcall', 'out call', 'come to me', 'come to my', 'my place',
            'my hotel', 'my apartment', 'my airbnb', 'my room', 'staying at',
            'can you come', 'you come to', 'come see me', 'come and see me', 'see me',
            'visit me', 'come over',
        )
        _has_new_time = bool(re.search(r'\b\d{1,2}(:\d{2})?\s*(am|pm)\b', message))
        _has_outcall = any(kw in message for kw in _OUTCALL_KWS)
        if _has_new_time or _has_outcall:
            # Client wants to rebook or switch to outcall \u2014 transition to COLLECTING
            return handle_modify_booking(context)

        # AI-first for casual follow-ups after confirmation (no hard cap).
        try:
            from handlers.ai_fallback import get_ai_confirmed_booking_response

            ai_reply = get_ai_confirmed_booking_response(context)
            if ai_reply:
                state_manager.update_fields(
                    phone_number,
                    {"confirmed_ai_reply_count": int(state.get("confirmed_ai_reply_count") or 0) + 1},
                )
                return {"messages": [ai_reply], "new_state": None, "actions": []}
        except Exception as e:
            logger.warning("AI confirmed-state reply failed: %s", e)
        return {
            "messages": ["Your booking is confirmed! If you need to change or cancel, just let me know."],
            "new_state": None,
            "actions": [],
        }


def handle_optional_deposit_screenshot(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle optional deposit screenshot for confirmed incall bookings.
    
    Args:
        context: Context dict with phone_number, media_urls, state, etc.
        
    Returns:
        Dict with messages, new_state, actions
    """
    phone_number = context['phone_number']
    if not optional_deposit_enabled():
        return {
            "messages": ["Your booking is already confirmed and no deposit is required."],
            "new_state": None,
            "actions": []
        }

    media_urls = context.get('media_urls', [])
    state = context['state']
    state_manager = context['state_manager']

    # Check if screenshot provided
    if not media_urls:
        return {
            "messages": [OPTIONAL_DEPOSIT_SCREENSHOT_PROMPT],
            "new_state": None,
            "actions": []
        }

    # Get deposit amount
    try:
        from core.rates_from_config import get_deposit_incall
        deposit_amount = get_deposit_incall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        deposit_amount = 50

    # Validate screenshot
    from handlers.deposit_flow import _download_media_bytes_safe
    from services.vision_service import validate_deposit_screenshot_from_bytes

    image_content = _download_media_bytes_safe(media_urls[0], log_prefix="confirmed_booking")
    if image_content is None:
        return {
            "messages": [IMAGE_DOWNLOAD_FAILED],
            "new_state": None,
            "actions": []
        }

    result = validate_deposit_screenshot_from_bytes(
        image_content,
        phone_number,
        required_amount=deposit_amount,
        expected_reference=(state.get('deposit_payment_reference') or '').strip() or None,
        require_payment_reference=False,
    )

    if result['valid']:
        # Valid optional deposit - mark as paid
        logger.info(f"Optional deposit received from {phone_number}")
        
        # Use the actual amount extracted from the screenshot, not the expected amount
        actual_deposit_amount = result['deposit_amount']
        
        pay_ref = (state.get('deposit_payment_reference') or '').strip() or None
        state_manager.update_fields(phone_number, {
            'optional_deposit_paid': True,
            'optional_deposit_amount': actual_deposit_amount,
            'optional_deposit_paid_at': datetime.now().isoformat(),
        })

        # Update calendar to BASIL with payment reference (all experience types): confirm in place first.
        confirmed_event_id = state.get('confirmed_event_id')
        if confirmed_event_id:
            booking_fields = state_manager.get_booking_fields(phone_number)
            is_outcall = booking_fields.get('incall_outcall') == 'outcall'
            client_nm = booking_fields.get('client_name', 'Client')
            exp = booking_fields.get('experience_type')
            exp_s = str(exp).strip() if exp else None

            total_cost_opt = state.get('total_booking_cost')
            if total_cost_opt is None:
                try:
                    from templates.confirmations import calculate_price

                    total_cost_opt = calculate_price(
                        int(booking_fields.get('duration') or 60),
                        experience_type=booking_fields.get('experience_type'),
                        incall_outcall=booking_fields.get('incall_outcall', 'incall'),
                        booking_fields=booking_fields,
                    )
                except Exception:
                    total_cost_opt = None

            calendar_ok = False
            try:
                from services.calendar_service import confirm_calendar_event, confirm_travel_time_blocks

                travel_out = state.get('travel_outbound_event_id')
                travel_ret = state.get('travel_return_event_id')

                travel_step_ok = True
                if is_outcall and (travel_out or travel_ret):
                    travel_step_ok = bool(
                        confirm_travel_time_blocks(
                            travel_out,
                            travel_ret,
                        )
                    )
                if travel_step_ok:
                    calendar_ok = bool(
                        confirm_calendar_event(
                            confirmed_event_id,
                            actual_deposit_amount,
                            client_nm,
                            is_outcall=is_outcall,
                            experience_type=exp_s,
                            payment_reference=pay_ref,
                            total_booking_cost=total_cost_opt,
                        )
                    )
                if calendar_ok:
                    logger.info(
                        "Optional deposit: calendar confirmed in-place id=%s phone=%s",
                        confirmed_event_id,
                        phone_number,
                    )
            except Exception as _opt_cal_err:
                logger.warning(
                    "Optional deposit in-place calendar confirm failed; will recreate: %s",
                    _opt_cal_err,
                    exc_info=False,
                )

            if not calendar_ok:
                from services.calendar_service import create_calendar_event, delete_calendar_event

                if delete_calendar_event(confirmed_event_id):
                    logger.info(
                        "Deleted prior calendar event %s before BASIL recreate (optional deposit)",
                        confirmed_event_id,
                    )

                cal_result = create_calendar_event(
                    booking_fields,
                    phone_number,
                    is_confirmed=True,
                    awaiting_deposit=False,
                    client_name=client_nm,
                    return_travel_ids=is_outcall,
                    is_outcall=is_outcall,
                    deposit_amount=actual_deposit_amount,
                    payment_reference=pay_ref,
                    total_booking_cost=total_cost_opt,
                )

                if cal_result:
                    event_id = (
                        cal_result.get('event_id')
                        if isinstance(cal_result, dict)
                        else cal_result
                    )
                    updates_ev = {
                        'confirmed_event_id': event_id,
                        'graphite_event_id': None,
                        'peacock_event_id': None,
                    }
                    if isinstance(cal_result, dict):
                        tid_o = cal_result.get('travel_outbound_id')
                        tid_r = cal_result.get('travel_return_id')
                        if tid_o:
                            updates_ev['travel_outbound_event_id'] = tid_o
                        if tid_r:
                            updates_ev['travel_return_event_id'] = tid_r
                    state_manager.update_fields(phone_number, updates_ev)
                    logger.info(f"Created BASIL event {event_id} for optional deposit")
                else:
                    logger.error("Failed to create BASIL event for optional deposit")
            else:
                state_manager.update_fields(phone_number, {
                    'graphite_event_id': None,
                    'peacock_event_id': None,
                })

        # Optional deposit accepted – keep response simple and inline
        confirmation_msg = (
            "Thank you – your optional deposit has been received and noted. "
            "Your booking remains confirmed and I’m looking forward to seeing you."
        )
        return {
            "messages": [confirmation_msg],
            "new_state": None,
            "actions": []
        }
    else:
        # Invalid screenshot
        error_message = "I couldn't verify your deposit screenshot. "
        if not result['details'].get('payid_found'):
            error_message += "Please ensure the PayID is visible. "
        if not result['details'].get('amount_found'):
            error_message += f"Please ensure the amount ${deposit_amount} is visible. "
        if not result['details'].get('date_found'):
            error_message += "Please ensure today's date is visible. "
        
        error_message += "\n\nYou can try again or simply skip - your booking is already confirmed!"
        
        return {
            "messages": [error_message],
            "new_state": None,
            "actions": []
        }


def handle_check_appointment_passed(context: dict[str, Any]) -> dict[str, Any]:
    """
    Check if appointment time has passed - transition to POST_BOOKING.

    This should be called periodically or on each message.

    Args:
        context: Context dict

    Returns:
        Dict with new_state if appointment passed, None otherwise
    """
    state = context['state']
    phone_number = context['phone_number']

    # Get booking datetime
    confirmed_at = state.get('confirmed_at')
    booking_date = state.get('date')
    booking_time = state.get('time')

    if not confirmed_at or not booking_date or not booking_time:
        return {"messages": [], "new_state": None, "actions": []}

    # Calculate appointment end time
    try:
        import pytz

        # Combine date and time
        if isinstance(booking_time, tuple):
            hour, minute = booking_time
        else:
            # Parse time string
            hour, minute = 12, 0  # Default

        # Get duration
        duration_mins = state.get('duration', 60)

        # Create appointment datetime (Location tab: saved ``timezone`` + city)
        try:
            from utils.timezone import get_local_timezone

            tz = get_local_timezone()
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            import pytz
            from config import DEFAULT_TIMEZONE

            tz = pytz.timezone(DEFAULT_TIMEZONE)
        appointment_start = tz.localize(datetime.combine(booking_date, datetime.min.time().replace(hour=hour, minute=minute)))
        appointment_end = appointment_start + timedelta(minutes=duration_mins)

        # Check if appointment has passed
        now = datetime.now(tz)

        if now > appointment_end:
            logger.info(f"Appointment passed for {phone_number}, transitioning to POST_BOOKING")
            return {
                "messages": [],
                "new_state": "POST_BOOKING",
                "actions": ["transition_post_booking"]
            }

    except Exception as e:
        logger.error(f"Error checking appointment time: {e}")

    return {"messages": [], "new_state": None, "actions": []}


def handle_rate_negotiation(context: dict[str, Any]) -> dict[str, Any]:
    """Handle rate negotiation attempt in CONFIRMED state — politely decline, show booking recap."""
    booking_fields = context['state_manager'].get_booking_fields(context['phone_number'])
    from templates.confirmations import format_booking_summary
    summary = format_booking_summary(booking_fields)
    return {
        "messages": [
            f"My rates are set and I'm unable to negotiate 😊\n\nYour booking is confirmed:\n\n{summary}"
        ],
        "new_state": None,
        "actions": [],
    }


def handle_goodbye(context: dict[str, Any]) -> dict[str, Any]:
    """Handle farewell in CONFIRMED state — booking stays confirmed, just say goodbye."""
    booking_fields = context['state_manager'].get_booking_fields(context['phone_number'])
    from templates.confirmations import format_booking_summary
    summary = format_booking_summary(booking_fields)
    return {
        "messages": [f"Take care! Looking forward to seeing you \U0001f600\n\n{summary}"],
        "new_state": None,
        "actions": [],
    }
