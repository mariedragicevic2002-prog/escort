from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.config_governance import (
    ConfigFieldContract,
    ConfigRegistryContract,
    ConfigValidationError,
    TypedConfigRegistry,
    allowed_values,
    numeric_bounds,
    resolve_registered_contract,
)
from app.cost_controls import (
    ProcessingBudgetController,
    ProcessingBudgetSettings,
    QueueCostSignals,
    build_cost_control_advisories,
)
from app.migrations import (
    MigrationPlanStep,
    MigrationSchemaExecutor,
    SchemaRequirement,
    capture_rollback_metadata,
    run_preflight_checks,
    validate_migration_plan,
)
from app.resilience import (
    DeterministicFailureInjector,
    DeterministicFailurePlan,
    INGRESS_ENQUEUE_FAILURE_POINT,
    ResilienceDrillFailure,
    ResilienceDrillContext,
    ResilienceDrillRunner,
    ResilienceDrillScenario,
    ResilienceDrillStep,
    RetryableDrillError,
    WORKER_LEASE_EXPIRY_POINT,
)
from app.security.rotation import (
    build_secret_validation_window,
    match_secret,
    resolve_secret_rotation_config,
)


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, 0, 0, tzinfo=UTC)


class _ScriptedSchemaExecutor(MigrationSchemaExecutor):
    def __init__(
        self,
        *,
        tables: dict[str, dict[str, set[str]]],
        table_failures: dict[str, str] | None = None,
    ) -> None:
        self._tables = {
            str(table).lower(): {
                "columns": {str(column).lower() for column in payload.get("columns", set())},
                "indexes": {str(index).lower() for index in payload.get("indexes", set())},
            }
            for table, payload in tables.items()
        }
        self._table_failures = {str(table).lower(): str(reason) for table, reason in (table_failures or {}).items()}

    def execute_query(
        self,
        query: str,
        params: Sequence[Any] = (),
        *,
        fetch: bool | None = True,
        conn: Any | None = None,
        **_kwargs: Any,
    ) -> Any:
        _ = (fetch, conn)
        sql = " ".join(str(query).split()).lower()
        if "from information_schema.tables" in sql:
            table_name = str(params[0]).lower()
            if table_name in self._table_failures:
                raise RuntimeError(self._table_failures[table_name])
            return [{"table_name": table_name}] if table_name in self._tables else []
        if "from information_schema.columns" in sql:
            table_name = str(params[0]).lower()
            column_name = str(params[1]).lower()
            if table_name in self._tables and column_name in self._tables[table_name]["columns"]:
                return [{"column_name": column_name}]
            return []
        if "from pg_indexes" in sql:
            table_name = str(params[0]).lower()
            return [{"indexname": index_name} for index_name in sorted(self._tables.get(table_name, {}).get("indexes", set()))]
        raise AssertionError(f"Unexpected SQL in scripted schema DB: {query}")


class _MutableNow:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: int) -> None:
        self.value = self.value + timedelta(seconds=max(0, int(seconds)))


def _run_deterministic_resilience_scenario() -> dict[str, Any]:
    lease_expiry_advances: list[str] = []
    injector = DeterministicFailureInjector(
        enabled=True,
        test_mode=True,
        plan=DeterministicFailurePlan(
            ingress_enqueue_failure=(1,),
            worker_lease_expiry=(1,),
        ),
        lease_expiry_advancer=lambda queue_name, item_id: lease_expiry_advances.append(f"{queue_name}:{item_id}"),
    )

    def _enqueue_step(context: ResilienceDrillContext) -> None:
        try:
            injector.before_ingress_enqueue(
                channel="sms",
                request_id="req-phase6",
                dedup_key="dedup-phase6",
            )
        except ResilienceDrillFailure:
            context.transition(component="ingress", to_state="retry", reason="deterministic_failure")
            raise RetryableDrillError("retry enqueue")
        context.transition(component="ingress", to_state="accepted", reason="enqueued")

    def _dispatch_step(context: ResilienceDrillContext) -> None:
        injector.before_worker_dispatch(queue_name="refactor_outbox", item_id="evt-phase6")
        context.transition(component="worker", to_state="processing", reason="dispatch_ok")
        context.artifacts["lease_expiry_advances"] = len(lease_expiry_advances)

    report = ResilienceDrillRunner(test_mode=True, max_step_executions=6).run(
        ResilienceDrillScenario(
            scenario_id="phase6-deterministic-drill",
            description="Inject deterministic ingress and lease failures with bounded retries",
            steps=(
                ResilienceDrillStep(step_id="enqueue", handler=_enqueue_step, retry_limit=1),
                ResilienceDrillStep(step_id="dispatch", handler=_dispatch_step),
            ),
            max_step_executions=6,
        ),
        context=ResilienceDrillContext(),
        drill_hook=injector,
    )
    return report.to_artifact()


