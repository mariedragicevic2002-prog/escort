from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.guardrails import SLOGuardrailAction
from app.incidents.contracts import (
    GuardrailIncidentEvent,
    IncidentActionRecord,
    IncidentExecutionResult,
    QueueControlPort,
    RecoveryActionPort,
)
from app.ops.operator_recovery_service import (
    INBOUND_QUEUE_NAME,
    OUTBOUND_QUEUE_NAME,
    OperatorDLQReplayInvoker,
    QueuePauseCommand,
    QueuePauseService,
    QUEUE_PAUSE_PERMISSION,
    DLQReplayInvocation,
)
from app.workers.dlq_replay import DLQ_REPLAY_PERMISSION


def _safe_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _safe_text(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


@dataclass(frozen=True)
class BoundedActionPolicy:
    degrade_pause_seconds: int = 120
    rollback_pause_seconds: int = 300
    max_replay_batch: int = 25
    execute_replay_on_rollback: bool = False


class OperatorQueueControlExecutor:
    def __init__(
        self,
        *,
        pause_service: QueuePauseService,
        actor: str = "guardrail-automation",
        granted_permissions: Sequence[str] = (QUEUE_PAUSE_PERMISSION,),
    ) -> None:
        self._pause_service = pause_service
        self._actor = _safe_text(actor, default="guardrail-automation")
        self._permissions = tuple(str(item) for item in granted_permissions if str(item).strip()) or (
            QUEUE_PAUSE_PERMISSION,
        )

    def apply_degrade(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        queue_name = _safe_text(metadata.get("queue_name"), default=INBOUND_QUEUE_NAME)
        state = self._pause_service.pause_queue(
            QueuePauseCommand(
                actor=self._actor,
                reason=f"guardrail_degrade:{_safe_text(feature, default='unknown')}",
                queue_name=queue_name,
                duration_seconds=duration_seconds,
                granted_permissions=self._permissions,
                requested_at=_safe_text(metadata.get("requested_at")),
            )
        )
        return {
            "queue_name": state.queue_name,
            "paused": state.paused,
            "expires_at": state.expires_at,
            "reason": _safe_text(reason, default="guardrail_degrade"),
        }

    def apply_pause(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        requested = metadata.get("queue_names")
        queue_names: tuple[str, ...]
        if isinstance(requested, Sequence) and not isinstance(requested, (str, bytes)):
            normalized = [_safe_text(name) for name in requested]
            queue_names = tuple(
                name
                for name in normalized
                if name in {INBOUND_QUEUE_NAME, OUTBOUND_QUEUE_NAME}
            )[:2]
        else:
            queue_names = (INBOUND_QUEUE_NAME, OUTBOUND_QUEUE_NAME)
        if not queue_names:
            queue_names = (INBOUND_QUEUE_NAME, OUTBOUND_QUEUE_NAME)
        paused: list[dict[str, Any]] = []
        for queue_name in queue_names:
            state = self._pause_service.pause_queue(
                QueuePauseCommand(
                    actor=self._actor,
                    reason=f"guardrail_rollback:{_safe_text(feature, default='unknown')}",
                    queue_name=queue_name,
                    duration_seconds=duration_seconds,
                    granted_permissions=self._permissions,
                    requested_at=_safe_text(metadata.get("requested_at")),
                )
            )
            paused.append(
                {
                    "queue_name": state.queue_name,
                    "paused": state.paused,
                    "expires_at": state.expires_at,
                }
            )
        return {
            "paused_queues": tuple(paused),
            "reason": _safe_text(reason, default="guardrail_rollback"),
        }


class OperatorRecoveryActionExecutor:
    def __init__(
        self,
        *,
        replay_invoker: OperatorDLQReplayInvoker,
        actor: str = "guardrail-automation",
        granted_permissions: Sequence[str] = (DLQ_REPLAY_PERMISSION,),
        max_batch_limit: int = 25,
    ) -> None:
        self._replay_invoker = replay_invoker
        self._actor = _safe_text(actor, default="guardrail-automation")
        self._permissions = tuple(str(item) for item in granted_permissions if str(item).strip()) or (
            DLQ_REPLAY_PERMISSION,
        )
        self._max_batch_limit = _safe_positive_int(max_batch_limit, default=25, maximum=500)

    def suggest_recovery(
        self,
        *,
        feature: str,
        reason: str,
        batch_limit: int,
        dry_run: bool,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        bounded_limit = min(self._max_batch_limit, _safe_positive_int(batch_limit, default=25, maximum=500))
        result = self._replay_invoker.invoke(
            DLQReplayInvocation(
                actor=self._actor,
                reason=f"guardrail_{_safe_text(feature, default='unknown')}_{_safe_text(reason, default='incident')}",
                granted_permissions=self._permissions,
                direction=_safe_text(metadata.get("replay_direction"), default="all"),
                mode="batch",
                batch_limit=bounded_limit,
                dry_run=bool(dry_run),
                requested_at=_safe_text(metadata.get("requested_at")),
            )
        )
        return {
            "replay_run_id": result.replay_run_id,
            "dry_run": result.dry_run,
            "requested": result.requested,
            "replayed": result.replayed,
            "batch_limit": bounded_limit,
        }


class BoundedActionExecutor:
    def __init__(
        self,
        *,
        queue_controls: QueueControlPort | None = None,
        recovery_actions: RecoveryActionPort | None = None,
        policy: BoundedActionPolicy | None = None,
    ) -> None:
        self._queue_controls = queue_controls
        self._recovery_actions = recovery_actions
        self._policy = policy or BoundedActionPolicy()

    def execute(self, event: GuardrailIncidentEvent) -> IncidentExecutionResult:
        policy = self._policy
        records: list[IncidentActionRecord] = []
        errors: list[str] = []
        metadata = dict(event.metadata)
        if event.action == SLOGuardrailAction.OBSERVE:
            return IncidentExecutionResult(executed=False, records=(), errors=(), bounded=True)

        if event.action == SLOGuardrailAction.DEGRADE and self._queue_controls is not None:
            try:
                details = self._queue_controls.apply_degrade(
                    feature=event.feature,
                    reason=event.reason,
                    duration_seconds=_safe_positive_int(
                        metadata.get("degrade_pause_seconds"),
                        default=policy.degrade_pause_seconds,
                        maximum=3600,
                    ),
                    metadata=metadata,
                )
                records.append(IncidentActionRecord(name="queue_degrade", status="applied", details=dict(details)))
            except Exception as exc:
                errors.append(f"queue_degrade_failed:{exc}")

        if event.action == SLOGuardrailAction.ROLLBACK:
            if self._queue_controls is not None:
                try:
                    details = self._queue_controls.apply_pause(
                        feature=event.feature,
                        reason=event.reason,
                        duration_seconds=_safe_positive_int(
                            metadata.get("rollback_pause_seconds"),
                            default=policy.rollback_pause_seconds,
                            maximum=86400,
                        ),
                        metadata=metadata,
                    )
                    records.append(IncidentActionRecord(name="queue_pause", status="applied", details=dict(details)))
                except Exception as exc:
                    errors.append(f"queue_pause_failed:{exc}")
            if self._recovery_actions is not None:
                try:
                    batch_limit = min(
                        _safe_positive_int(
                            metadata.get("replay_batch_limit"),
                            default=policy.max_replay_batch,
                            maximum=500,
                        ),
                        _safe_positive_int(policy.max_replay_batch, default=25, maximum=500),
                    )
                    details = self._recovery_actions.suggest_recovery(
                        feature=event.feature,
                        reason=event.reason,
                        batch_limit=batch_limit,
                        dry_run=not bool(policy.execute_replay_on_rollback),
                        metadata=metadata,
                    )
                    records.append(
                        IncidentActionRecord(
                            name="recovery_replay",
                            status="suggested" if not policy.execute_replay_on_rollback else "applied",
                            details=dict(details),
                        )
                    )
                except Exception as exc:
                    errors.append(f"recovery_replay_failed:{exc}")

        return IncidentExecutionResult(
            executed=bool(records),
            records=tuple(records),
            errors=tuple(errors),
            bounded=True,
        )
