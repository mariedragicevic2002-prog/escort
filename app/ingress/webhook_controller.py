from __future__ import annotations

import logging
from typing import Any, Callable

from app.ingress.rollout_controls import (
    WebhookIngressRolloutDecision,
    resolve_webhook_ingress_rollout_decision,
)
from app.observability.operations_metrics import (
    OperationsMetricsRecorder,
    infer_ingress_processing_mode,
)

logger = logging.getLogger(__name__)

LegacyWebhookProcessor = Callable[[str], Any]
RefactorWebhookProcessor = Callable[[str], Any]
DecisionResolver = Callable[[str], WebhookIngressRolloutDecision]
MetricsRecorder = Callable[..., None]


def _extract_rollout_phone_number(request_obj: Any) -> str:
    try:
        from main_v2.webhook_helpers import extract_webhook_contact_phone  # noqa: PLC0415

        return str(extract_webhook_contact_phone(request_obj) or "").strip()
    except Exception:
        return ""


def process_webhook_ingress_with_rollout(
    *,
    request_id: str,
    request_obj: Any,
    legacy_processor: LegacyWebhookProcessor,
    refactor_processor: RefactorWebhookProcessor,
    decision_resolver: DecisionResolver = resolve_webhook_ingress_rollout_decision,
    metrics_recorder: MetricsRecorder | None = None,
    operations_metrics: OperationsMetricsRecorder | None = None,
) -> Any:
    phone_number = _extract_rollout_phone_number(request_obj)
    metrics = operations_metrics or OperationsMetricsRecorder()
    try:
        decision = decision_resolver(phone_number)
    except Exception as exc:
        logger.warning(
            "webhook_controller: rollout resolution failed; using legacy path (%s)",
            type(exc).__name__,
        )
        result = legacy_processor(request_id)
        metrics.record_ingress_path(
            decision=WebhookIngressRolloutDecision(
                use_refactor_runtime=False,
                reason="decision_resolution_failed",
                canary_percent=0,
                canary_bucket=0,
                enabled=False,
                emergency_rollback=False,
            ),
            runtime_path="legacy",
            processing_mode=infer_ingress_processing_mode(result),
        )
        return result

    if metrics_recorder is not None:
        try:
            metrics_recorder(
                decision=decision,
                phone_number=phone_number,
                request_id=request_id,
            )
        except Exception as exc:
            logger.warning(
                "webhook_controller: rollout metrics skipped (%s)",
                type(exc).__name__,
            )

    if not decision.use_refactor_runtime or decision.emergency_rollback:
        result = legacy_processor(request_id)
        metrics.record_ingress_path(
            decision=decision,
            runtime_path="legacy",
            processing_mode=infer_ingress_processing_mode(result),
        )
        return result

    try:
        result = refactor_processor(request_id)
        metrics.record_ingress_path(
            decision=decision,
            runtime_path="refactor",
            processing_mode=infer_ingress_processing_mode(result),
        )
        return result
    except Exception as exc:
        logger.exception(
            "webhook_controller: refactor ingress failed; falling back to legacy: %s",
            exc,
        )
        result = legacy_processor(request_id)
        metrics.record_ingress_path(
            decision=decision,
            runtime_path="legacy_fallback",
            processing_mode=infer_ingress_processing_mode(result),
        )
        return result
