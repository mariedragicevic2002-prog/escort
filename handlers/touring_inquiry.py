"""

Touring Inquiry Handler

Handles client inquiries about touring schedules and city notification subscriptions.

Flow:
1. Client asks "when are you back in Perth?" \u2192 touring_inquiry intent
2. Bot responds with current tour city, profile link, and offer to notify
3. Client replies "TOURING" \u2192 touring_subscribe intent
4. Bot saves subscription and confirms
5. When escort arrives in subscribed city (via admin), SMS is sent automatically
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
from datetime import datetime
from typing import Any

from templates.touring_messages import (
    TOURING_INTEREST_FALLBACK,
    TOURING_SUBSCRIBED,
    TOURING_SUBSCRIBED_FALLBACK,
)

logger = logging.getLogger("escort_chatbot.touring_inquiry")

PROFILE_URL = "(profile_url)"


def _get_profile_url() -> str:
    """Get profile URL from settings, fallback to default."""
    try:
        from core.settings_manager import get_setting
        saved = (get_setting("profile_url") or "").strip()
        if saved:
            return saved if saved.startswith("http") else f"https://{saved}"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return PROFILE_URL


def _get_client_name(phone_number: str, state_manager: Any) -> str:
    """Get client name from state."""
    try:
        if state_manager:
            fields = state_manager.get_booking_fields(phone_number)
            return (fields.get('client_name') or '').strip()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return ''


def extract_australian_city_from_message(message: str) -> str | None:
    """Extract Australian city name from message (booking geo guard + touring flow)."""
    message_lower = message.lower()
    cities = {
        'sydney': 'Sydney',
        'nsw': 'Sydney',
        'new south wales': 'Sydney',
        'melbourne': 'Melbourne',
        'victoria': 'Melbourne',
        'brisbane': 'Brisbane',
        'queensland': 'Brisbane',
        'qld': 'Brisbane',
        'perth': 'Perth',
        'western australia': 'Perth',
        'wa': 'Perth',
        'adelaide': 'Adelaide',
        'south australia': 'Adelaide',
        'hobart': 'Hobart',
        'tasmania': 'Hobart',
        'canberra': 'Canberra',
        'act': 'Canberra',
        'gold coast': 'Gold Coast',
    }
    for keyword, city in cities.items():
        if keyword in message_lower:
            return city
    return None


def _format_date_range(start_date: str, end_date: str) -> str:
    """Format tour dates for display, e.g. '5 Apr – 10 Apr'."""
    try:
        from datetime import datetime
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                s = datetime.strptime(start_date.strip(), fmt)
                e = datetime.strptime(end_date.strip(), fmt)
                return f"{s.day} {s.strftime('%b')} – {e.day} {e.strftime('%b')}"
            except ValueError:
                continue
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return f"{start_date} – {end_date}"


def handle_touring_inquiry(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle touring Australia inquiries.

    **Location** (admin) = where the escort is working right now (bookings, 15km, etc.).
    **Touring** fields = optional schedule used to tell clients when you will visit *their* city
    (notifications / future visits) — not a second "where I am" source.

    Sends:
    - Profile link for touring dates/info
    - Webpage subscription reminder for the asked city
    - SMS opt-in instruction using "TOURING <city>"
    """
    try:
        message = (context.get('message') or '').strip()
        phone_number = context.get('phone_number')
        state_manager = context.get('state_manager')

        # Get client's name and the city they're asking about
        client_name = _get_client_name(phone_number or "", state_manager)
        asked_city = extract_australian_city_from_message(message)

        profile_url = _get_profile_url()
        name_prefix = f"Hi {client_name}\n\n" if client_name else ""
        city_label = asked_city or "your town"
        touring_keyword = f"TOURING {asked_city}" if asked_city else "TOURING <city>"
        touring_message = (
            f"{name_prefix}All my tour dates and info can be seen by visiting my profile\n\n"
            f"{profile_url}\n\n"
            f"You can also subscribe to my tours on the webpage so you will know the next time I'm in {city_label}.\n\n"
            f"Alternatively if you text back the word {touring_keyword} I'll send you a text the next time I'm in {city_label}."
        )

        # Save the city they asked about so we can use it when they reply TOURING
        if state_manager and asked_city:
            try:
                state_manager.update_fields(phone_number, {
                    'last_touring_inquiry_city': asked_city,
                })
            except Exception as e:
                logger.warning(f"Could not save last_touring_inquiry_city: {e}")

        logger.info(f"Touring inquiry from {phone_number} about {asked_city or 'unknown city'}")

        return {
            "messages": [touring_message],
            "new_state": None,  # Stay in current state
            "actions": []
        }

    except Exception as e:
        logger.error(f"Error in touring inquiry handler: {e}")
        return {
            "messages": [TOURING_INTEREST_FALLBACK],
            "new_state": None,
            "actions": []
        }


