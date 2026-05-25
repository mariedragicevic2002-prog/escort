"""Admin authentication and session management with security features."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import hashlib
import hmac
import logging
import os
import secrets
import threading
import time as _time
from functools import wraps

from flask import jsonify, redirect, request, session, url_for

from admin.rate_limiter import (
    clear_login_attempts,
    get_lockout_remaining,
    is_ip_locked_out,
    record_failed_login,
)
from utils.net import get_client_ip as _resolve_client_ip

logger = logging.getLogger("escort_chatbot.admin.auth")


def _get_client_ip() -> str:
    """Return the best-guess client IP for rate limiting — see utils.net.get_client_ip."""
    return _resolve_client_ip(request)

# Security token for session validation
_session_tokens: dict = {}  # session_id -> token
_session_tokens_lock = threading.Lock()

# DB-backed session token TTL (30 days — matches Flask permanent session lifetime)
_SESSION_TOKEN_TTL = 60 * 60 * 24 * 30


def _db_token_key(session_id: str) -> str:
    """Deterministic, non-reversible admin_settings key for a session token."""
    return "_sess_tok:" + hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:24]


def _db_save_token(session_id: str, token: str) -> None:
    """Persist a session token to the DB so it survives uWSGI worker restarts."""
    try:
        from core.settings_manager import _get_db
        db = _get_db()
        if db is None:
            return
        key = _db_token_key(session_id)
        expiry = str(int(_time.time()) + _SESSION_TOKEN_TTL)
        value = f"{token}:{expiry}"
        db.execute_query(
            """INSERT INTO admin_settings (setting_key, setting_value)
               VALUES (%s, %s)
               ON CONFLICT (setting_key) DO UPDATE SET setting_value = EXCLUDED.setting_value""",
            (key, value),
        )
    except Exception as e:
        logger.warning("_db_save_token failed: %s", e)


def _db_load_token(session_id: str) -> str | None:
    """Load a session token from the DB (used on memory miss after worker restart)."""
    try:
        from core.settings_manager import _get_db
        db = _get_db()
        if db is None:
            return None
        key = _db_token_key(session_id)
        rows = db.execute_query(
            "SELECT setting_value FROM admin_settings WHERE setting_key = %s",
            (key,),
            fetch=True,
        )
        if not rows:
            return None
        row = rows[0]
        raw = row.get("setting_value") if isinstance(row, dict) else (row[0] if row else None)
        if not raw:
            return None
        parts = str(raw).rsplit(":", 1)
        if len(parts) != 2:
            return None
        token, expiry_str = parts
        if int(expiry_str) < int(_time.time()):
            _db_delete_token(session_id)
            return None
        return token
    except Exception as e:
        logger.warning("_db_load_token failed: %s", e)
        return None


def _db_delete_token(session_id: str) -> None:
    """Remove a session token from the DB on logout."""
    try:
        from core.settings_manager import _get_db
        db = _get_db()
        if db is None:
            return
        key = _db_token_key(session_id)
        db.execute_query("DELETE FROM admin_settings WHERE setting_key = %s", (key,))
    except Exception as e:
        logger.warning("_db_delete_token failed: %s", e)

# Argon2id hasher (current canonical format for new/rotated admin hashes).
# Soft import: if argon2-cffi is not installed yet, auth still works with legacy hashes
# and new hashes fall back to werkzeug pbkdf2 until the dep is installed.
try:
    from argon2 import PasswordHasher as _Argon2Hasher
    from argon2.exceptions import InvalidHashError, VerifyMismatchError

    _argon2_hasher = _Argon2Hasher()
    _ARGON2_AVAILABLE = True
except ImportError:
    _argon2_hasher = None
    _ARGON2_AVAILABLE = False
    VerifyMismatchError = InvalidHashError = Exception  # type: ignore[assignment,misc]


def _legacy_sha256_hex(password: str) -> str:
    """Legacy hash used only to verify pre-argon2 ADMIN_PASSWORD_HASH values."""
    return hashlib.sha256(password.encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash a password for storage. Uses argon2id when available, else werkzeug pbkdf2."""
    if _ARGON2_AVAILABLE:
        assert _argon2_hasher is not None
        return _argon2_hasher.hash(password)
    from werkzeug.security import generate_password_hash
    return generate_password_hash(password)


def _is_legacy_hash(stored: str) -> bool:
    """True if the stored hash is not argon2id and should be upgraded on next successful verify."""
    if not stored:
        return False
    return not stored.strip().startswith("$argon2")


