# ruff: noqa: E402
"""
Vision Service - Deposit Screenshot Validation using Google Cloud Vision API
Detects text, validates PayID, amount, and payment date, tracks failed attempts.
"""

import logging
import os
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Conditional import - google-cloud-vision may not be installed
try:
    from google.cloud import vision as gcloud_vision
    from google.oauth2 import service_account
    VISION_AVAILABLE = True
except ImportError:
    gcloud_vision = None
    service_account = None
    VISION_AVAILABLE = False
    logger.warning("google-cloud-vision package not installed - Vision disabled")

from config import SERVICE_ACCOUNT_FILE, get_account_name, get_payid

# Pre-compiled regex patterns for amount extraction (avoid recompiling on every call)
_RE_PAYMENT_KEYWORDS = re.compile(
    r'(?:amount|paid|payment|transfer(?:red)?|sent|deposit(?:ed)?|you\s+(?:paid|sent)|total)\s*[:.]?\s*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d{1,4}(?:\.\d{2})?)\b',
    re.IGNORECASE
)
_RE_DOLLAR_AMOUNT = re.compile(r'\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d{1,4}(?:\.\d{2})?)\b')
_RE_SIMPLE_NUMBER = re.compile(r'\b(\d{2,3})\b')

# Import error handling utilities
try:
    from utils.circuit_breaker import circuit_breaker
    from utils.error_handler import retry_with_backoff
except ImportError:
    # Fallback decorators if utilities not available
    def circuit_breaker(*args, **kwargs):
        def decorator(func):
            return func
        return decorator
    def retry_with_backoff(*args, **kwargs):
        def decorator(func):
            return func
        return decorator

# Import Google API errors
try:
    from google.api_core.exceptions import GoogleAPIError, RetryError
except ImportError:
    class _GoogleAPIError(Exception):
        pass
    class _RetryError(Exception):
        pass
    GoogleAPIError = _GoogleAPIError
    RetryError = _RetryError

# Initialize Vision client
vision_client = None

if VISION_AVAILABLE:
    try:
        if SERVICE_ACCOUNT_FILE and os.path.exists(SERVICE_ACCOUNT_FILE):
            assert service_account is not None
            assert gcloud_vision is not None
            credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE)
            vision_client = gcloud_vision.ImageAnnotatorClient(credentials=credentials)
            logger.info("Google Vision client initialized")
        else:
            logger.warning("No service account file - Vision disabled")
    except Exception as e:
        logger.error(f"Vision client init failed: {e}")
        vision_client = None

HAS_VISION = vision_client is not None


class VisionImageReadError(Exception):
    """Raised when Vision API cannot read or decode the image (e.g. invalid/corrupt format)."""
    pass


# Amount regex pattern
MIN_DEPOSIT = 100


def _get_today_date_patterns():
    """Generate regex patterns for today's date in various formats."""
    try:
        from utils.timezone import get_current_datetime
        today = get_current_datetime()
    except Exception as e:
        logger.warning("Timezone fetch failed: %s", e)
        from utils.timezone import get_local_timezone

        today = datetime.now(get_local_timezone())

    # Generate various date format strings for today
    day = today.day
    month = today.month
    year = today.year
    year_short = str(year)[-2:]

    month_names = ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
                   'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
    month_full = ['january', 'february', 'march', 'april', 'may', 'june',
                  'july', 'august', 'september', 'october', 'november', 'december']
    month_name = month_names[month - 1]
    month_name_full = month_full[month - 1]

    # Various date formats to look for
    date_patterns = [
        # DD/MM/YYYY or DD-MM-YYYY
        rf'\b{day:02d}[/\-\.]{month:02d}[/\-\.]{year}\b',
        rf'\b{day}[/\-\.]{month}[/\-\.]{year}\b',
        rf'\b{day:02d}[/\-\.]{month:02d}[/\-\.]{year_short}\b',
        rf'\b{day}[/\-\.]{month}[/\-\.]{year_short}\b',
        # DD/MM without year (common in bank apps)
        rf'\b{day:02d}[/\-\.]{month:02d}\b',
        rf'\b{day}[/\-\.]{month}\b',
        # DD Mon or DD Month
        rf'\b{day}\s*{month_name}\b',
        rf'\b{day}\s*{month_name_full}\b',
        rf'\b{day:02d}\s*{month_name}\b',
        rf'\b{day:02d}\s*{month_name_full}\b',
        # Mon DD or Month DD
        rf'\b{month_name}\s*{day}\b',
        rf'\b{month_name_full}\s*{day}\b',
        rf'\b{month_name}\s*{day:02d}\b',
        rf'\b{month_name_full}\s*{day:02d}\b',
        # With ordinal suffix (1st, 2nd, 3rd, 4th, etc.)
        rf'\b{day}(?:st|nd|rd|th)\s*{month_name}\b',
        rf'\b{day}(?:st|nd|rd|th)\s*{month_name_full}\b',
        # "Today" keyword
        r'\btoday\b',
    ]

    return date_patterns


