"""Dinner date booking rules: 2h duration, 5–9pm offer window, default outcall."""

from __future__ import annotations

from utils.log_sanitize import LOG_SUPPRESSED_FMT

from datetime import datetime, time, timedelta
from typing import Any

import logging
logger = logging.getLogger("adella_chatbot.dinner_date")

DINNER_DURATION_MINUTES = 120
# Start must be between 17:00 and 21:00 inclusive (last bookable start is 9pm). The 2h session may end after 9pm.
DINNER_WINDOW_START = time(17, 0)
DINNER_WINDOW_END = time(21, 0)


def strip_sms_simulator_echo_prefix(message: str) -> str:
    """Remove leading 'You (+61...): ' paste noise from CLI simulator (may repeat)."""
    m = (message or "").strip()
    if not m:
        return m
    while "): " in m:
        part = m.rsplit("): ", 1)[-1].strip()
        if part == m:
            break
        m = part
    return m


def looks_like_home_address_line(message: str) -> str | None:
    """If message looks like a residential address, return cleaned text for extraction; else None."""
    import re

    m = strip_sms_simulator_echo_prefix(message)
    if not m or len(m) < 10:
        return None
    low = m.lower()
    if re.search(
        r"\b(st|street|rd|road|ave|avenue|dr|drive|court|ct|pl|place|terrace|tce|way|pde|parade)\b",
        low,
    ):
        return m
    if re.search(r"\b\d{1,5}\s+[a-z]", low):
        return m
    if any(
        x in low
        for x in (
            "live at",
            "i'm at ",
            "im at ",
            "staying at",
            "address is",
            "address:",
            "my address",
        )
    ):
        return m
    return None


def extract_client_address_from_message(message: str) -> str:
    """Pull home / hotel text from a reply (street address or venue like Oaks Embassy)."""
    import re

    m = strip_sms_simulator_echo_prefix(message).strip()
    if not m:
        return m
    low = m.lower()
    # Longer markers first — hotel / short replies: "im staying at Oaks Embassy"
    for marker in (
        "my address is ",
        "my address is: ",
        "address is ",
        "address: ",
        "i'm staying at ",
        "im staying at ",
        "i am staying at ",
        "staying at ",
        "i live at ",
        "i'm at ",
        "im at ",
    ):
        idx = low.rfind(marker)
        if idx >= 0:
            tail = m[idx + len(marker) :].strip()
            return _strip_wrapping_quotes(tail)
    for prefix in (
        "my address ",
    ):
        if low.startswith(prefix):
            return m[len(prefix) :].strip()
    # "my place 158 X St" (numbered street)
    match = re.search(r"\bmy\s+place\s+(\d[\w\s,./-]+)", low)
    if match:
        start = match.start(1)
        return m[start : start + len(match.group(1))].strip()
    # "go to my place … Oaks Embassy" / trailing venue after my place (no street number)
    match2 = re.search(
        r"\b(?:go to\s+)?my\s+place\b[^a-z0-9]*(.+)$",
        low,
        re.IGNORECASE,
    )
    if match2 and len(match2.group(1).strip()) >= 3:
        tail = match2.group(1).strip()
        # Drop leading "im staying at" if regex captured it
        for rm in ("im staying at ", "i'm staying at ", "staying at "):
            if tail.lower().startswith(rm):
                tail = tail[len(rm) :].strip()
                break
        return _strip_wrapping_quotes(tail)
    return _strip_wrapping_quotes(m)


def _strip_wrapping_quotes(s: str) -> str:
    """Remove surrounding quotes from venue names (e.g. \"Oaks Horizon\")."""
    t = (s or "").strip()
    if len(t) >= 2 and t[0] in '"\'' and t[-1] == t[0]:
        return t[1:-1].strip()
    return t