def test_config_governance_strict_validation_defaulting_and_drift_are_deterministic() -> None:
    registry = TypedConfigRegistry()
    registry.register(
        ConfigRegistryContract(
            namespace="phase6.governance",
            fields={
                "mode": ConfigFieldContract(
                    name="mode",
                    default="safe",
                    parser=lambda value: str(value).strip().lower(),
                    validators=(allowed_values(("safe", "degraded")),),
                ),
                "max_retries": ConfigFieldContract(
                    name="max_retries",
                    default=3,
                    parser=int,
                    validators=(numeric_bounds(minimum=1, maximum=5),),
                ),
                "shadow_read": ConfigFieldContract(
                    name="shadow_read",
                    default=False,
                    parser=lambda value: str(value).strip().lower() in {"1", "true", "yes"},
                ),
            },
        )
    )

    resolution = resolve_registered_contract(
        registry=registry,
        namespace="phase6.governance",
        raw_values={"mode": "chaos", "max_retries": "not-a-number"},
        strict=False,
    )

    assert resolution.values["mode"] == "safe"
    assert resolution.values["max_retries"] == 3
    assert resolution.values["shadow_read"] is False
    assert set(resolution.report.defaults_applied) == {"mode", "max_retries", "shadow_read"}
    assert {issue.code for issue in resolution.report.issues} == {"constraint_violation", "parse_error"}
    assert {entry.field for entry in (resolution.report.drift.entries if resolution.report.drift else ())} == {
        "mode",
        "max_retries",
    }

    with pytest.raises(ConfigValidationError):
        resolve_registered_contract(
            registry=registry,
            namespace="phase6.governance",
            raw_values={"mode": "chaos", "max_retries": "not-a-number"},
            strict=True,
        )


def test_migration_preflight_dry_run_and_rollback_metadata_cover_failure_paths() -> None:
    preflight = run_preflight_checks(
        _ScriptedSchemaExecutor(
            tables={
                "conversation_states": {
                    "columns": {"current_state"},
                    "indexes": set(),
                }
            },
            table_failures={"legacy_shadow": "injected metadata failure"},
        ),
        (
            SchemaRequirement(
                table="conversation_states",
                required_columns=("current_state", "queue_cursor"),
                required_indexes=("idx_conversation_states_phone_number",),
            ),
            SchemaRequirement(table="legacy_shadow", required_columns=("id",)),
            SchemaRequirement(table="refactor_inbound_queue_messages"),
        ),
        now_provider=_fixed_now,
    )

    assert preflight.passed is False
    assert ("conversation_states", "queue_cursor") in preflight.missing_columns
    assert ("conversation_states", "idx_conversation_states_phone_number") in preflight.missing_indexes
    assert "refactor_inbound_queue_messages" in preflight.missing_tables
    assert {"missing_column", "missing_index", "missing_table", "table_check_error"}.issubset(
        {issue.check_type for issue in preflight.issues}
    )

    steps = (
        MigrationPlanStep(
            operation_id="step-001",
            forward_sql="CREATE TABLE IF NOT EXISTS refactor_inbound_queue_messages (id TEXT PRIMARY KEY)",
            description="create queue table",
            reversible=True,
            rollback_sql="DROP TABLE IF EXISTS refactor_inbound_queue_messages",
            metadata={"phase": 6},
        ),
        MigrationPlanStep(
            operation_id="step-002",
            forward_sql="DELETE FROM conversation_states WHERE current_state = 'orphaned'",
            description="cleanup orphaned rows",
            reversible=False,
        ),
    )

    validation = validate_migration_plan(
        steps,
        allow_destructive=False,
        dry_run=True,
        now_provider=_fixed_now,
    )
    assert validation.valid is False
    assert any("destructive SQL detected" in error for error in validation.errors)

    first_bundle = capture_rollback_metadata(
        steps,
        dry_run=True,
        plan_hash=validation.plan_hash,
        actor="phase6-suite",
        run_id="dry-run-01",
        now_provider=_fixed_now,
    ).to_dict()
    second_bundle = capture_rollback_metadata(
        steps,
        dry_run=True,
        plan_hash=validation.plan_hash,
        actor="phase6-suite",
        run_id="dry-run-01",
        now_provider=_fixed_now,
    ).to_dict()

    assert first_bundle == second_bundle
    assert first_bundle["dry_run"] is True
    assert first_bundle["plan_hash"] == validation.plan_hash
    assert first_bundle["entry_count"] == 1
    assert first_bundle["entries"][0]["operation_id"] == "step-001"
    assert first_bundle["entries"][0]["metadata"]["captured_by"] == "phase6-suite"
    assert first_bundle["entries"][0]["metadata"]["run_id"] == "dry-run-01"


