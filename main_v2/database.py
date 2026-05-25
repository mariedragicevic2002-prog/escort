"""Database initialization and optional startup migrations."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import os
import re

import config
from app.migrations import (
    MigrationPlanStep,
    SchemaRequirement,
    capture_rollback_metadata,
    run_preflight_checks,
    validate_migration_plan,
)
from services.database_service import get_shared_db, normalize_database_url

from .log import logger

_CREATE_TABLE_RE = re.compile(r"(?is)^create\s+table\s+if\s+not\s+exists\s+([^\s(]+)")
_CREATE_INDEX_RE = re.compile(r"(?is)^create\s+(?:unique\s+)?index\s+if\s+not\s+exists\s+([^\s]+)")
_ALTER_ADD_COLUMN_RE = re.compile(
    r"(?is)^alter\s+table\s+([^\s]+)\s+add\s+column\s+if\s+not\s+exists\s+([^\s]+)"
)


def _normalize_sql_for_migration(sql: str) -> str:
    return " ".join(str(sql or "").split())


def _infer_rollback_sql(forward_sql: str) -> str | None:
    normalized = _normalize_sql_for_migration(forward_sql)
    if not normalized:
        return None
    create_table_match = _CREATE_TABLE_RE.match(normalized)
    if create_table_match:
        table_name = create_table_match.group(1)
        return f"DROP TABLE IF EXISTS {table_name}"
    create_index_match = _CREATE_INDEX_RE.match(normalized)
    if create_index_match:
        index_name = create_index_match.group(1)
        return f"DROP INDEX IF EXISTS {index_name}"
    alter_match = _ALTER_ADD_COLUMN_RE.match(normalized)
    if alter_match:
        table_name = alter_match.group(1)
        column_name = alter_match.group(2).rstrip(",")
        return f"ALTER TABLE {table_name} DROP COLUMN IF EXISTS {column_name}"
    return None


def _build_startup_migration_plan(statements: list[str]) -> list[MigrationPlanStep]:
    plan: list[MigrationPlanStep] = []
    for index, statement in enumerate(statements, start=1):
        rollback_sql = _infer_rollback_sql(statement)
        plan.append(
            MigrationPlanStep(
                operation_id=f"startup-column-{index:03d}",
                forward_sql=_normalize_sql_for_migration(statement),
                description="startup schema migration",
                reversible=bool(rollback_sql),
                rollback_sql=rollback_sql,
                metadata={"source": "main_v2.database._startup_column_migrations"},
            )
        )
    return plan


def _startup_preflight_requirements(plan: list[MigrationPlanStep]) -> list[SchemaRequirement]:
    required_tables: set[str] = set()
    for step in plan:
        match = _ALTER_ADD_COLUMN_RE.match(_normalize_sql_for_migration(step.forward_sql))
        if not match:
            continue
        required_tables.add(match.group(1).strip('"').lower())
    return [SchemaRequirement(table=table_name) for table_name in sorted(required_tables)]


def _plan_targets_missing_table(step: MigrationPlanStep, missing_tables: set[str]) -> bool:
    if not missing_tables:
        return False
    match = _ALTER_ADD_COLUMN_RE.match(_normalize_sql_for_migration(step.forward_sql))
    if not match:
        return False
    table_name = match.group(1).strip('"').lower()
    return table_name in missing_tables


class _CursorMigrationExecutor:
    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def execute_query(self, query, params=(), fetch=True, conn=None, **_kwargs):
        _ = conn
        self._cursor.execute(query, params)
        if not fetch:
            return None
        try:
            rows = self._cursor.fetchall()
        except Exception:
            return []
        return list(rows or [])


def initialize_database_service():
    """Initialize the shared DB service.

    Previously we returned None silently on any failure — that turned a DB
    outage at boot into "every request 500s" with zero startup-time signal
    (H8). Now:

    * If ``DATABASE_URL`` is unset we still return None — that's a dev/test
      scenario (no DB configured) and must not crash-loop the worker.
    * If ``DATABASE_URL`` is set but the pool fails to connect, log the failure
      and let the app boot in degraded mode so PythonAnywhere does not serve a
      blank 502 page.
    """
    db_url = (getattr(config, "DATABASE_URL", "") or "").strip()
    in_production = not getattr(config, "DEBUG", False)
    try:
        service = get_shared_db(config.DATABASE_URL)
        if service:
            logger.info("Database service initialized")
        elif db_url:
            logger.error(
                "DATABASE_URL was provided but the pool could not be initialized at startup; "
                "continuing without a database connection."
            )
        return service
    except Exception as e:
        logger.exception("Database service init failed at startup; continuing without DB")
        return None


def _startup_migrations_enabled() -> bool:
    """Resolve startup migration toggle from env first, then admin settings."""
    env_raw = os.environ.get("RUN_STARTUP_DB_MIGRATIONS", "").strip()
    if env_raw:
        return env_raw.lower() == "true"

    try:
        from core.settings_manager import get_setting

        setting_raw = (get_setting("run_startup_db_migrations") or "").strip().lower()
        return setting_raw in ("true", "1", "yes")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return False


def ensure_bookings_compat(db_service):
    """Unconditionally fix legacy bookings table constraints on every startup.

    Drops NOT NULL from phone_number/date/time columns that the old schema left
    as required but current code no longer populates. Idempotent — safe to run
    repeatedly. Runs regardless of RUN_STARTUP_DB_MIGRATIONS.
    """
    if not db_service:
        return
    fixes = [
        "ALTER TABLE bookings ALTER COLUMN phone_number DROP NOT NULL",
        "ALTER TABLE bookings ALTER COLUMN phone_number SET DEFAULT ''",
        "ALTER TABLE bookings ALTER COLUMN date DROP NOT NULL",
        "ALTER TABLE bookings ALTER COLUMN time DROP NOT NULL",
    ]
    for sql in fixes:
        try:
            db_service.execute_query(sql, fetch=False)
        except Exception as e:
            logger.debug("bookings compat patch skipped (%s): %s", sql[:60], e)


def maybe_run_startup_database_tasks(db_service):
    """Run startup schema/migration work only when explicitly enabled."""
    if not db_service:
        logger.info("Skipping startup database tasks - database service unavailable")
        return

    if not _startup_migrations_enabled():
        logger.info("Skipping startup schema/migration tasks (RUN_STARTUP_DB_MIGRATIONS=false)")
        return

    try:
        schema_file = os.path.join(config.BASE_DIR, "migrations", "schema.sql")
        if os.path.exists(schema_file):
            db_service.init_schema(schema_file)
            logger.info("Database schema initialized (complete schema)")
        else:
            logger.warning(f"Schema file not found: {schema_file}")
    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}")

    _run_startup_column_migrations()


def _startup_column_migrations() -> list[str]:
    return [
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS room_detail_reminder_scheduled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS deposit_reason VARCHAR(255)",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS optional_deposit_amount INTEGER",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS optional_deposit_paid BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS optional_deposit_paid_at TIMESTAMP",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS manual_review_required BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS _verified_address TEXT",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS _verified_distance_km DOUBLE PRECISION",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS total_booking_cost INTEGER",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS special_requests TEXT",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS feedback_request_sent BOOLEAN DEFAULT FALSE",
        """CREATE TABLE IF NOT EXISTS client_feedback (
            id SERIAL PRIMARY KEY,
            client_phone_number VARCHAR(20) NOT NULL,
            client_name VARCHAR(100),
            booking_date DATE,
            booking_time TIME,
            duration INTEGER,
            experience_type VARCHAR(50),
            incall_outcall VARCHAR(10),
            arrived_on_time BOOLEAN,
            was_respectful BOOLEAN,
            would_see_again BOOLEAN,
            star_rating SMALLINT,
            feedback_received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        """CREATE TABLE IF NOT EXISTS feedback_pending (
            id SERIAL PRIMARY KEY,
            client_phone_number VARCHAR(20) NOT NULL,
            requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE client_feedback ADD COLUMN IF NOT EXISTS comments TEXT",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS awaiting_refund_details BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS outcall_awaiting_yes BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS incall_awaiting_yes BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS awaiting_name BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS deposit_payment_reference VARCHAR(20)",
        """CREATE TABLE IF NOT EXISTS upload_tokens (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(20) NOT NULL,
            short_code VARCHAR(6) NOT NULL UNIQUE,
            deposit_amount INTEGER NOT NULL DEFAULT 100,
            payment_reference VARCHAR(20),
            created_at TIMESTAMP DEFAULT NOW(),
            used BOOLEAN DEFAULT FALSE,
            used_at TIMESTAMP,
            upload_attempts INTEGER DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_upload_tokens_short_code ON upload_tokens(short_code)",
        "CREATE INDEX IF NOT EXISTS idx_upload_tokens_phone_number ON upload_tokens(phone_number)",
        "CREATE INDEX IF NOT EXISTS idx_upload_tokens_created ON upload_tokens(created_at DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_upload_tokens_payment_reference ON upload_tokens(payment_reference)",
        "ALTER TABLE upload_tokens ADD COLUMN IF NOT EXISTS token_hash VARCHAR(64) NOT NULL DEFAULT ''",
        "ALTER TABLE upload_tokens ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(20)",
        """CREATE TABLE IF NOT EXISTS admin_audit_log (
            id SERIAL PRIMARY KEY,
            action VARCHAR(100) NOT NULL,
            details TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_admin_audit_log_created ON admin_audit_log(created_at DESC)",
        """CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            component VARCHAR(50) NOT NULL,
            message TEXT,
            severity VARCHAR(20) DEFAULT 'warning',
            created_at TIMESTAMP DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC)",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS confirmed_ai_reply_count INTEGER DEFAULT 0",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS earliest_slot_auto_selected BOOLEAN DEFAULT FALSE",
        """CREATE TABLE IF NOT EXISTS safety_screening_watchlist (
            id SERIAL PRIMARY KEY,
            normalized_phone VARCHAR(20) NOT NULL UNIQUE,
            raw_phone VARCHAR(40),
            source_label VARCHAR(64) DEFAULT 'config_excel_upload',
            is_active BOOLEAN DEFAULT TRUE,
            warning_recency_rank INTEGER,
            report_count INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "ALTER TABLE safety_screening_watchlist ADD COLUMN IF NOT EXISTS warning_recency_rank INTEGER",
        "ALTER TABLE safety_screening_watchlist ADD COLUMN IF NOT EXISTS report_count INTEGER DEFAULT 0",
        "CREATE INDEX IF NOT EXISTS idx_safety_watchlist_active ON safety_screening_watchlist (is_active, normalized_phone)",
        """CREATE TABLE IF NOT EXISTS safety_screening_match_log (
            id SERIAL PRIMARY KEY,
            phone_number VARCHAR(20),
            normalized_phone VARCHAR(20),
            matched BOOLEAN DEFAULT FALSE,
            action_taken VARCHAR(20) DEFAULT 'warn_only',
            escort_notified BOOLEAN DEFAULT FALSE,
            note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""",
        "CREATE INDEX IF NOT EXISTS idx_safety_match_log_phone ON safety_screening_match_log (normalized_phone, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_safety_match_log_notified ON safety_screening_match_log (escort_notified, created_at DESC)",
        """CREATE TABLE IF NOT EXISTS httpsms_message_dedup (
            message_id TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""",
        "CREATE INDEX IF NOT EXISTS idx_httpsms_message_dedup_created ON httpsms_message_dedup (created_at DESC)",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS forward_incall_replies_to_escort BOOLEAN DEFAULT FALSE",
        "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS _consecutive_same_response_count INTEGER DEFAULT 0",
    ]


def _run_startup_column_migrations() -> None:
    try:
        import psycopg2 as _psycopg2

        plan = _build_startup_migration_plan(_startup_column_migrations())
        validation = validate_migration_plan(
            plan,
            allow_destructive=False,
            dry_run=False,
        )
        if not validation.valid:
            logger.error("Startup migration plan validation failed: %s", "; ".join(validation.errors))
            return

        _db_url = normalize_database_url(config.DATABASE_URL)
        _mig_conn = _psycopg2.connect(_db_url, connect_timeout=5)
        _mig_conn.autocommit = True
        _mig_cur = _mig_conn.cursor()
        try:
            _mig_cur.execute("SET statement_timeout TO 5000")
            _mig_cur.execute("SET lock_timeout TO 2000")

            preflight_requirements = _startup_preflight_requirements(plan)
            missing_tables: set[str] = set()
            if preflight_requirements:
                preflight = run_preflight_checks(
                    _CursorMigrationExecutor(_mig_cur),
                    preflight_requirements,
                )
                missing_tables = set(preflight.missing_tables)
                if not preflight.passed:
                    logger.warning(
                        "Startup migration preflight missing_tables=%s missing_columns=%s missing_indexes=%s",
                        list(preflight.missing_tables),
                        list(preflight.missing_columns),
                        list(preflight.missing_indexes),
                    )

            rollback_bundle = capture_rollback_metadata(
                plan,
                dry_run=False,
                plan_hash=validation.plan_hash,
                actor="startup_migration_runner",
            )
            logger.info(
                "Startup migration rollback metadata captured for %s reversible steps",
                len(rollback_bundle.entries),
            )

            for step in plan:
                if _plan_targets_missing_table(step, missing_tables):
                    logger.warning("Migration skipped by preflight: %s", step.operation_id)
                    continue
                try:
                    _mig_cur.execute(step.forward_sql)
                    logger.info("Migration applied: %s...", step.forward_sql[:60])
                except Exception as _e:
                    logger.warning("Migration skipped: %s", _e)
        finally:
            _mig_cur.close()
            _mig_conn.close()
    except Exception as _e:
        logger.error("Startup migration failed: %s", _e)
