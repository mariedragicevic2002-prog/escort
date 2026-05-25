from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from collections.abc import Sequence

from app.events.outbox import OutboxEventRecord
from app.ingress.rollout_controls import WebhookIngressRolloutDecision


class MetricLogger(Protocol):
    def __call__(self, metric_name: str, **kwargs: Any) -> None: ...


def _default_metric_logger(metric_name: str, **kwargs: Any) -> None:
    from utils.structured_logging import log_quality_metric  # noqa: PLC0415

    log_quality_metric(metric_name, **kwargs)


def _safe_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if len(text) > 80:
        return text[:80]
    return text


def _parse_iso_datetime(value: str | None) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        normalised = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _event_age_seconds(event: OutboxEventRecord, *, now: datetime) -> float | None:
    baseline = _parse_iso_datetime(event.created_at) or _parse_iso_datetime(event.occurred_at)
    if baseline is None:
        return None
    return max(0.0, (now - baseline).total_seconds())


def infer_ingress_processing_mode(result: Any) -> str:
    status_code: int | None = None
    payload_status = ""

    if isinstance(result, tuple) and len(result) >= 2 and isinstance(result[1], int):
        status_code = int(result[1])
        candidate = result[0]
        if isinstance(candidate, dict):
            payload_status = str(candidate.get("status") or "").strip().lower()
        elif hasattr(candidate, "get_json"):
            try:
                payload = candidate.get_json(silent=True)  # type: ignore[call-arg]
            except Exception:
                payload = None
            if isinstance(payload, dict):
                payload_status = str(payload.get("status") or "").strip().lower()
    elif hasattr(result, "status_code"):
        try:
            status_code = int(getattr(result, "status_code"))
        except Exception:
            status_code = None

    if status_code == 202:
        return "quick_ack"
    if payload_status in {"accepted", "queued", "quick_ack"}:
        return "quick_ack"
    return "sync_path"


class OperationsMetricsRecorder:
    def __init__(self, *, metric_logger: MetricLogger | None = None) -> None:
        self._metric_logger = metric_logger or _default_metric_logger

    def _emit(self, metric_name: str, **tags: Any) -> None:
        try:
            self._metric_logger(metric_name, **tags)
        except Exception:
            return

    def record_queue_snapshot(
        self,
        *,
        queue_name: str,
        events: Sequence[OutboxEventRecord],
        batch_size: int,
    ) -> None:
        now = datetime.now(UTC)
        ages = [age for age in (_event_age_seconds(event, now=now) for event in events) if age is not None]
        oldest_lag_seconds = max(ages) if ages else 0.0
        self._emit(
            "refactor_queue_depth_lag",
            queue_name=_safe_text(queue_name),
            queue_depth=int(len(events)),
            batch_size=int(max(1, batch_size)),
            oldest_lag_seconds=round(oldest_lag_seconds, 3),
        )

    def record_processing_latency(
        self,
        *,
        queue_name: str,
        event: OutboxEventRecord,
        outcome: str,
        duration_ms: float,
    ) -> None:
        self._emit(
            "refactor_queue_processing_latency",
            queue_name=_safe_text(queue_name),
            event_type=_safe_text(event.event_type),
            aggregate_type=_safe_text(event.aggregate_type),
            outcome=_safe_text(outcome),
            duration_ms=round(max(0.0, float(duration_ms)), 3),
        )

    def record_retry(
        self,
        *,
        queue_name: str,
        event: OutboxEventRecord,
        retry_count: int,
    ) -> None:
        self._emit(
            "refactor_queue_retry_total",
            queue_name=_safe_text(queue_name),
            event_type=_safe_text(event.event_type),
            aggregate_type=_safe_text(event.aggregate_type),
            retry_count=max(0, int(retry_count)),
            max_retries=max(0, int(event.max_retries)),
        )

    def record_dead_letter(
        self,
        *,
        queue_name: str,
        event: OutboxEventRecord,
        retry_count: int,
    ) -> None:
        self._emit(
            "refactor_queue_dead_letter_total",
            queue_name=_safe_text(queue_name),
            event_type=_safe_text(event.event_type),
            aggregate_type=_safe_text(event.aggregate_type),
            retry_count=max(0, int(retry_count)),
            max_retries=max(0, int(event.max_retries)),
        )

    def record_ingress_path(
        self,
        *,
        decision: WebhookIngressRolloutDecision,
        runtime_path: str,
        processing_mode: str,
    ) -> None:
        runtime_path_text = _safe_text(runtime_path)
        rollback_activated = bool(decision.emergency_rollback)
        rollout_exposed = bool(decision.use_refactor_runtime and not rollback_activated)
        safeguard_fallback = bool(
            runtime_path_text in {"legacy_fallback", "legacy"}
            and (
                rollback_activated
                or _safe_text(decision.reason) in {"decision_resolution_failed", "emergency_rollback"}
            )
        )
        self._emit(
            "refactor_ingress_path_total",
            runtime_path=runtime_path_text,
            processing_mode=_safe_text(processing_mode),
            reason=_safe_text(decision.reason),
            canary_percent=max(0, min(100, int(decision.canary_percent))),
            enabled=bool(decision.enabled),
            emergency_rollback=bool(decision.emergency_rollback),
            rollout_exposed=rollout_exposed,
            rollback_activated=rollback_activated,
            safeguard_fallback=safeguard_fallback,
        )
