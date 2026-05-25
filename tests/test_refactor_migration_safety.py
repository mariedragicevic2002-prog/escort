from __future__ import annotations

from datetime import UTC, datetime

from main_v2 import database as startup_database
from refactor.app.migrations import (
    MigrationPlanStep,
    SchemaRequirement,
    build_plan_hash,
    capture_rollback_metadata,
    run_preflight_checks,
    validate_migration_plan,
)


class _FakeSchemaDB:
    def __init__(self, tables: dict[str, dict[str, set[str]]]) -> None:
        self._tables = {
            str(table_name).lower(): {
                "columns": {str(column).lower() for column in payload.get("columns", set())},
                "indexes": {str(index_name).lower() for index_name in payload.get("indexes", set())},
            }
            for table_name, payload in tables.items()
        }

    def execute_query(self, query, params=(), fetch=True, conn=None, **_kwargs):
        _ = (fetch, conn)
        sql = " ".join(str(query).split()).lower()
        if "from information_schema.tables" in sql:
            table_name = str(params[0]).lower()
            return [{"table_name": table_name}] if table_name in self._tables else []
        if "from information_schema.columns" in sql:
            table_name = str(params[0]).lower()
            column_name = str(params[1]).lower()
            if table_name in self._tables and column_name in self._tables[table_name]["columns"]:
                return [{"column_name": column_name}]
            return []
        if "from pg_indexes" in sql:
            table_name = str(params[0]).lower()
            return [
                {"indexname": index_name}
                for index_name in sorted(self._tables.get(table_name, {}).get("indexes", set()))
            ]
        raise AssertionError(f"Unexpected SQL in fake schema DB: {query}")


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


def test_preflight_checks_detect_pass_and_fail_states() -> None:
    passing_report = run_preflight_checks(
        _FakeSchemaDB(
            {
                "conversation_states": {
                    "columns": {"current_state", "manual_review_required"},
                    "indexes": {"idx_conversation_states_phone_number"},
                }
            }
        ),
        [
            SchemaRequirement(
                table="conversation_states",
                required_columns=("current_state", "manual_review_required"),
                required_indexes=("idx_conversation_states_phone_number",),
            )
        ],
        now_provider=_fixed_now,
    )
    assert passing_report.passed is True
    assert passing_report.missing_tables == ()
    assert passing_report.missing_columns == ()
    assert passing_report.missing_indexes == ()

    failing_report = run_preflight_checks(
        _FakeSchemaDB(
            {
                "conversation_states": {
                    "columns": {"current_state"},
                    "indexes": set(),
                }
            }
        ),
        [
            SchemaRequirement(
                table="conversation_states",
                required_columns=("current_state", "manual_review_required"),
                required_indexes=("idx_conversation_states_phone_number",),
            ),
            SchemaRequirement(table="refactor_inbound_queue_messages"),
        ],
        now_provider=_fixed_now,
    )
    assert failing_report.passed is False
    assert ("conversation_states", "manual_review_required") in failing_report.missing_columns
    assert ("conversation_states", "idx_conversation_states_phone_number") in failing_report.missing_indexes
    assert "refactor_inbound_queue_messages" in failing_report.missing_tables


def test_dry_run_plan_validation_report_is_stable() -> None:
    steps = [
        MigrationPlanStep(
            operation_id="step-001",
            forward_sql=" CREATE TABLE   IF NOT EXISTS refactor_inbound_queue_messages (id TEXT PRIMARY KEY) ",
            description="create inbound queue table",
            reversible=True,
            rollback_sql=" DROP TABLE IF EXISTS refactor_inbound_queue_messages ",
            metadata={"phase": 6},
        ),
        MigrationPlanStep(
            operation_id="step-002",
            forward_sql="CREATE INDEX IF NOT EXISTS idx_refactor_inbound_queue_status_retry ON refactor_inbound_queue_messages (id)",
            description="create inbound queue index",
            reversible=True,
            rollback_sql="DROP INDEX IF EXISTS idx_refactor_inbound_queue_status_retry",
        ),
    ]

    report_one = validate_migration_plan(steps, dry_run=True, now_provider=_fixed_now).to_dict()
    report_two = validate_migration_plan(steps, dry_run=True, now_provider=_fixed_now).to_dict()

    assert report_one == report_two
    assert report_one["valid"] is True
    assert report_one["error_count"] == 0
    assert len(report_one["plan_hash"]) == 64


def test_rollback_metadata_captures_reversible_operations_only() -> None:
    steps = [
        MigrationPlanStep(
            operation_id="step-001",
            forward_sql="CREATE TABLE IF NOT EXISTS refactor_outbox_events (event_id TEXT PRIMARY KEY)",
            description="create outbox table",
            reversible=True,
            rollback_sql="DROP TABLE IF EXISTS refactor_outbox_events",
        ),
        MigrationPlanStep(
            operation_id="step-002",
            forward_sql="ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS queue_cursor TEXT",
            description="add state cursor",
            reversible=True,
            rollback_sql="ALTER TABLE conversation_states DROP COLUMN IF EXISTS queue_cursor",
        ),
        MigrationPlanStep(
            operation_id="step-003",
            forward_sql="UPDATE conversation_states SET current_state = current_state",
            description="no-op data statement",
            reversible=False,
        ),
    ]

    bundle = capture_rollback_metadata(
        steps,
        dry_run=False,
        actor="pytest",
        run_id="run-42",
        now_provider=_fixed_now,
    )
    payload = bundle.to_dict()

    assert payload["plan_hash"] == build_plan_hash(steps)
    assert payload["entry_count"] == 2
    assert [entry["operation_id"] for entry in payload["entries"]] == ["step-001", "step-002"]
    assert all(entry["metadata"]["captured_by"] == "pytest" for entry in payload["entries"])
    assert all(entry["metadata"]["run_id"] == "run-42" for entry in payload["entries"])


def test_startup_migration_helpers_produce_preflight_and_rollback_ready_steps() -> None:
    plan = startup_database._build_startup_migration_plan(
        [
            "ALTER TABLE conversation_states ADD COLUMN IF NOT EXISTS queue_cursor TEXT",
            "CREATE INDEX IF NOT EXISTS idx_conversation_states_queue_cursor ON conversation_states(queue_cursor)",
        ]
    )
    requirements = startup_database._startup_preflight_requirements(plan)

    assert [step.reversible for step in plan] == [True, True]
    assert [step.rollback_sql for step in plan] == [
        "ALTER TABLE conversation_states DROP COLUMN IF EXISTS queue_cursor",
        "DROP INDEX IF EXISTS idx_conversation_states_queue_cursor",
    ]
    assert len(requirements) == 1
    assert requirements[0].table == "conversation_states"
    assert startup_database._plan_targets_missing_table(plan[0], {"conversation_states"}) is True
