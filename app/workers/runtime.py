from __future__ import annotations

from dataclasses import dataclass
import logging
from time import perf_counter
from threading import Event
from typing import Any

from app.cost_controls import ProcessingBudgetController, ProcessingBudgetSettings
from app.events.outbox import OutboxEventRecord, OutboxRepository
from app.observability.operations_metrics import OperationsMetricsRecorder
from app.resilience.injection import (
    ResilienceDrillFailure,
    ResilienceDrillHook,
    WORKER_CRASH_POINT,
)
from app.workers.dispatcher import OutboxEventDispatcher
from app.workers.idempotency import IdempotentConsumerGuard
from app.workers.retry import ExponentialBackoffRetryPolicy
from app.workers.supervision import LeaseClaim, WorkerSupervisionRuntime

logger = logging.getLogger("adella_chatbot.refactor.worker_runtime")


@dataclass(frozen=True)
class WorkerBatchResult:
    polled: int = 0
    sent: int = 0
    retried: int = 0
    dead_lettered: int = 0
    duplicates: int = 0
    skipped: int = 0


class OutboxWorkerRuntime:
    """Poll/process loop for outbox side effects with retries and dedup."""

    def __init__(
        self,
        *,
        outbox_repository: OutboxRepository,
        dispatcher: OutboxEventDispatcher,
        idempotency_guard: IdempotentConsumerGuard,
        retry_policy: ExponentialBackoffRetryPolicy | None = None,
        operations_metrics: OperationsMetricsRecorder | None = None,
        supervision: WorkerSupervisionRuntime | None = None,
        queue_pause_reader: Any | None = None,
        queue_name: str = "refactor_outbox",
        processing_budget: ProcessingBudgetController | None = None,
        processing_budget_settings: ProcessingBudgetSettings | None = None,
        drill_hook: ResilienceDrillHook | None = None,
    ) -> None:
        self._outbox_repository = outbox_repository
        self._dispatcher = dispatcher
        self._idempotency_guard = idempotency_guard
        self._retry_policy = retry_policy or ExponentialBackoffRetryPolicy()
        self._operations_metrics = operations_metrics or OperationsMetricsRecorder()
        self._supervision = supervision or WorkerSupervisionRuntime(queue_name="refactor_outbox")
        self._queue_pause_reader = queue_pause_reader
        self._queue_name = str(queue_name or "refactor_outbox")
        self._processing_budget = processing_budget or ProcessingBudgetController(
            settings=processing_budget_settings or ProcessingBudgetSettings()
        )
        self._drill_hook = drill_hook

    def run_once(
        self,
        *,
        batch_size: int = 25,
        conn: Any | None = None,
    ) -> WorkerBatchResult:
        if self._is_queue_paused(conn=conn):
            logger.info("outbox worker queue paused queue_name=%s", self._queue_name)
            return WorkerBatchResult()
        budget = self._processing_budget.evaluate(requested_items=batch_size)
        if budget.allowed_items <= 0:
            logger.info(
                "outbox worker budget exhausted queue_name=%s reason=%s requested=%s remaining=%s",
                self._queue_name,
                budget.reason,
                budget.requested_items,
                budget.interval_remaining,
            )
            return WorkerBatchResult()
        self._supervision.recover_stale_claims(requeue_claim=lambda claim: self._recover_stale_claim(claim, conn=conn))
        events = self._outbox_repository.list_pending(limit=budget.allowed_items, conn=conn)
        self._operations_metrics.record_queue_snapshot(
            queue_name="refactor_outbox",
            events=events,
            batch_size=batch_size,
        )
        sent = 0
        retried = 0
        dead_lettered = 0
        duplicates = 0
        skipped = 0
        for event in events:
            if not self._supervision.claim_item(event.event_id, conn=conn):
                skipped += 1
                continue
            started = perf_counter()
            outcome = "skipped"
            skip_release = False
            try:
                self._supervision.heartbeat(event.event_id, conn=conn)
                outcome = self._process_single_event(event, conn=conn)
            except ResilienceDrillFailure as exc:
                if exc.point == WORKER_CRASH_POINT:
                    skip_release = True
                    outcome = "crash_injected"
                else:
                    outcome = "skipped"
            finally:
                if not skip_release:
                    self._supervision.release_item(event.event_id, reason=outcome, conn=conn)
            self._operations_metrics.record_processing_latency(
                queue_name="refactor_outbox",
                event=event,
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
        return WorkerBatchResult(
            polled=len(events),
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
        while not stop_event.is_set():
            self.run_once(batch_size=batch_size, conn=conn)
            stop_event.wait(max(0.05, float(poll_interval_seconds)))

    def _process_single_event(
        self,
        event: OutboxEventRecord,
        *,
        conn: Any | None = None,
    ) -> str:
        if not self._outbox_repository.mark_processing(event.event_id, conn=conn):
            return "skipped"
        self._supervision.heartbeat(event.event_id, conn=conn)

        dedup_key = event.idempotency_key or event.event_id
        if self._idempotency_guard.was_processed(
            event_id=event.event_id,
            dedup_key=dedup_key,
            conn=conn,
        ):
            self._outbox_repository.mark_published(event.event_id, conn=conn)
            return "duplicate"

        self._invoke_drill_hook(
            "before_worker_dispatch",
            queue_name=self._queue_name,
            item_id=event.event_id,
        )
        try:
            self._dispatcher.dispatch(event)
            self._supervision.heartbeat(event.event_id, conn=conn)
        except Exception as exc:
            next_retry_count = int(event.retry_count) + 1
            is_dead_letter = next_retry_count >= int(event.max_retries)
            retry_decision = self._retry_policy.evaluate(event)
            marked = self._outbox_repository.mark_failure(
                event.event_id,
                error_message=str(exc),
                retry_delay_seconds=retry_decision.retry_delay_seconds,
                conn=conn,
            )
            if not marked:
                return "skipped"
            if is_dead_letter:
                self._operations_metrics.record_dead_letter(
                    queue_name="refactor_outbox",
                    event=event,
                    retry_count=next_retry_count,
                )
                logger.warning(
                    "worker dead-letter event_id=%s event_type=%s retries=%s max_retries=%s",
                    event.event_id,
                    event.event_type,
                    next_retry_count,
                    event.max_retries,
                )
                return "dead_letter"
            self._operations_metrics.record_retry(
                queue_name="refactor_outbox",
                event=event,
                retry_count=next_retry_count,
            )
            return "retry"

        self._idempotency_guard.mark_processed(
            event_id=event.event_id,
            dedup_key=dedup_key,
            event_type=event.event_type,
            metadata={"aggregate_type": event.aggregate_type, "aggregate_id": event.aggregate_id},
            conn=conn,
        )
        self._outbox_repository.mark_published(event.event_id, conn=conn)
        return "sent"

    def _recover_stale_claim(self, claim: LeaseClaim, *, conn: Any | None = None) -> bool:
        recover = getattr(self._outbox_repository, "recover_stale_processing", None)
        if not callable(recover):
            return False
        return bool(
            recover(
                claim.item_id,
                error_message="worker supervision lease expired",
                conn=conn,
            )
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