def _validate_date_in_text(text):
    """
    Check if today's date appears in the extracted text.

    Returns:
        tuple: (date_valid: bool, date_found: str or None)
    """
    text_lower = text.lower()
    date_patterns = _get_today_date_patterns()

    for pattern in date_patterns:
        match = re.search(pattern, text_lower, re.IGNORECASE)
        if match:
            return True, match.group(0)

    return False, None


def _is_vision_image_read_error(message):
    """True if Vision API error indicates the image could not be read/decoded."""
    if not message:
        return False
    msg = message.lower()
    return any(
        x in msg for x in
        ('could not read', 'cannot read', 'invalid image', 'failed to load image',
         'image format', 'decode', 'invalid content', 'not a valid image')
    )


def _extract_text_from_image(image_content):
    """Extract text from image bytes using Vision API."""
    if not HAS_VISION:
        return ""

    try:
        assert vision_client is not None
        assert gcloud_vision is not None
        image = gcloud_vision.Image(content=image_content)
        response = vision_client.text_detection(image=image, timeout=10.0)
        if response.error and response.error.message:
            err_msg = response.error.message
            if _is_vision_image_read_error(err_msg):
                raise VisionImageReadError(err_msg)
            raise Exception(err_msg)
        texts = response.text_annotations
        if texts:
            return texts[0].description  # Full text block
        return ""
    except VisionImageReadError:
        raise
    except Exception as e:
        logger.warning(f"Vision text detection failed: {e}")
        return ""


def _normalize_phone_number(s):
    if not s:
        return ""
    d = re.sub(r'\D+', '', s)
    if d.startswith('61') and len(d) >= 9:
        return '0' + d[2:]
    if d.startswith('0'):
        return d
    return d

def _validate_payid_in_text(text, expected_payid):
    """
    Check if expected PayID appears in extracted text.
    Accepts phone numbers formatted as +61... by normalizing to local 0-prefixed numbers.
    """
    if not expected_payid:
        logger.warning("No PayID configured - cannot validate")
        return False

    text_lower = text.lower()
    expected_lower = expected_payid.lower()

    # Direct match (emails or exact strings)
    if expected_lower in text_lower:
        return True

    # Also check without @ symbol in case OCR misreads it (emails)
    _parts = expected_lower.split('@') if '@' in expected_lower else []
    if len(_parts) == 2 and _parts[0] in text_lower and _parts[1] in text_lower:
        return True

    # Try phone normalization: compare normalized digit sequences
    expected_digits = _normalize_phone_number(expected_payid)
    if expected_digits:
        # Find digit substrings in OCR text
        digit_candidates = re.findall(r'\d{7,}', text)
        for c in digit_candidates:
            if _normalize_phone_number(c) == expected_digits:
                return True

    return False


def _validate_account_name_in_text(text, expected_name):
    """Check if expected account name appears in extracted text."""
    if not expected_name:
        return True  # Skip if no account name configured

    text_lower = text.lower()
    expected_lower = expected_name.lower()

    # Direct match
    if expected_lower in text_lower:
        return True

    # Try matching individual parts of name (first name, last name)
    name_parts = expected_lower.split()
    if len(name_parts) >= 2:
        # Check if both first and last name appear somewhere in text
        first_name = name_parts[0]
        last_name = name_parts[-1]
        if first_name in text_lower and last_name in text_lower:
            return True

    return False


