from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from adapters.sms_outbound_adapter import SMSOutboundAdapter
from app.events.outbox import DatabaseOutboxRepository, OutboxEventRecord
from app.observability.operations_metrics import OperationsMetricsRecorder
from app.outbound import (
    OUTBOUND_SMS_EVENT_TYPE,
    OutboundDispatcher,
    OutboundMessage,
)
from app.outbound.contracts import BeforeSendHook, OutboundChannelAdapter
from app.workers.dispatcher import OutboxEventDispatcher
from app.workers.idempotency import DatabaseIdempotentConsumerGuard
from app.workers.retry import ExponentialBackoffRetryPolicy
from app.workers.runtime import OutboxWorkerRuntime


def _event_to_outbound_message(event: OutboxEventRecord) -> OutboundMessage:
    payload = dict(event.payload or {})
    metadata_raw = payload.get("metadata")
    metadata = dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    channel = str(payload.get("channel") or metadata.get("channel") or "").strip().lower()
    recipient = str(payload.get("recipient") or event.aggregate_id or "").strip()
    body = str(payload.get("body") or "").strip()
    if not channel:
        raise ValueError(f"missing outbound channel for event_id={event.event_id}")
    if not recipient:
        raise ValueError(f"missing outbound recipient for event_id={event.event_id}")
    if not body:
        raise ValueError(f"missing outbound body for event_id={event.event_id}")
    return OutboundMessage(
        channel=channel,
        recipient=recipient,
        body=body,
        metadata=metadata,
    )


class OutboundQueueSenderHandler:
    """Outbox event handler that emits a single outbound side-effect message."""

    def __init__(self, *, outbound_dispatcher: OutboundDispatcher) -> None:
        self._outbound_dispatcher = outbound_dispatcher

    def __call__(self, event: OutboxEventRecord) -> None:
        message = _event_to_outbound_message(event)
        result = self._outbound_dispatcher.dispatch([message])
        if result.sent != 1 or result.failed > 0:
            raise RuntimeError(
                f"outbound send failed event_id={event.event_id} "
                f"sent={result.sent} failed={result.failed}"
            )


def register_outbound_sender_handler(
    *,
    dispatcher: OutboxEventDispatcher,
    outbound_dispatcher: OutboundDispatcher,
    event_type: str = OUTBOUND_SMS_EVENT_TYPE,
) -> OutboundQueueSenderHandler:
    handler = OutboundQueueSenderHandler(outbound_dispatcher=outbound_dispatcher)
    dispatcher.register(event_type, handler)
    return handler


def build_outbound_sender_worker_runtime(
    *,
    db_service: Any,
    adapters: Iterable[OutboundChannelAdapter],
    before_send: BeforeSendHook | None = None,
    retry_policy: ExponentialBackoffRetryPolicy | None = None,
    operations_metrics: OperationsMetricsRecorder | None = None,
) -> OutboxWorkerRuntime:
    outbox_repository = DatabaseOutboxRepository(db_service)
    idempotency_guard = DatabaseIdempotentConsumerGuard(db_service)
    dispatcher = OutboxEventDispatcher()
    outbound_dispatcher = OutboundDispatcher(adapters=adapters, before_send=before_send)
    register_outbound_sender_handler(dispatcher=dispatcher, outbound_dispatcher=outbound_dispatcher)
    return OutboxWorkerRuntime(
        outbox_repository=outbox_repository,
        dispatcher=dispatcher,
        idempotency_guard=idempotency_guard,
        retry_policy=retry_policy,
        operations_metrics=operations_metrics,
    )


def build_sms_outbound_sender_worker_runtime(
    *,
    db_service: Any,
    sender,
    before_send: BeforeSendHook | None = None,
    retry_policy: ExponentialBackoffRetryPolicy | None = None,
    operations_metrics: OperationsMetricsRecorder | None = None,
) -> OutboxWorkerRuntime:
    return build_outbound_sender_worker_runtime(
        db_service=db_service,
        adapters=[SMSOutboundAdapter(sender)],
        before_send=before_send,
        retry_policy=retry_policy,
        operations_metrics=operations_metrics,
    )
