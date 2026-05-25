"""
Admin 2FA — TOTP (RFC 6238), SMS one-time codes, and backup codes.

Storage (all in admin_settings via core.settings_manager):
  admin_totp_enabled      — "true"/"false" (master 2FA switch)
  admin_2fa_delivery      — "totp" (authenticator) or "sms" (text message)
  admin_totp_secret       — base32-encoded TOTP seed (totp mode only)
  admin_2fa_sms_phone     — optional E.164 override; else escort_phone_number / env
  admin_backup_codes_hashed — JSON array of argon2id-hashed recovery codes

Emergency bypass: set env ADMIN_2FA_DISABLED=1 to skip 2FA (e.g. lost phone).
The bypass is logged at WARNING level on every admin request so it cannot be
left on quietly.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from urllib.parse import quote

logger = logging.getLogger("escort_chatbot.admin.totp")

# Soft imports — if the deps aren't installed yet, auth still works (2FA just
# reports as unavailable so an admin can't accidentally lock themselves out).
try:
    import pyotp
    _PYOTP_AVAILABLE = True
except ImportError:
    pyotp = None  # type: ignore[assignment]
    _PYOTP_AVAILABLE = False

try:
    import qrcode
    _QRCODE_AVAILABLE = True
except ImportError:
    qrcode = None  # type: ignore[assignment]
    _QRCODE_AVAILABLE = False


# ---------------------------------------------------------------------------
# Settings accessors
# ---------------------------------------------------------------------------

def _get(key: str) -> str | None:
    try:
        from core.settings_manager import get_setting
        v = get_setting(key)
        return (v.strip() or None) if isinstance(v, str) else v
    except Exception as e:
        logger.warning("settings_manager get_setting failed for %s: %s", key, e)
        return None


def _set(key: str, value: str) -> None:
    from core.settings_manager import set_setting
    set_setting(key, value)


def get_2fa_delivery() -> str:
    """Return ``totp`` (authenticator app) or ``sms`` (text message code)."""
    v = (_get("admin_2fa_delivery") or "totp").strip().lower()
    return "sms" if v == "sms" else "totp"


def get_sms_destination_phone() -> str:
    """
    Mobile number (E.164) for admin SMS 2FA codes.
    Prefer explicit admin_2fa_sms_phone, else escort/dashboard mobile.
    """
    override = (_get("admin_2fa_sms_phone") or "").strip()
    if override:
        return override
    try:
        from config import get_escort_phone_number

        return (get_escort_phone_number() or "").strip()
    except Exception as e:
        logger.warning("get_escort_phone_number failed: %s", e)
        return ""


def _sms_gateway_ready() -> bool:
    try:
        from services.sms_service import is_configured

        return bool(is_configured())
    except Exception as e:
        logger.warning("SMS gateway check failed: %s", e)
        return False


def sms_gateway_is_configured() -> bool:
    """True when httpSMS gateway is active (for setup UI)."""
    return _sms_gateway_ready()


def sms_2fa_ready() -> bool:
    """SMS 2FA can be used: gateway configured and a destination number exists."""
    return bool(_sms_gateway_ready() and get_sms_destination_phone())


def is_enabled() -> bool:
    """True when 2FA is fully enabled (by mode) and the env bypass is OFF."""
    if (os.environ.get("ADMIN_2FA_DISABLED") or "").strip() in ("1", "true", "yes"):
        logger.warning("ADMIN_2FA_DISABLED env var is set — 2FA is being bypassed")
        return False
    flag = (_get("admin_totp_enabled") or "").strip().lower()
    if flag not in ("true", "1", "yes"):
        return False
    if get_2fa_delivery() == "sms":
        return sms_2fa_ready()
    return bool(_get("admin_totp_secret"))


def has_pending_setup() -> bool:
    """True if 2FA flag is on but enrollment is incomplete for the chosen delivery method."""
    flag = (_get("admin_totp_enabled") or "").strip().lower()
    if flag not in ("true", "1", "yes"):
        return False
    if get_2fa_delivery() == "sms":
        return not sms_2fa_ready()
    return not _get("admin_totp_secret")


def deps_available() -> bool:
    """True if pyotp is importable — required for any verify call to succeed."""
    return _PYOTP_AVAILABLE


# ---------------------------------------------------------------------------
# Secret + provisioning
# ---------------------------------------------------------------------------

def generate_new_secret() -> str:
    """Generate a fresh base32 TOTP secret. Not persisted by this function."""
    if not _PYOTP_AVAILABLE:
        raise RuntimeError("pyotp not installed — cannot generate TOTP secret")
    assert pyotp is not None
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str = "admin", issuer: str = "Escort Chatbot") -> str:
    """Build an otpauth:// URI for a QR code / authenticator app."""
    if not _PYOTP_AVAILABLE:
        raise RuntimeError("pyotp not installed")
    assert pyotp is not None
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def qr_code_data_uri(provisioning_uri_str: str) -> str:
    """Return a data:image/png;base64,... URI for inline <img> embedding."""
    if not _QRCODE_AVAILABLE:
        raise RuntimeError("qrcode not installed — cannot render enrollment QR")
    import base64
    from io import BytesIO

    assert qrcode is not None
    img = qrcode.make(provisioning_uri_str)
    buf = BytesIO()
    img.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def otpauth_uri_fallback(secret: str, account_name: str = "admin", issuer: str = "Escort Chatbot") -> str:
    """URL-encoded otpauth URI for manual paste when QR scanning isn't practical."""
    return (
        f"otpauth://totp/{quote(issuer)}:{quote(account_name)}"
        f"?secret={secret}&issuer={quote(issuer)}"
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_totp(code: str, secret: str | None = None) -> bool:
    """
    Verify a 6-digit TOTP code against the stored secret (or a supplied one for setup).

    Accepts one 30-second step of drift in each direction (the pyotp default).
    """
    if not _PYOTP_AVAILABLE or not code:
        return False
    secret = secret if secret is not None else _get("admin_totp_secret")
    if not secret:
        return False
    code = str(code).strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    try:
        assert pyotp is not None
        return bool(pyotp.TOTP(secret).verify(code, valid_window=1))
    except Exception as e:
        logger.warning("TOTP verify raised: %s", e)
        return False


# ---------------------------------------------------------------------------
# Backup codes
# ---------------------------------------------------------------------------

BACKUP_CODE_COUNT = 10


def _hash_code(code: str) -> str:
    """Hash a backup code with argon2id. Falls back to werkzeug pbkdf2 if argon2 is missing."""
    from admin.auth import hash_password
    return hash_password(code)


def _verify_code_hash(code: str, stored_hash: str) -> bool:
    from admin.auth import _check_password
    return _check_password(code, stored_hash)


def _normalize_code(code: str) -> str:
    return (code or "").strip().replace(" ", "").replace("-", "").lower()


def generate_backup_codes(count: int = BACKUP_CODE_COUNT) -> list[str]:
    """
    Generate `count` one-time recovery codes (10 hex chars each, ~40 bits of entropy).

    Returns the plaintext codes for display to the admin. Callers MUST then call
    store_backup_codes() with the same list — plaintext is never persisted.
    """
    return [secrets.token_hex(5) for _ in range(count)]


def store_backup_codes(codes: list[str]) -> None:
    """Hash each code with argon2id and persist the JSON array. Overwrites existing codes."""
    hashed = [_hash_code(_normalize_code(c)) for c in codes]
    _set("admin_backup_codes_hashed", json.dumps(hashed))


def _load_backup_code_hashes() -> list[str]:
    raw = _get("admin_backup_codes_hashed")
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [h for h in val if isinstance(h, str)]
    except Exception as e:
        logger.warning("admin_backup_codes_hashed is not valid JSON: %s", e)
        return []


def backup_codes_remaining() -> int:
    return len(_load_backup_code_hashes())


def verify_and_consume_backup_code(code: str) -> bool:
    """
    Verify `code` against stored hashes; on match, remove that hash (single-use) and return True.
    Returns False if no match.
    """
    normalized = _normalize_code(code)
    if not normalized:
        return False
    hashes = _load_backup_code_hashes()
    for idx, h in enumerate(hashes):
        if _verify_code_hash(normalized, h):
            remaining = hashes[:idx] + hashes[idx + 1 :]
            _set("admin_backup_codes_hashed", json.dumps(remaining))
            logger.info("Backup code consumed (%d remaining)", len(remaining))
            return True
    return False


# ---------------------------------------------------------------------------
# Enable / disable
# ---------------------------------------------------------------------------

def finalize_enrollment(secret: str, verified_code: str) -> tuple[bool, list[str] | None]:
    """
    Called after the admin scans the QR and enters their first TOTP code.
    If the code verifies, persist the secret + generate backup codes.

    Returns (success, backup_codes_plaintext_or_None).
    """
    if not verify_totp(verified_code, secret=secret):
        return (False, None)
    _set("admin_totp_secret", secret)
    _set("admin_totp_enabled", "true")
    _set("admin_2fa_delivery", "totp")
    _set("admin_2fa_sms_phone", "")
    codes = generate_backup_codes()
    store_backup_codes(codes)
    logger.info("2FA enrollment finalized — %d backup codes issued", len(codes))
    return (True, codes)


SMS_LOGIN_TTL = 600
SMS_RESEND_COOLDOWN_SEC = 60


def issue_sms_login_code(*, record_resend_cooldown: bool = True) -> tuple[bool, str | None]:
    """
    Send a 6-digit code via SMS for the pending admin login session.
    Call after :func:`admin.auth.begin_2fa_challenge` when delivery is ``sms``.

    ``record_resend_cooldown``: when False (automatic send right after password),
    do not start the resend cooldown timer so the verify page can offer an
    immediate **Resend** if the first SMS was delayed or filtered.

    Returns ``(success, error_message)``.
    """
    from flask import session

    from admin.auth import hash_password
    from services.sms_service import send_sms

    dest = get_sms_destination_phone()
    if not dest:
        return False, (
            "No mobile number for SMS codes. Set **Admin 2FA mobile** on the setup page "
            "or configure **escort mobile** in settings."
        )
    if not _sms_gateway_ready():
        return False, "SMS is not configured (enable httpSMS on the Config page)."

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    session["sms_2fa_hash"] = hash_password(code)
    session["sms_2fa_expires"] = str(time.time() + SMS_LOGIN_TTL)
    session.modified = True

    msg = (
        f"Escort admin login code: {code}. Valid {SMS_LOGIN_TTL // 60} minutes. "
        "If you did not try to log in, ignore this message."
    )
    if not send_sms(dest, msg):
        session.pop("sms_2fa_hash", None)
        session.pop("sms_2fa_expires", None)
        return False, "Failed to send SMS. Check the server SMS configuration and logs."
    if record_resend_cooldown:
        session["sms_2fa_last_sent"] = str(time.time())
    logger.info("SMS 2FA code issued to destination ending …%s", dest[-3:] if len(dest) >= 3 else "?")
    return True, None


def verify_sms_login_code(code: str) -> bool:
    """Validate the 6-digit SMS code for the current pending login; clears session fields on success."""
    from flask import session

    from admin.auth import _check_password

    raw_hash = session.get("sms_2fa_hash")
    exp_raw = session.get("sms_2fa_expires")
    if not raw_hash or not exp_raw:
        return False
    try:
        if time.time() > float(exp_raw):
            return False
    except (TypeError, ValueError):
        return False
    digits = str(code or "").strip().replace(" ", "")
    if not digits.isdigit() or len(digits) != 6:
        return False
    if not _check_password(digits, raw_hash):
        return False
    session.pop("sms_2fa_hash", None)
    session.pop("sms_2fa_expires", None)
    session.pop("sms_2fa_last_sent", None)
    session.modified = True
    return True


def clear_sms_login_session() -> None:
    """Remove SMS login challenge data from the session (logout / successful login)."""
    from flask import session

    session.pop("sms_2fa_hash", None)
    session.pop("sms_2fa_expires", None)
    session.pop("sms_2fa_last_sent", None)


def can_resend_sms_login() -> bool:
    from flask import session

    last = session.get("sms_2fa_last_sent")
    if not last:
        return True
    try:
        return time.time() - float(last) >= SMS_RESEND_COOLDOWN_SEC
    except (TypeError, ValueError):
        return True


def issue_sms_enrollment_code(phone_e164: str) -> tuple[bool, str | None]:
    """
    Send a verification SMS during setup. Stores pending state in session.
    ``phone_e164`` is normalized to digits/plus only.
    """
    from flask import session

    from admin.auth import hash_password
    from services.sms_service import send_sms

    raw = (phone_e164 or "").strip()
    if not raw:
        return False, "Enter a mobile number."
    if not _sms_gateway_ready():
        return False, "SMS is not configured on this server."

    code = f"{secrets.randbelow(900000) + 100000:06d}"
    session["sms_enroll_hash"] = hash_password(code)
    session["sms_enroll_expires"] = str(time.time() + SMS_LOGIN_TTL)
    session["sms_enroll_phone"] = raw
    session.modified = True

    msg = f"Escort admin: confirm this number with code {code}. Valid {SMS_LOGIN_TTL // 60} minutes."
    if not send_sms(raw, msg):
        session.pop("sms_enroll_hash", None)
        session.pop("sms_enroll_expires", None)
        session.pop("sms_enroll_phone", None)
        return False, "Could not send SMS to that number."
    return True, None


def verify_sms_enrollment_code(code: str) -> tuple[bool, list[str] | None]:
    """
    Confirm enrollment SMS code; persist SMS 2FA mode and issue backup codes.
    Returns ``(success, backup_codes_or_None)``.
    """
    from flask import session

    from admin.auth import _check_password

    raw_hash = session.get("sms_enroll_hash")
    exp_raw = session.get("sms_enroll_expires")
    phone = session.get("sms_enroll_phone")
    if not raw_hash or not exp_raw or not phone:
        return False, None
    try:
        if time.time() > float(exp_raw):
            return False, None
    except (TypeError, ValueError):
        return False, None
    digits = str(code or "").strip().replace(" ", "")
    if not digits.isdigit() or len(digits) != 6:
        return False, None
    if not _check_password(digits, raw_hash):
        return False, None

    _set("admin_2fa_sms_phone", str(phone).strip())
    _set("admin_2fa_delivery", "sms")
    _set("admin_totp_secret", "")
    _set("admin_totp_enabled", "true")
    codes = generate_backup_codes()
    store_backup_codes(codes)

    for k in ("sms_enroll_hash", "sms_enroll_expires", "sms_enroll_phone"):
        session.pop(k, None)
    session.modified = True
    logger.info("SMS 2FA enrollment complete — backup codes issued")
    return True, codes


def clear_sms_enrollment_session() -> None:
    from flask import session

    for k in ("sms_enroll_hash", "sms_enroll_expires", "sms_enroll_phone"):
        session.pop(k, None)


def mask_phone_tail(phone: str) -> str:
    """Show only last 3 digits for UI hints."""
    p = "".join(c for c in (phone or "") if c.isdigit())
    if len(p) < 3:
        return "your phone"
    return f"…{p[-3:]}"


def disable_2fa() -> None:
    """
    Turn 2FA off and wipe the secret + backup codes so re-enabling starts fresh.
    Caller must gate this behind password re-verification.
    """
    _set("admin_totp_enabled", "false")
    _set("admin_totp_secret", "")
    _set("admin_backup_codes_hashed", "")
    _set("admin_2fa_delivery", "totp")
    _set("admin_2fa_sms_phone", "")
    logger.warning("2FA disabled — secret, backup codes, and SMS 2FA settings wiped")


def regenerate_backup_codes() -> list[str]:
    """Issue a fresh set of backup codes (invalidating old ones). Returns plaintext list."""
    codes = generate_backup_codes()
    store_backup_codes(codes)
    logger.info("Backup codes regenerated — %d new codes issued", len(codes))
    return codes
