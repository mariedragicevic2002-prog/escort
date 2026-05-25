"""
Webform Security - Secure token-based booking form links.

Features:
- One-time use tokens
- Time-limited tokens (1 hour)
- Phone number binding
- Short URL support
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytz

import config
from services.database_service import get_shared_db

logger = logging.getLogger("adella_chatbot.webform_security")


def generate_short_code():
    """Generate a short 6-character code for URLs."""
    from core.token_utils import generate_short_code as _gen
    return _gen()


def generate_secure_token(phone_number, use_short_url=True):
    """Generate a cryptographically secure token for webform access.

    Args:
        phone_number: Client's phone number to bind token to
        use_short_url: If True, also generate a short code

    Returns:
        dict: {'token': full_token, 'short_code': code} or just str token
    """
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        logger.warning("generate_secure_token: no database connection (DATABASE_URL / pool)")
        return None

    # Validate phone_number is plausible before proceeding
    if not phone_number or not isinstance(phone_number, str) or len(phone_number.strip()) < 6:
        logger.warning("generate_secure_token: invalid phone_number supplied")
        return None

    # Generate random 32-byte token
    token = secrets.token_urlsafe(32)

    # Create SHA-256 hash for database storage
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Set expiration (1 hour from now)
    expires_at = datetime.now(pytz.UTC) + timedelta(hours=1)

    try:
        if use_short_url:
            # Generate unique short code
            short_code = generate_short_code()

            try:
                db.execute_query("""
                    INSERT INTO webform_tokens
                    (phone_number, token_hash, short_code, created_at, expires_at, used)
                    VALUES (%s, %s, %s, NOW(), %s, false)
                """, (phone_number, token_hash, short_code, expires_at), fetch=False)

                return {'token': token, 'short_code': short_code}
            except Exception as e:
                logger.warning(f"Short code generation failed: {e}")
                use_short_url = False

        if not use_short_url:
            db.execute_query("""
                INSERT INTO webform_tokens
                (phone_number, token_hash, created_at, expires_at, used)
                VALUES (%s, %s, NOW(), %s, false)
            """, (phone_number, token_hash, expires_at), fetch=False)

            return token
    except Exception as e:
        logger.error(f"Failed to generate webform token: {e}")
        return None


def get_webform_url(phone_number: str) -> str:
    """Return a secure short booking-form URL bound to ``phone_number``.

    Generates a short-code token via :func:`generate_secure_token` and
    returns ``<base_url>/b/<short_code>``.  Falls back to the generic
    ``<base_url>/booking`` path if token generation fails so callers
    always receive a usable URL.

    This is the single authoritative helper — use it instead of inlining
    the ``generate_secure_token`` + fallback pattern throughout handlers.
    """
    try:
        token_data = generate_secure_token(phone_number, use_short_url=True)
        if token_data and isinstance(token_data, dict) and token_data.get("short_code"):
            return f"{config.get_base_url()}/b/{token_data['short_code']}"
    except Exception as e:
        logger.warning("get_webform_url: token generation failed for %s: %s", phone_number, type(e).__name__)
    return f"{config.get_base_url()}/booking"


def get_webform_payment_url(
    phone_number: str,
    *,
    mandatory: bool = True,
    amount: int | None = None,
    reason: str = "",
    outcall_address: str | None = None,
) -> str:
    """Return ``/b/<short_code>/payment`` URL with optional query parameters.

    Prefers the latest stored short code for the phone number; creates a new
    one if needed. Falls back to ``/booking`` when token creation fails.
    """
    base = (config.get_base_url() or "").strip().rstrip("/") or "(base_url)"
    if not phone_number:
        return f"{base}/booking"

    short_code = None
    try:
        short_code = get_latest_short_code_for_phone(phone_number)
        if not short_code:
            token_data = generate_secure_token(phone_number, use_short_url=True)
            if token_data and isinstance(token_data, dict):
                short_code = (token_data.get("short_code") or "").strip() or None
    except Exception as e:
        logger.warning("get_webform_payment_url: short_code lookup/create failed: %s", type(e).__name__)
        short_code = None

    if not short_code:
        return f"{base}/booking"

    query: dict[str, str | int] = {"mode": "mandatory" if mandatory else "optional"}
    if amount is not None:
        try:
            query["amount"] = int(amount)
        except (TypeError, ValueError):
            pass
    if reason:
        query["reason"] = str(reason)
    if outcall_address and str(outcall_address).strip():
        query["address"] = str(outcall_address).strip()[:500]

    return f"{base}/b/{short_code}/payment?{urlencode(query)}"


def validate_webform_token(token, phone_number, is_token_hash=False):
    """Validate a webform token.

    Args:
        token: The token from URL parameter
        phone_number: Phone number from form submission
        is_token_hash: If True, token is already a hash

    Returns:
        tuple: (is_valid, error_message)
    """
    if not token or not phone_number:
        return False, "Missing token or phone number"

    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("validate_webform_token: database unavailable")
            return False, "Booking service is temporarily unavailable. Please try again shortly."

        # Hash the provided token if needed
        if is_token_hash:
            token_hash = token
        else:
            token_hash = hashlib.sha256(token.encode()).hexdigest()

        # Look up token in database
        result = db.execute_query("""
            SELECT phone_number, expires_at, used, COALESCE(use_count, 0) as use_count
            FROM webform_tokens
            WHERE token_hash = %s
        """, (token_hash,), fetch=True)

        if not result:
            return False, "Invalid or expired link. Please request a new booking link."

        from utils.row_utils import row_get
        token_row = result[0]
        if isinstance(token_row, dict):
            token_data = token_row
        else:
            token_data = {
                'phone_number': row_get(token_row, 0, None),
                'expires_at': row_get(token_row, 1, None),
                'used': row_get(token_row, 2, False),
                'use_count': row_get(token_row, 3, 0),
            }

        # Check if token has been used
        use_count = token_data.get('use_count', 0)
        if use_count >= 1:
            return False, "This booking link has already been used. Please request a new link."

        # Check if phone number matches
        if token_data['phone_number'] != phone_number:
            return False, "This booking link was issued to a different phone number."

        # Check if token has expired
        now = datetime.now(pytz.UTC)
        expires_at = token_data['expires_at']

        # Handle timezone
        if expires_at.tzinfo is None:
            expires_at = pytz.UTC.localize(expires_at)
        else:
            expires_at = expires_at.astimezone(pytz.UTC)

        if now > expires_at:
            hours_expired = (now - expires_at).total_seconds() / 3600
            return False, f"This booking link expired {int(hours_expired)} hours ago."

        # Token is valid
        return True, None

    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return False, "Unable to validate booking link. Please try again."


def mark_token_as_used(token, is_token_hash=False, *, conn=None):
    """Mark token as used.

    Args:
        token: The token that was just used
        is_token_hash: If True, token is already a hash
        conn: Optional DB connection (same transaction as other writes)

    Returns:
        bool: True if marked successfully
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            return False

        if is_token_hash:
            token_hash = token
        else:
            token_hash = hashlib.sha256(token.encode()).hexdigest()

        db.execute_query(
            """
            UPDATE webform_tokens
            SET use_count = COALESCE(use_count, 0) + 1,
                used = true
            WHERE token_hash = %s
            """,
            (token_hash,),
            fetch=False,
            conn=conn,
        )

        return True
    except Exception as e:
        logger.error(f"Failed to mark token as used: {e}")
        return False