def _extract_amount_from_text(text):
    """
    Extract payment amount from text.
    Prioritizes amounts near payment keywords, then falls back to $ amounts.
    When multiple amounts are found near payment keywords, returns the HIGHEST
    (to avoid returning partial misread values like '1' from '1,000').
    When balance indicators are present and multiple dollar amounts exist,
    returns the lowest dollar amount (balance is usually the largest number).
    """
    text_lower = text.lower()

    # PRIORITY 1: Look for amounts directly after payment keywords (most reliable)
    # This catches "Amount: $20.00", "Paid $100", "Transfer $1,000.00", etc.
    # Handles comma-formatted amounts like 1,000 or 1,000.00
    payment_keywords_pattern = _RE_PAYMENT_KEYWORDS
    keyword_matches = payment_keywords_pattern.findall(text)

    payment_amounts = []
    for m in keyword_matches:
        try:
            amount = int(float(m.replace(',', '')))
            # Accept any amount from $1 to $9999 when near payment keywords
            if 1 <= amount <= 9999:
                payment_amounts.append(amount)
        except ValueError:
            continue

    # If found amounts near payment keywords, return the largest one
    # (avoids picking up partial amounts like the "1" from a misread "1,000")
    if payment_amounts:
        return max(payment_amounts)

    # PRIORITY 2: Look for standalone $ amounts, but be more careful
    # Handles comma-formatted amounts like $1,000 or $1,000.00
    balance_indicators = ['balance', 'available', 'remaining', 'savings']
    has_balance_indicator = any(ind in text_lower for ind in balance_indicators)

    dollar_pattern = _RE_DOLLAR_AMOUNT
    dollar_matches = dollar_pattern.findall(text)

    dollar_amounts = []
    for m in dollar_matches:
        try:
            amount = int(float(m.replace(',', '')))
            # Accept amounts from $10 to $9999
            if 10 <= amount <= 9999:
                dollar_amounts.append(amount)
        except ValueError:
            continue

    if dollar_amounts:
        if has_balance_indicator and len(dollar_amounts) > 1:
            # Balance is usually the largest number \u2014 return the smallest to get the payment
            return min(dollar_amounts)
        else:
            # Return the largest amount found (handles partial comma-split captures)
            return max(dollar_amounts)

    # PRIORITY 3: Last resort - any 2-3 digit number (but NOT 4 digits which could be year)
    simple_pattern = _RE_SIMPLE_NUMBER
    simple_matches = simple_pattern.findall(text)
    for m in simple_matches:
        try:
            amount = int(m)
            # Only consider amounts $10-$999 (3 digits max to avoid years)
            if 10 <= amount <= 999:
                return amount
        except ValueError:
            continue

    return 0


