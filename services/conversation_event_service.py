"""
Append-only conversation event logging.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.conversation_events")


_CREATE_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS conversation_events (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(20),
    event_type VARCHAR(80) NOT NULL,
    from_state VARCHAR(40),
    to_state VARCHAR(40),
    intent VARCHAR(80),
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_EVENTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_conversation_events_phone_created
ON conversation_events (phone_number, created_at DESC);
"""


def ensure_conversation_events_schema(db_service) -> None:
    if not db_service:
        return
    try:
        db_service.execute_query(_CREATE_EVENTS_TABLE_SQL, fetch=False)
        db_service.execute_query(_CREATE_EVENTS_INDEX_SQL, fetch=False)
    except Exception as e:
        logger.warning("ensure conversation_events schema failed: %s", e)


def record_conversation_event(
    db_service,
    *,
    phone_number: str | None,
    event_type: str,
    from_state: str | None = None,
    to_state: str | None = None,
    intent: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    if not db_service:
        return False
    ensure_conversation_events_schema(db_service)
    try:
        db_service.execute_query(
            """
            INSERT INTO conversation_events (phone_number, event_type, from_state, to_state, intent, metadata)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                phone_number,
                (event_type or "").strip() or "unknown",
                from_state,
                to_state,
                intent,
                json.dumps(metadata or {}),
            ),
            fetch=False,
        )
        return True
    except Exception as e:
        logger.warning("record conversation event failed (%s): %s", event_type, e)
        return False
