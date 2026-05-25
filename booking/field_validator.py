"""
Field Validator - Validation rules for booking fields.
"""

import logging
import re
from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any

from booking.outcall_verification import GeocoderUnavailable

from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("escort_chatbot.field_validator")

# Sentinel for outcall duration < 1 hour so handler can return the dedicated template
OUTCALL_MINIMUM_1_HOUR = "OUTCALL_MINIMUM_1_HOUR"

# Normalized keys: strip, collapse spaces, uppercase, underscores -> spaces (see _normalize_experience_key).
_EXPERIENCE_TYPES_ALLOWED = frozenset(
    {
        "GFE",
        "PSE",
        "DGFE",
        "DINNER DATE",
        "COUPLES MFF",
        "DOUBLES MFF",
        "DOUBLES MMF",
        "MMF",
        "FFM",
        "MFF",
        "DOUBLES MFF GFE",
        "DOUBLES MFF DGFE",
        "DOUBLES MFF PSE",
        "MMF THREESOME",
    }
)


def _normalize_experience_key(experience_type: str) -> str:
    s = str(experience_type).strip().upper().replace("_", " ")
    return " ".join(s.split())


def is_outcall_too_far_error(message: str) -> bool:
    """
    True if the CBD/15km "too far" outcall verification error is returned.
    Used so handlers can distinguish "too far" (block) from other verification
    failures (e.g. address not found, wrong city) for available-now leniency.
    """
    if not message or not isinstance(message, str):
        return False
    m = message.lower()
    return "15km" in m or ("current location" in m and "max" in m)


