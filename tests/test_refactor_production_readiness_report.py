from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
from typing import Any

from refactor.app.ops.production_readiness_service import ProductionReadinessReportService


@dataclass(frozen=True)
class _Metadata:
    enqueued_at: str


@dataclass(frozen=True)
class _QueueRecord:
    message_id: str
    status: str
    metadata: _Metadata
    created_at: str


@dataclass(frozen=True)
class _DeadRecord:
    message_id: str
    status: str = "dead"
    created_at: str = "2026-01-01T00:00:00+00:00"
    metadata: _Metadata = _Metadata(enqueued_at="2026-01-01T00:00:00+00:00")


@dataclass(frozen=True)
class _StaleClaim:
    item_id: str


class _QueueProvider:
    def __init__(self, *, pending: list[Any] | None = None, dead: list[Any] | None = None) -> None:
        self._pending = list(pending or [])
        self._dead = list(dead or [])

    def list_pending(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        return list(self._pending)[: max(1, int(limit))]

    def list_dead(self, *, limit: int = 100, conn: Any | None = None):
        _ = conn
        return list(self._dead)[: max(1, int(limit))]


class _LeaseStore:
    def __init__(self, claims_by_queue: dict[str, list[_StaleClaim]] | None = None) -> None:
        self._claims_by_queue = claims_by_queue or {}

    def list_stale_claims(self, *, queue_name: str, limit: int = 100, conn: Any | None = None):
        _ = conn
        return list(self._claims_by_queue.get(queue_name, []))[: max(1, int(limit))]


def _fixed_now() -> datetime:
    return datetime(2026, 1, 1, 0, 10, tzinfo=UTC)


def _build_service(
    *,
    inbound: _QueueProvider | None = None,
    outbound: _QueueProvider | None = None,
    lease_store: _LeaseStore | None = None,
    settings: dict[str, Any] | None = None,
    recent_guardrail_actions: dict[str, list[dict[str, Any]]] | None = None,
) -> ProductionReadinessReportService:
    values = settings or {}

    def _get_setting(key: str, default: Any = None) -> Any:
        return values.get(key, default)

    return ProductionReadinessReportService(
        inbound_provider=inbound,
        outbound_provider=outbound,
        lease_store=lease_store,
        setting_getter=_get_setting,
        now_provider=_fixed_now,
        sample_window=10,
        stale_claim_window=4,
        recent_guardrail_actions=recent_guardrail_actions,
    )


def test_report_schema_and_deterministic_fields() -> None:
    inbound = _QueueProvider(
        pending=[
            _QueueRecord(
                message_id="in-1",
                status="retry",
                metadata=_Metadata(enqueued_at="2026-01-01T00:05:00+00:00"),
                created_at="2026-01-01T00:05:00+00:00",
            ),
            _QueueRecord(
                message_id="in-2",
                status="pending",
                metadata=_Metadata(enqueued_at="2026-01-01T00:08:00+00:00"),
                created_at="2026-01-01T00:08:00+00:00",
            ),
        ],
    )
    outbound = _QueueProvider(
        pending=[
            _QueueRecord(
                message_id="out-1",
                status="pending",
                metadata=_Metadata(enqueued_at="2026-01-01T00:09:00+00:00"),
                created_at="2026-01-01T00:09:00+00:00",
            )
        ],
    )
    service = _build_service(
        inbound=inbound,
        outbound=outbound,
        lease_store=_LeaseStore(),
        settings={
            "refactor_worker_supervision_enabled": True,
            "refactor_worker_supervision_canary_percent": 100,
            "refactor_operator_recovery_enabled": True,
            "refactor_operator_recovery_canary_percent": 100,
        },
    )

    report = service.build_scrubbed_report()

    assert report["schema_version"] == "production-readiness.v1"
    assert report["generated_at"] == "2026-01-01T00:10:00+00:00"
    assert report["overall_status"] == "degraded"
    assert [item["queue_name"] for item in report["queues"]] == ["refactor_inbound", "refactor_outbox"]
    inbound_queue = next(item for item in report["queues"] if item["queue_name"] == "refactor_inbound")
    assert inbound_queue["cost_throttle_advisory"]["advised_mode"] in {"allow", "throttle"}
    assert "queue_compaction_hint" in inbound_queue
    assert {item["feature"] for item in report["guardrails"]} == {"operator_recovery", "worker_supervision"}
    assert {item["feature"] for item in report["rollouts"]} == {
        "operator_recovery",
        "webhook_ingress",
        "worker_supervision",
    }


def test_report_derives_unhealthy_status_for_backlog_and_rollout_rollback() -> None:
    inbound = _QueueProvider(
        pending=[
            _QueueRecord(
                message_id="in-old",
                status="retry",
                metadata=_Metadata(enqueued_at="2025-12-31T22:00:00+00:00"),
                created_at="2025-12-31T22:00:00+00:00",
            )
        ],
        dead=[_DeadRecord(message_id=f"dead-{idx}") for idx in range(4)],
    )
    outbound = _QueueProvider()
    lease_store = _LeaseStore(
        claims_by_queue={
            "refactor_inbound": [_StaleClaim(item_id="claim-1"), _StaleClaim(item_id="claim-2")],
            "refactor_outbox": [_StaleClaim(item_id="claim-3"), _StaleClaim(item_id="claim-4")],
        }
    )
    service = _build_service(
        inbound=inbound,
        outbound=outbound,
        lease_store=lease_store,
        settings={
            "refactor_worker_supervision_enabled": True,
            "refactor_worker_supervision_emergency_rollback": True,
        },
    )

    report = service.build_scrubbed_report()
    inbound_snapshot = next(item for item in report["queues"] if item["queue_name"] == "refactor_inbound")

    assert inbound_snapshot["status"] == "unhealthy"
    assert report["worker_supervision"]["status"] == "unhealthy"
    assert report["overall_status"] == "unhealthy"


def test_report_redacts_sensitive_data_in_recent_guardrail_actions() -> None:
    service = _build_service(
        inbound=_QueueProvider(),
        outbound=_QueueProvider(),
        lease_store=_LeaseStore(),
        recent_guardrail_actions={
            "worker_supervision": [
                {
                    "action": "degrade",
                    "reason": "retry spike token=abc123",
                    "occurred_at": "2026-01-01T00:09:30+00:00",
                    "triggered_signals": ["retry_rate"],
                    "details": {
                        "api_key": "super-secret",
                        "message": "Authorization: Bearer xyz",
                    },
                }
            ]
        },
    )

    report = service.build_scrubbed_report()
    encoded = json.dumps(report).lower()

    assert "abc123" not in encoded
    assert "super-secret" not in encoded
    assert "bearer xyz" not in encoded
    assert "[redacted]" in encoded
