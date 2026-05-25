"""
Deposit Handler
Calculates deposit requirements based on booking type and client behavior.
Uses admin settings for incall/outcall amounts when available.
"""

import logging
import re

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger(__name__)

PROFANITY_DEPOSIT_THRESHOLD = 3
_DEFAULT_PROFANITY_WORDS = [
    "shit",
    "crap",
    "damn",
    "idiot",
    "stupid",
    "fucking",
    "fuck",
]


def _get_deposit_incall() -> int:
    """Incall deposit amount from Rates page."""
    try:
        from core.rates_from_config import get_deposit_incall
        return get_deposit_incall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return 50


def _get_deposit_outcall() -> int:
    """Outcall deposit amount from Rates page."""
    try:
        from core.rates_from_config import get_deposit_outcall
        return get_deposit_outcall()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return 100

# Phrases that look like unsafe requests but are NOT \u2014 strip these first
_UNSAFE_EXCLUSIONS = [
    'blowjob without condom', 'blowjob no condom', 'blowjob without a condom',
    'natural blowjob', 'natural bj',
    'bj without condom', 'bj no condom', 'bj without a condom',
]

# Patterns that, after exclusions are removed, signal unsafe service
_UNSAFE_KEYWORDS = [
    'bareback',
    'raw sex', 'raw',
    'no condom', 'without condom', 'without a condom',
    'unprotected',
    'natural sex',
    'do u do natural', 'do you do natural',
]


def check_unsafe_service(message: str) -> bool:
    """Return True if the message requests an unsafe/unprotected service.

    Blowjob-specific 'no condom' requests are excluded because the escort
    only offers protected services regardless; those are handled by a
    separate 'protected only' response, not the unsafe-service deposit flag.
    """
    if not message:
        return False
    msg = message.lower()
    for excl in _UNSAFE_EXCLUSIONS:
        msg = msg.replace(excl, '')
    return any(kw in msg for kw in _UNSAFE_KEYWORDS)


def get_profanity_words() -> list[str]:
    """Return profanity words configured in Admin settings only."""
    try:
        from core.settings_manager import get_setting
        raw = (get_setting("profanity_words") or "").strip()
        if raw:
            return list(dict.fromkeys([w.strip().lower() for w in raw.splitlines() if w.strip()]))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return list(_DEFAULT_PROFANITY_WORDS)


def count_profanity_words(message: str) -> int:
    """Count profanity words in message.

    Args:
        message: Message text to check

    Returns:
        Number of profanity words found
    """
    if not message:
        return 0

    words = get_profanity_words()
    message_lower = message.lower()
    count = 0
    for word in words:
        count += len(re.findall(r'\b' + re.escape(word) + r'\b', message_lower))
    return count


def check_profanity_trigger(message: str) -> bool:
    """Returns True if 3+ profanity words detected (triggers mandatory deposit).

    Args:
        message: Message text to check

    Returns:
        True if message contains 3 or more profanity words
    """
    return count_profanity_words(message) >= PROFANITY_DEPOSIT_THRESHOLD


def _setting_enabled(setting_key: str, default: bool = False) -> bool:
    try:
        from core.settings_manager import get_setting

        raw = get_setting(setting_key)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return default

    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ('true', '1', 'yes')


def _setting_float(setting_key: str, default: float) -> float:
    try:
        from core.settings_manager import get_setting

        raw = get_setting(setting_key)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return default

    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _scale_deposit_for_duration(base_amount: int, booking_minutes: int, base_hours: float) -> int:
    """Scale deposit amount proportionally with booking duration."""
    if base_hours <= 0:
        return base_amount
    base_minutes = base_hours * 60
    if booking_minutes <= base_minutes:
        return base_amount
    scaled = int(base_amount * booking_minutes / base_minutes)
    return scaled


def _get_scaled_amount(
    base_amount: int,
    booking_minutes: int,
    scale_setting_key: str,
    base_hours_setting_key: str,
    default_base_hours: float,
) -> int:
    if not _setting_enabled(scale_setting_key):
        return base_amount
    base_hours = _setting_float(base_hours_setting_key, default_base_hours)
    return _scale_deposit_for_duration(base_amount, booking_minutes, base_hours)


