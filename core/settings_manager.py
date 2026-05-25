"""

Settings manager for database-backed configuration.
Replaces hardcoded config values with admin_settings table.
Uses in-memory cache with database fallback.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import os
import threading
import time
from typing import Any

from services.database_service import DatabaseService, get_shared_db_with_retry

logger = logging.getLogger("adella_chatbot.settings_manager")

# In-memory cache with 5-minute TTL
_settings_cache: dict[str, dict[str, Any]] = {}
_CACHE_TTL = 300  # 5 minutes
# Short TTL for safety/rollout-critical keys where a stale value is dangerous.
# Workers still cross-check the DB version every _VERSION_CHECK_INTERVAL seconds,
# but that only flushes when SOME worker calls set_setting(). When an operator
# toggles via SQL or another path that doesn't bump _cache_version, the normal
# TTL is the backstop — and 30s is the floor we want for these keys.
_SAFETY_CACHE_TTL = 30  # seconds
_SAFETY_CRITICAL_KEYS = frozenset({
    "chatbot_rollout_percent",
    "run_startup_db_migrations",
    "safety_screening_enabled",
    "safety_screening_action",
    "safety_screening_mode",
    "chatbot_enabled",
    "httpsms_enabled",
})
_cache_lock = threading.Lock()

# Cross-process cache invalidation: track the last DB-level update timestamp.
# Guarded by `_version_lock` so concurrent workers don't race on the
# _last_version_check / _last_db_version pair, which previously could skip
# the DB check or use a torn pair across threads.
_last_db_version: float | None = None
_last_version_check: float = 0.0
_VERSION_CHECK_INTERVAL = 5  # seconds between version checks
_version_lock = threading.Lock()

# Singleton database service - one shared pool for all settings calls
_db: DatabaseService | None = None
_db_lock = threading.Lock()


def _get_db() -> DatabaseService | None:
    """Return the shared DatabaseService singleton, creating it once if needed."""
    global _db
    if _db is not None:
        return _db
    with _db_lock:
        if _db is not None:
            return _db
        database_url = os.environ.get('DATABASE_URL', '')
        # Production fallback: If not in environment, read directly from .env file
        # (PythonAnywhere WSGI doesn't inherit parent env, so direct read is necessary)
        if not database_url:
            try:
                from dotenv import dotenv_values
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                env_file = os.path.join(base_dir, ".env")
                if os.path.exists(env_file):
                    env_vars = dotenv_values(env_file)
                    database_url = env_vars.get('DATABASE_URL', '')
            except Exception as e:
                logger.warning(f"Failed to read DATABASE_URL from .env file: {e}")
        
        if not database_url:
            logger.error("DATABASE_URL not configured")
            return None
        try:
            # Prefer retry wrapper so a cold-start pool failure does not strand settings reads for the whole worker.
            _db = get_shared_db_with_retry(database_url)
            logger.debug("Settings manager DB singleton initialized")
        except Exception as e:
            logger.error(f"Settings manager failed to connect to DB: {e}")
            _db = None
    return _db


def _first_row_value_for_cache_version_query(result) -> Any:
    """Read setting_value from execute_query first row; supports dict- or tuple-shaped rows."""
    if not result or len(result) < 1:
        return None
    row = result[0]
    if isinstance(row, dict):
        return row.get("setting_value")
    if isinstance(row, (list, tuple)) and len(row) > 0:
        return row[0]
    return None


def _check_db_version() -> None:
    """
    Check if another worker updated settings since our last check.
    If so, flush the local cache so we pick up fresh values.
    """
    global _last_db_version, _last_version_check
    now = time.time()
    # Admission gate: only one thread per worker does the DB round-trip per
    # _VERSION_CHECK_INTERVAL window. Others see the updated _last_version_check
    # under the same lock and bail out.
    with _version_lock:
        if now - _last_version_check < _VERSION_CHECK_INTERVAL:
            return
        _last_version_check = now
        known_version = _last_db_version
    try:
        db = _get_db()
        if db is None:
            return
        result = db.execute_query(
            "SELECT setting_value FROM admin_settings WHERE setting_key = '_cache_version'",
            fetch=True
        )
        raw = _first_row_value_for_cache_version_query(result)
        version = 0.0
        if raw is not None and str(raw).strip() != "":
            try:
                version = float(str(raw).strip())
            except (TypeError, ValueError):
                version = 0.0
        should_flush = known_version is not None and version != known_version
        with _version_lock:
            _last_db_version = version
        if should_flush:
            with _cache_lock:
                _settings_cache.clear()
            logger.debug("Settings cache flushed (new DB version %.0f)", version)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)


def _transient_db_error(exc: BaseException) -> bool:
    """True for flaky Postgres/SSL errors common on hosted DBs (e.g. PythonAnywhere)."""
    s = str(exc).lower()
    if any(
        x in s
        for x in (
            "ssl",
            "eof",
            "cipher",
            "connection",
            "closed",
            "timeout",
            "syscall",
            "write error",
            "write",
            "broken pipe",
        )
    ):
        return True
    name = type(exc).__name__
    if name in ("OperationalError", "InterfaceError", "InternalError", "OSError", "SSLError"):
        return True
    return False


def _reset_settings_db_handle() -> None:
    """Drop cached pool handle so the next read uses a fresh connection (after reset_shared_db_connection)."""
    global _db
    try:
        from services.database_service import reset_shared_db_connection

        reset_shared_db_connection()
    except Exception as e:
        logger.warning("reset_shared_db_connection: %s", e, exc_info=True)
    _db = None


def get_setting(key: str, default: str | None = None) -> str | None:
    """
    Get setting from admin_settings table with in-memory caching.

    Cache strategy:
    1. Check DB version every 5s to detect cross-process updates
    2. Check in-memory cache
    3. On miss, query database and cache result
    """
    _check_db_version()
    now = time.time()
    with _cache_lock:
        if key in _settings_cache:
            entry = _settings_cache[key]
            if entry['expires'] > now:
                return entry['value']

    max_attempts = 3
    for attempt in range(max_attempts):
        try:
            db = _get_db()
            if db is None:
                return default

            result = db.execute_query(
                "SELECT setting_value FROM admin_settings WHERE setting_key = %s",
                (key,),
                fetch=True
            )

            try:
                if result and len(result) > 0 and isinstance(result[0], dict):
                    value = result[0].get('setting_value', default)
                else:
                    value = default
            except (IndexError, KeyError, TypeError):
                value = default

            if value is not None:
                ttl = _SAFETY_CACHE_TTL if key in _SAFETY_CRITICAL_KEYS else _CACHE_TTL
                with _cache_lock:
                    _settings_cache[key] = {'value': value, 'expires': now + ttl}

            return value

        except Exception as e:
            if attempt < max_attempts - 1 and _transient_db_error(e):
                logger.warning(
                    "get_setting transient DB error for %s (attempt %s/%s): %s",
                    key,
                    attempt + 1,
                    max_attempts,
                    e,
                )
                time.sleep(0.12 * (attempt + 1))
                _reset_settings_db_handle()
                continue
            logger.error(f"Error getting setting {key}: {e}")
            return default
    return default


def set_setting(key: str, value: str) -> bool:
    """
    Set setting in admin_settings table and invalidate cache for that key.
    Returns True if successful, False otherwise.
    """
    try:
        db = _get_db()
        if db is None:
            logger.error("Cannot save setting - no database connection")
            return False

        db.execute_query(
            """
            INSERT INTO admin_settings (setting_key, setting_value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (setting_key)
            DO UPDATE SET setting_value = EXCLUDED.setting_value,
                          updated_at = NOW()
            """,
            (key, value),
            fetch=False
        )

        # Bump the DB version so other worker processes flush their caches
        db.execute_query(
            """INSERT INTO admin_settings (setting_key, setting_value, updated_at)
               VALUES ('_cache_version', %s, NOW())
               ON CONFLICT (setting_key)
               DO UPDATE SET setting_value = EXCLUDED.setting_value, updated_at = NOW()""",
            (str(time.time()),),
            fetch=False
        )

        with _cache_lock:
            _settings_cache.pop(key, None)
        logger.info(f"Setting updated: {key}")
        try:
            from utils.admin_audit import log_setting_updated
            log_setting_updated(key)
        except Exception as e:
            logger.warning(f"Audit log failed: {e}")
        return True

    except Exception as e:
        logger.error(f"Error setting {key}: {e}")
        return False


def get_all_settings() -> dict[str, str]:
    """Fetch all settings directly from the database (bypasses per-key cache)."""
    try:
        db = _get_db()
        if db is None:
            return {}

        results = db.execute_query(
            "SELECT setting_key, setting_value FROM admin_settings",
            fetch=True
        ) or []

        return {row['setting_key']: row['setting_value'] for row in results}

    except Exception as e:
        logger.error(f"Error getting all settings: {e}")
        return {}


def clear_cache():
    """Clear in-memory settings cache. Useful after bulk updates."""
    with _cache_lock:
        _settings_cache.clear()
    logger.info("Settings cache cleared")


def get_escort_name(default: str = "escort") -> str:
    """Return the escort/business display name from settings (used in messages and admin)."""
    return get_setting("escort_name", default) or default
