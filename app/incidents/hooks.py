from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from app.guardrails import SLOGuardrailAction, SLOGuardrailDecision
from app.incidents.contracts import (
    GuardrailIncidentEvent,
    IncidentExecutionResult,
    IncidentHookResult,
)
from app.incidents.executor import BoundedActionExecutor
from app.incidents.notifier import (
    AlertNotifier,
    NullAlertNotifier,
    build_guardrail_incident_alert,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_non_negative_int(value: Any, *, default: int, maximum: int = 86400) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, min(maximum, parsed))


def _safe_positive_int(value: Any, *, default: int, minimum: int = 1, maximum: int = 1000) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


@dataclass(frozen=True)
class IncidentAutomationSafetyPolicy:
    cooldown_seconds: int = 180
    debounce_seconds: int = 30
    max_actions_per_interval: int = 5
    interval_seconds: int = 300
    duplicate_ttl_seconds: int = 900


class GuardrailIncidentHook:
    def __init__(
        self,
        *,
        executor: BoundedActionExecutor,
        notifier: AlertNotifier | None = None,
        safety_policy: IncidentAutomationSafetyPolicy | None = None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        self._executor = executor
        self._notifier = notifier or NullAlertNotifier()
        self._safety = safety_policy or IncidentAutomationSafetyPolicy()
        self._now_provider = now_provider or _utc_now
        self._lock = Lock()
        self._cooldown_until: dict[str, datetime] = {}
        self._last_action_at: dict[str, datetime] = {}
        self._recent_actions: dict[str, deque[datetime]] = {}
        self._dedupe_until: dict[str, datetime] = {}

    def handle_guardrail_decision(
        self,
        *,
        feature: str,
        decision: SLOGuardrailDecision,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> IncidentHookResult:
        event_time = (now or self._now_provider()).astimezone(UTC)
        event = GuardrailIncidentEvent.from_guardrail_decision(
            feature=feature,
            decision=decision,
            metadata=metadata,
            occurred_at=event_time,
        )
        if event.action == SLOGuardrailAction.OBSERVE:
            return IncidentHookResult(
                triggered=False,
                suppressed=True,
                suppression_reason="observe_action",
                event=event,
            )

        suppression_reason = self._evaluate_safety(event=event, now=event_time)
        if suppression_reason is not None:
            return IncidentHookResult(
                triggered=False,
                suppressed=True,
                suppression_reason=suppression_reason,
                event=event,
            )

        execution = self._executor.execute(event)
        self._emit_alert(event=event, execution=execution)
        return IncidentHookResult(
            triggered=True,
            suppressed=False,
            event=event,
            execution=execution,
        )

    def _emit_alert(
        self,
        *,
        event: GuardrailIncidentEvent,
        execution: IncidentExecutionResult,
    ) -> None:
        try:
            self._notifier.notify(
                build_guardrail_incident_alert(
                    event=event,
                    execution=execution,
                    emitted_at=event.occurred_at,
                )
            )
        except Exception:
            return

    def _evaluate_safety(self, *, event: GuardrailIncidentEvent, now: datetime) -> str | None:
        with self._lock:
            self._prune(now=now)
            dedupe_until = self._dedupe_until.get(event.incident_key)
            if dedupe_until is not None and dedupe_until > now:
                return "duplicate_incident"

            feature = event.feature
            cooldown_until = self._cooldown_until.get(feature)
            if cooldown_until is not None and cooldown_until > now:
                return "cooldown_active"

            debounce_seconds = _safe_non_negative_int(self._safety.debounce_seconds, default=30)
            last_action = self._last_action_at.get(feature)
            if (
                debounce_seconds > 0
                and last_action is not None
                and (now - last_action).total_seconds() < debounce_seconds
            ):
                return "debounced"

            interval_seconds = _safe_positive_int(self._safety.interval_seconds, default=300, maximum=86400)
            action_limit = _safe_positive_int(self._safety.max_actions_per_interval, default=5, maximum=500)
            interval_cutoff = now - timedelta(seconds=interval_seconds)
            timeline = self._recent_actions.setdefault(feature, deque())
            while timeline and timeline[0] <= interval_cutoff:
                timeline.popleft()
            if len(timeline) >= action_limit:
                return "max_actions_interval"

            timeline.append(now)
            self._last_action_at[feature] = now
            cooldown_seconds = _safe_non_negative_int(self._safety.cooldown_seconds, default=180)
            if cooldown_seconds > 0:
                self._cooldown_until[feature] = now + timedelta(seconds=cooldown_seconds)
            duplicate_ttl = _safe_non_negative_int(self._safety.duplicate_ttl_seconds, default=900)
            if duplicate_ttl > 0:
                self._dedupe_until[event.incident_key] = now + timedelta(seconds=duplicate_ttl)
            return None

    def _prune(self, *, now: datetime) -> None:
        for feature, until in tuple(self._cooldown_until.items()):
            if until <= now:
                self._cooldown_until.pop(feature, None)
        for incident_key, until in tuple(self._dedupe_until.items()):
            if until <= now:
                self._dedupe_until.pop(incident_key, None)
