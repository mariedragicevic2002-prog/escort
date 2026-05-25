"""
Production Configuration Validator

Ensures all critical production environment variables are explicitly configured.
Prevents silent failures due to misconfiguration (e.g. missing DEPOSIT_MEDIA_ALLOWED_HOSTS).

This module is imported early during app startup to fail fast on config issues.
"""

import logging
import os
import sys
from typing import Callable, List, Tuple

logger = logging.getLogger("adella_chatbot.production_config")


def _is_production() -> bool:
    """Return True if running in production mode (DEBUG=False)."""
    production_signal = (
        bool(os.getenv("PYTHONANYWHERE_DOMAIN"))
        or bool(os.getenv("PYTHONANYWHERE_SITE"))
        or (os.getenv("ENVIRONMENT") or "").strip().lower() == "production"
    )
    if production_signal:
        return True
    debug_raw = (os.getenv("DEBUG") or "false").strip().lower()
    return debug_raw not in ("true", "1", "yes", "on")


def _check_local_secret_files_absent() -> Tuple[bool, str | None]:
    """
    In production, refuse startup when deploy-time secret files exist in repo root.
    Secrets must come from host environment/admin settings, not packaged files.
    """
    if not _is_production():
        return True, None

    allow_local_secret_files = (os.getenv("ALLOW_LOCAL_SECRET_FILES") or "").strip().lower()
    if allow_local_secret_files in ("1", "true", "yes", "on"):
        logger.warning("ALLOW_LOCAL_SECRET_FILES enabled — skipping local secret file startup guard.")
        return True, None

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    forbidden = [
        (".env", os.path.join(project_root, ".env")),
        ("credentials.json", os.path.join(project_root, "credentials.json")),
    ]
    present = [name for name, path in forbidden if os.path.isfile(path)]
    if not present:
        return True, None

    logger.warning(
        "Local secret files detected in production startup: %s. "
        "Prefer host environment variables/admin settings and remove these files when possible. "
        "Continuing startup to avoid full-site outage.",
        ", ".join(present),
    )
    return True, None


def _check_redis_configured() -> Tuple[bool, str | None]:
    """Check if Redis is configured. Redis is optional — warn but do not block startup."""
    if not _is_production():
        return True, None

    try:
        from config import get_redis_url
        redis_url = (get_redis_url() or "").strip()
    except Exception:
        redis_url = (os.getenv("REDIS_URL") or "").strip()

    if not redis_url:
        # Redis is optional; app falls back to per-process in-memory rate limiting.
        logger.warning(
            "Redis URL is not set (admin_settings.redis_url / REDIS_URL) — "
            "rate limiting will use in-memory fallback."
        )
        return True, None

    try:
        from services.redis_client import get_redis
        client = get_redis()
        if not client:
            logger.warning("Redis URL set but client unavailable — using in-memory fallback.")
            return True, None
        try:
            client.ping()
        except Exception as e:
            logger.warning("Redis ping failed (%s) — using in-memory fallback.", e)
            return True, None
    except Exception as e:
        logger.warning("Redis client load failed (%s) — using in-memory fallback.", e)
        return True, None

    return True, None


def _check_deposit_media_allowlist() -> Tuple[bool, str | None]:
    """Check if DEPOSIT_MEDIA_ALLOWED_HOSTS is explicitly set in production."""
    if not _is_production():
        return True, None

    allowed_env = (os.getenv("DEPOSIT_MEDIA_ALLOWED_HOSTS") or "").strip()
    if not allowed_env:
        return (
            False,
            "DEPOSIT_MEDIA_ALLOWED_HOSTS is not configured. "
            "In production, you must explicitly set allowed domains (comma-separated). "
            "Example: DEPOSIT_MEDIA_ALLOWED_HOSTS=s3.amazonaws.com,cdn.example.com",
        )

    return True, None


def _check_trusted_proxy_ips() -> Tuple[bool, str | None]:
    """Warn if TRUSTED_PROXY_IPS is not set (optional but recommended in production)."""
    if not _is_production():
        return True, None

    trusted_env = (os.getenv("TRUSTED_PROXY_IPS") or "").strip()
    if not trusted_env:
        logger.warning(
            "TRUSTED_PROXY_IPS is not configured. X-Forwarded-For headers will not be trusted. "
            "If behind a reverse proxy, set TRUSTED_PROXY_IPS to the proxy's IP address."
        )
        return True, None  # Not a hard failure, just a warning

    return True, None


