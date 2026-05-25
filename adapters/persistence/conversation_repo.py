"""
PostgreSQL conversation state repository.
Implements core/ports/conversation_repo.ConversationRepo.

Adapter layer: knows about DB, knows nothing about business logic.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PsycopgConversationRepo:
    """Reads and writes conversation state using a psycopg2 connection pool."""

    def __init__(self, pool) -> None:
        self._pool = pool

    def get_state(self, phone: str) -> Optional[Dict[str, Any]]:
        conn = self._pool.getconn()
        try:
            metadata = None
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT current_state, metadata, version FROM conversation_states WHERE phone_number = %s",
                        (phone,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    current_state, metadata, version = row
            except Exception:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT current_state, version FROM conversation_states WHERE phone_number = %s",
                        (phone,),
                    )
                    row = cur.fetchone()
                    if not row:
                        return None
                    current_state, version = row

            result = {"state": current_state, "current_state": current_state, "version": version}
            if metadata:
                try:
                    result.update(json.loads(metadata) if isinstance(metadata, str) else metadata)
                except Exception:
                    logger.warning(
                        "conversation_repo.get_state.metadata_parse_failed",
                        extra={"phone": phone[:4] + "****"},
                    )
            return result
        except Exception:
            logger.exception("conversation_repo.get_state.error", extra={"phone": phone[:4] + "****"})
            return None
        finally:
            self._pool.putconn(conn)

    def save_state(self, phone: str, state: Dict[str, Any]) -> None:
        conn = self._pool.getconn()
        current_state = str(state.get("state") or state.get("current_state") or "NEW")
        metadata = json.dumps(state)
        try:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO conversation_states (phone_number, current_state, metadata, version)
                        VALUES (%s, %s, %s, 1)
                        ON CONFLICT (phone_number) DO UPDATE
                        SET current_state = EXCLUDED.current_state,
                            metadata = EXCLUDED.metadata,
                            version = conversation_states.version + 1,
                            updated_at = NOW()
                        """,
                        (phone, current_state, metadata),
                    )
            except Exception:
                conn.rollback()
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO conversation_states (phone_number, current_state, version)
                        VALUES (%s, %s, 1)
                        ON CONFLICT (phone_number) DO UPDATE
                        SET current_state = EXCLUDED.current_state,
                            version = conversation_states.version + 1,
                            updated_at = NOW()
                        """,
                        (phone, current_state),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("conversation_repo.save_state.error", extra={"phone": phone[:4] + "****"})
            raise
        finally:
            self._pool.putconn(conn)
