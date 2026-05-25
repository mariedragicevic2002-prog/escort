from __future__ import annotations

from dataclasses import dataclass
import logging
from threading import Event
from time import perf_counter
from typing import Any, NamedTuple, cast

from app.cost_controls import ProcessingBudgetController, ProcessingBudgetSettings
from app.observability.operations_metrics import OperationsMetricsRecorder
from app.queue.inbound import InboundQueueRecord
from app.queue.providers import InboundQueueProvider
from app.resilience.injection import (
    ResilienceDrillFailure,
    ResilienceDrillHook,
    WORKER_CRASH_POINT,
)
from app.workers.inbound_idempotency import InboundIdempotencyGuard
from app.workers.inbound_orchestrator import InboundOrchestrationExecutor
from app.workers.retry import ExponentialBackoffRetryPolicy
from app.workers.supervision import LeaseClaim, WorkerSupervisionRuntime

logger = logging.getLogger("adella_chatbot.refactor.inbound_worker_runtime")


@dataclass(frozen=True)
class InboundWorkerBatchResult:
    polled: int = 0
    sent: int = 0
    retried: int = 0
    dead_lettered: int = 0
    duplicates: int = 0
    skipped: int = 0


class _MetricsInfo(NamedTuple):
    """Minimal projection of InboundQueueRecord fields for metrics methods."""
    event_type: str
    aggregate_type: str
    max_retries: int
    created_at: str | None
    occurred_at: str


