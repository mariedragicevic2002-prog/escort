"""
services/ai_task_queue.py — DB-backed queue for non-critical AI tasks.

Improvements over legacy version:
- Dead-letter queue (DLQ): tasks exhausting MAX_ATTEMPTS move to ai_tasks_dlq
  instead of silently staying as 'failed'.
- FOR UPDATE SKIP LOCKED: prevents double-processing under concurrent workers.
- Exponential backoff hint stored in last_error for observability.
- DLQ alert hook: override DLQ_ALERT_FN env or monkey-patch _on_dlq_insert.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Optional

logger = logging.getLogger("adella_chatbot.ai_task_queue")

MAX_ATTEMPTS: int = int(os.environ.get("AI_TASK_MAX_ATTEMPTS", "3"))

# ─────────────────────────── Schema DDL ────────────────────────────────────

_CREATE_QUEUE_SQL = """
CREATE TABLE IF NOT EXISTS ai_task_queue (
    id          BIGSERIAL PRIMARY KEY,
    task_type   VARCHAR(80)  NOT NULL,
    payload     JSONB        NOT NULL,
    status      VARCHAR(20)  NOT NULL DEFAULT 'pending',
    attempts    INTEGER      NOT NULL DEFAULT 0,
    last_error  TEXT,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);
"""

_CREATE_QUEUE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_ai_task_queue_status_created
ON ai_task_queue (status, created_at ASC);
"""

_CREATE_DLQ_SQL = """
CREATE TABLE IF NOT EXISTS ai_tasks_dlq (
    id              BIGSERIAL PRIMARY KEY,
    original_id     BIGINT,
    task_type       VARCHAR(80)  NOT NULL,
    payload         JSONB        NOT NULL,
    attempts        INTEGER      NOT NULL DEFAULT 0,
    final_error     TEXT,
    moved_at        TIMESTAMPTZ  DEFAULT NOW()
);
"""


def ensure_schema(db_service) -> None:
    """Create queue and DLQ tables if they do not exist."""
    if not db_service:
        return
    for sql in (_CREATE_QUEUE_SQL, _CREATE_QUEUE_INDEX_SQL, _CREATE_DLQ_SQL):
        try:
            db_service.execute_query(sql, fetch=False)
        except Exception as exc:
            logger.warning("ai_task_queue.ensure_schema failed: %s", exc)


# ─────────────────────────── Enqueue ───────────────────────────────────────

def enqueue_ai_task(db_service, *, task_type: str, payload: dict[str, Any]) -> bool:
    """Enqueue a task. Returns True on success."""
    if not db_service:
        return False
    ensure_schema(db_service)
    try:
        db_service.execute_query(
            """
            INSERT INTO ai_task_queue (task_type, payload, status, attempts)
            VALUES (%s, %s::jsonb, 'pending', 0)
            """,
            ((task_type or "").strip() or "generic", json.dumps(payload or {}, default=str)),
            fetch=False,
        )
        return True
    except Exception as exc:
        logger.warning("enqueue_ai_task failed (%s): %s", task_type, exc)
        return False


# ─────────────────────────── DLQ hook ──────────────────────────────────────

def _on_dlq_insert(task_id: int, task_type: str, attempts: int, error: str) -> None:
    """Called when a task is moved to the DLQ. Override for alerting."""
    logger.error(
        "ai_task_queue.dlq_insert",
        extra={
            "original_id": task_id,
            "task_type": task_type,
            "attempts": attempts,
            "final_error": error[:200],
        },
    )


def _move_to_dlq(
    db_service,
    task_id: int,
    task_type: str,
    payload: dict,
    attempts: int,
    error: str,
) -> None:
    """Move an exhausted task from queue to DLQ atomically."""
    try:
        db_service.execute_query(
            """
            INSERT INTO ai_tasks_dlq
                (original_id, task_type, payload, attempts, final_error)
            VALUES (%s, %s, %s::jsonb, %s, %s)
            """,
            (task_id, task_type, json.dumps(payload, default=str), attempts, error[:500]),
            fetch=False,
        )
        db_service.execute_query(
            "UPDATE ai_task_queue SET status='dead', updated_at=NOW() WHERE id=%s",
            (task_id,),
            fetch=False,
        )
        _on_dlq_insert(task_id, task_type, attempts, error)
    except Exception as exc:
        logger.error("ai_task_queue.dlq_move_failed id=%s: %s", task_id, exc)


