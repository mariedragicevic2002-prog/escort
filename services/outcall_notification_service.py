"""
Outcall Notification Service
Sends 1-hour prior notifications to escort for outcall bookings with Uber link.
"""

import logging
import urllib.parse
from datetime import datetime, timedelta
from typing import Any

try:
    import pytz
except ImportError:
    pytz = None



def _get_tz(name: str):
    if pytz is not None:
        return pytz.timezone(name)
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)

logger = logging.getLogger("adella_chatbot.outcall_notification")


def _escort_location_fallback():
    return "my location"


def generate_uber_link(destination_address: str, origin_address: str = "") -> str:
    """
    Generate Uber deep link for navigation to destination.
    
    Args:
        destination_address: Client's outcall address
        origin_address: Optional origin address (escort's location)
        
    Returns:
        Uber deep link URL
    """
    # Uber deep link format: uber://?action=setPickup&pickup=my_location&dropoff[formatted_address]=ADDRESS
    # For web: https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[formatted_address]=ADDRESS
    
    # URL encode the destination address
    encoded_dest = urllib.parse.quote(destination_address)
    
    if origin_address:
        encoded_origin = urllib.parse.quote(origin_address)
        # Uber link with both origin and destination
        uber_link = f"https://m.uber.com/ul/?action=setPickup&pickup[formatted_address]={encoded_origin}&dropoff[formatted_address]={encoded_dest}"
    else:
        # Uber link with just destination (uses current location as origin)
        uber_link = f"https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[formatted_address]={encoded_dest}"
    
    return uber_link


def _build_outcall_notification_message(
    booking_fields: dict[str, Any],
    phone_number: str,
    heading: str,
    closing_line: str,
) -> str:
    """Build an outcall escort notification with Uber link."""
    from config import get_current_incall_location

    client_name = booking_fields.get('client_name', 'Client')
    outcall_address = str(booking_fields.get('outcall_address') or 'Address not provided')

    booking_date = booking_fields.get('date')
    if booking_date:
        if hasattr(booking_date, 'strftime'):
            date_str = booking_date.strftime("%A, %d %B %Y")
        else:
            date_str = str(booking_date)
    else:
        date_str = "Not specified"

    booking_time = booking_fields.get('time')
    if booking_time:
        if isinstance(booking_time, tuple):
            hour, minute = booking_time
            period = "PM" if hour >= 12 else "AM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            time_str = f"{display_hour}:{minute:02d}{period}"
        else:
            time_str = str(booking_time)
    else:
        time_str = "Not specified"

    duration = booking_fields.get('duration')
    if duration:
        if duration >= 60:
            hours = duration // 60
            mins = duration % 60
            if mins > 0:
                duration_str = f"{hours}h {mins}min"
            else:
                duration_str = f"{hours}h"
        else:
            duration_str = f"{duration}min"
    else:
        duration_str = "Not specified"

    try:
        location_info = get_current_incall_location()
        origin_address = location_info.get('address') or location_info.get('hotel_name') or location_info.get('city') or ''
        if not origin_address:
            try:
                from core.settings_manager import get_setting
                origin_address = get_setting('address') or get_setting('hotel_name') or get_setting('city') or _escort_location_fallback()
            except Exception as e:
                logger.warning("Escort origin from settings failed: %s", e)
                origin_address = _escort_location_fallback()
    except Exception as e:
        logger.warning("get_current_incall_location failed: %s", e)
        origin_address = ""

    uber_link = generate_uber_link(outcall_address, origin_address or "")
    return f"""{heading}

Client: {client_name}
Phone: {phone_number}

\U0001F4C5 {date_str}
\U0001F550 {time_str}
\u23F1 {duration_str}

\U0001F4CD Destination: {outcall_address}

\U0001F695 Uber Link: {uber_link}

{closing_line}"""


def send_outcall_booking_notification(booking_fields: dict[str, Any], phone_number: str) -> bool:
    """Send an immediate escort notification for a confirmed outcall booking with Uber link."""
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("Escort phone number not configured - cannot send notification")
        return False

    try:
        message = _build_outcall_notification_message(
            booking_fields,
            phone_number,
            heading="\U0001F697 OUTCALL BOOKING CONFIRMED",
            closing_line="Deposit confirmed - travel arrangements can begin now."
        )
        success = send_escort_sms(escort_phone, message, category='outcall_notifications')
        if success:
            logger.info(f"Sent immediate outcall notification to escort for {phone_number}")
        else:
            logger.error(f"Failed to send immediate outcall notification to escort for {phone_number}")
        return success
    except Exception as e:
        logger.error(f"Error sending immediate outcall notification: {e}")
        return False