def parse_dinner_after_preference(message: str) -> str | None:
    """Return 'hotel' or 'client_place' from a short reply, or None."""
    m = strip_sms_simulator_echo_prefix(message or "").lower()
    if not m.strip():
        return None
    if any(
        x in m
        for x in (
            "your place",
            "your home",
            "head home",
            "to mine",
            "my place",
            "go home",
            "back to mine",
        )
    ):
        return "client_place"
    if any(
        x in m
        for x in (
            "hotel",
            "back to yours",
            "your hotel",
            "come back to",
            "my hotel",
        )
    ):
        return "hotel"
    return None


def looks_like_dinner_food_preference_chat(message: str) -> bool:
    """
    Client is asking about cuisines / where to go / your favourites — not naming a restaurant yet.
    Replying with a quick-close avoids treating the whole SMS as a venue and jumping to after-dinner.
    """
    m = strip_sms_simulator_echo_prefix(message or "").strip()
    if len(m) < 14:
        return False
    low = m.lower()
    # Short venue-like lines (no question, few words) — let normal venue flow handle.
    if "?" not in m and len(m.split()) <= 8:
        return False
    triggers = (
        "where would you",
        "where do you",
        "where shall we",
        "where should we",
        "would you like to go",
        "do you have a favourite",
        "do you have a favorite",
        "what's your favourite",
        "whats your favourite",
        "what is your favourite",
        "what's your favorite",
        "your favourite food",
        "your favorite food",
        "favourite food or restaurant",
        "favorite food or restaurant",
        "kind of food do you",
        "type of food",
        "what food do you",
        "what would you like to eat",
        "what do you feel like eating",
        "any favourite",
        "any favorite",
    )
    if any(t in low for t in triggers):
        return True
    # Long multi-clause message with a question mark — almost never a venue name.
    if "?" in m and len(m.split()) > 14:
        return True
    return False


def looks_like_restaurant_reply(message: str) -> bool:
    """Heuristic: message is probably a venue name, not only a time."""
    import re

    m = (message or "").strip()
    if len(m) < 4:
        return False
    if looks_like_dinner_food_preference_chat(m):
        return False
    if len(m) <= 36 and re.match(
        r"^\d{1,2}(:\d{2})?\s*(am|pm)\b|^\d{1,2}\s*(am|pm)\b", m, re.IGNORECASE
    ):
        return False
    mlow = m.lower()
    # Conversational dinner booking openers — not a venue string (would wrongly skip to "after dinner" prompt).
    if any(
        k in mlow
        for k in (
            "take you to dinner",
            "take you out to dinner",
            "take you out for dinner",
            "want to take you",
            "i want to take you",
            "can i take you",
            "could i take you",
            "would like to take you",
            "keen to take you",
        )
    ):
        return False
    return True


