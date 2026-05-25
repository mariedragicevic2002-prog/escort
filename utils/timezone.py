"""
Escort-local time (admin Location page).

Single source of truth: :func:`config.get_effective_escort_timezone` (``timezone``,
``location_timezone``, then city mapping, else default). All booking-related
"now", "today", reminders, and client-facing time interpretation should use
:func:`get_local_timezone` / :func:`get_current_datetime` here — not
``config.DEFAULT_TIMEZONE`` at import time and not the client PC clock.
"""

from datetime import datetime

import pytz

from config import get_effective_escort_timezone


def get_local_timezone():
    """Return a ``pytz`` zone for the escort's current location (see module doc)."""
    return pytz.timezone(get_effective_escort_timezone())


def get_current_datetime():
    """Current wall-clock time in the escort's configured timezone (aware ``datetime``)."""
    return datetime.now(get_local_timezone())


def format_date_for_client(date_obj) -> str:
    """Format date in client-friendly format.

    Args:
        date_obj: datetime or date object

    Returns:
        Formatted string like "Friday 12th February"
    """
    suffixes = {1: 'st', 2: 'nd', 3: 'rd'}
    day = date_obj.day
    suffix = suffixes.get(day if day < 20 else day % 10, 'th')
    return date_obj.strftime(f"%A %d{suffix} %B")


def get_today_or_tonight() -> str:
    """Returns 'today' always."""
    return 'today'