def handle_touring_subscribe(context: dict[str, Any]) -> dict[str, Any]:
    """
    Handle client replying 'TOURING' to subscribe to city notifications.
    Saves their phone number + requested city. They'll be SMSed when escort arrives.
    """
    try:
        phone_number = context.get('phone_number')
        state_manager = context.get('state_manager')

        # Get the city they last asked about
        city = None
        if state_manager:
            state = context.get('state') or state_manager.get_state(phone_number) or {}
            city = state.get('last_touring_inquiry_city')

        if not city:
            # Fallback: try to extract from the current message
            message = (context.get('message') or '').strip()
            city = extract_australian_city_from_message(message)

        if not city:
            return {
                "messages": [
                    "To subscribe to touring notifications, first ask me about a specific city "
                    "(e.g. 'When are you next in Perth?') and then reply TOURING."
                ],
                "new_state": None,
                "actions": []
            }

        # Save subscription
        if state_manager:
            try:
                state_manager.update_fields(phone_number, {
                    'tour_sms_subscription': True,
                    'tour_subscription_city': city,
                    'tour_subscribed_at': datetime.now(),
                })
            except Exception as e:
                logger.warning(f"Could not save tour subscription for {phone_number}: {e}")

        logger.info(f"Client {phone_number} subscribed to touring notifications for {city}")

        return {
            "messages": [TOURING_SUBSCRIBED.format(city=city)],
            "new_state": None,
            "actions": ["save_tour_subscription"]
        }

    except Exception as e:
        logger.error(f"Error in touring subscribe handler: {e}")
        return {
            "messages": [TOURING_SUBSCRIBED_FALLBACK],
            "new_state": None,
            "actions": []
        }


def check_and_send_touring_notifications(db_service=None) -> int:
    """
    Called by background job every 5 minutes.
    Sends 2-day-prior SMS to all clients subscribed to the upcoming tour city.

    Returns number of notifications sent.
    """
    try:
        from datetime import date, timedelta

        import config

        touring = config.get_touring_australia()
        if not touring:
            return 0

        is_touring = touring.get('is_touring', False)
        tour_city = (touring.get('tour_city') or '').strip()
        tour_start_str = (touring.get('tour_start_date') or '').strip()
        tour_end_str = (touring.get('tour_end_date') or '').strip()

        if not (is_touring and tour_city and tour_start_str):
            return 0

        # Parse the tour start date
        tour_start_date = None
        for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
            try:
                tour_start_date = datetime.strptime(tour_start_str, fmt).date()
                break
            except ValueError:
                continue

        if not tour_start_date:
            return 0

        # Only fire when today is exactly 2 days before tour start
        today = date.today()
        if today != tour_start_date - timedelta(days=2):
            return 0

        return _send_touring_notifications(tour_city, tour_start_str, tour_end_str, db_service)

    except Exception as e:
        logger.error(f"Error in check_and_send_touring_notifications: {e}")
        return 0


def _send_touring_notifications(tour_city: str, tour_start: str, tour_end: str, db_service=None) -> int:
    """Send 2-day-prior SMS to all subscribers for tour_city. Clears subscription after send."""
    return send_touring_arrival_notifications(tour_city, tour_start, tour_end, db_service)


def send_touring_arrival_notifications(tour_city: str, tour_start: str, tour_end: str, db_service=None) -> int:
    """
    Called when escort marks themselves as arriving in a city.
    Sends SMS to all clients subscribed to that city.

    Returns number of notifications sent.
    """
    sent = 0
    try:
        if not db_service:
            from services.database_service import get_shared_db
            db_service = get_shared_db()
        if db_service is None:
            return 0

        # Find all subscribers for this city (case-insensitive)
        results = db_service.execute_query(
            """SELECT phone_number, client_name
               FROM conversation_states
               WHERE tour_sms_subscription = TRUE
                 AND LOWER(tour_subscription_city) = LOWER(%s)""",
            (tour_city,),
            fetch=True
        )

        if not results:
            logger.info(f"No touring subscribers found for {tour_city}")
            return 0

        from services.sms_service import send_sms
        profile_url = _get_profile_url()

        if tour_start and tour_end:
            try:
                date_str = _format_date_range(tour_start, tour_end)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                date_str = f"{tour_start} to {tour_end}"
            date_line = f"I'll be there {date_str}."
        else:
            date_line = ""

        for row in results:
            phone = row.get('phone_number') or row['phone_number']
            name = (row.get('client_name') or '').strip()
            name_part = f"Hi {name}! " if name else "Hi! "

            msg = (
                f"{name_part}\U0001F31F Exciting news \u2014 I'm heading to {tour_city} in 2 days! "
                f"{date_line}\n\n"
                f"Would you like to book a session while I'm there? "
                f"Check my profile for all the details:\n\n{profile_url}"
            ).strip()

            try:
                send_sms(phone, msg)
                sent += 1
                logger.info(f"Sent touring arrival notification to {phone} for {tour_city}")
            except Exception as e:
                logger.error(f"Failed to send touring notification to {phone}: {e}")

            # Clear subscription after notifying (one-shot notification)
            try:
                db_service.execute_query(
                    """UPDATE conversation_states
                       SET tour_sms_subscription = FALSE,
                           tour_subscription_city = NULL,
                           tour_subscribed_at = NULL
                       WHERE phone_number = %s""",
                    (phone,),
                    fetch=False
                )
            except Exception as e:
                logger.warning(f"Could not clear subscription for {phone}: {e}")

    except Exception as e:
        logger.error(f"Error sending touring arrival notifications: {e}")

    return sent