def _looks_like_password_hash(stored: str) -> bool:
    """
    True if the value looks like a real hash (Werkzeug/bcrypt/argon2/legacy SHA256 hex).
    If admin_settings has a mistaken plain string or junk in admin_password_hash, we skip it
    so verification can fall back to admin_password or environment variables.
    """
    if not stored:
        return False
    s = stored.strip()
    if not s:
        return False
    if "$" in s or s.startswith("pbkdf2:") or s.startswith("scrypt:"):
        return len(s) >= 20
    if len(s) == 64 and all(c in "0123456789abcdef" for c in s.lower()):
        return True
    return False


def _env_password_credentials():
    """Always read from current environment (not import time — WSGI may differ)."""
    eh = (os.environ.get("ADMIN_PASSWORD_HASH") or "").strip() or None
    ep = (os.environ.get("ADMIN_PASSWORD") or "").strip() or None
    return eh, ep


def _get_stored_password():
    """
    Get stored admin password: (hash_or_none, plain_or_none).
    Order: valid DB hash → env hash → env plain.

    **Plaintext passwords from the DB are REJECTED** (H4). A value in
    ``admin_settings.admin_password`` that is not a recognised hash turns an
    ordinary SQL-injection or DB backup leak into admin takeover with no
    cracking required. If a non-hash is found we log an error and ignore it;
    operators recover by setting ``ADMIN_PASSWORD`` in the environment (same
    env the bot boots from) or writing a proper hash to
    ``admin_password_hash`` via the admin UI.
    """
    env_hash, env_plain = _env_password_credentials()
    try:
        from core.settings_manager import get_setting
        settings_hash = get_setting("admin_password_hash")
        settings_plain = get_setting("admin_password")
        if settings_hash is not None:
            settings_hash = (settings_hash.strip() or None)
        if settings_plain is not None:
            settings_plain = (settings_plain.strip() or None)
        if settings_hash and _looks_like_password_hash(settings_hash):
            return (settings_hash, None)
        if settings_plain:
            # Never trust a non-hash value from the DB. Fall through to env-based creds.
            logger.error(
                "admin_settings.admin_password contains a non-hashed value. "
                "Ignoring for security. Set ADMIN_PASSWORD env var, or write an "
                "argon2 hash into admin_password_hash to restore login."
            )
    except Exception as e:
        logger.warning("Could not load admin password from settings: %s", e)
    if env_hash and _looks_like_password_hash(env_hash):
        return (env_hash, None)
    if env_plain:
        return (None, env_plain)
    return (None, None)


def _maybe_upgrade_stored_hash(password: str) -> None:
    """
    Transparently rehash the DB-stored admin password with argon2id when it is in a legacy
    format (sha256 hex or werkzeug pbkdf2/scrypt). Called after a successful login.

    Only touches the DB hash — env vars are left alone so operators can still rotate via .env
    without surprise DB writes. A successful verify is a precondition, so this can't smuggle
    in a wrong password.
    """
    if not _ARGON2_AVAILABLE:
        return
    try:
        from core.settings_manager import get_setting, set_setting
    except Exception as e:
        logger.warning("Rehash skipped — settings_manager unavailable: %s", e)
        return
    try:
        db_hash = get_setting("admin_password_hash")
        if not db_hash:
            return
        db_hash = db_hash.strip()
        if not _looks_like_password_hash(db_hash) or not _is_legacy_hash(db_hash):
            return
        # Re-verify against the DB hash specifically before rewriting it (defence in depth —
        # protects against an env match upgrading a mismatched DB hash).
        if not _check_password(password, db_hash):
            return
        set_setting("admin_password_hash", hash_password(password))
        logger.info("Admin password rehashed to argon2id (transparent upgrade from legacy format)")
    except Exception as e:
        logger.warning("Rehash-on-verify skipped: %s", e)