def _check_upload_secret() -> Tuple[bool, str | None]:
    """Check if UPLOAD_SECRET is set (deploy endpoint only — not core functionality)."""
    upload_secret = (os.getenv("UPLOAD_SECRET") or "").strip()
    if not upload_secret:
        logger.warning(
            "UPLOAD_SECRET is not set — the /deploy endpoint will reject all requests. "
            "Set UPLOAD_SECRET in host environment variables to enable auto-deployment."
        )
        return True, None  # Non-fatal: bot works fine without it

    return True, None


def _check_secret_key() -> Tuple[bool, str | None]:
    """Check if SECRET_KEY is set (required for Flask session/CSRF)."""
    secret_key = (os.getenv("SECRET_KEY") or "").strip()
    if not secret_key:
        logger.warning(
            "SECRET_KEY is not set in host env. Continuing startup so app can read "
            "flask_secret_key from admin settings after DB initialization."
        )
        return True, None

    return True, None


def _check_admin_password() -> Tuple[bool, str | None]:
    """Check if ADMIN_PASSWORD is set and not an unsafe placeholder."""
    admin_password = (os.getenv("ADMIN_PASSWORD") or "").strip()
    weak_admin_passwords = {"change-this-password-now", "changeme", "admin", "password"}
    if not admin_password or admin_password in weak_admin_passwords:
        return (
            False,
            "ADMIN_PASSWORD is not set or is a weak placeholder default.",
        )
    return True, None


def _check_database_url() -> Tuple[bool, str | None]:
    """Check if DATABASE_URL is set."""
    db_url = (os.getenv("DATABASE_URL") or "").strip()
    if not db_url:
        try:
            from config import DATABASE_URL as _CONFIG_DATABASE_URL
            db_url = (_CONFIG_DATABASE_URL or "").strip()
        except Exception:
            db_url = ""
    if not db_url:
        return False, "DATABASE_URL is not set. Database connection will fail."

    return True, None


def validate_production_config() -> bool:
    """
    Validate all critical production environment variables.

    Returns True if all checks pass, False if any check fails.
    Logs errors and exits with code 1 if validation fails.
    """
    checks: List[Tuple[str, Callable[[], tuple[bool, str | None]]]] = [
        ("Local Secret Files (if DEBUG=False)", _check_local_secret_files_absent),
        ("Database URL", _check_database_url),
        ("Secret Key", _check_secret_key),
        ("Admin Password", _check_admin_password),
        ("Upload Secret", _check_upload_secret),
        ("Redis (if DEBUG=False)", _check_redis_configured),
        ("Deposit Media Allowlist (if DEBUG=False)", _check_deposit_media_allowlist),
        ("Trusted Proxy IPs (if DEBUG=False)", _check_trusted_proxy_ips),
    ]

    all_passed = True
    errors: List[str] = []

    for check_name, check_func in checks:
        try:
            passed, error_msg = check_func()
            if not passed:
                all_passed = False
                if error_msg:
                    errors.append(f"❌ {check_name}: {error_msg}")
                logger.error(f"Config validation failed: {check_name}")
        except Exception as e:
            all_passed = False
            errors.append(f"❌ {check_name}: Unexpected error: {e}")
            logger.error(f"Config validation exception: {e}", exc_info=True)

    if not all_passed:
        logger.critical("=" * 70)
        logger.critical("PRODUCTION CONFIGURATION VALIDATION FAILED")
        logger.critical("=" * 70)
        for error in errors:
            logger.critical(error)
        logger.critical("=" * 70)
        logger.critical("Please set all required environment variables and try again.")
        logger.critical("=" * 70)
        # Exit to prevent app from running in misconfigured state
        sys.exit(1)

    logger.info("✅ All production configuration checks passed.")
    return True


if __name__ == "__main__":
    # Allow direct testing: python core/production_config.py
    print("Testing production configuration...")
    if validate_production_config():
        print("✅ All checks passed!")
        sys.exit(0)
    else:
        print("❌ Configuration validation failed!")
        sys.exit(1)
