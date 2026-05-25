from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Protocol

from app.guardrails import SLOGuardrailAction
from app.incidents.contracts import GuardrailIncidentEvent, IncidentExecutionResult
from app.security.log_scrubbing import scrub_payload_for_logging


def _utc_now() -> datetime:
    return datetime.now(UTC)


def scrub_alert_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    return scrub_payload_for_logging(source, allowlist=tuple(source.keys()))


def _severity_for_action(action: SLOGuardrailAction) -> str:
    if action == SLOGuardrailAction.ROLLBACK:
        return "critical"
    if action == SLOGuardrailAction.DEGRADE:
        return "warning"
    return "info"


@dataclass(frozen=True)
class IncidentAlert:
    incident_key: str
    severity: str
    feature: str
    action: str
    emitted_at: str
    payload: dict[str, Any] = field(default_factory=dict)


class AlertNotifier(Protocol):
    def notify(self, alert: IncidentAlert) -> None:
        ...


class NullAlertNotifier:
    def notify(self, alert: IncidentAlert) -> None:
        _ = alert


class InMemoryAlertNotifier:
    def __init__(self) -> None:
        self.alerts: list[IncidentAlert] = []

    def notify(self, alert: IncidentAlert) -> None:
        self.alerts.append(alert)


def build_guardrail_incident_alert(
    *,
    event: GuardrailIncidentEvent,
    execution: IncidentExecutionResult,
    emitted_at: datetime | None = None,
) -> IncidentAlert:
    payload = scrub_alert_payload(
        {
            "feature": event.feature,
            "action": event.action.value,
            "reason": event.reason,
            "triggered_signals": tuple(event.triggered_signals),
            "metadata": dict(event.metadata),
            "records": [
                {
                    "name": record.name,
                    "status": record.status,
                    "details": dict(record.details),
                }
                for record in execution.records
            ],
            "errors": tuple(execution.errors),
            "bounded": bool(execution.bounded),
        }
    )
    timestamp = (emitted_at or _utc_now()).astimezone(UTC).isoformat()
    return IncidentAlert(
        incident_key=event.incident_key,
        severity=_severity_for_action(event.action),
        feature=event.feature,
        action=event.action.value,
        emitted_at=timestamp,
        payload=payload,
    )
