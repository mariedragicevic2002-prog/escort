"""

Field prompts - Templates for requesting missing booking fields.
Enhanced with dynamic builders from old chatbot for better natural language.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from templates.booking_collection_messages import (
    ADDRESS_LOCATION_PROMPT,
    EXPERIENCE_TYPE_QUESTION,
    LOCATION_CHOICE_PROMPT,
    MOOD_EXPERIENCE_PROMPT,
    REPLY_WITH_BOTH_EXAMPLE,
    SHORT_ADDRESS_PROMPT,
    SHORT_LOCATION_CHOICE_PROMPT,
    append_outcall_duration_minimum_if_needed,
)
from utils.date_formatting import format_date_ordinal_full

import logging
logger = logging.getLogger("adella_chatbot.field_prompts")

EXPERIENCE_URL_FALLBACK = "https://www.adella-allure.com.au/experience"


def _get_experience_url() -> str:
    """Return the experience page URL (admin setting > env > fallback)."""
    try:
        from config import get_base_url
        base = get_base_url()
        if base:
            return f"{base}/experience"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return EXPERIENCE_URL_FALLBACK


def _experience_suffix(experience_already_set: bool = False) -> str:
    """Append GFE/DGFE/PSE + experience URL when the caller marks style not yet chosen."""
    if experience_already_set:
        return ""
    return f"\n\n{EXPERIENCE_TYPE_QUESTION}\n\n{_get_experience_url()}\n\n{REPLY_WITH_BOTH_EXAMPLE}"


def _mood_experience_prompt_with_url() -> str:
    """MOOD_EXPERIENCE_PROMPT with experience page URL appended."""
    return f"{MOOD_EXPERIENCE_PROMPT}\n\n{_get_experience_url()}"


def _extract_time_context(message: str) -> str:
    """Return a contextual time word from the message (tonight, tomorrow, etc.) or empty string."""
    if not message:
        return ""
    msg = message.lower()
    for word in ("tonight", "tonite", "tomorrow", "this weekend", "friday", "saturday",
                 "sunday", "monday", "tuesday", "wednesday", "thursday"):
        if word in msg:
            return word
    return ""


def build_missing_fields_message(
    missing_fields: list,
    context_message: str = "",
    experience_already_set: bool = False,
    is_outcall: bool = False,
) -> str:
    """Build message asking for missing fields dynamically.

    Returns empty string when only core booking fields (date/time/duration) are missing,
    since the first contact template already asks for those.

    Args:
        missing_fields: List of missing field names like ['date', 'time', 'duration']
        context_message: The client's original message — used to tailor the prompt

    Returns:
        Natural language message asking for specific fields, or empty string if
        only core fields are missing (first contact already asks for them)
    """
    if not missing_fields:
        return ""

    # Core booking fields - first contact template already asks for these
    core_fields = {'date', 'time', 'duration'}
    missing_set = set(missing_fields)
    time_ctx = _extract_time_context(context_message)
    time_ctx_suffix = f" {time_ctx}" if time_ctx else ""

    # Single field - always give a specific prompt (never return empty for a single missing field)
    if len(missing_fields) == 1:
        field = missing_fields[0]
        if field == 'time':
            return f"What time{time_ctx_suffix} works for you?"
        field_map = {
            'date': "When were you thinking?",
            'duration': append_outcall_duration_minimum_if_needed(
                "How long would you like to book for?"
                + _experience_suffix(experience_already_set=experience_already_set),
                is_outcall,
            ),
            'experience': _mood_experience_prompt_with_url(),
            'experience_type': _mood_experience_prompt_with_url(),
            'incall_outcall': LOCATION_CHOICE_PROMPT,
            'location_type': LOCATION_CHOICE_PROMPT,
            'address': ADDRESS_LOCATION_PROMPT,
            'outcall_address': ADDRESS_LOCATION_PROMPT,
        }
        return field_map.get(field, f"I just need your {field}...")

    # Filter out core fields - only prompt for non-core fields
    non_core_missing = [f for f in missing_fields if f not in core_fields]

    # If only core fields are missing return a specific prompt based on which ones
    if not non_core_missing:
        if missing_set == {'time'}:
            return f"What time{time_ctx_suffix} works for you?"
        if missing_set == {'duration'}:
            return append_outcall_duration_minimum_if_needed(
                "How long would you like to book for?"
                + _experience_suffix(experience_already_set=experience_already_set),
                is_outcall,
            )
        if missing_set == {'date'}:
            return "What date were you thinking?"
        if missing_set == {'time', 'duration'}:
            return append_outcall_duration_minimum_if_needed(
                (
                    f"What time{time_ctx_suffix} works for you, and how long would you like to book for?"
                    + _experience_suffix(experience_already_set=experience_already_set)
                ),
                is_outcall,
            )
        # date + time + duration all missing \u2014 fall through to nudge
        return ""
    
    # For non-core fields (experience, location, etc.), use friendly prompts
    if len(non_core_missing) == 1:
        field = non_core_missing[0]
        field_map = {
            'experience': _mood_experience_prompt_with_url(),
            'experience_type': _mood_experience_prompt_with_url(),
            'incall_outcall': SHORT_LOCATION_CHOICE_PROMPT,
            'location_type': SHORT_LOCATION_CHOICE_PROMPT,
            'address': SHORT_ADDRESS_PROMPT,
            'outcall_address': SHORT_ADDRESS_PROMPT,
        }
        return field_map.get(field, f"I just need your {field}...")
    
    # Multiple non-core fields
    field_descriptions = {
        'experience': "what experience you're after (GFE/DGFE/PSE)",
        'experience_type': "what experience you're after (GFE/DGFE/PSE)",
        'incall_outcall': "incall or outcall",
        'location_type': "incall or outcall",
        'address': "your location for outcall",
        'outcall_address': "your location for outcall"
    }
    
    descriptions = [field_descriptions.get(f, f) for f in non_core_missing if f in field_descriptions]

    if not descriptions:
        return ""

    experience_in_missing = any(f in non_core_missing for f in ('experience', 'experience_type'))
    exp_url_suffix = f"\n\n{_get_experience_url()}" if experience_in_missing else ""

    if len(descriptions) == 1:
        return f"I just need to know {descriptions[0]}...{exp_url_suffix}"
    else:
        return f"I also need {' and '.join(descriptions)}...{exp_url_suffix}"


# Field prompts (kept for backward compatibility)
PROMPTS = {
    'date': "When were you thinking?",
    'time': "And what TIME works for you?",
    'duration': "How long do you want me for? (DURATION)" + _experience_suffix(),
    'experience_type': MOOD_EXPERIENCE_PROMPT,
    'incall_outcall': LOCATION_CHOICE_PROMPT,
    'outcall_address': ADDRESS_LOCATION_PROMPT,
}


def get_field_prompt(field: str) -> str:
    """
    Get prompt for a missing field.

    Args:
        field: Field name

    Returns:
        Prompt string
    """
    return PROMPTS.get(field, f"Please provide {field}")


# Central prompts for standard incall/outcall booking flow (used by handlers instead of hardcoded strings).
# Admin can override via settings: prompt_duration_only, prompt_date_time_duration.

def get_duration_only_prompt(experience_already_set: bool = False, is_outcall: bool = False) -> str:
    """
    When we have date and time but duration is missing.
    Used after first contact when client gave date+time only.
    Overridable via get_setting("prompt_duration_only").
    """
    try:
        from core.settings_manager import get_setting
        custom = (get_setting("prompt_duration_only") or "").strip()
        if custom:
            return append_outcall_duration_minimum_if_needed(custom, is_outcall)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    base = (
        "How long do you want to book for? - (e.g. \"30 mins, 1 or 2 hours\")"
        if not is_outcall
        else 'How long do you want to book for? — e.g. "1 hr", "90 mins", or "2 hours"'
    )
    return append_outcall_duration_minimum_if_needed(
        base + _experience_suffix(experience_already_set=experience_already_set),
        is_outcall,
    )


def get_ask_date_time_duration_prompt(experience_already_set: bool = False, is_outcall: bool = False) -> str:
    """
    Generic prompt asking for date, time and duration (when we need all three or context was lost).
    Used for fallbacks and when nudging client to provide booking details.
    Overridable via get_setting("prompt_date_time_duration").
    """
    try:
        from core.settings_manager import get_setting
        custom = (get_setting("prompt_date_time_duration") or "").strip()
        if custom:
            return append_outcall_duration_minimum_if_needed(custom, is_outcall)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    base = (
        "What time works for you, and how long would you like to book?"
        + _experience_suffix(experience_already_set=experience_already_set)
    )
    return append_outcall_duration_minimum_if_needed(base, is_outcall)


def get_prompt_for_missing_core_fields(
    missing_fields: list, experience_already_set: bool = False, is_outcall: bool = False
) -> str:
    """
    When only core fields (date, time, duration) are missing, return a prompt
    that asks for what's missing only \u2014 never re-ask for what the client already gave.
    """
    if not missing_fields:
        return get_ask_date_time_duration_prompt(experience_already_set=experience_already_set, is_outcall=is_outcall)
    missing_set = set(missing_fields)
    core = {'date', 'time', 'duration'}
    if not missing_set.issubset(core):
        return get_ask_date_time_duration_prompt(experience_already_set=experience_already_set, is_outcall=is_outcall)
    if missing_set == {'duration'}:
        return get_duration_only_prompt(experience_already_set=experience_already_set, is_outcall=is_outcall)
    if missing_set == {'date'}:
        return "What day were you thinking?"
    if missing_set == {'time'}:
        return "What time works for you? E.g. 8pm or 9pm."
    if missing_set == {'date', 'time'}:
        return "What time works for you?"
    if missing_set == {'date', 'duration'}:
        return append_outcall_duration_minimum_if_needed(
            "How long would you like?"
            + _experience_suffix(experience_already_set=experience_already_set),
            is_outcall,
        )
    if missing_set == {'time', 'duration'}:
        return append_outcall_duration_minimum_if_needed(
            "What time and how long? E.g. 8pm for 1 hr PSE."
            + _experience_suffix(experience_already_set=experience_already_set),
            is_outcall,
        )
    return get_ask_date_time_duration_prompt(experience_already_set=experience_already_set, is_outcall=is_outcall)


# Summary of collected fields
def format_booking_summary(fields: dict) -> str:
    """
    Format booking fields into a summary.

    Args:
        fields: Dict with booking fields

    Returns:
        Formatted summary string
    """
    lines = []

    if fields.get('date'):
        date_str = format_date_ordinal_full(fields['date']) if hasattr(fields['date'], 'strftime') or isinstance(fields['date'], str) else str(fields['date'])
        lines.append(f"\U0001F4C5 {date_str}")

    if fields.get('time'):
        if isinstance(fields['time'], tuple):
            hour, minute = fields['time']
            period = "AM" if hour < 12 else "PM"
            display_hour = hour if hour <= 12 else hour - 12
            if display_hour == 0:
                display_hour = 12
            time_str = f"{display_hour}:{minute:02d}{period}"
        else:
            time_str = str(fields['time'])
        lines.append(f"\U0001F550 {time_str}")

    if fields.get('duration'):
        duration_mins = fields['duration']
        if duration_mins >= 60:
            hours = duration_mins // 60
            mins = duration_mins % 60
            if mins > 0:
                duration_str = f"{hours} hour{'s' if hours > 1 else ''} {mins} min"
            else:
                duration_str = f"{hours} hour{'s' if hours > 1 else ''}"
        else:
            duration_str = f"{duration_mins} minutes"
        lines.append(f"\u23F1 {duration_str}")

    if fields.get('experience_type'):
        lines.append(f"\u2728 {fields['experience_type']}")

    if fields.get('incall_outcall'):
        location = fields['incall_outcall'].capitalize()
        if location == 'Outcall' and fields.get('outcall_address'):
            lines.append(f"\U0001F4CD {location} - {fields['outcall_address']}")
        else:
            lines.append(f"\U0001F4CD {location}")

    return "\n".join(lines)
