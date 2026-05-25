"""
Deposit Upload Token Management
Generates secure tokens for deposit screenshot uploads.
"""

import hashlib
import logging
import secrets

import config
from services.database_service import get_shared_db
from utils.log_sanitize import LOG_SUPPRESSED_FMT, sanitize_log_value

logger = logging.getLogger("escort_chatbot.deposit_upload_tokens")


from utils.row_utils import row_get as _row_get


def _derive_fallback_reference(phone_number: str, short_code: str) -> str:
    """
    Derive a stable 5-digit reference when the DB cannot persist payment_reference.
    """
    seed = f"{phone_number}|{short_code}".encode()
    digest = hashlib.sha256(seed).hexdigest()
    return str(int(digest[:10], 16) % 100000).zfill(5)


def _supports_payment_reference_column(db) -> bool:
    """Return True when upload_tokens.payment_reference exists."""
    try:
        result = db.execute_query(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = 'upload_tokens'
              AND column_name = 'payment_reference'
            LIMIT 1
            """,
            fetch=True,
        )
        return bool(result)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return False


def generate_short_code():
    """Generate a short 6-character code for upload URLs."""
    from core.token_utils import generate_short_code as _gen
    return _gen()


def generate_payment_reference():
    """Generate a 5-digit numeric payment reference."""
    import string
    return ''.join(secrets.choice(string.digits) for _ in range(5))


def resolve_deposit_upload_and_reference(
    phone_number: str, deposit_amount: int
) -> tuple[str | None, str | None]:
    """
    Return (upload_url, payment_reference) for SMS and web pages.

    Retries token creation so clients almost always get a /d/ link and 5-digit ref.
    """
    if not phone_number:
        return None, None
    for force_new in (False, True):
        td = generate_deposit_upload_token(phone_number, deposit_amount, force_new=force_new)
        if td:
            upload_url = (td.get("upload_url") or "").strip() or None
            ref = (td.get("payment_reference") or "").strip() or None
            if upload_url and ref:
                return upload_url, ref
            if upload_url and not ref:
                import re as _re

                m = _re.search(r"/d/([^/?]+)", upload_url)
                if m:
                    return upload_url, _derive_fallback_reference(phone_number, m.group(1))
    return None, None


def _fetch_existing_token(
    db, phone_number: str, deposit_amount: int, supports_reference: bool, base_url: str
) -> dict | None:
    """Return the latest unused upload token for this phone/amount, if any (no age cutoff)."""
    try:
        if supports_reference:
            rows = db.execute_query(
                """
                SELECT short_code, payment_reference
                FROM upload_tokens
                WHERE phone_number = %s
                  AND deposit_amount = %s
                  AND used = FALSE
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (phone_number, deposit_amount),
                fetch=True,
            )
            if rows:
                row = rows[0]
                short_code = _row_get(row, "short_code", _row_get(row, 0))
                payment_reference = _row_get(row, "payment_reference", _row_get(row, 1))
                if short_code:
                    return {
                        'short_code': short_code,
                        'upload_url': f"{base_url}/d/{short_code}",
                        'payment_reference': payment_reference or _derive_fallback_reference(phone_number, short_code),
                    }
        else:
            rows = db.execute_query(
                """
                SELECT short_code
                FROM upload_tokens
                WHERE phone_number = %s
                  AND deposit_amount = %s
                  AND used = FALSE
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (phone_number, deposit_amount),
                fetch=True,
            )
            if rows:
                row = rows[0]
                short_code = _row_get(row, "short_code", _row_get(row, 0))
                if short_code:
                    return {
                        'short_code': short_code,
                        'upload_url': f"{base_url}/d/{short_code}",
                        'payment_reference': _derive_fallback_reference(phone_number, short_code),
                    }
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return None


def _generate_unique_short_code(db, max_attempts: int = 10) -> str | None:
    """Generate a short code that does not yet exist in upload_tokens."""
    for _ in range(max_attempts):
        code = generate_short_code()
        try:
            existing = db.execute_query(
                "SELECT id FROM upload_tokens WHERE short_code = %s",
                (code,),
                fetch=True,
            )
            if not existing:
                return code
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
    return None


def _generate_unique_payment_reference(db, max_attempts: int = 10) -> str | None:
    """Generate a payment reference that does not yet exist in upload_tokens."""
    for _ in range(max_attempts):
        candidate = generate_payment_reference()
        existing = db.execute_query(
            "SELECT id FROM upload_tokens WHERE payment_reference = %s",
            (candidate,),
            fetch=True,
        )
        if not existing:
            return candidate
    return None


def _insert_upload_token(
    db, phone_number: str, short_code: str, deposit_amount: int,
    token_hash: str, payment_reference: str | None, supports_reference: bool,
) -> None:
    """Persist the new upload token row to the database."""
    if supports_reference:
        db.execute_query(
            """
            INSERT INTO upload_tokens
            (phone_number, short_code, deposit_amount, created_at, used, upload_attempts, token_hash, payment_reference)
            VALUES (%s, %s, %s, NOW(), FALSE, 0, %s, %s)
            """,
            (phone_number, short_code, deposit_amount, token_hash, payment_reference),
            fetch=False,
        )
    else:
        db.execute_query(
            """
            INSERT INTO upload_tokens
            (phone_number, short_code, deposit_amount, created_at, used, upload_attempts, token_hash)
            VALUES (%s, %s, %s, NOW(), FALSE, 0, %s)
            """,
            (phone_number, short_code, deposit_amount, token_hash),
            fetch=False,
        )


def generate_deposit_upload_token(phone_number: str, deposit_amount: int, force_new: bool = False) -> dict | None:
    """
    Generate a secure upload token for deposit screenshot upload.

    Args:
        phone_number: Client's phone number
        deposit_amount: Required deposit amount

    Returns:
        dict: {'short_code': str, 'upload_url': str, 'payment_reference': str} or None if failed
    """
    try:
        db = get_shared_db(config.DATABASE_URL)
        supports_reference = _supports_payment_reference_column(db)
        base_url = config.get_base_url()

        if not force_new:
            cached = _fetch_existing_token(db, phone_number, deposit_amount, supports_reference, base_url)
            if cached:
                return cached

        short_code = _generate_unique_short_code(db)
        if not short_code:
            logger.error("Failed to generate unique short code after multiple attempts")
            try:
                from utils.alerts import log_alert
                log_alert("deposit_upload_token", "Failed to generate unique short code after multiple attempts", "error")
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
            return None

        if supports_reference:
            payment_reference = _generate_unique_payment_reference(db)
            if not payment_reference:
                logger.error("Failed to generate unique payment reference after multiple attempts")
                return None
        else:
            payment_reference = generate_payment_reference()
            logger.warning(
                "upload_tokens.payment_reference column is missing; "
                "continuing without persisted payment reference"
            )

        token_hash = hashlib.sha256((phone_number + short_code).encode()).hexdigest()
        _insert_upload_token(db, phone_number, short_code, deposit_amount, token_hash, payment_reference, supports_reference)

        upload_url = f"{base_url}/d/{short_code}"
        logger.info(
            "Generated deposit upload token for %s: %s",
            sanitize_log_value(phone_number),
            sanitize_log_value(short_code),
        )
        return {
            'short_code': short_code,
            'upload_url': upload_url,
            'payment_reference': payment_reference,
        }

    except Exception as e:
        logger.exception("Failed to generate deposit upload token: %s", e)
        try:
            from utils.alerts import log_alert
            log_alert("deposit_upload_token", str(e)[:500], "error")
        except Exception as log_err:
            logger.warning("log_alert after deposit token failure: %s", log_err, exc_info=True)
        return None


def get_upload_url_from_short_code(short_code: str) -> str:
    """
    Get full upload URL from short code.
    
    Args:
        short_code: 6-character short code
        
    Returns:
        str: Full upload URL
    """
    return f"{config.get_base_url()}/d/{short_code}"