def send_outcall_travel_notification(booking_fields: dict[str, Any], phone_number: str) -> bool:
    """
    Send 1-hour prior notification to escort for outcall booking with Uber link.
    
    Args:
        booking_fields: Dict with booking details
        phone_number: Client's phone number
        
    Returns:
        True if notification sent successfully
    """
    from config import get_escort_phone_number
    from services.sms_service import send_escort_sms

    escort_phone = get_escort_phone_number()
    if not escort_phone:
        logger.warning("Escort phone number not configured - cannot send notification")
        return False

    try:
        message = _build_outcall_notification_message(
            booking_fields,
            phone_number,
            heading="\U0001F697 OUTCALL BOOKING - 1 HOUR PRIOR",
            closing_line="Get ready to travel!"
        )
        success = send_escort_sms(escort_phone, message, category='outcall_notifications')

        if success:
            logger.info(f"Sent 1-hour outcall notification to escort for {phone_number}")
        else:
            logger.error(f"Failed to send outcall notification to escort for {phone_number}")

        return success

    except Exception as e:
        logger.error(f"Error sending outcall notification: {e}")
        return False


def schedule_outcall_travel_notification(booking_fields: dict[str, Any], phone_number: str, state_manager) -> bool:
    """
    Schedule 1-hour prior notification for outcall booking.
    
    Args:
        booking_fields: Dict with booking details
        phone_number: Client's phone number
        state_manager: State manager instance
        
    Returns:
        True if scheduled successfully
    """
    try:
        date = booking_fields.get('date')
        time = booking_fields.get('time')
        
        if not date or not time:
            logger.warning(f"Cannot schedule outcall notification - missing date/time for {phone_number}")
            return False
        
        # Parse datetime
        from config import get_effective_escort_timezone

        tz = _get_tz(get_effective_escort_timezone())
        
        if isinstance(date, str):
            booking_date = datetime.strptime(date, "%Y-%m-%d").date()
        elif hasattr(date, 'date'):
            booking_date = date.date() if hasattr(date, 'date') else date
        else:
            booking_date = date
        
        if isinstance(time, tuple):
            hour, minute = time
        else:
            hour, minute = 0, 0
        
        # Create booking datetime
        booking_datetime = tz.localize(datetime.combine(booking_date, datetime.min.time().replace(hour=hour, minute=minute)))
        
        # Calculate notification time (1 hour before booking)
        notification_time = booking_datetime - timedelta(hours=1)
        
        # Store notification time in state
        state_manager.update_fields(phone_number, {
            'outcall_travel_notification_scheduled': notification_time.isoformat(),
            'outcall_travel_notification_sent': False
        })
        
        logger.info(f"Scheduled outcall travel notification for {phone_number}: {notification_time}")
        return True
        
    except Exception as e:
        logger.error(f"Failed to schedule outcall notification for {phone_number}: {e}")
        return False


def check_and_send_outcall_notifications(state_manager, db_service) -> int:
    """
    Check for due outcall travel notifications and send them.
    Called periodically by background job.
    
    Args:
        state_manager: State manager instance
        db_service: Database service instance
        
    Returns:
        Number of notifications sent
    """
    try:
        from config import get_effective_escort_timezone

        tz = _get_tz(get_effective_escort_timezone())
        now = datetime.now(tz)
        
        # Find outcall bookings with unsent notifications
        query = """
            SELECT phone_number, date, time, duration, experience_type, 
                   incall_outcall, outcall_address, client_name,
                   outcall_travel_notification_scheduled,
                   outcall_travel_notification_sent
            FROM conversation_states
            WHERE current_state = 'CONFIRMED'
              AND incall_outcall = 'outcall'
              AND date IS NOT NULL
              AND time IS NOT NULL
              AND outcall_address IS NOT NULL
              AND outcall_travel_notification_scheduled IS NOT NULL
              AND outcall_travel_notification_sent = FALSE
        """
        
        results = db_service.execute_query(query, fetch=True)
        if not results:
            return 0
        
        notifications_sent = 0
        
        for row in results:
            phone_number = row['phone_number']
            
            # Check if notification is due
            notification_time_str = row['outcall_travel_notification_scheduled']
            if notification_time_str:
                try:
                    notification_time = datetime.fromisoformat(notification_time_str.replace('Z', '+00:00'))
                    if notification_time <= now:
                        # Send notification
                        booking_fields = {
                            'date': row['date'],
                            'time': row['time'],
                            'duration': row['duration'],
                            'experience_type': row['experience_type'],
                            'incall_outcall': row['incall_outcall'],
                            'outcall_address': row['outcall_address'],
                            'client_name': row['client_name']
                        }
                        
                        if send_outcall_travel_notification(booking_fields, phone_number):
                            state_manager.update_fields(phone_number, {
                                'outcall_travel_notification_sent': True
                            })
                            notifications_sent += 1
                except Exception as e:
                    logger.error(f"Error processing outcall notification for {phone_number}: {e}")
        
        if notifications_sent > 0:
            logger.info(f"Sent {notifications_sent} outcall travel notifications")
        
        return notifications_sent
        
    except Exception as e:
        logger.error(f"Error checking outcall notifications: {e}")
        return 0
