from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Mapping

from app.cost_controls import QueueCostSignals, build_cost_control_advisories
from app.ingress.adaptive_rate_limiter import (
    emit_adaptive_rate_limit_metric,
    load_adaptive_rate_limiter_settings,
    resolve_adaptive_rate_limit_decision,
    sample_adaptive_ingress_signals,
)
from app.ingress.backpressure_policy import (
    emit_ingress_backpressure_metric,
    load_ingress_backpressure_settings,
    resolve_ingress_backpressure_decision,
)
from app.ingress.rollout_controls import (
    SettingsGetter,
    emit_phase4_rollout_guardrail_metrics,
    resolve_backpressure_rollout_decision,
    load_sms_quick_ack_settings,
    load_webhook_ingress_quick_ack_settings,
)
from app.queue import (
    InboundQueueEnvelope,
    QueueMessageMetadata,
)
from app.queue.providers import InboundQueueProvider, QueueProvider, resolve_inbound_queue_provider
from app.resilience.injection import ResilienceDrillFailure, ResilienceDrillHook
from services.httpsms_dedup import build_inbound_dedup_key

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuickAckEnqueueOutcome:
    accepted: bool
    duplicate: bool
    reason: str


def _coerce_map(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): inner for key, inner in value.items()}
    return {}


def _fallback_dedup_key(*, prefix: str, phone_number: str, message_body: str, request_id: str) -> str:
    digest = hashlib.sha256(
        f"{prefix}|{phone_number}|{message_body}|{request_id}".encode("utf-8")
    ).hexdigest()[:32]
    return f"{prefix}-fallback:{digest}"


