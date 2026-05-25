"""
Semantic memory service.

This provides a structured + semantic-friendly store for conversation snippets.
It uses text retrieval by default.  When ``EMBEDDINGS_ENABLED=true`` **and** the
``pgvector`` extension is installed in PostgreSQL, it upgrades to cosine-similarity
vector search automatically.  Everything degrades gracefully if either is missing.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.semantic_memory")


_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS semantic_memory (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(20) NOT NULL,
    memory_type VARCHAR(50) NOT NULL,
    memory_text TEXT NOT NULL,
    embedding_payload JSONB,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_semantic_memory_phone_created
ON semantic_memory (phone_number, created_at DESC);
"""

# pgvector DDL — run only when EMBEDDINGS_ENABLED=true
_ENABLE_PGVECTOR_SQL = "CREATE EXTENSION IF NOT EXISTS vector;"
_ADD_VECTOR_COL_SQL = "ALTER TABLE semantic_memory ADD COLUMN IF NOT EXISTS embedding vector(768);"
_CREATE_VECTOR_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_semantic_memory_embedding
ON semantic_memory USING ivfflat (embedding vector_cosine_ops)
WITH (lists = 100);
"""


class SemanticMemoryService:
    def __init__(self, db_service, embedding_service=None):
        self.db = db_service
        self._embed = embedding_service
        self._schema_ready = False
        self._vector_ready = False  # True once pgvector column confirmed

    def _ensure_schema(self) -> None:
        if self._schema_ready or not self.db:
            return
        try:
            self.db.execute_query(_CREATE_TABLE_SQL, fetch=False)
            self.db.execute_query(_CREATE_INDEX_SQL, fetch=False)
            self._schema_ready = True
        except Exception as e:
            logger.warning("semantic memory schema ensure failed: %s", e)

    def _ensure_vector_schema(self) -> bool:
        """Try to enable pgvector extension and add vector column.  Returns True on success."""
        if self._vector_ready:
            return True
        if not self.db:
            return False
        self._ensure_schema()
        try:
            self.db.execute_query(_ENABLE_PGVECTOR_SQL, fetch=False)
            self.db.execute_query(_ADD_VECTOR_COL_SQL, fetch=False)
            # Index creation can fail on small tables (IVFFlat needs >lists rows); ignore
            try:
                self.db.execute_query(_CREATE_VECTOR_INDEX_SQL, fetch=False)
            except Exception:
                pass
            self._vector_ready = True
            return True
        except Exception as exc:
            logger.warning("pgvector schema setup failed (text search will be used): %s", exc)
            return False

    def _vec_to_pg(self, vec: list[float]) -> str:
        """Format vector as PostgreSQL literal: '[1.0,2.0,...]'."""
        return "[" + ",".join(str(float(v)) for v in vec) + "]"

    def store_memory(
        self,
        *,
        phone_number: str,
        memory_type: str,
        memory_text: str,
        metadata: dict[str, Any] | None = None,
        embedding_payload: list[float] | dict[str, Any] | None = None,
    ) -> bool:
        if not self.db:
            return False
        text = (memory_text or "").strip()
        if not text:
            return False
        self._ensure_schema()

        # Generate embedding if service is available
        embed_vec: list[float] | None = None
        if self._embed is not None and getattr(self._embed, "enabled", False):
            if embedding_payload and isinstance(embedding_payload, list):
                embed_vec = embedding_payload
            else:
                embed_vec = self._embed.embed(text)
            if embed_vec is not None:
                self._ensure_vector_schema()

        try:
            if embed_vec is not None and self._vector_ready:
                self.db.execute_query(
                    """
                    INSERT INTO semantic_memory
                        (phone_number, memory_type, memory_text, embedding_payload, metadata, embedding)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s::vector)
                    """,
                    (
                        phone_number,
                        (memory_type or "general").strip() or "general",
                        text,
                        json.dumps(embed_vec),
                        json.dumps(metadata or {}),
                        self._vec_to_pg(embed_vec),
                    ),
                    fetch=False,
                )
            else:
                self.db.execute_query(
                    """
                    INSERT INTO semantic_memory (phone_number, memory_type, memory_text, embedding_payload, metadata)
                    VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                    """,
                    (
                        phone_number,
                        (memory_type or "general").strip() or "general",
                        text,
                        json.dumps(embedding_payload) if embedding_payload is not None else None,
                        json.dumps(metadata or {}),
                    ),
                    fetch=False,
                )
            return True
        except Exception as e:
            logger.warning("store semantic memory failed for %s: %s", phone_number, e)
            return False

    def get_relevant_snippets(
        self,
        *,
        phone_number: str,
        query_text: str,
        limit: int = 3,
    ) -> list[str]:
        if not self.db:
            return []
        self._ensure_schema()
        q = (query_text or "").strip()
        capped = max(1, min(limit, 10))

        # Attempt vector similarity search first
        if q and self._embed is not None and getattr(self._embed, "enabled", False) and self._vector_ready:
            query_vec = self._embed.embed(q)
            if query_vec is not None:
                try:
                    vec_literal = self._vec_to_pg(query_vec)
                    rows = self.db.execute_query(
                        """
                        SELECT memory_text
                        FROM semantic_memory
                        WHERE phone_number = %s
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (phone_number, vec_literal, capped),
                        fetch=True,
                    ) or []
                    out = [str((r or {}).get("memory_text") or "").strip() for r in rows]
                    out = [x for x in out if x]
                    if out:
                        return out
                except Exception as exc:
                    logger.warning("vector similarity search failed, falling back to text: %s", exc)

        # Text-based fallback (always available)
        try:
            if q:
                rows = self.db.execute_query(
                    """
                    SELECT memory_text
                    FROM semantic_memory
                    WHERE phone_number = %s
                      AND (
                        memory_text ILIKE %s
                        OR memory_text ILIKE %s
                        OR memory_type ILIKE %s
                      )
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (phone_number, f"%{q}%", f"%{q[:24]}%", f"%{q}%", capped),
                    fetch=True,
                ) or []
            else:
                rows = self.db.execute_query(
                    """
                    SELECT memory_text
                    FROM semantic_memory
                    WHERE phone_number = %s
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (phone_number, capped),
                    fetch=True,
                ) or []
            out = [str((r or {}).get("memory_text") or "").strip() for r in rows]
            return [x for x in out if x]
        except Exception as e:
            logger.warning("semantic memory lookup failed for %s: %s", phone_number, e)
            return []
