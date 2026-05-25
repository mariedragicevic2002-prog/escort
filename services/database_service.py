"""

Database service - PostgreSQL connection and query execution.
Migrated from old system with connection pooling and retry logic.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
import os
import ssl
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Optional

import psycopg2
from psycopg2 import OperationalError, pool
from psycopg2.extras import RealDictCursor

logger = logging.getLogger("adella_chatbot.database")

# Transient I/O and TLS on pooled sockets (catch dead SSL sessions, broken pipe, closed server side)
_CONNECTION_HEALTH_EXC = (psycopg2.Error, OSError, ssl.SSLError)
_MAX_POOL_GET_ATTEMPTS = 12


def _cursor_description_column_name(col) -> str:
    """Name for one ``cursor.description`` entry (tuple or object); avoids index errors on empty tuples."""
    # Some cursor implementations expose a ``name`` property that can itself
    # raise IndexError when the descriptor tuple is malformed/empty.
    try:
        name_attr = getattr(col, "name", None)
    except Exception:
        name_attr = None
    if name_attr is not None:
        return str(name_attr)
    if isinstance(col, (list, tuple)) and len(col) > 0:
        return str(col[0])
    return ""


def normalize_database_url(database_url: str) -> str:
    """Normalize postgres:// URLs to postgresql:// for psycopg2 compatibility.

    Ensure sslmode=require is present to avoid SSL handshake/cipher issues when
    connecting from some hosting providers. If sslmode already provided, leave it.
    """
    if not database_url:
        return database_url
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    # Ensure sslmode is present
    if "sslmode=" not in database_url.lower():
        connector = '&' if '?' in database_url else '?'
        database_url = f"{database_url}{connector}sslmode=require"
    return database_url


class DatabaseService:
    """Database service with connection pooling and optimistic locking."""

    def __init__(self, database_url: str, min_conn: int = 2, max_conn: int = 5):
        """
        Initialize database service.

        Args:
            database_url: PostgreSQL connection URL
            min_conn: Minimum connections in pool
            max_conn: Maximum connections in pool
        """
        self.database_url = normalize_database_url(database_url)
        self._connection_pool = None
        self._pool_lock = threading.Lock()
        self._connection_timestamps = {}
        self._connection_timestamps_lock = threading.Lock()
        # Recycle before managed Postgres / PgBouncer / cloud proxy idle or TLS rekey (reduces
        # "SSL error: cipher operation failed" and OSError: write error on half-dead sockets)
        self._MAX_CONNECTION_AGE = 120  # seconds — balance stale TLS avoidance vs pool reuse

        # Initialize pool
        self._init_pool(min_conn, max_conn)

    def _init_pool(self, min_conn: int, max_conn: int) -> None:
        """Initialize connection pool."""
        with self._pool_lock:
            if self._connection_pool is None:
                try:
                    self._connection_pool = pool.ThreadedConnectionPool(
                        min_conn,
                        max_conn,
                        self.database_url,
                        cursor_factory=RealDictCursor,
                        connect_timeout=3,
                        options="-c statement_timeout=30000",
                        keepalives=1,
                        keepalives_idle=30,
                        keepalives_interval=10,
                        keepalives_count=5
                    )
                    logger.debug("Database connection pool initialized")
                except Exception as e:
                    logger.error("Failed to initialize connection pool: %s", e)
                    self._connection_pool = None
                    raise

    def _reset_pool(self) -> None:
        """Reset connection pool."""
        with self._pool_lock:
            self._connection_pool = None
        with self._connection_timestamps_lock:
            self._connection_timestamps.clear()

    def _drop_pooled_connection(self, conn, conn_id: int, reason: str) -> None:
        """Return a bad or stale connection to the pool with close=True and clear our timestamp."""
        if self._connection_pool is None:
            return
        try:
            self._connection_pool.putconn(conn, close=True)
        except (psycopg2.Error, OSError) as e:
            logger.warning("putconn(close=True) after %s: %s", reason, e)
        with self._connection_timestamps_lock:
            self._connection_timestamps.pop(conn_id, None)

    def get_connection(self):
        """
        Get database connection from pool.

        Returns:
            tuple: (connection, from_pool)
        """
        # Try pool first — retry getconn if we discard dead/stale connections (common on hosted TLS).
        # Hold _pool_lock only for the brief getconn() call; release it before the SELECT 1 health
        # check so a slow/dead DB doesn't block all threads behind the lock for up to 36 s.
        for _attempt in range(_MAX_POOL_GET_ATTEMPTS):
            conn = None
            with self._pool_lock:
                if self._connection_pool is None:
                    break
                try:
                    conn = self._connection_pool.getconn()
                except Exception as e:
                    logger.warning("Pool getconn failed: %s", e)
                    break
            if conn is None:
                break
            if conn.closed != 0:
                self._drop_pooled_connection(conn, id(conn), "closed")
                continue
            conn_id = id(conn)
            current_time = time.time()
            with self._connection_timestamps_lock:
                last_check = self._connection_timestamps.get(conn_id, current_time)
                age = current_time - last_check
            if age > self._MAX_CONNECTION_AGE:
                logger.debug("Connection too old (%.1fs), recycling", age)
                self._drop_pooled_connection(conn, conn_id, "stale")
                continue
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
            except _CONNECTION_HEALTH_EXC as e:
                # Includes psycopg2, OSError (write/EOF), ssl.SSLError (cipher failed)
                logger.debug("Pool connection health check failed (stale SSL, recycling): %s", e)
                self._drop_pooled_connection(conn, conn_id, "health check")
                continue
            with self._connection_timestamps_lock:
                self._connection_timestamps[conn_id] = current_time
            return conn, True

        # Fallback to direct connection
        max_retries = 3
        for attempt in range(max_retries):
            try:
                conn_string = self.database_url
                if 'sslmode=' not in conn_string:
                    connector = '&' if '?' in conn_string else '?'
                    conn_string = f"{conn_string}{connector}sslmode=prefer"

                conn = psycopg2.connect(
                    conn_string,
                    connect_timeout=3,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5
                )
                return conn, False

            except OperationalError as e:
                if attempt < max_retries - 1:
                    time.sleep(1)
                    continue
                logger.warning("Database connection failed after %d retries: %s", max_retries, e)
                raise

        raise RuntimeError("Failed to connect to database")

    def return_connection(self, conn, from_pool: bool = False) -> None:
        """Return connection to pool or close it."""
        if conn is None:
            return

        try:
            if from_pool and self._connection_pool is not None:
                with self._pool_lock:
                    if self._connection_pool is not None:
                        self._connection_pool.putconn(conn)
                        return
            conn.close()
            with self._connection_timestamps_lock:
                self._connection_timestamps.pop(id(conn), None)
        except (psycopg2.Error, OSError) as e:
            logger.warning("Connection cleanup failed: %s", e)

    def _execute_on_connection(self, conn, query, params, fetch: bool):
        """
        Run one statement on an existing connection. Does not commit or release the connection.
        """
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        try:
            _eff = params or ()
            try:
                _qs = query if isinstance(query, str) else str(query)
                _ph = _qs.count("%s")
                _pc = len(_eff) if isinstance(_eff, (list, tuple)) else 0
                if _ph != _pc:
                    logger.error(
                        "PARAM COUNT MISMATCH (conn path): query has %d %%s but %d params. query=%r params=%r",
                        _ph, _pc, _qs[:400], _eff, exc_info=True,
                    )
            except Exception:
                pass
            cursor.execute(query, _eff)

            if fetch:
                rows = cursor.fetchall()
                # Ensure callers always receive list[dict] for fetch=True
                if rows and not isinstance(rows[0], dict):
                    desc = cursor.description or ()
                    names = [
                        (_cursor_description_column_name(c) or f"col_{i}")
                        for i, c in enumerate(desc)
                    ]
                    rows = [dict(zip(names, r)) for r in rows]
                return rows

            # If the statement produced a result set (e.g. INSERT ... RETURNING),
            # return the first value for compatibility with existing callers.
            if cursor.description is not None:
                try:
                    result = cursor.fetchone()
                    if result:
                        if isinstance(result, dict):
                            return next(iter(result.values()))
                        return result[0]
                except (IndexError, KeyError, TypeError) as e:
                    logger.warning("Failed to extract single value from cursor result: %s", e)

            return None
        finally:
            try:
                cursor.close()
            except psycopg2.Error as e:
                logger.warning("cursor.close failed: %s", e)

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """
        Run multiple statements on one connection; commit on success, rollback on error.

        Usage::

            with db.transaction() as conn:
                db.execute_query(sql, params, fetch=False, conn=conn)
        """
        conn = None
        from_pool = False
        try:
            conn, from_pool = self.get_connection()
            yield conn
            conn.commit()
        except Exception:
            if conn:
                try:
                    conn.rollback()
                except psycopg2.Error as e:
                    logger.warning("transaction rollback failed: %s", e)
            raise
        finally:
            if conn:
                self.return_connection(conn, from_pool)

    def execute_query(
        self,
        query: Any,
        params: tuple[Any, ...] | None = None,
        fetch: bool = False,
        max_retries: int = 3,
        *,
        conn=None,
    ):
        """
        Execute query with retry logic.

        Args:
            query: SQL string or psycopg2.sql.Composable
            params: Query parameters
            fetch: Whether to fetch results
            max_retries: Maximum retry attempts (ignored when ``conn`` is passed)
            conn: If set, run on this connection only (no commit here — use :meth:`transaction`)

        Returns:
            Query results if fetch=True, otherwise None
        """
        if conn is not None:
            try:
                return self._execute_on_connection(conn, query, params, fetch)
            except OperationalError:
                raise
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                raise

        for attempt in range(max_retries):
            conn = None
            cursor = None
            from_pool = False

            try:
                conn, from_pool = self.get_connection()
                cursor = conn.cursor(cursor_factory=RealDictCursor)

                _effective_params = params or ()
                # Defensive: detect %s count vs param count mismatch before psycopg2 raises
                # an opaque "tuple index out of range" with no traceback context.
                try:
                    _query_str = query if isinstance(query, str) else str(query)
                    _ph_count = _query_str.count("%s")
                    _param_count = len(_effective_params) if isinstance(_effective_params, (list, tuple)) else 0
                    if _ph_count != _param_count:
                        logger.error(
                            "PARAM COUNT MISMATCH: query has %d %%s placeholders but %d params were supplied. "
                            "query=%r params=%r",
                            _ph_count, _param_count, _query_str[:400], _effective_params,
                            exc_info=True,
                        )
                except Exception:
                    pass
                cursor.execute(query, _effective_params)

                if fetch:
                    rows = cursor.fetchall()
                    # Convert to list[dict] when source rows are tuples
                    if rows and not isinstance(rows[0], dict):
                        desc = cursor.description or ()
                        names = [
                            (_cursor_description_column_name(c) or f"col_{i}")
                            for i, c in enumerate(desc)
                        ]
                        rows = [dict(zip(names, r)) for r in rows]
                    conn.commit()
                    return rows

                conn.commit()

                # If the statement produced a result set (e.g. INSERT ... RETURNING),
                # return the first value for compatibility with existing callers.
                if cursor.description is not None:
                    try:
                        result = cursor.fetchone()
                        if result:
                            if isinstance(result, dict):
                                return next(iter(result.values()))
                            return result[0]
                    except (IndexError, KeyError, TypeError) as e:
                        logger.warning("Failed to extract single value from cursor result: %s", e)

                return None

            except OperationalError as e:
                error_str = str(e).lower()
                if conn:
                    try:
                        conn.rollback()
                    except psycopg2.Error as rb_err:
                        logger.warning("rollback after OperationalError: %s", rb_err)

                # Retry on connection issues
                if any(err in error_str for err in ['connection', 'ssl', 'timeout', 'cipher', 'eof', 'syscall']) and attempt < max_retries - 1:
                    time.sleep(0.3 * (attempt + 1))
                    self._reset_pool()
                    continue
                raise

            except ssl.SSLError:
                if conn:
                    try:
                        conn.rollback()
                    except (psycopg2.Error, OSError) as rb_err:
                        logger.warning("rollback after SSLError: %s", rb_err)
                if attempt < max_retries - 1:
                    time.sleep(0.3 * (attempt + 1))
                    self._reset_pool()
                    continue
                raise

            except OSError as e:
                if conn:
                    try:
                        conn.rollback()
                    except (psycopg2.Error, OSError) as rb_err:
                        logger.warning("rollback after OSError: %s", rb_err)
                el = str(e).lower()
                if attempt < max_retries - 1 and any(
                    x in el for x in ('write', 'read', 'broken pipe', 'connection', 'ssl', 'socket', 'cipher')
                ):
                    time.sleep(0.3 * (attempt + 1))
                    self._reset_pool()
                    continue
                raise

            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
                if conn:
                    try:
                        conn.rollback()
                    except psycopg2.Error as rb_err:
                        logger.warning("rollback after query error: %s", rb_err)
                raise

            finally:
                if cursor:
                    try:
                        cursor.close()
                    except psycopg2.Error as e:
                        logger.warning("cursor.close failed: %s", e)
                if conn:
                    self.return_connection(conn, from_pool)

        raise RuntimeError("Database operation failed after all retries")

    def init_schema(self, schema_file: str) -> None:
        """
        Initialize database schema from SQL file.
        Handles PostgreSQL functions and DO blocks properly.

        Args:
            schema_file: Path to SQL schema file
        """
        try:
            with open(schema_file, encoding='utf-8') as f:
                schema_sql = f.read()

            # Use psycopg2's execute() which can handle multiple statements
            # This properly handles PostgreSQL functions with $$ delimiters
            conn = None
            cursor = None
            from_pool = False
            
            try:
                conn, from_pool = self.get_connection()
                cursor = conn.cursor()
                
                # Execute entire SQL file - psycopg2 handles multiple statements
                cursor.execute(schema_sql)
                conn.commit()
                
                logger.info("Database schema initialized from %s", schema_file)
            except psycopg2.Error as e:
                logger.warning("Schema execute failed: %s", e)
                if conn:
                    try:
                        conn.rollback()
                    except psycopg2.Error as rb_err:
                        logger.warning("Schema rollback failed: %s", rb_err)
                raise
            finally:
                if cursor:
                    try:
                        cursor.close()
                    except psycopg2.Error as e:
                        logger.warning("init_schema cursor.close failed: %s", e)
                if conn:
                    self.return_connection(conn, from_pool)
                    
        except Exception as e:
            logger.error("Failed to initialize schema: %s", e)
            raise


# Shared instance so we don't create a new connection pool per request (e.g. in admin).
_shared_db: Optional["DatabaseService"] = None
_shared_db_init_failed = False
_shared_db_retry_once_done = False
_shared_db_lock = threading.Lock()


def _resolve_database_url(database_url: str | None = None) -> str:
    """Resolve DATABASE_URL from explicit arg, env, config snapshot, then local .env."""
    candidates = [database_url, os.environ.get("DATABASE_URL")]
    try:
        import config as _config

        candidates.append(getattr(_config, "DATABASE_URL", ""))
    except Exception:
        pass
    for c in candidates:
        v = (c or "").strip()
        if v:
            return v

    # Final local fallback for WSGI/CLI contexts where env wasn't injected.
    try:
        from dotenv import dotenv_values

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env_file = os.path.join(project_root, ".env")
        if os.path.isfile(env_file):
            return (dotenv_values(env_file).get("DATABASE_URL") or "").strip()
    except Exception:
        pass
    return ""


def get_shared_db_health_snapshot() -> dict:
    """
    Non-secret pool state for /healthcheck (combine with DATABASE_URL presence in the app).
    """
    return {
        "pool_initialized": _shared_db is not None,
        "pool_init_failed": _shared_db_init_failed,
    }


def reset_shared_db_connection() -> None:
    """Clear pool and failure flag so the next get_shared_db() can try again (e.g. after fixing DATABASE_URL)."""
    global _shared_db, _shared_db_init_failed
    with _shared_db_lock:
        _shared_db = None
        _shared_db_init_failed = False


def get_shared_db(database_url: str | None = None) -> Optional["DatabaseService"]:
    """Return a single shared DatabaseService for the app. Use this instead of DatabaseService(...) in request handlers."""
    global _shared_db, _shared_db_init_failed
    resolved_url = _resolve_database_url(database_url)
    if _shared_db is not None:
        return _shared_db
    if _shared_db_init_failed:
        return None
    if not resolved_url:
        return None
    with _shared_db_lock:
        if _shared_db is not None:
            return _shared_db
        if _shared_db_init_failed:
            return None
        try:
            import os as _os
            _min = max(1, int(_os.environ.get("DB_POOL_MIN", "2")))
            _max = max(_min, int(_os.environ.get("DB_POOL_MAX", "15")))
            _shared_db = DatabaseService(resolved_url, min_conn=_min, max_conn=_max)
        except Exception as e:
            logger.error("get_shared_db: failed to create pool: %s", e, exc_info=True)
            _shared_db_init_failed = True
            return None
    return _shared_db


def get_shared_db_with_retry(database_url: str | None = None) -> Optional["DatabaseService"]:
    """
    Like get_shared_db, but if the pool previously failed to start, reset and try again
    at most once per process (avoids hammering Postgres on every request when the DB is down).
    """
    global _shared_db_retry_once_done
    resolved_url = _resolve_database_url(database_url)
    db = get_shared_db(resolved_url)
    if db is not None:
        return db
    if not resolved_url:
        return None
    if not _shared_db_init_failed:
        return None
    with _shared_db_lock:
        if _shared_db_retry_once_done:
            return None
        _shared_db_retry_once_done = True
    logger.info("One-time retry of shared database pool after initial connection failure")
    reset_shared_db_connection()
    return get_shared_db(resolved_url)