@circuit_breaker(
    name="vision_api",
    failure_threshold=5,
    recovery_timeout=60.0,
    expected_exception=(GoogleAPIError, RetryError, ConnectionError),
    fallback=lambda *args, **kwargs: {
        'valid': False,
        'manual_review_required': True,
        'error': 'Vision API unavailable - manual review required',
        'details': {}
    }
)
@retry_with_backoff(max_retries=2, initial_delay=1.0, exceptions=(GoogleAPIError, RetryError))
def validate_deposit_screenshot_from_bytes(
    image_content,
    phone_number,
    required_amount=MIN_DEPOSIT,
    expected_reference=None,
    *,
    require_payment_reference: bool = True,
):
    """
    Validate deposit screenshot from raw image bytes.

    Args:
        image_content: Raw image bytes
        phone_number: Client's phone number
        required_amount: Required deposit amount (default 100)
        require_payment_reference: When True (default), missing ``expected_reference`` fails closed
            with manual review — required for primary SMS deposit flows. When False, missing
            reference uses legacy rules (amount + >=2 of payid/account/date); use only for
            optional post-confirmation deposits where a reference may never have been issued.

    Returns:
        dict with 'valid': bool, 'deposit_amount': int, 'error': str, 'details': dict
    """
    result: dict[str, Any] = {
        "valid": False,
        "deposit_amount": 0,
        "error": None,
        "details": {
            "amount_found": False,
            "payid_found": False,
            "account_name_found": False,
            "date_found": False,
            "reference_found": False,
            "checks_passed": 0
        }
    }

    # Config toggle: when off, route the screenshot to manual review instead of
    # auto-accepting it. Auto-accepting was a fail-OPEN that let any image through
    # as a valid deposit; the escort must explicitly OK these instead.
    try:
        from core.settings_manager import get_setting
        enabled = (get_setting('deposit_verification_vision') or 'true').strip().lower() in ('true', '1', 'yes')
    except Exception as e:
        logger.warning("Vision setting read failed: %s", e)
        enabled = True
    if not enabled:
        logger.warning(
            "deposit_verification_vision is disabled — routing deposit from %s to manual review",
            phone_number,
        )
        result["valid"] = False
        result["manual_review_required"] = True
        result["error"] = "Vision verification disabled - manual review required"
        return result

    if not HAS_VISION:
        logger.warning(
            "Vision API not configured — routing deposit from %s to manual review",
            phone_number,
        )
        result["manual_review_required"] = True
        result["error"] = "Vision API not configured - manual review required"
        return result

    # Extract text from image bytes
    try:
        extracted_text = _extract_text_from_image(image_content)
    except VisionImageReadError as e:
        logger.warning(f"Vision could not read image: {e}")
        result["error"] = "Could not read image file. Please try saving as JPEG or PNG and upload again."
        return result

    if not extracted_text.strip():
        result["error"] = "No text detected in image"
        return result

    # Validate PayID, account name, amount, and date
    expected_payid = get_payid()
    expected_account_name = get_account_name()
    payid_valid = _validate_payid_in_text(extracted_text, expected_payid)
    account_name_valid = _validate_account_name_in_text(extracted_text, expected_account_name)
    amount = _extract_amount_from_text(extracted_text)
    date_valid, date_found_str = _validate_date_in_text(extracted_text)
    # Always check for expected payment reference (clients receive unique 5-digit refs)
    reference_valid = False
    ref_missing = expected_reference is None or not str(expected_reference).strip()
    if not ref_missing:
        normalized_text = re.sub(r"[^A-Z0-9]", "", extracted_text.upper())
        normalized_reference = re.sub(r"[^A-Z0-9]", "", str(expected_reference).upper())
        reference_valid = bool(normalized_reference and normalized_reference in normalized_text)
    elif require_payment_reference:
        logger.warning(
            "No expected_reference provided to validate against — failing closed (manual review required)",
        )
    result["details"]["reference_found"] = reference_valid

    # Calculate checks passed
    checks_passed = 0
    
    # Amount validation: different logic for incall vs outcall deposits
    amount_valid = False
    if required_amount == 50:  # Incall deposit
        # Accept either $50 or $100 for incall bookings
        amount_valid = amount == 50 or amount == 100
    else:  # Outcall or other deposits
        # Require exact match to avoid accepting underpayments
        amount_valid = amount == required_amount

    if amount_valid:
        checks_passed += 1
        result["details"]["amount_found"] = True
    if payid_valid:
        checks_passed += 1
        result["details"]["payid_found"] = True
    if account_name_valid:
        checks_passed += 1
        result["details"]["account_name_found"] = True
    if date_valid:
        checks_passed += 1
        result["details"]["date_found"] = True

    result["details"]["checks_passed"] = checks_passed
    result["deposit_amount"] = amount

    # Validation rule:
    # Accept if (amount matches AND reference matches) OR (amount matches AND >=2 of {account_name, pay_id, today's date} match)
    non_amount_checks = int(payid_valid) + int(account_name_valid) + int(date_valid)

    if ref_missing and require_payment_reference:
        valid = False
        result["manual_review_required"] = True
    elif amount_valid and reference_valid:
        valid = True
    elif amount_valid and non_amount_checks >= 2:
        valid = True
    else:
        valid = False

    result["valid"] = valid

    if valid:
        logger.info(f"Deposit validated for {phone_number}: ${amount}, date: {date_found_str}")
    else:
        # Build error message - list all failures
        errors = []
        if not payid_valid:
            errors.append("PayID not found on screenshot")
        if not account_name_valid:
            errors.append(f"Account name '{expected_account_name}' not found")
        if not amount_valid:
            if required_amount == 50:  # Incall deposit
                errors.append(f"Amount ${amount} is not a valid incall deposit ($50 or $100)")
            else:
                errors.append(f"Amount ${amount} below required ${required_amount}")
        if not date_valid:
            errors.append("Today's date not showing on payment")
        # Always report missing reference when expected
        if expected_reference and not reference_valid:
            errors.append(f"Payment reference {expected_reference} not found")
        elif ref_missing and require_payment_reference:
            errors.append("No expected payment reference provided to validate against")
        result["error"] = "; ".join(errors) if errors else "Validation failed"

        logger.warning(f"Deposit validation failed for {phone_number}: {result['error']}")

    return result
