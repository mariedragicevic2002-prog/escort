from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.guardrails.contracts import (
    SLOGuardrailAction,
    SLOGuardrailDecision,
    SLOGuardrailPolicy,
    SLOGuardrailSignals,
    SLOGuardrailState,
)

_SEVERITY = {
    SLOGuardrailAction.OBSERVE: 0,
    SLOGuardrailAction.DEGRADE: 1,
    SLOGuardrailAction.ROLLBACK: 2,
}


class SLOGuardrailEngine:
    def __init__(
        self,
        *,
        policy: SLOGuardrailPolicy | None = None,
        drill_hook: Any | None = None,
    ) -> None:
        self._policy = policy or SLOGuardrailPolicy()
        self._drill_hook = drill_hook

    def evaluate(
        self,
        *,
        signals: SLOGuardrailSignals,
        state: SLOGuardrailState | None = None,
        now: datetime | None = None,
    ) -> tuple[SLOGuardrailDecision, SLOGuardrailState]:
        policy = self._policy
        current_state = state or SLOGuardrailState()
        current = current_state.action
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        telemetry = signals.normalized()
        in_cooldown = bool(current_state.cooldown_until and current_time < current_state.cooldown_until)

        if not policy.enabled:
            next_state = SLOGuardrailState(action=SLOGuardrailAction.OBSERVE)
            return SLOGuardrailDecision(
                action=SLOGuardrailAction.OBSERVE,
                reason="guardrail_disabled",
                triggered_signals=(),
                previous_action=current_state.action,
                transitioned=(current_state.action != SLOGuardrailAction.OBSERVE),
                in_cooldown=False,
                cooldown_until=None,
            ), next_state

        if telemetry.sample_size < max(1, int(policy.min_sample_size)):
            reason = "insufficient_sample_hold" if current != SLOGuardrailAction.OBSERVE else "insufficient_sample_observe"
            return self._hold(
                action=current,
                current_state=current_state,
                reason=reason,
                in_cooldown=in_cooldown,
                now=current_time,
                triggered_signals=(),
            )
        desired, triggered_signals = _desired_action(policy=policy, signals=telemetry)
        drill_override = self._resolve_drill_override()
        desired_reason = f"{desired.value}_threshold_breach"
        if drill_override is not None:
            desired = drill_override
            triggered_signals = tuple(sorted({*triggered_signals, "drill_override"}))
            desired_reason = "drill_guardrail_rollback_trigger"

        if _SEVERITY[desired] > _SEVERITY[current]:
            return self._transition(
                target=desired,
                current_state=current_state,
                now=current_time,
                triggered_signals=triggered_signals,
                reason=desired_reason,
            )
        if desired == current:
            return self._hold(
                action=current,
                current_state=current_state,
                reason="state_stable",
                in_cooldown=in_cooldown,
                now=current_time,
                triggered_signals=triggered_signals,
            )

        if in_cooldown:
            return self._hold(
                action=current,
                current_state=current_state,
                reason="cooldown_hold",
                in_cooldown=True,
                now=current_time,
                triggered_signals=triggered_signals,
            )

        bounded_target = desired
        if current == SLOGuardrailAction.ROLLBACK and desired == SLOGuardrailAction.OBSERVE:
            bounded_target = SLOGuardrailAction.DEGRADE

        if _recovery_cleared(
            policy=policy,
            current=current,
            target=bounded_target,
            signals=telemetry,
        ):
            return self._transition(
                target=bounded_target,
                current_state=current_state,
                now=current_time,
                triggered_signals=triggered_signals,
                reason="recovery_hysteresis_clear",
            )

        return self._hold(
            action=current,
            current_state=current_state,
            reason="hysteresis_hold",
            in_cooldown=False,
            now=current_time,
            triggered_signals=triggered_signals,
        )

    def _transition(
        self,
        *,
        target: SLOGuardrailAction,
        current_state: SLOGuardrailState,
        now: datetime,
        triggered_signals: tuple[str, ...],
        reason: str,
    ) -> tuple[SLOGuardrailDecision, SLOGuardrailState]:
        cooldown_until = now + timedelta(seconds=max(1, int(self._policy.cooldown_seconds)))
        next_state = SLOGuardrailState(
            action=target,
            last_transition_at=now,
            cooldown_until=cooldown_until,
        )
        decision = SLOGuardrailDecision(
            action=target,
            reason=reason,
            triggered_signals=triggered_signals,
            previous_action=current_state.action,
            transitioned=(target != current_state.action),
            in_cooldown=False,
            cooldown_until=cooldown_until,
        )
        return decision, next_state

    def _hold(
        self,
        *,
        action: SLOGuardrailAction,
        current_state: SLOGuardrailState,
        reason: str,
        in_cooldown: bool,
        now: datetime,
        triggered_signals: tuple[str, ...],
    ) -> tuple[SLOGuardrailDecision, SLOGuardrailState]:
        if current_state.cooldown_until and now >= current_state.cooldown_until:
            next_state = replace(current_state, cooldown_until=None)
        else:
            next_state = current_state
        decision = SLOGuardrailDecision(
            action=action,
            reason=reason,
            triggered_signals=triggered_signals,
            previous_action=current_state.action,
            transitioned=False,
            in_cooldown=in_cooldown,
            cooldown_until=next_state.cooldown_until,
        )
        return decision, next_state

    def _resolve_drill_override(self) -> SLOGuardrailAction | None:
        hook = getattr(self._drill_hook, "guardrail_action_override", None)
        if not callable(hook):
            return None
        try:
            override = hook(feature="slo_guardrail")
        except Exception:
            return None
        normalized = str(override or "").strip().lower()
        if normalized == SLOGuardrailAction.ROLLBACK.value:
            return SLOGuardrailAction.ROLLBACK
        if normalized == SLOGuardrailAction.DEGRADE.value:
            return SLOGuardrailAction.DEGRADE
        if normalized == SLOGuardrailAction.OBSERVE.value:
            return SLOGuardrailAction.OBSERVE
        return None