def calculate_deposit_requirement(booking_fields: dict, phone_number: str, state_manager=None) -> tuple[bool, int, str]:
    """Calculate if deposit required and amount.

    Mandatory Deposit Triggers (amounts from Rates page):
    1. Outcalls → deposit_outcall
    2. Doubles Experience (MFF) bookings (escort + another escort) → deposit_mff_pair
    3. Overnight (4+ hours duration) → deposit_overnight; outcall also uses deposit_extended_experience_outcall
    4. Dinner date outcall → deposit_dinner_date_outcall
    5. Incall 2+ hours (120+ minutes) → deposit_incall
    6. Profanity (3+ words from list) → deposit_outcall (MANDATORY - even for incall)

    Args:
        booking_fields: Dict with booking details (incall_outcall, experience_type, duration)
        phone_number: Client's phone number
        state_manager: Optional state manager to check profanity flag

    Returns:
        (required: bool, amount: int, reason: str)
    """
    triggers = []
    amount = 0
    mandatory_triggers = {'profanity', 'unsafe_service'}

    duration_raw = booking_fields.get('duration', 60)  # in minutes
    duration = 60 if duration_raw is None else duration_raw
    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 60

    is_outcall = booking_fields.get('incall_outcall') == 'outcall'
    if is_outcall:
        triggers.append("outcall")
        amount = _get_scaled_amount(
            _get_deposit_outcall(),
            duration,
            'deposit_outcall_scale_duration',
            'deposit_outcall_base_hours',
            1.0,
        )

    booking_type = (booking_fields.get('booking_type') or '').lower()
    experience = (booking_fields.get('experience_type') or '').lower()
    combined = f"{booking_type} {experience}"
    if booking_type == 'dinner_date' or 'dinner' in experience:
        triggers.append("dinner_date")
        if is_outcall:
            try:
                from core.rates_from_config import get_deposit_dinner_date_outcall

                dinner_amount = _get_scaled_amount(
                    get_deposit_dinner_date_outcall(),
                    duration,
                    'deposit_dinner_date_outcall_scale_duration',
                    'deposit_dinner_date_outcall_base_hours',
                    2.0,
                )
                amount = max(amount, dinner_amount)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                amount = max(amount, 100)
        else:
            amount = max(amount, 100)

    if any(word in experience for word in ['double', 'threesome', 'couple', 'doubles', 'doubles_mff', 'mff']):
        triggers.append("doubles_mff")
        try:
            from core.rates_from_config import get_deposit_mff_pair

            group_amount = _get_scaled_amount(
                get_deposit_mff_pair(),
                duration,
                'deposit_group_scale_duration',
                'deposit_group_base_hours',
                2.0,
            )
            amount = max(amount, group_amount)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            amount = max(amount, 200)

    if duration >= 240:
        triggers.append("overnight")
        try:
            from core.rates_from_config import (
                get_deposit_extended_experience_outcall,
                get_deposit_overnight,
            )

            amount = max(amount, get_deposit_overnight())
            if is_outcall:
                extended_amount = _get_scaled_amount(
                    get_deposit_extended_experience_outcall(),
                    duration,
                    'deposit_extended_experience_outcall_scale_duration',
                    'deposit_extended_experience_outcall_base_hours',
                    2.0,
                )
                amount = max(amount, extended_amount)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            amount = max(amount, 200)

    if booking_type in ("dirty_weekend", "weekend", "weekend_booking") or any(
        marker in combined for marker in ("dirty weekend", "weekend package", "whole weekend", "48 hour", "48hr")
    ):
        triggers.append("weekend")
        try:
            from core.rates_from_config import get_deposit_overnight
            amount = max(amount, get_deposit_overnight())
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            amount = max(amount, 200)

    if booking_type in ("fly_me", "fmty", "fly_me_to_you") or any(
        marker in combined for marker in ("fly me", "fly-me", "fmty", "fly you out")
    ):
        triggers.append("fly_me_to_you")
        try:
            from core.rates_from_config import get_deposit_overnight
            amount = max(amount, get_deposit_overnight())
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            amount = max(amount, 200)

    if booking_type in ("filming", "pse_filming") or "filming" in combined:
        triggers.append("filming")
        filming_amount = _get_deposit_outcall() if is_outcall else _get_deposit_incall()
        if is_outcall:
            filming_amount = _get_scaled_amount(
                filming_amount,
                duration,
                'deposit_outcall_scale_duration',
                'deposit_outcall_base_hours',
                1.0,
            )
        amount = max(amount, filming_amount)

    if not is_outcall and duration >= 120:
        triggers.append("extended_duration")
        incall_amount = _get_scaled_amount(
            _get_deposit_incall(),
            duration,
            'deposit_incall_scale_duration',
            'deposit_incall_base_hours',
            1.0,
        )
        amount = max(amount, incall_amount)

    if state_manager:
        state = state_manager.get_state(phone_number)
        profanity_on = _setting_enabled('profanity_deposit_enabled', default=True)
        if profanity_on and state and state.get('profanity_detected'):
            triggers.append("profanity")
            amount = max(amount, _get_deposit_outcall())

        if state and state.get('unsafe_service_requested'):
            triggers.append("unsafe_service")
            amount = max(amount, _get_deposit_incall())

    required = len(triggers) > 0
    if required and not any(trigger in mandatory_triggers for trigger in triggers):
        if is_outcall and not _setting_enabled('require_deposits', default=True):
            required = False
        elif not is_outcall and not _setting_enabled('require_incall_deposits', default=True):
            required = False

    reason = ", ".join(triggers) if required and triggers else ""
    if not required:
        amount = 0

    if required:
        logger.info(f"Deposit required for {phone_number}: ${amount} ({reason})")
    else:
        logger.info(f"No deposit required for {phone_number}")

    return required, amount, reason