def _check_password(password: str, stored_hash: str) -> bool:
    """
    Verify password against stored hash.
    Supports: argon2id (canonical), Werkzeug hashes (pbkdf2/scrypt), legacy 64-char SHA256 hex.
    """
    if not stored_hash or not password:
        return False
    stored = stored_hash.strip()
    # Argon2id: $argon2id$v=...$m=...,t=...,p=...$salt$hash
    if stored.startswith("$argon2"):
        if not _ARGON2_AVAILABLE:
            logger.warning("Stored hash is argon2 but argon2-cffi is not installed")
            return False
        try:
            assert _argon2_hasher is not None
            return _argon2_hasher.verify(stored, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
        except Exception as e:  # type: ignore[misc]
            logger.warning("argon2 verify failed: %s", e)
            return False
    # Werkzeug format: pbkdf2:sha256:... or scrypt:...
    if "$" in stored or stored.startswith("pbkdf2:") or stored.startswith("scrypt:"):
        try:
            from werkzeug.security import check_password_hash
            return check_password_hash(stored, password)
        except Exception as e:
            logger.warning("check_password_hash failed: %s", e)
            return False
    # Legacy: 64-char hex (SHA256 from env) — kept for backward compat only; rehashed on next login.
    if len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower()):
        return _legacy_sha256_hex(password) == stored
    return False


