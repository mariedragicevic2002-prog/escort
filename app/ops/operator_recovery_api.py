from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
import hashlib
import logging
from typing import Any

from app.ingress.rollout_controls import (
    Phase4FeatureRolloutDecision,
    emit_phase4_rollout_guardrail_metrics,
    resolve_operator_recovery_rollout_decision,
)
from app.ops.operator_recovery_service import (
    DLQReplayInvocation,
    OperatorQueueArchivalInvoker,
    OperatorDLQReplayInvoker,
    QueueArchivalInvocation,
    QueuePauseCommand,
    QueuePauseService,
    QueueResumeCommand,
    StuckJobInspectionQuery,
    StuckJobInspectionService,
)
from app.retention.archival import QueueArchivalSafetyError
from app.security.rbac import PermissionDeniedError
from app.workers.dlq_replay import DLQReplaySafetyError

logger = logging.getLogger("adella_chatbot.refactor.operator_recovery_api")


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _coerce_str_seq(raw_values: Any) -> tuple[str, ...]:
    if not isinstance(raw_values, Sequence) or isinstance(raw_values, (str, bytes)):
        return ()
    output: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        item = _safe_text(raw)
        if not item or item in seen:
            continue
        seen.add(item)
        output.append(item)
    return tuple(output)


def _is_feature_enabled_default() -> bool:
    from app.ingress.rollout_controls import load_operator_recovery_settings  # noqa: PLC0415

    return bool(load_operator_recovery_settings().enabled)


def _rollout_decision_default(actor: str) -> Phase4FeatureRolloutDecision:
    return resolve_operator_recovery_rollout_decision(actor)


def _rollout_decision_always_enabled(_actor: str) -> Phase4FeatureRolloutDecision:
    return Phase4FeatureRolloutDecision(
        feature="operator_recovery",
        use_feature=True,
        reason="canary_full",
        enabled=True,
        canary_percent=100,
        canary_bucket=0,
        emergency_rollback=False,
        rollout_exposed=True,
        rollback_activated=False,
        safeguard_fallback=False,
    )


def _actor_hash(actor: str) -> str:
    return hashlib.sha256(str(actor or "").encode("utf-8")).hexdigest()[:12]


def _forbidden_payload(exc: PermissionDeniedError) -> dict[str, Any]:
    return {
        "error": "forbidden",
        "required_permission": exc.required_permission,
    }


