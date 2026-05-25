from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Any, Mapping, Protocol

from app.guardrails import SLOGuardrailAction, SLOGuardrailDecision


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_text(value: Any, *, default: str = "") -> str:
    text = str(value or "").strip()
    return text or default


def build_incident_key(
    *,
    feature: str,
    action: SLOGuardrailAction,
    reason: str,
    triggered_signals: tuple[str, ...],
) -> str:
    payload = json.dumps(
        {
            "feature": _safe_text(feature, default="unknown"),
            "action": action.value,
            "reason": _safe_text(reason, default="unknown"),
            "signals": tuple(str(item) for item in triggered_signals),
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"guardrail:{digest}"


@dataclass(frozen=True)
class GuardrailIncidentEvent:
    incident_key: str
    feature: str
    action: SLOGuardrailAction
    reason: str
    triggered_signals: tuple[str, ...] = ()
    occurred_at: datetime = field(default_factory=_utc_now)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_guardrail_decision(
        cls,
        *,
        feature: str,
        decision: SLOGuardrailDecision,
        metadata: Mapping[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> "GuardrailIncidentEvent":
        normalized_feature = _safe_text(feature, default="unknown")
        normalized_reason = _safe_text(decision.reason, default="unknown")
        triggered = tuple(str(item) for item in decision.triggered_signals)
        event_metadata = {str(k): v for k, v in dict(metadata or {}).items()}
        return cls(
            incident_key=build_incident_key(
                feature=normalized_feature,
                action=decision.action,
                reason=normalized_reason,
                triggered_signals=triggered,
            ),
            feature=normalized_feature,
            action=decision.action,
            reason=normalized_reason,
            triggered_signals=triggered,
            occurred_at=(occurred_at or _utc_now()).astimezone(UTC),
            metadata=event_metadata,
        )


@dataclass(frozen=True)
class IncidentActionRecord:
    name: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IncidentExecutionResult:
    executed: bool
    records: tuple[IncidentActionRecord, ...] = ()
    errors: tuple[str, ...] = ()
    bounded: bool = True


@dataclass(frozen=True)
class IncidentHookResult:
    triggered: bool
    suppressed: bool = False
    suppression_reason: str | None = None
    event: GuardrailIncidentEvent | None = None
    execution: IncidentExecutionResult | None = None


class QueueControlPort(Protocol):
    def apply_degrade(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...

    def apply_pause(
        self,
        *,
        feature: str,
        reason: str,
        duration_seconds: int,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class RecoveryActionPort(Protocol):
    def suggest_recovery(
        self,
        *,
        feature: str,
        reason: str,
        batch_limit: int,
        dry_run: bool,
        metadata: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class IncidentHook(Protocol):
    def handle_guardrail_decision(
        self,
        *,
        feature: str,
        decision: SLOGuardrailDecision,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> IncidentHookResult:
        ...