def test_secret_rotation_dual_window_and_cutover_validation_paths() -> None:
    dual_window_config = resolve_secret_rotation_config(
        active_key="active-v1",
        next_key="active-v2",
        deprecated_key="legacy-v0",
        cutover_state="dual_window",
    )
    dual_window = build_secret_validation_window(dual_window_config)

    assert [entry.version for entry in dual_window.accepted] == ["active", "next"]
    assert match_secret("active-v2", dual_window).matched is True
    assert match_secret("active-v2", dual_window).version == "next"

    post_cutover_config = resolve_secret_rotation_config(
        active_key="active-v2",
        next_key="candidate-v3",
        deprecated_key="active-v1",
        cutover_state="post_cutover",
    )
    post_cutover_window = build_secret_validation_window(post_cutover_config)
    next_match = match_secret("candidate-v3", post_cutover_window)
    deprecated_match = match_secret("active-v1", post_cutover_window)

    assert post_cutover_config.dual_window_enabled is False
    assert [entry.version for entry in post_cutover_window.accepted] == ["active"]
    assert next_match.matched is False
    assert next_match.version == "none"
    assert deprecated_match.deprecated_match is True
    assert deprecated_match.version == "deprecated"


def test_resilience_drill_scenario_execution_is_deterministic_with_injected_failures() -> None:
    first = _run_deterministic_resilience_scenario()
    second = _run_deterministic_resilience_scenario()

    assert first == second
    assert first["succeeded"] is True
    assert first["bounded_execution"] is True
    assert first["step_attempts"] == {"dispatch": 1, "enqueue": 2}
    assert first["errors"] == []
    assert first["artifacts"]["lease_expiry_advances"] == 1
    assert [record["point"] for record in first["injected_failures"]] == [
        INGRESS_ENQUEUE_FAILURE_POINT,
        WORKER_LEASE_EXPIRY_POINT,
    ]


def test_cost_control_budget_and_advisory_behaviors_across_signal_bands() -> None:
    now = _MutableNow(datetime(2026, 1, 1, tzinfo=UTC))
    controller = ProcessingBudgetController(
        settings=ProcessingBudgetSettings(
            max_items_per_worker_pass=3,
            max_items_per_interval=5,
            interval_seconds=30,
        ),
        now_provider=now,
    )

    first = controller.evaluate(requested_items=9)
    controller.record_processed(first.allowed_items)
    second = controller.evaluate(requested_items=4)
    controller.record_processed(second.allowed_items)
    exhausted = controller.evaluate(requested_items=1)
    now.advance(31)
    reset = controller.evaluate(requested_items=2)

    assert first.allowed_items == 3
    assert first.reason == "pass_cap_applied"
    assert second.allowed_items == 2
    assert second.reason == "pass_and_interval_cap"
    assert exhausted.allowed_items == 0
    assert exhausted.reason == "interval_budget_exhausted"
    assert reset.allowed_items == 2
    assert reset.reason == "within_budget"

    low_signals = QueueCostSignals(
        queue_depth=1,
        retry_ratio=0.02,
        dead_depth=0,
        oldest_lag_seconds=2.0,
        sample_size=20,
        provider_available=True,
        source="phase6-test",
    )
    medium_signals = QueueCostSignals(
        queue_depth=15,
        retry_ratio=0.40,
        dead_depth=1,
        oldest_lag_seconds=120.0,
        sample_size=20,
        provider_available=True,
        source="phase6-test",
    )
    critical_signals = QueueCostSignals(
        queue_depth=30,
        retry_ratio=0.80,
        dead_depth=8,
        oldest_lag_seconds=420.0,
        sample_size=20,
        provider_available=True,
        source="phase6-test",
    )
    unavailable_signals = QueueCostSignals(
        queue_depth=0,
        retry_ratio=0.0,
        dead_depth=0,
        oldest_lag_seconds=0.0,
        sample_size=20,
        provider_available=False,
        source="phase6-test",
    )

    low = build_cost_control_advisories(signals=low_signals)
    medium = build_cost_control_advisories(signals=medium_signals)
    critical = build_cost_control_advisories(signals=critical_signals)
    unavailable = build_cost_control_advisories(signals=unavailable_signals)

    assert low.throttle.advised_mode == "allow"
    assert medium.throttle.advised_mode == "throttle"
    assert critical.throttle.advised_mode == "reject"
    assert critical.compaction.strategy == "archive_dead_letter"
    assert unavailable.throttle.advised_mode == "sync_fallback"
    assert unavailable.compaction.reason == "signals_unavailable"
