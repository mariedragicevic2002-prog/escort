from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import re
from typing import Any

from app.cost_controls import QueueCostSignals, build_cost_control_advisories
from app.guardrails import SLOGuardrailAction, SLOGuardrailDecision, SLOGuardrailEngine, SLOGuardrailState
from app.ingress.rollout_controls import (
    SettingsGetter,
    load_phase4_slo_guardrail_policy,
    load_phase4_slo_guardrail_signals,
    load_phase4_slo_guardrail_state,
    resolve_operator_recovery_rollout_decision,
    resolve_webhook_ingress_rollout_decision,
    resolve_worker_supervision_rollout_decision,
)
from app.ops.production_readiness_contracts import (
    GuardrailActionSnapshot,
    GuardrailHealthSnapshot,
    ProductionReadinessReport,
    QueueHealthSnapshot,
    RolloutExposureSnapshot,
    WorkerSupervisionHealth,
    WorkerSupervisionQueueHealth,
)
from app.queue.providers import InboundQueueProvider, OutboundQueueProvider
from app.queue.status import QueueStatus, canonical_status
from app.security.log_scrubbing import scrub_payload_for_logging
from app.workers.supervision.lease import WorkerLeaseStore

_STATUS_SEVERITY = {"healthy": 0, "degraded": 1, "unhealthy": 2}
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|authorization)\b(?:\s*[:=]\s*|\s+)([^\s,;]+)"
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now(now_provider: Callable[[], datetime]) -> str:
    return now_provider().astimezone(UTC).isoformat()


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _max_status(*statuses: str) -> str:
    result = "healthy"
    for status in statuses:
        normalized = str(status or "degraded").strip().lower()
        if _STATUS_SEVERITY.get(normalized, 1) > _STATUS_SEVERITY[result]:
            result = normalized if normalized in _STATUS_SEVERITY else "degraded"
    return result


def _hash_identifier(raw: Any) -> str:
    return hashlib.sha256(str(raw or "").encode("utf-8")).hexdigest()[:12]