def _desired_action(
    *,
    policy: SLOGuardrailPolicy,
    signals: SLOGuardrailSignals,
) -> tuple[SLOGuardrailAction, tuple[str, ...]]:
    rollback_hits = _threshold_hits(
        signals=signals,
        retry_rate=policy.rollback_retry_rate,
        dead_letter_rate=policy.rollback_dead_letter_rate,
        queue_lag_seconds=policy.rollback_queue_lag_seconds,
        failure_ratio=policy.rollback_failure_ratio,
        error_budget_remaining=policy.rollback_error_budget_remaining,
    )
    if rollback_hits:
        return SLOGuardrailAction.ROLLBACK, rollback_hits

    degrade_hits = _threshold_hits(
        signals=signals,
        retry_rate=policy.degrade_retry_rate,
        dead_letter_rate=policy.degrade_dead_letter_rate,
        queue_lag_seconds=policy.degrade_queue_lag_seconds,
        failure_ratio=policy.degrade_failure_ratio,
        error_budget_remaining=policy.degrade_error_budget_remaining,
    )
    if degrade_hits:
        return SLOGuardrailAction.DEGRADE, degrade_hits
    return SLOGuardrailAction.OBSERVE, ()


def _threshold_hits(
    *,
    signals: SLOGuardrailSignals,
    retry_rate: float,
    dead_letter_rate: float,
    queue_lag_seconds: float,
    failure_ratio: float,
    error_budget_remaining: float,
) -> tuple[str, ...]:
    hits: list[str] = []
    if signals.retry_rate is not None and float(signals.retry_rate) >= float(retry_rate):
        hits.append("retry_rate")
    if signals.dead_letter_rate is not None and float(signals.dead_letter_rate) >= float(dead_letter_rate):
        hits.append("dead_letter_rate")
    if signals.queue_lag_seconds is not None and float(signals.queue_lag_seconds) >= float(queue_lag_seconds):
        hits.append("queue_lag_seconds")
    if signals.failure_ratio is not None and float(signals.failure_ratio) >= float(failure_ratio):
        hits.append("failure_ratio")
    if signals.error_budget_remaining is not None and float(signals.error_budget_remaining) <= float(error_budget_remaining):
        hits.append("error_budget_remaining")
    return tuple(hits)


def _recovery_cleared(
    *,
    policy: SLOGuardrailPolicy,
    current: SLOGuardrailAction,
    target: SLOGuardrailAction,
    signals: SLOGuardrailSignals,
) -> bool:
    factor = max(0.1, min(0.99, float(policy.hysteresis_factor)))
    if current == SLOGuardrailAction.DEGRADE and target == SLOGuardrailAction.OBSERVE:
        return _below_recovery_threshold(
            signals=signals,
            retry_rate=policy.degrade_retry_rate * factor,
            dead_letter_rate=policy.degrade_dead_letter_rate * factor,
            queue_lag_seconds=policy.degrade_queue_lag_seconds * factor,
            failure_ratio=policy.degrade_failure_ratio * factor,
            error_budget_remaining=min(1.0, policy.degrade_error_budget_remaining / factor),
        )
    if current == SLOGuardrailAction.ROLLBACK and target in {SLOGuardrailAction.DEGRADE, SLOGuardrailAction.OBSERVE}:
        return _below_recovery_threshold(
            signals=signals,
            retry_rate=policy.rollback_retry_rate * factor,
            dead_letter_rate=policy.rollback_dead_letter_rate * factor,
            queue_lag_seconds=policy.rollback_queue_lag_seconds * factor,
            failure_ratio=policy.rollback_failure_ratio * factor,
            error_budget_remaining=min(1.0, policy.rollback_error_budget_remaining / factor),
        )
    return True


def _below_recovery_threshold(
    *,
    signals: SLOGuardrailSignals,
    retry_rate: float,
    dead_letter_rate: float,
    queue_lag_seconds: float,
    failure_ratio: float,
    error_budget_remaining: float,
) -> bool:
    checked = False
    if signals.retry_rate is not None:
        checked = True
        if float(signals.retry_rate) > float(retry_rate):
            return False
    if signals.dead_letter_rate is not None:
        checked = True
        if float(signals.dead_letter_rate) > float(dead_letter_rate):
            return False
    if signals.queue_lag_seconds is not None:
        checked = True
        if float(signals.queue_lag_seconds) > float(queue_lag_seconds):
            return False
    if signals.failure_ratio is not None:
        checked = True
        if float(signals.failure_ratio) > float(failure_ratio):
            return False
    if signals.error_budget_remaining is not None:
        checked = True
        if float(signals.error_budget_remaining) < float(error_budget_remaining):
            return False
    return checked