def get_admin_login_block_reason() -> str | None:
    """
    If login cannot succeed until the situation changes (lockout or no password configured),
    return a user-facing message. Otherwise None. Use when rendering standalone login pages
    (e.g. /stats) so users see lockout / misconfiguration instead of a generic invalid password.
    """
    ip = _get_client_ip()
    if is_ip_locked_out(ip):
        remaining = get_lockout_remaining(ip)
        minutes = max(1, remaining // 60)
        return f"Too many failed attempts. Try again in about {minutes} minutes."

    stored_hash, stored_plain = _get_stored_password()
    if not stored_hash and not stored_plain:
        return (
            "No admin password is configured, or the app cannot read it. "
            "Set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH in the server environment and reload the web app, "
            "or ensure DATABASE_URL is correct so admin_settings (admin_password_hash) can be loaded."
        )

    return None


def _record_login_attempt(ip: str, is_valid: bool, password: str) -> None:
    """Record the outcome of a login attempt and log accordingly."""
    if is_valid:
        clear_login_attempts(ip)
        logger.info(f"Successful login from IP {ip}")
        _maybe_upgrade_stored_hash(password)
    else:
        locked_out = record_failed_login(ip)
        if locked_out:
            logger.warning(f"IP {ip} locked out after failed attempts")
        else:
            logger.warning(f"Failed login attempt from IP {ip}")


def verify_password(password: str) -> bool:
    """
    Verify admin password with rate limiting.

    Primary: valid admin_password_hash from DB, else admin_password from DB, else env hash/plain
    (see _get_stored_password). If that fails, ADMIN_PASSWORD / ADMIN_PASSWORD_HASH in the
    environment are tried again so a stale DB hash does not block login when env matches.
    """
    # Get client IP for rate limiting
    ip = _get_client_ip()
    if is_ip_locked_out(ip):
        logger.warning(f"Locked out IP {ip} attempted password verification")
        return False

    password = (password or "").strip()
    if not password:
        return False

    stored_hash, stored_plain = _get_stored_password()

    is_valid = False
    if stored_hash:
        is_valid = _check_password(password, stored_hash)
    elif stored_plain:
        # Constant-time compare so a timing side-channel can't probe the plaintext
        # password byte-by-byte (plaintext fallback is already deprecated, but while
        # it exists every comparison on it must be constant-time).
        is_valid = hmac.compare_digest(password, stored_plain.strip())
    else:
        logger.error("No admin password configured")
        return False

    # If DB had a valid hash that did not match, still allow env credentials (recovery when DB and env diverge).
    if not is_valid:
        env_hash, env_plain = _env_password_credentials()
        if (env_plain and hmac.compare_digest(password, env_plain)) or (
            env_hash and _check_password(password, env_hash)
        ):
            is_valid = True

    _record_login_attempt(ip, is_valid, password)
    return is_valid


def generate_session_token():
    """Generate a secure random token for session validation."""
    return secrets.token_urlsafe(32)


def _wants_json_response():
    """True if the client expects JSON (e.g. fetch for API). Avoids returning HTML redirect to JSON parsers."""
    accept = request.headers.get("Accept", "")
    return "application/json" in accept or request.headers.get("X-Requested-With") == "XMLHttpRequest"


def require_auth(f):
    """Decorator to require authentication and valid session token for admin routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        def auth_failed():
            if _wants_json_response():
                return jsonify({"success": False, "error": "Unauthorized", "login_required": True}), 401
            return redirect(url_for("admin.admin_dashboard", next=request.url))

        # Check basic authentication
        if not session.get("admin_authenticated"):
            return auth_failed()

        # Require session_id and session_token and validate both against the
        # token store (in-memory first, then DB on memory miss after worker restart).
        session_id = session.get("session_id")
        session_token = session.get("session_token")

        if not session_id or not session_token:
            session.pop("admin_authenticated", None)
            logger.warning("Session missing security token, forcing re-authentication")
            return auth_failed()

        # Check in-memory store first; fall back to DB on miss (worker restart).
        with _session_tokens_lock:
            stored_token = _session_tokens.get(session_id)
        if stored_token is None:
            stored_token = _db_load_token(session_id)
            if stored_token is not None:
                # Re-populate memory cache for subsequent requests on this worker.
                with _session_tokens_lock:
                    _session_tokens[session_id] = stored_token

        if stored_token is not None:
            try:
                token_ok = hmac.compare_digest(stored_token, session_token)
            except (TypeError, ValueError):
                token_ok = False
            if not token_ok:
                session.clear()
                _sid_fp = hashlib.sha256((session_id or "").encode("utf-8")).hexdigest()[:12]
                logger.warning("Session token mismatch (session_fp=%s) — forcing re-auth", _sid_fp)
                return auth_failed()
        else:
            # Token not found in memory or DB — treat as invalid session.
            session.clear()
            logger.warning("Session token not found in store — forcing re-authentication")
            return auth_failed()

        return f(*args, **kwargs)
    return decorated_function


def login_user():
    """Mark user as authenticated in session with security token."""
    try:
        from admin import totp as admin_totp

        admin_totp.clear_sms_login_session()
    except Exception as e:
        logger.warning("clear_sms_login_session failed on login: %s", e)
    session_id = secrets.token_urlsafe(16)
    session_token = generate_session_token()

    session['admin_authenticated'] = True
    session['session_id'] = session_id
    session['session_token'] = session_token
    session.pop('pending_2fa', None)
    session.permanent = True  # Session persists across browser restarts

    # Store token in memory and DB
    with _session_tokens_lock:
        _session_tokens[session_id] = session_token
    _db_save_token(session_id, session_token)

    # M1: don't log the full session_id — a log leak turns it into a hijack key.
    # Short sha256 prefix is enough to correlate across log lines while staying
    # un-invertible back to the cookie value.
    session_fp = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    logger.info("User authenticated (session_fp=%s)", session_fp)


def begin_2fa_challenge():
    """
    Password verified but 2FA still required. Marks the session as pending_2fa so
    require_auth treats the user as NOT logged in until they complete /admin/2fa/verify.
    """
    session['pending_2fa'] = True
    session['admin_authenticated'] = False
    session.permanent = True
    logger.info("Password accepted, awaiting 2FA verification")


def clear_2fa_challenge():
    session.pop('pending_2fa', None)


def logout_user():
    """Clear authentication from session and invalidate token."""
    session_id = session.get('session_id')
    if session_id:
        with _session_tokens_lock:
            _session_tokens.pop(session_id, None)
        _db_delete_token(session_id)

    session.pop('admin_authenticated', None)
    session.pop('session_id', None)
    session.pop('session_token', None)
    session.pop('stats_authenticated', None)
    session.pop('schedule_authenticated', None)
    try:
        from admin import totp as admin_totp

        admin_totp.clear_sms_login_session()
        admin_totp.clear_sms_enrollment_session()
    except Exception as e:
        logger.warning("clear SMS TOTP session on logout failed: %s", e)

    logger.info(f"User logged out, session {session_id} invalidated")


def create_session(_username: str = "", _ip_address: str = "", _user_agent: str = "") -> str:
    """Create a new admin session and return session ID."""
    session_id = secrets.token_urlsafe(16)
    session_token = generate_session_token()

    session['admin_authenticated'] = True
    session['session_id'] = session_id
    session['session_token'] = session_token
    session.permanent = True

    with _session_tokens_lock:
        _session_tokens[session_id] = session_token
    _db_save_token(session_id, session_token)

    session_fp = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
    logger.info("Session created (session_fp=%s)", session_fp)
    return session_id


def validate_session(session_id: str, token: str) -> bool:
    """Validate session ID and token."""
    if not session_id or not token:
        return False

    stored_token = _session_tokens.get(session_id)
    if not stored_token:
        return False

    return hmac.compare_digest(stored_token, token)



# Security logging (for test compatibility)
security_logger = logger

def log_security_event(event: str, details: dict | None = None):
    """Log a security event for audit trail."""
    msg = f"Security event: {event}"
    if details:
        msg += f" - {details}"
    logger.info(msg)


def get_shared_db():
    """Get shared database instance (for test compatibility)."""
    try:
        from services.database_service import get_db_service
        return get_db_service()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return None