def _message_id(*, prefix: str, dedup_key: str, request_id: str) -> str:
    seed = dedup_key or request_id
    digest = hashlib.sha1(f"{prefix}|{seed}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:24]}"


def _invoke_drill_hook(drill_hook: ResilienceDrillHook | None, method_name: str, **kwargs: Any) -> None:
    if drill_hook is None:
        return
    hook = getattr(drill_hook, method_name, None)
    if callable(hook):
        hook(**kwargs)


def _enqueue(
    *,
    db_service: Any,
    dedup_key: str,
    request_id: str,
    payload: Mapping[str, Any],
    attributes: Mapping[str, Any],
    max_attempts: int,
    channel_prefix: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    inbound_provider: InboundQueueProvider | None = None,
    queue_provider: QueueProvider | None = None,
    drill_hook: ResilienceDrillHook | None = None,
) -> QuickAckEnqueueOutcome:
    provider = resolve_inbound_queue_provider(
        inbound_provider=inbound_provider,
        queue_provider=queue_provider,
        db_service=db_service,
    )
    if provider is None:
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="queue_unavailable")

    backpressure_settings = load_ingress_backpressure_settings(
        channel=channel_prefix,
        env=env,
        setting_getter=setting_getter,
    )
    backpressure_rollout = resolve_backpressure_rollout_decision(
        channel=channel_prefix,
        context_key=request_id,
        env=env,
        setting_getter=setting_getter,
    )
    admission = resolve_ingress_backpressure_decision(
        settings=backpressure_settings,
        inbound_provider=provider,
        requested_max_attempts=max_attempts,
        rollout_decision=backpressure_rollout,
    )
    emit_phase4_rollout_guardrail_metrics(
        decision=backpressure_rollout,
        request_id=request_id,
    )
    emit_ingress_backpressure_metric(
        decision=admission,
        channel=channel_prefix,
        request_id=request_id,
    )
    limiter_settings = load_adaptive_rate_limiter_settings(
        channel=channel_prefix,
        base_queue_depth=backpressure_settings.max_queue_depth,
        base_lag_seconds=backpressure_settings.max_lag_seconds,
        env=env,
        setting_getter=setting_getter,
    )
    adaptive_signals = sample_adaptive_ingress_signals(
        inbound_provider=provider,
        backpressure_decision=admission,
        sample_size=limiter_settings.reliability_sample_size,
    )
    adaptive_decision = resolve_adaptive_rate_limit_decision(
        settings=limiter_settings,
        signals=adaptive_signals,
        backpressure_decision=admission,
        requested_max_attempts=max_attempts,
    )
    advisories = build_cost_control_advisories(
        signals=QueueCostSignals(
            queue_depth=int(adaptive_signals.queue_depth),
            retry_ratio=float(adaptive_signals.retry_rate),
            dead_depth=int(adaptive_signals.dead_sample_size),
            oldest_lag_seconds=float(adaptive_signals.oldest_lag_seconds),
            sample_size=max(
                1,
                int(adaptive_signals.pending_sample_size) + int(adaptive_signals.dead_sample_size),
            ),
            provider_available=bool(adaptive_signals.provider_available),
            source=str(adaptive_signals.source or "unknown"),
        )
    )
    emit_adaptive_rate_limit_metric(
        decision=adaptive_decision,
        channel=channel_prefix,
        request_id=request_id,
    )

    if admission.overloaded or not admission.provider_available:
        logger.warning(
            "%s quick-ack backpressure decision reason=%s behavior=%s trigger=%s depth=%s lag=%s",
            channel_prefix,
            admission.reason,
            admission.behavior,
            admission.trigger,
            admission.queue_depth if admission.queue_depth is not None else "unknown",
            (
                f"{admission.oldest_lag_seconds:.3f}"
                if admission.oldest_lag_seconds is not None
                else "unknown"
            ),
        )

    if not adaptive_decision.allow_enqueue:
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason=adaptive_decision.reason)

    message_id = _message_id(prefix=channel_prefix, dedup_key=dedup_key, request_id=request_id)
    metadata_attributes = _coerce_map(attributes)
    metadata_attributes.update(
        {
            "backpressure_reason": admission.reason,
            "backpressure_behavior": admission.behavior,
            "backpressure_trigger": admission.trigger,
            "backpressure_overloaded": admission.overloaded,
            "backpressure_provider_available": admission.provider_available,
            "adaptive_mode": adaptive_decision.mode,
            "adaptive_reason": adaptive_decision.reason,
            "adaptive_retry_rate": round(adaptive_decision.retry_rate, 4),
            "adaptive_failure_rate": round(adaptive_decision.failure_rate, 4),
            "adaptive_provider_available": adaptive_decision.provider_available,
            "adaptive_adjusted_queue_depth_threshold": adaptive_decision.adjusted_queue_depth_threshold,
            "adaptive_adjusted_lag_seconds_threshold": round(
                adaptive_decision.adjusted_lag_seconds_threshold,
                3,
            ),
            "cost_throttle_advisory_mode": advisories.throttle.advised_mode,
            "cost_throttle_advisory_reason": advisories.throttle.reason,
            "cost_throttle_pressure_score": round(advisories.throttle.pressure_score, 4),
            "cost_compaction_strategy": advisories.compaction.strategy,
            "cost_compaction_reason": advisories.compaction.reason,
            "cost_compaction_required": advisories.compaction.should_compact,
            "cost_compaction_severity": advisories.compaction.severity,
        }
    )
    if admission.queue_depth is not None:
        metadata_attributes["backpressure_queue_depth"] = int(admission.queue_depth)
    if admission.oldest_lag_seconds is not None:
        metadata_attributes["backpressure_oldest_lag_seconds"] = round(
            max(0.0, float(admission.oldest_lag_seconds)),
            3,
        )

    envelope = InboundQueueEnvelope(
        message_id=message_id,
        payload=_coerce_map(payload),
        metadata=QueueMessageMetadata(
            dedup_key=dedup_key,
            request_id=request_id,
            attributes=metadata_attributes,
        ),
        max_attempts=adaptive_decision.effective_max_attempts,
    )
    try:
        _invoke_drill_hook(
            drill_hook,
            "before_ingress_enqueue",
            channel=channel_prefix,
            request_id=request_id,
            dedup_key=dedup_key,
        )
        inserted = provider.enqueue(envelope)
    except ResilienceDrillFailure as exc:
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason=f"drill_{exc.point}")
    except Exception as exc:
        logger.warning("%s quick-ack enqueue failed (%s)", channel_prefix, type(exc).__name__)
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="enqueue_failed")
    if inserted:
        if adaptive_decision.mode == "throttle" and not (
            admission.overloaded and admission.behavior == "degrade_mode"
        ):
            return QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued_throttled")
        if admission.overloaded and admission.behavior == "degrade_mode":
            return QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued_degraded")
        return QuickAckEnqueueOutcome(accepted=True, duplicate=False, reason="enqueued")
    return QuickAckEnqueueOutcome(accepted=True, duplicate=True, reason="duplicate")