class ProductionReadinessReportService:
    def __init__(
        self,
        *,
        inbound_provider: InboundQueueProvider | None = None,
        outbound_provider: OutboundQueueProvider | None = None,
        lease_store: WorkerLeaseStore | None = None,
        setting_getter: SettingsGetter | None = None,
        now_provider: Callable[[], datetime] | None = None,
        sample_window: int = 50,
        stale_claim_window: int = 20,
        guardrail_action_window: int = 5,
        recent_guardrail_actions: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        rollout_context_key: str = "production-readiness",
    ) -> None:
        self._inbound_provider = inbound_provider
        self._outbound_provider = outbound_provider
        self._lease_store = lease_store
        self._setting_getter = setting_getter
        self._now = now_provider or _utc_now
        self._sample_window = max(1, min(200, int(sample_window)))
        self._stale_claim_window = max(1, min(200, int(stale_claim_window)))
        self._guardrail_action_window = max(1, min(20, int(guardrail_action_window)))
        self._recent_guardrail_actions = dict(recent_guardrail_actions or {})
        self._rollout_context_key = str(rollout_context_key or "production-readiness")

    def build_report(self) -> ProductionReadinessReport:
        generated_at = _iso_now(self._now)
        worker_rollout = resolve_worker_supervision_rollout_decision(
            self._rollout_context_key,
            setting_getter=self._setting_getter,
        )
        operator_rollout = resolve_operator_recovery_rollout_decision(
            self._rollout_context_key,
            setting_getter=self._setting_getter,
        )
        webhook_rollout = resolve_webhook_ingress_rollout_decision(
            self._rollout_context_key,
            setting_getter=self._setting_getter,
        )

        rollouts = tuple(
            sorted(
                (
                    self._phase4_rollout_snapshot(worker_rollout),
                    self._phase4_rollout_snapshot(operator_rollout),
                    RolloutExposureSnapshot(
                        feature="webhook_ingress",
                        use_feature=bool(webhook_rollout.use_refactor_runtime),
                        reason=str(webhook_rollout.reason),
                        enabled=bool(webhook_rollout.enabled),
                        canary_percent=int(webhook_rollout.canary_percent),
                        canary_bucket=int(webhook_rollout.canary_bucket),
                        emergency_rollback=bool(webhook_rollout.emergency_rollback),
                        rollout_exposed=bool(webhook_rollout.use_refactor_runtime),
                        rollback_activated=bool(webhook_rollout.emergency_rollback),
                        safeguard_fallback=not bool(webhook_rollout.use_refactor_runtime),
                        degrade_activated=False,
                        guardrail_action="observe",
                        status=(
                            "unhealthy"
                            if webhook_rollout.emergency_rollback
                            else "healthy"
                        ),
                    ),
                ),
                key=lambda item: item.feature,
            )
        )

        queues = tuple(
            sorted(
                (
                    self._build_queue_snapshot(queue_name="refactor_inbound", provider=self._inbound_provider),
                    self._build_queue_snapshot(queue_name="refactor_outbox", provider=self._outbound_provider),
                ),
                key=lambda item: item.queue_name,
            )
        )
        guardrails = self._build_guardrail_snapshots(generated_at=generated_at)
        worker_supervision = self._build_worker_supervision_health(
            rollout=self._phase4_rollout_snapshot(worker_rollout)
        )

        overall_status = _max_status(
            *(item.status for item in queues),
            worker_supervision.status,
            *(item.status for item in guardrails),
            *(item.status for item in rollouts),
        )

        return ProductionReadinessReport(
            schema_version="production-readiness.v1",
            generated_at=generated_at,
            window_seconds=300,
            overall_status=overall_status,
            queues=queues,
            worker_supervision=worker_supervision,
            guardrails=guardrails,
            rollouts=rollouts,
        )

    def build_scrubbed_report(self) -> dict[str, Any]:
        raw = self.build_report().to_dict()
        return self._scrub_payload(raw)

    def _phase4_rollout_snapshot(self, decision: Any) -> RolloutExposureSnapshot:
        reason = str(getattr(decision, "reason", "unknown") or "unknown")
        use_feature = bool(getattr(decision, "use_feature", False))
        rollback_activated = bool(getattr(decision, "rollback_activated", False))
        degrade_activated = bool(getattr(decision, "degrade_activated", False))
        if rollback_activated:
            status = "unhealthy"
        elif degrade_activated:
            status = "degraded"
        elif use_feature:
            status = "healthy"
        else:
            status = "healthy" if reason in {"canary_excluded", "canary_disabled", "rollout_disabled"} else "degraded"
        return RolloutExposureSnapshot(
            feature=str(getattr(decision, "feature", "unknown") or "unknown"),
            use_feature=use_feature,
            reason=reason,
            enabled=bool(getattr(decision, "enabled", False)),
            canary_percent=max(0, min(100, int(getattr(decision, "canary_percent", 0)))),
            canary_bucket=max(0, min(99, int(getattr(decision, "canary_bucket", 0)))),
            emergency_rollback=bool(getattr(decision, "emergency_rollback", False)),
            rollout_exposed=bool(getattr(decision, "rollout_exposed", use_feature)),
            rollback_activated=rollback_activated,
            safeguard_fallback=bool(getattr(decision, "safeguard_fallback", not use_feature)),
            degrade_activated=degrade_activated,
            guardrail_action=str(getattr(decision, "guardrail_action", "observe") or "observe"),
            status=status,
        )

    def _extract_oldest_lag(self, records: Sequence[Any]) -> float:
        now = self._now()
        oldest: datetime | None = None
        for record in records:
            metadata = getattr(record, "metadata", None)
            metadata_value = None
            if isinstance(metadata, Mapping):
                metadata_value = metadata.get("enqueued_at")
            else:
                metadata_value = getattr(metadata, "enqueued_at", None)
            candidate = (
                _parse_iso(metadata_value)
                or _parse_iso(getattr(record, "created_at", None))
                or _parse_iso(getattr(record, "occurred_at", None))
                or _parse_iso(getattr(record, "updated_at", None))
            )
            if candidate is None:
                continue
            if oldest is None or candidate < oldest:
                oldest = candidate
        if oldest is None:
            return 0.0
        return max(0.0, (now - oldest).total_seconds())

    def _build_queue_snapshot(self, *, queue_name: str, provider: Any | None) -> QueueHealthSnapshot:
        if provider is None:
            advisories = self._build_cost_advisories(
                queue_depth=0,
                dead_depth=0,
                retry_ratio=0.0,
                oldest_lag_seconds=0.0,
                provider_available=False,
                source="not_configured",
            )
            return QueueHealthSnapshot(
                queue_name=queue_name,
                sampled_pending_depth=0,
                sampled_dead_depth=0,
                retry_ratio=0.0,
                oldest_lag_seconds=0.0,
                sample_window=self._sample_window,
                source="not_configured",
                status="degraded",
                cost_throttle_advisory=advisories["throttle"],
                queue_compaction_hint=advisories["compaction"],
            )
        try:
            pending_rows = list(provider.list_pending(limit=self._sample_window) or [])
            dead_rows = list(provider.list_dead(limit=self._sample_window) or [])
        except Exception:
            advisories = self._build_cost_advisories(
                queue_depth=0,
                dead_depth=0,
                retry_ratio=1.0,
                oldest_lag_seconds=0.0,
                provider_available=False,
                source=type(provider).__name__,
            )
            return QueueHealthSnapshot(
                queue_name=queue_name,
                sampled_pending_depth=0,
                sampled_dead_depth=0,
                retry_ratio=1.0,
                oldest_lag_seconds=0.0,
                sample_window=self._sample_window,
                source=type(provider).__name__,
                status="unhealthy",
                cost_throttle_advisory=advisories["throttle"],
                queue_compaction_hint=advisories["compaction"],
            )

        retry_count = 0
        for row in pending_rows:
            status = canonical_status(str(getattr(row, "status", "")).strip().lower())
            if status == QueueStatus.RETRY:
                retry_count += 1
        pending_count = len(pending_rows)
        dead_count = len(dead_rows)
        retry_ratio = float(retry_count / pending_count) if pending_count else 0.0
        lag_seconds = self._extract_oldest_lag(pending_rows)

        if lag_seconds >= 900 or dead_count >= max(3, self._sample_window // 5):
            status = "unhealthy"
        elif lag_seconds >= 180 or dead_count > 0 or retry_ratio >= 0.35:
            status = "degraded"
        else:
            status = "healthy"
        advisories = self._build_cost_advisories(
            queue_depth=pending_count,
            dead_depth=dead_count,
            retry_ratio=retry_ratio,
            oldest_lag_seconds=lag_seconds,
            provider_available=True,
            source=type(provider).__name__,
        )

        return QueueHealthSnapshot(
            queue_name=queue_name,
            sampled_pending_depth=pending_count,
            sampled_dead_depth=dead_count,
            retry_ratio=retry_ratio,
            oldest_lag_seconds=lag_seconds,
            sample_window=self._sample_window,
            source=type(provider).__name__,
            status=status,
            cost_throttle_advisory=advisories["throttle"],
            queue_compaction_hint=advisories["compaction"],
        )

    def _build_cost_advisories(
        self,
        *,
        queue_depth: int,
        dead_depth: int,
        retry_ratio: float,
        oldest_lag_seconds: float,
        provider_available: bool,
        source: str,
    ) -> dict[str, dict[str, Any]]:
        advisories = build_cost_control_advisories(
            signals=QueueCostSignals(
                queue_depth=max(0, int(queue_depth)),
                retry_ratio=max(0.0, min(1.0, float(retry_ratio))),
                dead_depth=max(0, int(dead_depth)),
                oldest_lag_seconds=max(0.0, float(oldest_lag_seconds)),
                sample_size=max(1, int(self._sample_window)),
                provider_available=bool(provider_available),
                source=str(source or "unknown"),
            )
        )
        return advisories.to_public_dict()

    def _build_worker_supervision_health(self, *, rollout: RolloutExposureSnapshot) -> WorkerSupervisionHealth:
        snapshots: list[WorkerSupervisionQueueHealth] = []
        stale_total = 0
        for queue_name in ("refactor_inbound", "refactor_outbox"):
            stale_claims = self._list_stale_claims(queue_name=queue_name)
            stale_total += len(stale_claims)
            snapshots.append(
                WorkerSupervisionQueueHealth(
                    queue_name=queue_name,
                    stale_claim_count=len(stale_claims),
                    sampled_claim_ids=tuple(_hash_identifier(getattr(item, "item_id", "")) for item in stale_claims),
                )
            )

        stale_threshold = max(3, self._stale_claim_window // 2)
        if rollout.rollback_activated or stale_total >= stale_threshold:
            status = "unhealthy"
        elif stale_total > 0 or rollout.degrade_activated or not rollout.use_feature:
            status = "degraded"
        else:
            status = "healthy"

        return WorkerSupervisionHealth(
            status=status,
            stale_claim_count=stale_total,
            queues=tuple(snapshots),
            rollout=rollout,
        )

    def _list_stale_claims(self, *, queue_name: str) -> list[Any]:
        if self._lease_store is None:
            return []
        try:
            claims = self._lease_store.list_stale_claims(queue_name=queue_name, limit=self._stale_claim_window)
        except Exception:
            return []
        return list(claims or [])

    def _build_guardrail_snapshots(self, *, generated_at: str) -> tuple[GuardrailHealthSnapshot, ...]:
        snapshots: list[GuardrailHealthSnapshot] = []
        for feature in ("operator_recovery", "worker_supervision"):
            policy = load_phase4_slo_guardrail_policy(feature=feature, setting_getter=self._setting_getter)
            signals = load_phase4_slo_guardrail_signals(feature=feature, setting_getter=self._setting_getter)
            state = load_phase4_slo_guardrail_state(feature=feature, setting_getter=self._setting_getter)
            decision = self._evaluate_guardrail_decision(policy=policy, signals=signals, state=state)
            decision_action = (decision.action.value if decision else (state.action.value if state else "observe"))
            if decision_action == SLOGuardrailAction.ROLLBACK.value:
                status = "unhealthy"
            elif decision_action == SLOGuardrailAction.DEGRADE.value:
                status = "degraded"
            else:
                status = "healthy"
            snapshots.append(
                GuardrailHealthSnapshot(
                    feature=feature,
                    status=status,
                    policy_enabled=bool(policy.enabled),
                    state_action=state.action.value if state else SLOGuardrailAction.OBSERVE.value,
                    decision_action=decision_action,
                    reason=(decision.reason if decision else ("signals_unavailable" if signals is None else "state_snapshot")),
                    sample_size=int(getattr(signals, "sample_size", 0) or 0),
                    window_seconds=max(1, int(getattr(signals, "window_seconds", 300) or 300)),
                    triggered_signals=tuple(getattr(decision, "triggered_signals", ()) or ()),
                    recent_actions=self._guardrail_recent_actions(
                        feature=feature,
                        generated_at=generated_at,
                        state=state,
                        decision=decision,
                    ),
                )
            )
        snapshots.sort(key=lambda item: item.feature)
        return tuple(snapshots)

    def _evaluate_guardrail_decision(
        self,
        *,
        policy: Any,
        signals: Any,
        state: SLOGuardrailState | None,
    ) -> SLOGuardrailDecision | None:
        if signals is None:
            return None
        try:
            decision, _ = SLOGuardrailEngine(policy=policy).evaluate(
                signals=signals,
                state=state,
                now=self._now(),
            )
        except Exception:
            return None
        return decision

    def _guardrail_recent_actions(
        self,
        *,
        feature: str,
        generated_at: str,
        state: SLOGuardrailState | None,
        decision: SLOGuardrailDecision | None,
    ) -> tuple[GuardrailActionSnapshot, ...]:
        configured = list(self._recent_guardrail_actions.get(feature) or [])[: self._guardrail_action_window]
        if configured:
            actions: list[GuardrailActionSnapshot] = []
            for item in configured:
                actions.append(
                    GuardrailActionSnapshot(
                        action=str(item.get("action") or "observe"),
                        reason=str(item.get("reason") or "unknown"),
                        occurred_at=str(item.get("occurred_at") or generated_at),
                        triggered_signals=tuple(str(v) for v in (item.get("triggered_signals") or ())),
                        details=dict(item.get("details") or {}),
                    )
                )
            return tuple(actions)

        fallback: list[GuardrailActionSnapshot] = []
        if state is not None and state.last_transition_at is not None:
            fallback.append(
                GuardrailActionSnapshot(
                    action=state.action.value,
                    reason="state_transition",
                    occurred_at=state.last_transition_at.astimezone(UTC).isoformat(),
                )
            )
        if decision is not None:
            fallback.append(
                GuardrailActionSnapshot(
                    action=decision.action.value,
                    reason=decision.reason,
                    occurred_at=generated_at,
                    triggered_signals=tuple(decision.triggered_signals),
                )
            )
        return tuple(fallback[: self._guardrail_action_window])

    def _scrub_payload(self, payload: Any) -> Any:
        if isinstance(payload, Mapping):
            raw = {str(key): self._scrub_payload(value) for key, value in payload.items()}
            return scrub_payload_for_logging(raw, allowlist=tuple(raw.keys()))
        if isinstance(payload, list):
            return [self._scrub_payload(item) for item in payload]
        if isinstance(payload, tuple):
            return [self._scrub_payload(item) for item in payload]
        if isinstance(payload, str):
            return self._scrub_text(payload)
        return payload

    def _scrub_text(self, value: str) -> str:
        text = str(value or "")
        scrubbed = _SENSITIVE_VALUE_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
        if len(scrubbed) > 200:
            scrubbed = f"{scrubbed[:200]}…"
        return scrubbed