def get_latest_short_code_for_phone(phone_number: str):
    """Return the most recent webform short_code for this phone (for payment-info links).

    Used to build /b/<short_code>/payment URLs in SMS without embedding PayID/URLs in the text.
    """
    if not phone_number:
        return None
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("get_latest_short_code_for_phone: database unavailable")
            return None
        rows = db.execute_query(
            """
            SELECT short_code FROM webform_tokens
            WHERE phone_number = %s
            ORDER BY created_at DESC NULLS LAST
            LIMIT 1
            """,
            (phone_number,),
            fetch=True,
        )
        from utils.row_utils import row_get
        if rows and isinstance(rows[0], dict):
            return (rows[0].get("short_code") or "").strip() or None
        if rows and row_get(rows[0], 0, None) is not None:
            return (row_get(rows[0], 0, "") or "").strip() or None
    except Exception as e:
        logger.warning("get_latest_short_code_for_phone failed: %s", e)
    return None


def get_phone_number_from_token(token):
    """Get phone number associated with a token.

    Args:
        token: The token to look up (can be original token or token_hash)

    Returns:
        str: Phone number or None if not found/expired
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("get_phone_number_from_token: database unavailable")
            return None

        # Check if token is already a hash (64 characters) or original token
        # Short code links pass token_hash directly, regular links pass original token
        if len(token) == 64 and all(c in '0123456789abcdef' for c in token.lower()):
            # Already a hash (from short code link)
            token_hash = token.lower()
        else:
            # Original token - hash it
            token_hash = hashlib.sha256(token.encode()).hexdigest()

        result = db.execute_query("""
            SELECT phone_number, expires_at, used, COALESCE(use_count, 0) as use_count
            FROM webform_tokens
            WHERE token_hash = %s
        """, (token_hash,), fetch=True)

        if not result:
            return None

        from utils.row_utils import row_get
        token_row = result[0]
        if isinstance(token_row, dict):
            token_data = token_row
        else:
            token_data = {
                'phone_number': row_get(token_row, 0, None),
                'expires_at': row_get(token_row, 1, None),
                'used': row_get(token_row, 2, False),
                'use_count': row_get(token_row, 3, 0),
            }

        # Check expiration
        now = datetime.now(pytz.UTC)
        expires_at = token_data['expires_at']

        if expires_at.tzinfo is None:
            expires_at = pytz.UTC.localize(expires_at)
        else:
            expires_at = expires_at.astimezone(pytz.UTC)

        if now > expires_at:
            return None

        # Check if already used
        use_count = token_data.get('use_count', 0)
        if token_data.get('used') or use_count >= 1:
            return None

        return token_data['phone_number']

    except Exception as e:
        logger.error(f"Error getting phone from token: {e}")
        return None


def get_token_from_short_code(short_code):
    """Get token data from short code.

    Args:
        short_code: 6-character short code

    Returns:
        dict: Token data or None if not found
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("get_token_from_short_code: database unavailable")
            return None

        result = db.execute_query("""
            SELECT phone_number, token_hash, expires_at, used, COALESCE(use_count, 0) as use_count
            FROM webform_tokens
            WHERE short_code = %s
        """, (short_code,), fetch=True)

        if not result:
            return None

        from utils.row_utils import row_get
        token_row = result[0]
        if isinstance(token_row, dict):
            token_data = token_row
        else:
            token_data = {
                'phone_number': row_get(token_row, 0, None),
                'token_hash': row_get(token_row, 1, None),
                'expires_at': row_get(token_row, 2, None),
                'used': row_get(token_row, 3, False),
                'use_count': row_get(token_row, 4, 0),
            }

        # Check expiration
        now = datetime.now(pytz.UTC)
        expires_at = token_data['expires_at']

        if expires_at.tzinfo is None:
            expires_at = pytz.UTC.localize(expires_at)
        else:
            expires_at = expires_at.astimezone(pytz.UTC)

        if now > expires_at:
            return None

        # Check if already used
        use_count = token_data.get('use_count', 0)
        if token_data.get('used') or use_count >= 1:
            return None

        return {
            'phone_number': token_data['phone_number'],
            'token_hash': token_data['token_hash'],
            'expires_at': expires_at
        }

    except Exception as e:
        logger.error(f"Error getting token from short code: {e}")
        return None