def build_deposit_gate_response(
    *,
    booking_fields: dict,
    phone_number: str,
    state_manager,
    client_name: str | None = None,
    preamble: str = "Before we continue, a deposit is required.",
    default_reason: str = "booking",
    default_amount: int = 100,
    reason_filter: set[str] | None = None,
) -> dict | None:
    """Compute, persist, and render a DEPOSIT_REQUIRED response.

    Returns None when no deposit is required (or when reason_filter excludes this trigger).
    """
    required, amount, reason = calculate_deposit_requirement(
        booking_fields,
        phone_number,
        state_manager,
    )
    if not required:
        return None

    norm_reason = str(reason or default_reason).strip() or default_reason
    if reason_filter:
        reason_tokens = {tok.strip() for tok in norm_reason.split(",") if tok.strip()}
        allowed = {str(tok).strip() for tok in reason_filter if str(tok).strip()}
        if not (reason_tokens & allowed):
            return None

    try:
        amount_int = int(amount or default_amount)
    except (TypeError, ValueError):
        amount_int = int(default_amount)
    if amount_int <= 0:
        amount_int = int(default_amount)

    persisted = bool(
        state_manager.update_fields(
            phone_number,
            {
                "deposit_required": True,
                "deposit_amount": amount_int,
                "deposit_reason": norm_reason,
            },
        )
    )
    if not persisted:
        logger.error(
            "Failed to persist deposit gate state for %s (reason=%s amount=%s)",
            phone_number,
            norm_reason,
            amount_int,
        )
        return {
            "messages": [
                "Sorry, I couldn't save your booking details just now. Please try again in a moment."
            ],
            "new_state": None,
            "actions": [],
        }

    from templates.confirmations import get_deposit_request_message

    deposit_msg = get_deposit_request_message(
        amount_int,
        norm_reason,
        phone_number=phone_number,
        client_name=(client_name or None),
        booking_fields=booking_fields,
    )
    return {
        "messages": [f"{preamble}\n\n{deposit_msg}"],
        "new_state": "DEPOSIT_REQUIRED",
        "actions": [],
    }


def resolve_outcall_surcharge_and_deposit_for_message(
    booking_fields: dict | None,
    phone_number: str | None,
    state_manager=None,
) -> tuple[int, int]:
    """Return (surcharge, deposit) for outcall policy SMS copy.

    Surcharge follows ``get_outcall_travel_surcharge_for_booking`` (0 for dinner date,
    overnight, FMTY, dirty weekend packages). When ``booking_fields`` includes
    ``incall_outcall`` == ``'outcall'``, the deposit matches
    ``calculate_deposit_requirement`` (dinner, doubles, overnight, state flags).
    Otherwise the configured outcall deposit is used.
    """
    from core.rates_from_config import get_outcall_travel_surcharge_for_booking, get_surcharge

    deposit = _get_deposit_outcall()
    if not booking_fields or booking_fields.get("incall_outcall") != "outcall":
        try:
            surcharge = int(get_surcharge())
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            surcharge = 100
        return surcharge, deposit

    try:
        surcharge = int(get_outcall_travel_surcharge_for_booking(booking_fields))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        try:
            surcharge = int(get_surcharge())
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            surcharge = 100

    pn = (phone_number or "").strip() or "unknown"
    try:
        required, amount, _reason = calculate_deposit_requirement(
            booking_fields, pn, state_manager
        )
        if required and amount > 0:
            return surcharge, amount
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        logger.exception("resolve_outcall_surcharge_and_deposit_for_message failed")

    return surcharge, deposit
