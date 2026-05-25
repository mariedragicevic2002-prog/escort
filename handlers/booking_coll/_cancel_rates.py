"""

handlers/booking_coll/_cancel_rates.py

handle_cancel_booking and handle_ask_rates handlers.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import re
from typing import Any

from templates.booking_collection_messages import BOOKING_CANCELLED_NO_PROBLEM

logger = logging.getLogger("adella_chatbot.handlers.collecting")


def handle_goodbye(context: dict[str, Any]) -> dict[str, Any]:
    """Handle farewell mid-COLLECTING — acknowledge without clearing the pending booking."""
    return {
        "messages": ["No worries, feel free to message again when you're ready to book! 😊"],
        "new_state": None,
        "actions": [],
    }


_RESCHEDULE_RE = re.compile(
    r"\b(reschedule|rescheduled|change\s+(it\s+)?to|move\s+(it\s+)?to|"
    r"different\s+(time|day|date))\b",
    re.IGNORECASE,
)


def handle_cancel_booking(context: dict[str, Any]) -> dict[str, Any]:
    """Handle cancel_booking intent in COLLECTING state.

    When the message also contains a reschedule request (e.g.
    "can't make it, can we change to Friday?"), route to provide_field
    so the new date/time is extracted instead of clearing the booking.
    """
    phone_number = context['phone_number']
    state_manager = context['state_manager']
    message = context.get('message', '')

    if _RESCHEDULE_RE.search(message):
        from handlers.booking_coll._provide_field import handle_provide_field
        logger.info("cancel_booking: reschedule signal detected — routing to provide_field")
        return handle_provide_field(context)

    state_manager.clear_booking(phone_number)
    state_manager.update_fields(phone_number, {'message_count': 0})

    return {
        "messages": [BOOKING_CANCELLED_NO_PROBLEM],
        "new_state": "NEW",
        "actions": []
    }


def _format_duration_label(minutes: int) -> str:
    """Render a duration like '30 mins', '1 hour', '1.5 hours'."""
    if minutes < 60:
        return f"{minutes} mins"
    hours = minutes / 60.0
    if hours == int(hours):
        return f"{int(hours)} hour" if int(hours) == 1 else f"{int(hours)} hours"
    return f"{hours:g} hours"


def _price_for_duration(incall: dict, prefix: str, minutes: int) -> int | None:
    """Compute price for an experience prefix (gfe/dgfe/pse) at a given duration in minutes.

    Composes from 60/30/15 base prices: 1.5h = 60+30, 2h = 60*2, 2.5h = 60*2+30, etc.
    """
    try:
        p60 = int(incall.get(f"{prefix}_60") or 0) or None
        p30 = int(incall.get(f"{prefix}_30") or 0) or None
        p15 = int(incall.get(f"{prefix}_15") or 0) or None
    except (TypeError, ValueError):
        return None
    if minutes == 15 and p15:
        return p15
    if minutes == 30 and p30:
        return p30
    if minutes == 60 and p60:
        return p60
    if p60 is None:
        return None
    hours_full, rem = divmod(minutes, 60)
    if rem == 0:
        return p60 * hours_full
    if rem == 30 and p30:
        return p60 * hours_full + p30
    if rem == 15 and p15:
        return p60 * hours_full + p15
    return None


def _build_per_experience_rate_message(duration_minutes: int, phone_number: str) -> str:
    """Single-duration rate comparison across GFE / DGFE / PSE."""
    from core.rates_from_config import _load_pricing
    from config import get_base_url, get_profile_url

    incall = (_load_pricing().get("incall") or {})
    dur_label = _format_duration_label(duration_minutes)
    gfe = _price_for_duration(incall, "gfe", duration_minutes)
    dgfe = _price_for_duration(incall, "dgfe", duration_minutes)
    pse = _price_for_duration(incall, "pse", duration_minutes)

    lines = [f"My rate for {dur_label} would depend on what type of experience you're wanting."]
    if gfe is not None:
        lines.append(f"For GFE (Girlfriend Experience) {dur_label} is ${gfe}.")
    if dgfe is not None:
        lines.append(f"If you were wanting something a little more naughty my DGFE (Dirty Girlfriend Experience) rate for {dur_label} would be ${dgfe}.")
    if pse is not None:
        lines.append(f"Or for my PSE (Pornstar Experience) my rate for {dur_label} would be ${pse}.")

    try:
        experience_url = f"{get_base_url().rstrip('/')}/experience"
    except Exception:
        experience_url = ""
    try:
        profile_url = get_profile_url()
    except Exception:
        profile_url = ""
    try:
        from core.webform_security import get_webform_url
        webform_url = get_webform_url(phone_number)
    except Exception:
        webform_url = ""

    if experience_url:
        lines.append("")
        lines.append(f"For a detailed list of what each experience offers check out {experience_url}")
    if profile_url:
        lines.append(f"You can also view my full rates at {profile_url}")

    tail = "What experience were you thinking?"
    if webform_url:
        tail += f" To speed things up you could always make a booking using my webform {webform_url}"
    lines.append("")
    lines.append(tail)
    return "\n".join(lines)


_EXP_LABELS = {
    "gfe": "GFE (Girlfriend Experience)",
    "dgfe": "DGFE (Dirty Girlfriend Experience)",
    "pse": "PSE (Pornstar Experience)",
}


def _resolve_experience_prefix(experience_value: str) -> str | None:
    """Normalize a stored experience_type value to a pricing prefix (gfe/dgfe/pse)."""
    e = (experience_value or "").lower().replace("-", " ").replace("_", " ").strip()
    if not e:
        return None
    if "dgfe" in e or "dirty" in e:
        return "dgfe"
    if "pse" in e or "pornstar" in e or "porn star" in e:
        return "pse"
    if "gfe" in e or "girlfriend" in e:
        return "gfe"
    return None


def _build_short_duration_rate_message(experience_prefix: str, phone_number: str) -> str:
    """Short-duration price list (15/30/45/60 min) for a single known experience type."""
    from core.rates_from_config import _load_pricing
    from config import get_profile_url

    incall = (_load_pricing().get("incall") or {})
    label = _EXP_LABELS.get(experience_prefix, experience_prefix.upper())
    p15 = _price_for_duration(incall, experience_prefix, 15)
    p30 = _price_for_duration(incall, experience_prefix, 30)
    p45 = _price_for_duration(incall, experience_prefix, 45)
    p60 = _price_for_duration(incall, experience_prefix, 60)

    lines = [f"My {label} short rates:"]
    if p15 is not None:
        lines.append(f"15 mins: ${p15}")
    if p30 is not None:
        lines.append(f"30 mins: ${p30}")
    if p45 is not None:
        lines.append(f"45 mins: ${p45}")
    if p60 is not None:
        lines.append(f"1 hour: ${p60}")

    try:
        profile_url = get_profile_url()
    except Exception:
        profile_url = ""
    try:
        from core.webform_security import get_webform_url
        webform_url = get_webform_url(phone_number)
    except Exception:
        webform_url = ""

    lines.append("")
    if profile_url:
        lines.append(f"You can also view my full rates at {profile_url}")
    tail = "How long did you want to book for?"
    if webform_url:
        tail += f" Or you can fill in my booking webform {webform_url}"
    lines.append(tail)
    return "\n".join(lines)


def handle_ask_rates(context: dict[str, Any]) -> dict[str, Any]:
    """Handle ask_rates intent in COLLECTING state.

    Never ask for date/time/duration again if already provided — only ask for what's missing.
    """
    from handlers.booking_coll._provide_field import handle_provide_field

    phone_number = context['phone_number']
    message = context.get('message', '').strip()
    state_manager = context['state_manager']

    import config as cfg
    from booking.field_collector import FieldCollector

    ai_service = context.get('ai_service')
    field_collector = FieldCollector(cfg, ai_service=ai_service)
    current_fields = state_manager.get_booking_fields(phone_number)

    # Short-circuit: if message looks like a duration answer (e.g. "Hour", "1 hour", "2hrs"),
    # treat as provide_field to avoid sending unsolicited rates
    _duration_hints = re.compile(
        r'^(\d+\.?\d*\s*)?(hour|hr|hrs|hours|min|mins|minutes|h\b)',
        re.IGNORECASE
    )
    pre_missing = field_collector.get_missing_fields(current_fields)
    if _duration_hints.search(message) and 'duration' in (pre_missing or []):
        return handle_provide_field(context)

    extracted = field_collector.extract_fields(message, current_fields)
    if extracted:
        updates = {k: v for k, v in extracted.items() if v is not None and (v != '' or k not in ('outcall_address',))}
        if updates:
            state_manager.update_fields(phone_number, updates)
            current_fields = state_manager.get_booking_fields(phone_number)

    missing = field_collector.get_missing_fields(current_fields)
    _exp = (current_fields.get('experience_type') or '').lower()
    _is_group = any(k in _exp for k in ('doubles', 'mff', 'mmf', 'couples'))
    _exp_known = bool(_exp and _exp not in ('none', 'unspecified'))

    # Per-experience comparison: client asked the price for a specific duration but
    # hasn't picked an experience type yet. Show GFE/DGFE/PSE prices for that duration
    # only and stay in COLLECTING — don't claim "everything I need".
    _dur = current_fields.get('duration')
    if _dur and not _exp_known and not _is_group:
        try:
            _dur_minutes = int(_dur)
        except (TypeError, ValueError):
            try:
                _dur_minutes = int(float(_dur) * 60)
            except (TypeError, ValueError):
                _dur_minutes = 0
        if _dur_minutes > 0:
            return {
                "messages": [_build_per_experience_rate_message(_dur_minutes, phone_number)],
                "new_state": None,
                "actions": [],
            }

    # Single-experience short rate list: experience is known but duration isn't yet.
    # Show 15/30/45/60 min rates for that experience and stay in COLLECTING.
    if _exp_known and not _is_group and not _dur:
        _exp_prefix = _resolve_experience_prefix(_exp)
        if _exp_prefix:
            return {
                "messages": [_build_short_duration_rate_message(_exp_prefix, phone_number)],
                "new_state": None,
                "actions": [],
            }

    # Neither experience nor duration known yet: send the compact "view full rates" message
    # so we don't dump the full GFE rate card unsolicited.
    if not _exp_known and not _is_group and not _dur:
        from config import get_base_url, get_profile_url
        try:
            _profile_url_brief = get_profile_url()
        except Exception:
            _profile_url_brief = ""
        try:
            _experience_url_brief = f"{get_base_url().rstrip('/')}/experience"
        except Exception:
            _experience_url_brief = ""
        try:
            from core.webform_security import get_webform_url
            _webform_url_brief = get_webform_url(phone_number)
        except Exception:
            _webform_url_brief = ""
        _brief_lines: list[str] = []
        if _profile_url_brief:
            _brief_lines.append(f"You can view my full rates at {_profile_url_brief}")
        _brief_lines.append("Let me know how long and what experience you're after.")
        if _experience_url_brief:
            _brief_lines.append(f"For more detail on what each experience offers: {_experience_url_brief}")
        if _webform_url_brief:
            _brief_lines.append(f"You can also make a booking by using my webform: {_webform_url_brief}")
        return {
            "messages": ["\n\n".join(_brief_lines)],
            "new_state": None,
            "actions": [],
        }

    from core.rates_from_config import (
        format_rates_message,
        format_doubles_escort_supplied_rates_message,
    )

    if _is_group:
        return {
            "messages": [format_doubles_escort_supplied_rates_message()],
            "new_state": None,
            "actions": []
        }
    else:
        rates_message = format_rates_message(include_extended=False) + "\n\nFor longer bookings, please ask!"

    try:
        from config import get_profile_url
        profile_url = get_profile_url()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        profile_url = ""

    if profile_url:
        rates_message += f"\n\nYou can also view my full rates at {profile_url}"

    if not missing:
        return {
            "messages": [rates_message + "\n\nI have everything I need. Let me check my availability!"],
            "new_state": "CHECKING_AVAILABILITY",
            "actions": ["check_calendar"]
        }

    from templates.field_prompts import get_prompt_for_missing_core_fields
    _is_oc = str((current_fields.get("incall_outcall") or "")).lower() == "outcall"
    prompt = get_prompt_for_missing_core_fields(missing, experience_already_set=_is_group, is_outcall=_is_oc)

    return {
        "messages": [rates_message + "\n\nI still need: " + prompt],
        "new_state": None,
        "actions": []
    }
