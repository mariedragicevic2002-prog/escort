"""
Date and Time Formatting Utilities
Australian-style date/time formatting for client messages.
"""

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def format_date_australian(date_str, time_str=None):
    """
    Format date/time in friendly Australian style.

    Args:
        date_str: Date string in various formats (YYYY-MM-DD, DD/MM/YYYY, etc.) or datetime object
        time_str: Optional time string (HH:MM, HHam/pm, etc.) or tuple (hour, minute)

    Returns:
        str: Formatted date like "Sunday 4th Jan at 8pm"
    """
    try:
        # Parse the date
        date_obj = None

        if isinstance(date_str, datetime):
            date_obj = date_str.date() if hasattr(date_str, 'date') else date_str
        elif isinstance(date_str, type(datetime.now().date())):
            date_obj = date_str
        elif '-' in str(date_str):
            # YYYY-MM-DD format
            date_obj = datetime.strptime(str(date_str).split()[0], "%Y-%m-%d").date()
        elif '/' in str(date_str):
            # DD/MM/YYYY format
            date_str_clean = str(date_str).strip()
            # Remove day name if present (e.g., "Wednesday 14/01/2026" -> "14/01/2026")
            parts = date_str_clean.split()
            if len(parts) > 1 and '/' in parts[-1]:
                date_str_clean = parts[-1]
            date_obj = datetime.strptime(date_str_clean, "%d/%m/%Y").date()
        else:
            return str(date_str)  # Return as-is if unknown format

        # Get day suffix (1st, 2nd, 3rd, 4th, etc.)
        day = date_obj.day
        if 11 <= day <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')

        # Format: "Sunday 4th Jan"
        day_name = date_obj.strftime("%A")
        month_name = date_obj.strftime("%b")
        formatted_date = f"{day_name} {day}{suffix} {month_name}"

        # Add time if provided
        if time_str:
            formatted_time = format_time_australian(time_str)
            return f"{formatted_date} at {formatted_time}"

        return formatted_date

    except Exception as e:
        logger.warning(f"Date formatting error: {e}")
        # Return original if parsing fails
        return str(date_str)


def format_date_for_client(date_str):
    """
    Format date for client messages as "14th January" (without year).

    Args:
        date_str: Date string in format "Wednesday 14/01/2026" or "14/01/2026" or "2026-01-14" or datetime/date object

    Returns:
        str: Formatted date like "14th January"
    """
    try:
        # Parse the date - strip day name if present
        if isinstance(date_str, datetime):
            date_obj = date_str.date() if hasattr(date_str, 'date') else date_str
        elif isinstance(date_str, type(datetime.now().date())):
            date_obj = date_str
        else:
            # Remove day name if present (e.g., "Wednesday 14/01/2026" -> "14/01/2026")
            date_str_clean = str(date_str).strip()
            parts = date_str_clean.split()
            if len(parts) > 1 and ('/' in parts[-1] or '-' in parts[-1]):
                date_str_clean = parts[-1]  # Extract just the date part

            # Parse the date
            if '-' in date_str_clean:
                date_obj = datetime.strptime(date_str_clean, "%Y-%m-%d").date()
            elif '/' in date_str_clean:
                date_obj = datetime.strptime(date_str_clean, "%d/%m/%Y").date()
            else:
                return str(date_str)

        # Get day suffix (1st, 2nd, 3rd, 4th, etc.)
        day = date_obj.day
        if 11 <= day <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')

        # Format as "14th January"
        month_name = date_obj.strftime("%B")  # Full month name
        return f"{day}{suffix} {month_name}"

    except Exception as e:
        logger.warning(f"Date formatting error: {e}")
        # Return original if parsing fails
        return str(date_str)


def format_time_australian(time_str):
    """
    Format time in friendly Australian style (12-hour with am/pm).

    Args:
        time_str: Time string like "20:00", "8pm", "14:30" or tuple (hour, minute)

    Returns:
        str: Formatted time like "8pm" or "2:30pm"
    """
    try:
        # Handle tuple format (hour, minute)
        if isinstance(time_str, tuple):
            hour, minute = time_str
            period = "am" if hour < 12 else "pm"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            if minute == 0:
                return f"{display_hour}{period}"
            else:
                return f"{display_hour}:{minute:02d}{period}"

        time_obj = None
        time_str_clean = str(time_str).strip().lower()

        # Already in am/pm format
        if 'am' in time_str_clean or 'pm' in time_str_clean:
            # Clean up format (e.g., "8pm" -> "8pm", "8:30pm" -> "8:30pm")
            time_str_clean = time_str_clean.replace(' ', '').replace('.', '')
            # Parse and reformat for consistency
            if ':' in time_str_clean:
                time_obj = datetime.strptime(time_str_clean, "%I:%M%p")
            else:
                time_obj = datetime.strptime(time_str_clean, "%I%p")
        elif ':' in time_str_clean:
            # 24-hour format (HH:MM)
            time_obj = datetime.strptime(time_str_clean, "%H:%M")
        else:
            return time_str  # Return as-is

        # Format: "8pm" or "8:30pm" (no leading zero, lowercase)
        hour = time_obj.hour
        minute = time_obj.minute
        period = "am" if hour < 12 else "pm"

        if hour == 0:
            hour = 12
        elif hour > 12:
            hour = hour - 12

        if minute == 0:
            return f"{hour}{period}"
        else:
            return f"{hour}:{minute:02d}{period}"

    except Exception as e:
        logger.warning(f"Time formatting error: {e}")
        return str(time_str)


def format_friendly_date(dt_obj):
    """
    Format a datetime object as a friendly date like 'Thurs 8th Jan'.

    This is a convenience function that takes a datetime object directly,
    unlike format_date_australian() which parses string input.

    Args:
        dt_obj: datetime or date object

    Returns:
        str: Formatted date like "Thurs 8th Jan"
    """
    try:
        if hasattr(dt_obj, 'date'):
            dt_obj = dt_obj.date()
        
        day = dt_obj.day
        # Add ordinal suffix (st, nd, rd, th)
        if 11 <= day <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        return dt_obj.strftime(f"%a {day}{suffix} %b")
    except Exception as e:
        logger.warning(f"Friendly date formatting error: {e}")
        return str(dt_obj)


def format_date_ordinal_full(dt_obj):
    """
    Format a datetime object as "8th May 2026".

    Args:
        dt_obj: datetime or date object, or ISO date string (YYYY-MM-DD)

    Returns:
        str: Formatted date like "8th May 2026"
    """
    try:
        # Handle string input in ISO format
        if isinstance(dt_obj, str):
            if '-' in dt_obj:
                dt_obj = datetime.strptime(dt_obj.split()[0], "%Y-%m-%d").date()
            else:
                return str(dt_obj)
        
        # Handle datetime object
        if hasattr(dt_obj, 'date'):
            dt_obj = dt_obj.date()
        
        day = dt_obj.day
        # Add ordinal suffix (st, nd, rd, th)
        if 11 <= day <= 13:
            suffix = 'th'
        else:
            suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(day % 10, 'th')
        
        month_name = dt_obj.strftime("%B")  # Full month name
        year = dt_obj.year
        return f"{day}{suffix} {month_name} {year}"
    except Exception as e:
        logger.warning(f"Ordinal date formatting error: {e}")
        return str(dt_obj)