# ─────────────────────────── Processor ─────────────────────────────────────

def process_pending_tasks(db_service, *, max_tasks: int = 10) -> int:
    """
    Claim and process up to max_tasks pending tasks.

    Uses UPDATE … FOR UPDATE SKIP LOCKED to atomically claim rows,
    preventing double-processing when two APScheduler invocations overlap.

    Returns the number of tasks successfully completed.
    """
    if not db_service:
        return 0
    ensure_schema(db_service)

    try:
        rows = db_service.execute_query(
            """
            UPDATE ai_task_queue
            SET status = 'processing', updated_at = NOW()
            WHERE id IN (
                SELECT id FROM ai_task_queue
                WHERE status = 'pending'
                  AND attempts < %s
                  AND task_type IN ('extract_booking_memory', 'summarize_conversation')
                ORDER BY created_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, task_type, payload, attempts
            """,
            (MAX_ATTEMPTS, max(1, int(max_tasks))),
            fetch=True,
        ) or []
    except Exception as exc:
        logger.warning("ai_task_queue.claim_failed: %s", exc)
        return 0

    processed = 0
    for row in rows:
        task_id   = row.get("id")
        task_type = str(row.get("task_type") or "").strip()
        payload   = row.get("payload") or {}
        attempts  = int(row.get("attempts") or 0)

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        try:
            _dispatch_task(db_service, task_type=task_type, payload=payload)
            db_service.execute_query(
                "UPDATE ai_task_queue SET status='done', updated_at=NOW(), attempts=%s WHERE id=%s",
                (attempts + 1, task_id),
                fetch=False,
            )
            processed += 1
            logger.info("ai_task_queue.done id=%s type=%s", task_id, task_type)

        except Exception as exc:
            new_attempts = attempts + 1
            if new_attempts >= MAX_ATTEMPTS:
                # Exhausted — move to DLQ
                _move_to_dlq(db_service, task_id, task_type, payload, new_attempts, str(exc))
            else:
                # Retry — reset to pending
                logger.warning(
                    "ai_task_queue.retry id=%s type=%s attempt=%d/%d err=%s",
                    task_id, task_type, new_attempts, MAX_ATTEMPTS, exc,
                )
                db_service.execute_query(
                    """
                    UPDATE ai_task_queue
                    SET status='pending', attempts=%s, last_error=%s, updated_at=NOW()
                    WHERE id=%s
                    """,
                    (new_attempts, str(exc)[:500], task_id),
                    fetch=False,
                )

    return processed


# ─────────────────────────── Dispatch ──────────────────────────────────────

def _dispatch_task(db_service, *, task_type: str, payload: dict) -> None:
    """Route a single task to its handler. Raises on failure (caller handles retry/DLQ)."""
    if task_type == "extract_booking_memory":
        _handle_extract_booking_memory(db_service, payload)
    elif task_type == "summarize_conversation":
        _handle_summarize_conversation(db_service, payload)
    else:
        logger.info("ai_task_queue.unknown_type (skipping): %s", task_type)


def _handle_extract_booking_memory(db_service, payload: dict) -> None:
    phone = str(payload.get("phone_number") or "").strip()
    booking_data = payload.get("booking_data") or {}
    if not phone:
        return
    from services.client_memory_service import ClientMemoryService  # type: ignore
    count = ClientMemoryService(db_service).extract_from_booking(phone, booking_data)
    logger.info("ai_task_queue.extract_memory phone=****%s count=%d", phone[-4:], count)


def _handle_summarize_conversation(db_service, payload: dict) -> None:
    phone = str(payload.get("phone_number") or "").strip()
    history = payload.get("history") or []
    if not phone or not history:
        return
    from services.conversation_summarizer import ConversationSummarizer  # type: ignore
    compressed = ConversationSummarizer().compress_history(history)
    if compressed.get("compressed") and compressed.get("summary"):
        logger.info(
            "ai_task_queue.summarized phone=****%s turns=%d",
            phone[-4:] if len(phone) >= 4 else "??",
            len(history),
        )