class InboundWorkerRuntime:
    """Poll/process runtime for inbound queue orchestration with idempotent guards."""

    def __init__(
        self,
        *,
        inbound_repository: InboundQueueProvider,
        orchestrator: InboundOrchestrationExecutor,
        idempotency_guard: InboundIdempotencyGuard,
        retry_policy: ExponentialBackoffRetryPolicy | None = None,
        operations_metrics: OperationsMetricsRecorder | None = None,
        supervision: WorkerSupervisionRuntime | None = None,
        queue_pause_reader: Any | None = None,
        queue_name: str = "refactor_inbound",
        processing_budget: ProcessingBudgetController | None = None,
        processing_budget_settings: ProcessingBudgetSettings | None = None,
        drill_hook: ResilienceDrillHook | None = None,
    ) -> None:
        self._inbound_repository = inbound_repository
        self._orchestrator = orchestrator
        self._idempotency_guard = idempotency_guard
        self._retry_policy = retry_policy or ExponentialBackoffRetryPolicy()
        self._operations_metrics = operations_metrics or OperationsMetricsRecorder()
        self._supervision = supervision or WorkerSupervisionRuntime(queue_name="refactor_inbound")
        self._queue_pause_reader = queue_pause_reader
        self._queue_name = str(queue_name or "refactor_inbound")
        self._processing_budget = processing_budget or ProcessingBudgetController(
            settings=processing_budget_settings or ProcessingBudgetSettings()
        )
        self._drill_hook = drill_hook

    def run_once(
        self,
        *,
        batch_size: int = 25,
        conn: Any | None = None,
    ) -> InboundWorkerBatchResult:
        if self._is_queue_paused(conn=conn):
            logger.info("inbound worker queue paused queue_name=%s", self._queue_name)
            return InboundWorkerBatchResult()
        budget = self._processing_budget.evaluate(requested_items=batch_size)
        if budget.allowed_items <= 0:
            logger.info(
                "inbound worker budget exhausted queue_name=%s reason=%s requested=%s remaining=%s",
                self._queue_name,
                budget.reason,
                budget.requested_items,
                budget.interval_remaining,
            )
            return InboundWorkerBatchResult()
        self._supervision.recover_stale_claims(requeue_claim=lambda claim: self._recover_stale_claim(claim, conn=conn))
        messages = self._inbound_repository.list_pending(limit=budget.allowed_items, conn=conn)
        snapshot_events = cast(Any, [self._as_metrics_event(message) for message in messages])
        self._operations_metrics.record_queue_snapshot(
            queue_name="refactor_inbound",
            events=snapshot_events,
            batch_size=batch_size,
        )

        sent = 0
        retried = 0
        dead_lettered = 0
        duplicates = 0
        skipped = 0
        for message in messages:
            if not self._supervision.claim_item(message.message_id, conn=conn):
                skipped += 1
                continue
            started = perf_counter()
            outcome = "skipped"
            skip_release = False
            try:
                self._supervision.heartbeat(message.message_id, conn=conn)
                outcome = self._process_single_message(message, conn=conn)
            except ResilienceDrillFailure as exc:
                if exc.point == WORKER_CRASH_POINT:
                    skip_release = True
                    outcome = "crash_injected"
                else:
                    outcome = "skipped"
            finally:
                if not skip_release:
                    self._supervision.release_item(message.message_id, reason=outcome, conn=conn)
            self._operations_metrics.record_processing_latency(
                queue_name="refactor_inbound",
                event=cast(Any, self._as_metrics_event(message)),
                outcome=outcome,
                duration_ms=(perf_counter() - started) * 1000.0,
            )
            if outcome == "sent":
                sent += 1
            elif outcome == "retry":
                retried += 1
            elif outcome == "dead_letter":
                dead_lettered += 1
            elif outcome == "duplicate":
                duplicates += 1
            else:
                skipped += 1
            self._processing_budget.record_processed(1)

        return InboundWorkerBatchResult(
            polled=len(messages),
            sent=sent,
            retried=retried,
            dead_lettered=dead_lettered,
            duplicates=duplicates,
            skipped=skipped,
        )

    def run_loop(
        self,
        *,
        stop_event: Event,
        poll_interval_seconds: float = 1.0,
        batch_size: int = 25,
        conn: Any | None = None,
    ) -> None:
        _min_sleep = max(0.05, float(poll_interval_seconds) * 0.1)
        _max_sleep = max(float(poll_interval_seconds), 5.0)
        _sleep = _min_sleep
        while not stop_event.is_set():
            result = self.run_once(batch_size=batch_size, conn=conn)
            if result.polled == 0:
                # Empty batch — back off up to _max_sleep with jitter
                import random as _random
                _sleep = min(_sleep * 2.0, _max_sleep) * (0.8 + 0.4 * _random.random())
            else:
                # Work found — reset to minimum poll interval
                _sleep = _min_sleep
            stop_event.wait(_sleep)

    def _process_single_message(
        self,
        message: InboundQueueRecord,
        *,
        conn: Any | None = None,
    ) -> str:
        if not self._inbound_repository.mark_processing(message.message_id, conn=conn):
            return "skipped"
        self._supervision.heartbeat(message.message_id, conn=conn)

        dedup_key = message.normalized_dedup_key
        if self._idempotency_guard.was_processed(
            message_id=message.message_id,
            dedup_key=dedup_key,
            conn=conn,
        ):
            self._inbound_repository.mark_sent(message.message_id, conn=conn)
            return "duplicate"

        self._invoke_drill_hook(
            "before_worker_dispatch",
            queue_name=self._queue_name,
            item_id=message.message_id,
        )
        try:
            self._orchestrator.execute(message)
            self._supervision.heartbeat(message.message_id, conn=conn)
        except Exception as exc:
            attempt = max(0, int(message.metadata.attempt)) + 1
            retry_decision = self._retry_policy.evaluate_counts(
                retry_count=max(0, int(message.metadata.attempt)),
                max_retries=max(1, int(message.max_attempts)),
            )
            marked = self._inbound_repository.mark_retry(
                message.message_id,
                error_message=str(exc),
                retry_delay_seconds=retry_decision.retry_delay_seconds,
                conn=conn,
            )
            if not marked:
                return "skipped"
            if attempt >= int(message.max_attempts):
                self._operations_metrics.record_dead_letter(
                    queue_name="refactor_inbound",
                    event=cast(Any, self._as_metrics_event(message)),
                    retry_count=attempt,
                )
                logger.warning(
                    "inbound worker dead-letter message_id=%s retries=%s max_attempts=%s",
                    message.message_id,
                    attempt,
                    message.max_attempts,
                )
                return "dead_letter"
            self._operations_metrics.record_retry(
                queue_name="refactor_inbound",
                event=cast(Any, self._as_metrics_event(message)),
                retry_count=attempt,
            )
            return "retry"

        marked_processed = self._idempotency_guard.mark_processed(
            message_id=message.message_id,
            dedup_key=dedup_key,
            metadata={
                "request_id": message.metadata.request_id,
                "correlation_id": message.metadata.correlation_id,
                "status": "sent",
            },
            conn=conn,
        )
        if not marked_processed:
            self._inbound_repository.mark_sent(message.message_id, conn=conn)
            return "duplicate"
        if not self._inbound_repository.mark_sent(message.message_id, conn=conn):
            return "skipped"
        return "sent"

    def _recover_stale_claim(self, claim: LeaseClaim, *, conn: Any | None = None) -> bool:
        recover = getattr(self._inbound_repository, "recover_stale_processing", None)
        if not callable(recover):
            return False
        return bool(
            recover(
                claim.item_id,
                error_message="worker supervision lease expired",
                conn=conn,
            )
        )

    @staticmethod
    def _as_metrics_event(message: InboundQueueRecord) -> _MetricsInfo:
        payload_type = str(message.payload.get("event_type") or "").strip()
        event_type = payload_type or "inbound.sms"
        aggregate_type = str(message.payload.get("aggregate_type") or "conversation").strip() or "conversation"
        occurred_at = message.metadata.enqueued_at or message.created_at or ""
        return _MetricsInfo(
            event_type=event_type,
            aggregate_type=aggregate_type,
            max_retries=max(1, int(message.max_attempts)),
            created_at=message.created_at,
            occurred_at=occurred_at,
        )

    def _is_queue_paused(self, *, conn: Any | None = None) -> bool:
        checker = getattr(self._queue_pause_reader, "is_queue_paused", None)
        if not callable(checker):
            return False
        try:
            return bool(checker(self._queue_name, conn=conn))
        except TypeError:
            return bool(checker(self._queue_name))

    def _invoke_drill_hook(self, method_name: str, **kwargs: Any) -> None:
        if self._drill_hook is None:
            return
        hook = getattr(self._drill_hook, method_name, None)
        if callable(hook):
            hook(**kwargs)