def normalize_dinner_venue_name(text: str) -> str:
    """Strip conversational lead-in so the venue string geocodes well (e.g. 'Le Pas Sage')."""
    import re

    t = (text or "").strip()
    if not t:
        return t
    t = re.sub(r"^(?:how\s+about|what\s+about|maybe)\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:we\s+)?go\s+to\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:i\s*'?m|i\s+was)\s+thinking(?:\s+of)?\s+", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^(?:let'?s\s+go\s+to|at\s+the|at)\s+", "", t, flags=re.IGNORECASE)
    # "815 and lets eat at Eos" / "8:15pm and eat at Foo" — keep venue only for geocoding
    t = re.sub(
        r"^\d{1,2}:\d{2}\s*(?:am|pm)\s+and\s+(?:let'?s\s+)?(?:eat\s+at|go\s+to)\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"^\d{3,4}\s+and\s+(?:let'?s\s+)?(?:eat\s+at|go\s+to)\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"^\d{1,2}(:\d{2})?\s*(?:am|pm)\s+(?:let'?s\s+)?go\s+to\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    t = re.sub(
        r"^\d{3,4}\s*(?:am|pm)\s+(?:let'?s\s+)?go\s+to\s+",
        "",
        t,
        flags=re.IGNORECASE,
    )
    return t.strip().rstrip("?.!")


def is_dinner_date_booking(state: dict[str, Any] | None) -> bool:
    if not state:
        return False
    bt = (state.get("booking_type") or "").strip().lower()
    if bt == "dinner_date":
        return True
    exp = (state.get("experience_type") or "").strip().lower()
    return exp in ("dinner date", "dinner_date")


def dinner_slot_fits_window(slot_start: datetime, duration_minutes: int = DINNER_DURATION_MINUTES) -> bool:
    """
    True if the booking starts between 5pm and 9pm inclusive (local), and the 2h block
    stays on one calendar day. The end time may be after 9pm (e.g. 8:15pm–10:15pm is OK).
    """
    slot_end = slot_start + timedelta(minutes=duration_minutes)
    if slot_start.date() != slot_end.date():
        return False
    t0 = slot_start.time()
    return DINNER_WINDOW_START <= t0 <= DINNER_WINDOW_END


def slot_kwargs_from_booking_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """Pass into get_next_available_time_slots(..., **kwargs) for dinner date flows."""
    if not is_dinner_date_booking(state):
        return {}
    return {"booking_type": "dinner_date"}


DINNER_SOCIAL_MINUTES = 60
DINNER_PLAY_MINUTES = 60


def resolve_dinner_play_location_address(booking_details: dict[str, Any]) -> str | None:
    """
    Physical address for the second half (private time) of a dinner date: escort incall/hotel
    when the client chose after-dinner at the escort's place, otherwise the client's
    hotel/home when they chose that option.
    """
    restaurant = (booking_details.get("outcall_address") or "").strip()
    if not restaurant:
        return None
    _after = (booking_details.get("dinner_after_preference") or "").strip().lower()
    _outside = bool(booking_details.get("dinner_client_outside_15km"))
    _client_home = (booking_details.get("dinner_client_address") or "").strip()
    _skip = _after in ("hotel", "escort_hotel", "my_hotel", "your_hotel") or _outside
    try:
        from services.calendar.travel_routing import get_escort_base_address_for_travel

        if _skip or _outside:
            play_loc = get_escort_base_address_for_travel()
        elif _after == "client_place" and _client_home:
            play_loc = _client_home
        else:
            play_loc = get_escort_base_address_for_travel()
        if not play_loc:
            return restaurant
        return play_loc
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return restaurant


def compute_dinner_play_timeline(
    booking_details: dict[str, Any],
    start_dt: datetime,
) -> tuple[datetime, datetime, datetime, str] | None:
    """
    Split a dinner date into dinner at the restaurant (1h) then travel then play (1h).

    Returns (dinner_end, play_start, play_end, play_location_address) or None if restaurant is missing.
    """
    restaurant = (booking_details.get("outcall_address") or "").strip()
    if not restaurant or not start_dt:
        return None

    dinner_end = start_dt + timedelta(minutes=DINNER_SOCIAL_MINUTES)
    play_loc = resolve_dinner_play_location_address(booking_details)
    if not play_loc:
        play_loc = restaurant

    try:
        from services.calendar.travel_routing import get_travel_minutes_between

        tm = get_travel_minutes_between(restaurant, play_loc)
        try:
            tm = max(1, int(tm or 15))
        except (TypeError, ValueError):
            tm = 15

        play_start = dinner_end + timedelta(minutes=tm)
        play_end = play_start + timedelta(minutes=DINNER_PLAY_MINUTES)
        return dinner_end, play_start, play_end, play_loc
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return None


def bump_to_next_dinner_candidate(current: datetime, duration_minutes: int = DINNER_DURATION_MINUTES) -> datetime:
    """Advance search cursor for dinner slot iteration (last start 9pm local)."""
    tz = current.tzinfo
    day = current.date()
    lo = datetime.combine(day, DINNER_WINDOW_START, tzinfo=tz)
    last_start = datetime.combine(day, DINNER_WINDOW_END, tzinfo=tz)  # last bookable start 21:00

    if current < lo:
        return lo
    if current > last_start:
        nxt = (current + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
        return nxt
    step = max(1, int(duration_minutes))
    nxt = current + timedelta(minutes=step)
    if nxt > last_start:
        return (current + timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0)
    return nxt