def generate_experience_token(phone_number):
    """Generate token for experience guide page.

    Args:
        phone_number: Client's phone number

    Returns:
        dict: {'short_code': code} or None
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("generate_experience_token: database unavailable")
            return None

        short_code = generate_short_code()
        expires_at = datetime.now(pytz.UTC) + timedelta(hours=48)  # 48 hours for experience

        db.execute_query("""
            INSERT INTO experience_tokens
            (phone_number, short_code, created_at, expires_at)
            VALUES (%s, %s, NOW(), %s)
        """, (phone_number, short_code, expires_at), fetch=False)

        return {'short_code': short_code}

    except Exception as e:
        logger.error(f"Failed to generate experience token: {e}")
        return None


def get_experience_token_from_short_code(short_code):
    """Get experience token data from short code.

    Args:
        short_code: 6-character short code

    Returns:
        dict: Token data or None
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            logger.warning("get_experience_token_from_short_code: database unavailable")
            return None

        result = db.execute_query("""
            SELECT phone_number, expires_at
            FROM experience_tokens
            WHERE short_code = %s
        """, (short_code,), fetch=True)

        if not result:
            return None

        from utils.row_utils import row_get
        token_row = result[0]
        if isinstance(token_row, dict):
            token_data = token_row
        else:
            token_data = {
                'phone_number': row_get(token_row, 0, None),
                'expires_at': row_get(token_row, 1, None),
            }

        return {
            'phone_number': token_data['phone_number'],
            'expires_at': token_data['expires_at']
        }

    except Exception as e:
        logger.error(f"Error getting experience token: {e}")
        return None
