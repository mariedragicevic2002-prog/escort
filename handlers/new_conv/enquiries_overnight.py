# ruff: noqa: F401,F403,F405
from handlers.new_conv._shared import *  # noqa: F401,F403
from typing import Any

import logging

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.enquiries")


def handle_overnight_enquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle extended experience enquiries (Overnight, Dirty Weekend, Fly Me To You).

    These are high-value bookings — the chatbot does NOT process them. Instead it sends
    the escort an immediate notification and tells the client she will personally be in touch.
    """
    state = context['state']
    state_manager = context['state_manager']
    phone_number = context['phone_number']
    message = context.get('message', '')

    from config import get_escort_phone_number, get_escort_name

    msg_lower = message.lower()
    if any(k in msg_lower for k in ('filming', 'video shoot', 'content shoot', 'recording session', 'recorded session')):
        booking_type_label = "Filming Session"
        booking_type_key = "filming"
        duration_minutes = 120
        default_loc = "incall"
        experience_type_value = "pse_filming"
    elif any(k in msg_lower for k in ('fly me', 'fmty', 'fly me to you', 'fly you', 'fly out')):
        booking_type_label = "Fly Me To You"
        booking_type_key = "fly_me_to_you"
        duration_minutes = 240
        default_loc = "outcall"
        experience_type_value = booking_type_label
    elif any(k in msg_lower for k in ('dirty weekend', 'weekend', '48 hour', '48hr')):
        booking_type_label = "Dirty Weekend"
        booking_type_key = "dirty_weekend"
        duration_minutes = 2880
        default_loc = "outcall"
        experience_type_value = booking_type_label
    else:
        booking_type_label = "Overnight"
        booking_type_key = "overnight"
        duration_minutes = 240
        default_loc = "incall"
        experience_type_value = booking_type_label

    escort_name = get_escort_name()
    client_name = state.get('client_name') or greetings.extract_client_name(message) or ''

    try:
        from services.sms_service import send_escort_sms
        client_label = f" ({client_name})" if client_name else ""
        notif_msg = (
            f"⭐ HIGH VALUE ENQUIRY — {booking_type_label}\n\n"
            f"Client{client_label}: {phone_number}\n"
            f"Message: {message}\n\n"
            f"Please contact this client personally ASAP."
        )
        send_escort_sms(get_escort_phone_number(), notif_msg, category='special_bookings')
        logger.info(f"Sent {booking_type_label} enquiry notification to escort for {phone_number}")
    except Exception as e:
        logger.error(f"Failed to send extended enquiry notification to escort: {e}")

    incall_outcall = (state.get("incall_outcall") or default_loc).strip().lower() or default_loc
    booking_fields = {
        "booking_type": booking_type_key,
        "experience_type": experience_type_value,
        "duration": duration_minutes,
        "incall_outcall": "outcall" if incall_outcall == "outcall" else "incall",
        "client_name": client_name,
    }
    def _get_special_booking_rate() -> int:
        from core.rates_from_config import get_incall_pricing, get_outcall_pricing

        pricing = get_outcall_pricing() if booking_fields["incall_outcall"] == "outcall" else get_incall_pricing()
        if booking_type_key == "dirty_weekend":
            return int(pricing.get("weekend") or 9000)
        if booking_type_key == "fly_me_to_you":
            return int(pricing.get("fly_me") or 6000)
        if booking_type_key == "filming":
            return int(get_incall_pricing().get("pse_filming") or 1200)
        return int(pricing.get("overnight") or 5000)
    try:
        from booking.deposit_handler import calculate_deposit_requirement

        deposit_required, deposit_amount, deposit_reason = calculate_deposit_requirement(
            booking_fields,
            phone_number,
            state_manager,
        )
        if not deposit_required:
            deposit_required = True
            deposit_amount = 200
            deposit_reason = booking_type_key
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        deposit_required = True
        deposit_amount = 200
        deposit_reason = booking_type_key

    special_rate = _get_special_booking_rate()

    name_part = f" {client_name}" if client_name else ""
    client_msg = (
        f"Hey{name_part} you're through to {escort_name} automated message service.\n\n"
        f"As {booking_type_label.lower()} bookings are very special to {escort_name} I'm going to forward your "
        f"details to her so she can personally get back in touch with you.\n\n"
        f"Just so you know her {booking_type_label.lower()} rate is ${int(special_rate)} and just be aware "
        f"a ${int(deposit_amount or 0)} deposit would need to be paid to secure the booking.\n\n"
        f"I've forwarded your details on to {escort_name} so she should be in touch shortly \U0001F319"
    )

    updates: dict = {
        'extended_enquiry_notified': True,
        'booking_type': booking_type_key,
        'incall_outcall': booking_fields["incall_outcall"],
        'deposit_required': bool(deposit_required),
        'deposit_amount': int(deposit_amount or 0),
        'deposit_reason': str(deposit_reason or booking_type_key),
    }
    if client_name:
        updates['client_name'] = client_name
    if not state.get('first_contact_sent'):
        updates['first_contact_sent'] = True
    state_manager.update_fields(phone_number, updates)

    return {
        "messages": [client_msg],
        "new_state": "EXTENDED_ENQUIRY",
        "actions": []
    }