class OperatorRecoveryAPI:
    def __init__(
        self,
        *,
        pause_service: QueuePauseService,
        inspection_service: StuckJobInspectionService,
        replay_invoker: OperatorDLQReplayInvoker,
        archival_invoker: OperatorQueueArchivalInvoker | None = None,
        feature_enabled_getter: Callable[[], bool] | None = None,
        rollout_decider: Callable[[str], Phase4FeatureRolloutDecision] | None = None,
        rollout_metric_logger: Callable[..., None] | None = None,
    ) -> None:
        self._pause_service = pause_service
        self._inspection_service = inspection_service
        self._replay_invoker = replay_invoker
        self._archival_invoker = archival_invoker
        self._feature_enabled_getter = feature_enabled_getter or _is_feature_enabled_default
        if rollout_decider is not None:
            resolved_rollout_decider = rollout_decider
        elif feature_enabled_getter is not None:
            resolved_rollout_decider = _rollout_decision_always_enabled
        else:
            resolved_rollout_decider = _rollout_decision_default
        self._rollout_decider = resolved_rollout_decider
        self._rollout_metric_logger = rollout_metric_logger

    def pause_queue(
        self,
        *,
        actor: str,
        granted_permissions: Iterable[str],
        payload: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self._is_rollout_enabled(actor):
            return {"error": "operator_recovery_disabled"}, 404
        data = dict(payload or {})
        try:
            state = self._pause_service.pause_queue(
                QueuePauseCommand(
                    actor=_safe_text(actor),
                    reason=_safe_text(data.get("reason")),
                    queue_name=_safe_text(data.get("queue_name")),
                    duration_seconds=data.get("duration_seconds"),
                    granted_permissions=granted_permissions,
                    requested_at=_safe_text(data.get("requested_at")) or "",
                )
            )
            return {
                "queue_name": state.queue_name,
                "paused": state.paused,
                "paused_by": state.paused_by,
                "reason": state.reason,
                "paused_at": state.paused_at,
                "expires_at": state.expires_at,
            }, 200
        except PermissionDeniedError as exc:
            return _forbidden_payload(exc), 403
        except ValueError as exc:
            return {"error": "invalid_request", "message": str(exc)}, 400

    def resume_queue(
        self,
        *,
        actor: str,
        granted_permissions: Iterable[str],
        payload: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self._is_rollout_enabled(actor):
            return {"error": "operator_recovery_disabled"}, 404
        data = dict(payload or {})
        try:
            state = self._pause_service.resume_queue(
                QueueResumeCommand(
                    actor=_safe_text(actor),
                    reason=_safe_text(data.get("reason")),
                    queue_name=_safe_text(data.get("queue_name")),
                    granted_permissions=granted_permissions,
                    requested_at=_safe_text(data.get("requested_at")) or "",
                )
            )
            return {
                "queue_name": state.queue_name,
                "paused": state.paused,
                "paused_by": state.paused_by,
                "resumed_by": state.resumed_by,
                "paused_at": state.paused_at,
                "resumed_at": state.resumed_at,
            }, 200
        except PermissionDeniedError as exc:
            return _forbidden_payload(exc), 403
        except ValueError as exc:
            return {"error": "invalid_request", "message": str(exc)}, 400

    def inspect_stuck_jobs(
        self,
        *,
        actor: str,
        granted_permissions: Iterable[str],
        payload: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self._is_rollout_enabled(actor):
            return {"error": "operator_recovery_disabled"}, 404
        data = dict(payload or {})
        try:
            result = self._inspection_service.inspect(
                StuckJobInspectionQuery(
                    actor=_safe_text(actor),
                    granted_permissions=granted_permissions,
                    direction=_safe_text(data.get("direction")) or "all",
                    statuses=_coerce_str_seq(data.get("statuses")),
                    event_type=_safe_text(data.get("event_type")) or None,
                    aggregate_id=_safe_text(data.get("aggregate_id")) or None,
                    message_id=_safe_text(data.get("message_id")) or None,
                    limit=int(data.get("limit") or 50),
                ),
                conn=conn,
            )
            return {
                "scanned": result.scanned,
                "returned": result.returned,
                "summary": dict(result.summary),
                "items": [
                    {
                        "direction": item.direction,
                        "queue_name": item.queue_name,
                        "item_id": item.item_id,
                        "status": item.status,
                        "attempt": item.attempt,
                        "max_attempts": item.max_attempts,
                        "created_at": item.created_at,
                        "updated_at": item.updated_at,
                        "event_type": item.event_type,
                        "aggregate_id": item.aggregate_id,
                        "lease_owner_id": item.lease_owner_id,
                        "lease_expires_at": item.lease_expires_at,
                        "last_error": item.last_error,
                    }
                    for item in result.items
                ],
            }, 200
        except PermissionDeniedError as exc:
            return _forbidden_payload(exc), 403
        except ValueError as exc:
            return {"error": "invalid_request", "message": str(exc)}, 400

    def invoke_dlq_replay(
        self,
        *,
        actor: str,
        granted_permissions: Iterable[str],
        payload: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self._is_rollout_enabled(actor):
            return {"error": "operator_recovery_disabled"}, 404
        data = dict(payload or {})
        try:
            result = self._replay_invoker.invoke(
                DLQReplayInvocation(
                    actor=_safe_text(actor),
                    reason=_safe_text(data.get("reason")),
                    granted_permissions=granted_permissions,
                    direction=_safe_text(data.get("direction")) or "all",
                    message_ids=_coerce_str_seq(data.get("message_ids")),
                    event_type=_safe_text(data.get("event_type")) or None,
                    aggregate_id=_safe_text(data.get("aggregate_id")) or None,
                    mode=_safe_text(data.get("mode")) or "batch",
                    batch_limit=int(data.get("batch_limit") or 25),
                    dry_run=bool(data.get("dry_run", True)),
                    replay_run_id=_safe_text(data.get("replay_run_id")) or None,
                    idempotency_key=_safe_text(data.get("idempotency_key")) or None,
                    requested_at=_safe_text(data.get("requested_at")) or "",
                ),
                conn=conn,
            )
            return {
                "replay_run_id": result.replay_run_id,
                "idempotency_key": result.idempotency_key,
                "dry_run": result.dry_run,
                "requested": result.requested,
                "replayed": result.replayed,
                "dry_run_candidates": result.dry_run_candidates,
                "skipped": result.skipped,
                "decisions": [
                    {
                        "direction": decision.direction,
                        "message_id": decision.message_id,
                        "status_before": decision.status_before,
                        "outcome": decision.outcome,
                        "reason": decision.reason,
                    }
                    for decision in result.decisions
                ],
            }, 200
        except PermissionDeniedError as exc:
            return _forbidden_payload(exc), 403
        except (ValueError, DLQReplaySafetyError) as exc:
            return {"error": "invalid_request", "message": str(exc)}, 400

    def invoke_queue_archival(
        self,
        *,
        actor: str,
        granted_permissions: Iterable[str],
        payload: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> tuple[dict[str, Any], int]:
        if not self._is_rollout_enabled(actor):
            return {"error": "operator_recovery_disabled"}, 404
        if self._archival_invoker is None:
            return {"error": "queue_archival_not_configured"}, 404
        data = dict(payload or {})
        batch_limit_value = data.get("batch_limit")
        batch_limit = None if batch_limit_value is None else int(str(batch_limit_value))
        try:
            result = self._archival_invoker.invoke(
                QueueArchivalInvocation(
                    actor=_safe_text(actor),
                    reason=_safe_text(data.get("reason")),
                    granted_permissions=granted_permissions,
                    batch_limit=batch_limit,
                    requested_at=_safe_text(data.get("requested_at")) or "",
                ),
                conn=conn,
            )
            return {
                "requested_limit": result.requested_limit,
                "bounded_limit": result.bounded_limit,
                "archived_total": result.archived_total,
                "decisions": [
                    {
                        "direction": decision.direction,
                        "status": decision.status,
                        "older_than": decision.older_than,
                        "requested_limit": decision.requested_limit,
                        "archived": decision.archived,
                        "reason": decision.reason,
                    }
                    for decision in result.decisions
                ],
                "exceptions": [
                    {
                        "direction": failure.direction,
                        "status": failure.status,
                        "message": failure.message,
                    }
                    for failure in result.exceptions
                ],
            }, 200
        except PermissionDeniedError as exc:
            return _forbidden_payload(exc), 403
        except (ValueError, QueueArchivalSafetyError) as exc:
            return {"error": "invalid_request", "message": str(exc)}, 400

    def _is_rollout_enabled(self, actor: str) -> bool:
        if not self._feature_enabled_getter():
            return False
        actor_text = _safe_text(actor)
        try:
            decision = self._rollout_decider(actor_text)
        except Exception:
            decision = Phase4FeatureRolloutDecision(
                feature="operator_recovery",
                use_feature=True,
                reason="rollout_resolution_failed_fail_open",
                enabled=True,
                canary_percent=100,
                canary_bucket=0,
                emergency_rollback=False,
                rollout_exposed=True,
                rollback_activated=False,
                safeguard_fallback=False,
            )
        if not isinstance(decision, Phase4FeatureRolloutDecision):
            decision = Phase4FeatureRolloutDecision(
                feature="operator_recovery",
                use_feature=bool(decision),
                reason="custom_rollout_decider",
                enabled=True,
                canary_percent=100,
                canary_bucket=0,
                emergency_rollback=False,
                rollout_exposed=bool(decision),
                rollback_activated=False,
                safeguard_fallback=not bool(decision),
            )

        try:
            emit_phase4_rollout_guardrail_metrics(
                decision=decision,
                request_id=f"actor:{_actor_hash(actor_text)}",
                metric_logger=self._rollout_metric_logger,
            )
        except Exception:
            pass

        if decision.use_feature:
            return True

        logger.info(
            "operator recovery rollout gate actor_hash=%s reason=%s rollback=%s exposed=%s",
            _actor_hash(actor_text),
            decision.reason,
            decision.rollback_activated,
            decision.rollout_exposed,
        )
        return False
