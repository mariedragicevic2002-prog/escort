"""
Database connection pool — infrastructure layer.

Wraps the existing shared DB with explicit pool sizing,
timeout, and startup health check.
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import suppress

logger = logging.getLogger(__name__)


def _read_positive_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw_value = os.environ.get(name, str(default))
    try:
        return max(minimum, int(raw_value))
    except (TypeError, ValueError):
        logger.warning("db.pool.invalid_env", extra={"name": name, "value": raw_value})
        return default


_MIN_CONN = _read_positive_int("DB_POOL_MIN", 2)
_MAX_CONN = max(_MIN_CONN, _read_positive_int("DB_POOL_MAX", 10))
_CONNECT_TIMEOUT = _read_positive_int("DB_CONNECT_TIMEOUT", 5)
_STATEMENT_TIMEOUT_MS = _read_positive_int("DB_STATEMENT_TIMEOUT_MS", 30000, minimum=1000)

_pool = None
_pool_lock = threading.Lock()


def _normalize_database_url(dsn: str) -> str:
    normalized = (dsn or "").strip()
    if normalized.startswith("postgres://"):
        normalized = normalized.replace("postgres://", "postgresql://", 1)
    if normalized and "sslmode=" not in normalized.lower():
        separator = "&" if "?" in normalized else "?"
        normalized = f"{normalized}{separator}sslmode=require"
    return normalized


def get_pool():
    """Return the shared connection pool, initialising on first call."""
    global _pool
    if _pool is not None:
        return _pool

    with _pool_lock:
        if _pool is not None:
            return _pool
        try:
            import psycopg2.pool
            from infrastructure.config import require

            dsn = _normalize_database_url(require("DATABASE_URL"))
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=_MIN_CONN,
                maxconn=_MAX_CONN,
                dsn=dsn,
                connect_timeout=_CONNECT_TIMEOUT,
                options=f"-c statement_timeout={_STATEMENT_TIMEOUT_MS}",
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
            )
            logger.info("db.pool.initialised", extra={"min": _MIN_CONN, "max": _MAX_CONN})
        except Exception:
            logger.exception("db.pool.init_failed")
            raise
    return _pool


def health_check() -> bool:
    """Return True if a DB connection can be obtained. Used at startup."""
    conn = None
    close_conn = False
    try:
        pool = get_pool()
        conn = pool.getconn()
        with conn.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return True
    except Exception:
        close_conn = True
        logger.exception("db.pool.health_check_failed")
        return False
    finally:
        if conn is not None:
            with suppress(Exception):
                get_pool().putconn(conn, close=close_conn)


__all__ = ["get_pool", "health_check"]