def try_enqueue_sms_quick_ack(
    *,
    db_service: Any,
    phone_number: str,
    message_body: str,
    message_data: Mapping[str, Any] | None,
    request_payload: Mapping[str, Any] | None,
    request_headers: Mapping[str, Any] | None,
    remote_addr: str | None,
    request_id: str,
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    inbound_provider: InboundQueueProvider | None = None,
    queue_provider: QueueProvider | None = None,
    drill_hook: ResilienceDrillHook | None = None,
) -> QuickAckEnqueueOutcome:
    settings = load_sms_quick_ack_settings(env=env, setting_getter=setting_getter)
    if not settings.enabled:
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="disabled")

    message_map = _coerce_map(message_data)
    request_map = _coerce_map(request_payload)
    dedup_key = build_inbound_dedup_key(
        message_map,
        request_map,
        phone_number=phone_number,
        message_body=message_body,
    )
    if not dedup_key:
        dedup_key = _fallback_dedup_key(
            prefix="sms",
            phone_number=phone_number,
            message_body=message_body,
            request_id=request_id,
        )

    return _enqueue(
        db_service=db_service,
        dedup_key=dedup_key,
        request_id=request_id,
        payload={
            "channel": "sms",
            "phone_number": phone_number,
            "message_body": message_body,
            "message_data": message_map,
            "request_payload": request_map,
            "request_headers": _coerce_map(request_headers),
            "remote_addr": str(remote_addr or ""),
        },
        attributes={
            "channel": "sms",
            "ingress_path": "/sms/incoming",
            "phone_number": phone_number,
        },
        max_attempts=settings.max_attempts,
        channel_prefix="sms",
        env=env,
        setting_getter=setting_getter,
        inbound_provider=inbound_provider,
        queue_provider=queue_provider,
        drill_hook=drill_hook,
    )


def try_enqueue_webhook_quick_ack(
    *,
    db_service: Any,
    phone_number: str,
    message_body: str,
    payload: Mapping[str, Any] | None,
    message_data: Mapping[str, Any] | None,
    request_headers: Mapping[str, Any] | None,
    remote_addr: str | None,
    request_id: str,
    dedup_key: str,
    dedup_key_missing: bool,
    auth_reason: str,
    signature_verified: bool,
    auth_key_version: str = "unknown",
    auth_cutover_state: str = "unknown",
    signature_key_version: str = "unknown",
    signature_cutover_state: str = "unknown",
    env: Mapping[str, str] | None = None,
    setting_getter: SettingsGetter | None = None,
    inbound_provider: InboundQueueProvider | None = None,
    queue_provider: QueueProvider | None = None,
    drill_hook: ResilienceDrillHook | None = None,
) -> QuickAckEnqueueOutcome:
    settings = load_webhook_ingress_quick_ack_settings(env=env, setting_getter=setting_getter)
    if not settings.enabled:
        return QuickAckEnqueueOutcome(accepted=False, duplicate=False, reason="disabled")

    payload_map = _coerce_map(payload)
    message_map = _coerce_map(message_data)
    effective_dedup_key = str(dedup_key or "").strip()
    if not effective_dedup_key:
        effective_dedup_key = _fallback_dedup_key(
            prefix="webhook",
            phone_number=phone_number,
            message_body=message_body,
            request_id=request_id,
        )

    return _enqueue(
        db_service=db_service,
        dedup_key=effective_dedup_key,
        request_id=request_id,
        payload={
            "channel": "webhook",
            "phone_number": phone_number,
            "message_body": message_body,
            "payload": payload_map,
            "message_data": message_map,
            "request_headers": _coerce_map(request_headers),
            "remote_addr": str(remote_addr or ""),
            "event_type": str(
                payload_map.get("event")
                or payload_map.get("event_type")
                or payload_map.get("type")
                or ""
            ),
        },
        attributes={
            "channel": "webhook",
            "ingress_path": "/webhook",
            "phone_number": phone_number,
            "auth_reason": str(auth_reason or ""),
            "signature_verified": bool(signature_verified),
            "auth_key_version": str(auth_key_version or "unknown"),
            "auth_cutover_state": str(auth_cutover_state or "unknown"),
            "signature_key_version": str(signature_key_version or "unknown"),
            "signature_cutover_state": str(signature_cutover_state or "unknown"),
            "dedup_key_missing": bool(dedup_key_missing),
        },
        max_attempts=settings.max_attempts,
        channel_prefix="webhook",
        env=env,
        setting_getter=setting_getter,
        inbound_provider=inbound_provider,
        queue_provider=queue_provider,
        drill_hook=drill_hook,
    )