class FieldValidator:
    """Validates booking fields."""

    def __init__(self, config=None):
        """
        Initialize validator.

        Args:
            config: Configuration module (optional for testing)
        """
        self.config = config or self._default_config()
        self._last_verified_hotel_info: dict | None = None
    
    def _default_config(self):
        """Return default config for testing."""
        class DefaultConfig:
            DEFAULT_TIMEZONE = "Australia/Sydney"
        return DefaultConfig()

    def validate_date(self, date: datetime) -> tuple[bool, str]:
        """
        Validate date.

        Args:
            date: Date to validate

        Returns:
            (is_valid, error_message)
        """
        if not date:
            return False, "Date is required"
        
        # Check type
        if not isinstance(date, (datetime, date_type)):
            return False, "Invalid date format"

        # Single booking clock: same helper patched in simulations / deterministic tests.
        from utils.timezone import get_current_datetime

        today = get_current_datetime().date()

        # Normalise: accept both datetime and date objects
        booking_date = date.date() if isinstance(date, datetime) else date

        # Can't book in the past
        if booking_date < today:
            return False, "Cannot book dates in the past"

        # Can't book more than 30 days in advance
        max_advance = today + timedelta(days=30)
        if booking_date > max_advance:
            return False, "Cannot book more than 30 days in advance"

        return True, ""

    def validate_time(self, time: tuple[int, int]) -> tuple[bool, str]:
        """
        Validate time.

        Args:
            time: (hour, minute) tuple

        Returns:
            (is_valid, error_message)
        """
        if not time:
            return False, "Time is required"

        # Accept both (hour, minute) tuple and datetime.time object
        import datetime as _dt
        if isinstance(time, _dt.time):
            hour, minute = time.hour, time.minute
        elif hasattr(time, '__len__') and len(time) == 2:
            hour, minute = time
        else:
            return False, "Time is required"

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return False, "Invalid time format"

        # "Within available hours" is not checked here; it is enforced later in the
        # booking collection handler via check_within_available_hours_and_days using
        # admin settings (available_hours, available_days), so configured hours apply.

        return True, ""

    def validate_duration(self, duration: int, incall_outcall: str = None) -> tuple[bool, str]:
        """
        Validate duration. Incall minimum 15 minutes; outcall minimum 1 hour.

        Args:
            duration: Duration in minutes
            incall_outcall: Optional 'incall' or 'outcall' for type-specific minimums

        Returns:
            (is_valid, error_message). For outcall < 1 hour returns OUTCALL_MINIMUM_1_HOUR sentinel.
        """
        if not duration:
            return False, "Duration is required"

        incall_outcall_lower = (incall_outcall or "").lower()
        if incall_outcall_lower == "outcall" and duration < 60:
            return False, OUTCALL_MINIMUM_1_HOUR

        # Incall (or unspecified) minimum 15 minutes
        if duration < 15:
            return False, "Minimum booking duration is 15 minutes"

        # Maximum 4 hours (overnight bookings are handled separately)
        if duration > 240:
            return False, "For bookings over 4 hours, please contact me directly"

        # Must be in 15-minute increments (so 15, 30, 45, 60, etc. are valid)
        if duration % 15 != 0:
            return False, "Duration must be in 15-minute increments"

        return True, ""

    def validate_experience_type(self, experience_type: str) -> tuple[bool, str]:
        """
        Validate experience type.

        Args:
            experience_type: GFE, PSE, DGFE, Dinner Date, couples/doubles/group codes, etc.

        Returns:
            (is_valid, error_message)
        """
        # Experience type is optional, defaults to GFE
        if not experience_type:
            return True, ""

        try:
            import unicodedata

            experience_type = unicodedata.normalize("NFKC", str(experience_type)).strip()
        except (TypeError, ValueError) as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
            experience_type = str(experience_type).strip()

        key = _normalize_experience_key(experience_type)
        if key not in _EXPERIENCE_TYPES_ALLOWED:
            return False, "Experience type is not recognised (e.g. GFE, PSE, DGFE, Dinner Date, couples, doubles)"

        return True, ""

    def validate_incall_outcall(self, incall_outcall: str) -> tuple[bool, str]:
        """
        Validate incall/outcall.

        Args:
            incall_outcall: incall or outcall

        Returns:
            (is_valid, error_message)
        """
        # Incall/outcall is optional, defaults to incall
        if not incall_outcall:
            return True, ""

        if incall_outcall.lower() not in ["incall", "outcall"]:
            return False, "Must specify incall or outcall"

        return True, ""

    def validate_outcall_address(self, address: str, incall_outcall: str, city: str = None) -> tuple[bool, str]:
        """
        Validate outcall address and verify it's within 15km of the escort's current location.

        Args:
            address: Outcall address
            incall_outcall: incall or outcall
            city: City name (optional, uses current location if not provided)

        Returns:
            (is_valid, error_message)
        """
        # Only required for outcalls
        if incall_outcall != "outcall":
            return True, ""

        if not address or len(address.strip()) < 5:
            return False, "Outcall address is required. Please provide hotel name or address."

        # Reject obvious non-address text before external verification calls.
        _addr = address.strip().lower()
        if _addr in {"today", "tonight", "tomorrow", "now", "asap", "my place", "my home", "my hotel"}:
            return False, "Outcall address is required. Please provide hotel name or address."
        if re.fullmatch(
            r"(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?(?:\s+(?:today|tonight|tomorrow|now|asap))?",
            _addr,
            re.IGNORECASE,
        ):
            return False, "Outcall address is required. Please provide hotel name or address."

        # Verify address is within 15km of the escort's current location.
        # Failure messages from verify_hotel_in_cbd: "too far" (contains "15km" or "current location" + "max"),
        # "address not found", "can't find that hotel near...", or API exception (strict mode: "Verification failed...").
        # Handlers use is_outcall_too_far_error(message) to treat only "too far" as hard block; others can be
        # lenient for available-now outcall.
        try:
            from booking.outcall_verification import normalize_outcall_address_for_verification, verify_hotel_in_cbd
            address_to_verify = normalize_outcall_address_for_verification(address, city) or address
            logger.info(f"[OUTCALL VERIFY] Original: '{address}' -> Normalized: '{address_to_verify}' (city={city})")
            is_valid, message, hotel_info = verify_hotel_in_cbd(address_to_verify, city)
            logger.info(f"[OUTCALL VERIFY] Result: is_valid={is_valid}, message='{message}', distance={hotel_info.get('distance_km')}")
            
            if not is_valid:
                logger.warning(f"[OUTCALL VERIFY] Address validation failed for '{address}': {message}")
                return False, message
            
            # Address is valid - store verified info for handler to use
            self._last_verified_hotel_info = hotel_info
            logger.info(f"Outcall address validated: {address} - {hotel_info.get('distance_km')}km from escort location")
            return True, ""
            
        except GeocoderUnavailable as e:
            logger.error(f"[OUTCALL VERIFY] Geocoder unavailable: {e}")
            try:
                from core.settings_manager import get_setting
                strict_mode = get_setting('outcall_verification_strict', 'false').lower() == 'true'
                if strict_mode:
                    return False, "Verification failed. Please provide a valid hotel name or address."
                logger.warning("Outcall geocoder unavailable but allowing booking (lenient mode)")
                return True, ""
            except Exception as setting_err:
                logger.warning(LOG_SUPPRESSED_FMT, setting_err, exc_info=False)
                return True, ""

        except Exception as e:
            logger.error(f"Outcall validation error: {e}")
            # Check admin setting for strict/lenient mode
            try:
                from core.settings_manager import get_setting
                strict_mode = get_setting('outcall_verification_strict', 'false').lower() == 'true'
                if strict_mode:
                    # Strict mode: Reject booking if verification fails
                    return False, "Verification failed. Please provide a valid hotel name or address."
                else:
                    # Lenient mode: Allow booking if verification fails (API issues)
                    logger.warning(f"Outcall verification failed but allowing booking (lenient mode): {e}")
                    return True, ""
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                # If setting check fails, default to lenient mode
                logger.warning(f"Outcall verification failed but allowing booking (default lenient): {e}")
                return True, ""

    def validate_all(self, fields: dict[str, Any]) -> tuple[bool, list]:
        """
        Validate all booking fields.

        Args:
            fields: Dict with all booking fields

        Returns:
            (all_valid, list_of_errors)
        """
        errors = []

        # Validate date
        if fields.get('date'):
            valid, error = self.validate_date(fields['date'])
            if not valid:
                errors.append(error)

        # Validate time
        if fields.get('time'):
            valid, error = self.validate_time(fields['time'])
            if not valid:
                errors.append(error)

        # Validate duration (incall min 15 min, outcall min 1 hour)
        if fields.get('duration'):
            valid, error = self.validate_duration(
                fields['duration'],
                incall_outcall=fields.get('incall_outcall'),
            )
            if not valid:
                errors.append(error)

        # Validate experience type
        if fields.get('experience_type'):
            valid, error = self.validate_experience_type(fields['experience_type'])
            if not valid:
                errors.append(error)

        # Validate incall/outcall
        if fields.get('incall_outcall'):
            valid, error = self.validate_incall_outcall(fields['incall_outcall'])
            if not valid:
                errors.append(error)

        # Validate outcall address (skip until provided for dinner dates — venue is collected after date/time)
        if fields.get('incall_outcall') == 'outcall':
            from utils.dinner_date import is_dinner_date_booking

            if is_dinner_date_booking(fields) and not (fields.get('outcall_address') or '').strip():
                pass
            else:
                try:
                    import config
                    location_info = config.get_current_incall_location()
                    city = location_info.get('city')
                except Exception as e:
                    logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
                    city = None

                valid, error = self.validate_outcall_address(
                    fields.get('outcall_address'),
                    fields.get('incall_outcall'),
                    city=city
                )
                if not valid:
                    errors.append(error)

        return len(errors) == 0, errors
