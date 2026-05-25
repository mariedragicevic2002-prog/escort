"""Shared base for DB-backed idempotency guards.

Both outbox-consumer and inbound-worker guards use identical double-checked
locking for schema initialisation and the same ``INSERT … ON CONFLICT DO NOTHING
/ RETURNING`` dedup pattern. Only the table DDL and column names differ.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Mapping


class _BaseIdempotencyGuard:
    """Abstract parameterised base for DB idempotency guards."""

    _CREATE_TABLE_SQL: str
    _CREATE_INDEX_SQL: str | None = None

    def __init__(self, db_service: Any, *, init_schema: bool = False) -> None:
        self._db_service = db_service
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        if init_schema:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            if not hasattr(self._db_service, "execute_query"):
                raise RuntimeError(
                    f"{type(self).__name__} requires db_service.execute_query"
                )
            self._db_service.execute_query(self._CREATE_TABLE_SQL, fetch=False)
            if self._CREATE_INDEX_SQL:
                self._db_service.execute_query(self._CREATE_INDEX_SQL, fetch=False)
            self._schema_ready = True

    def _was_processed(
        self,
        *,
        id_value: str,
        dedup_key: str,
        table: str,
        id_column: str,
        conn: Any | None = None,
    ) -> bool:
        rows = self._db_service.execute_query(
            f"SELECT {id_column} FROM {table} WHERE {id_column} = %s OR dedup_key = %s LIMIT 1",
            (id_value, dedup_key),
            fetch=True,
            conn=conn,
        )
        return bool(rows)

    def _mark_processed(
        self,
        *,
        id_value: str,
        dedup_key: str,
        table: str,
        id_column: str,
        extra_columns: tuple[str, ...] = (),
        extra_values: tuple[Any, ...] = (),
        metadata: Mapping[str, Any] | None = None,
        conn: Any | None = None,
    ) -> bool:
        all_columns = (id_column, "dedup_key") + extra_columns + ("metadata",)
        placeholders = ", ".join(
            ["%s", "%s"] + ["%s"] * len(extra_columns) + ["%s::jsonb"]
        )
        col_list = ", ".join(all_columns)
        rows = self._db_service.execute_query(
            f"""
            INSERT INTO {table} ({col_list})
            VALUES ({placeholders})
            ON CONFLICT DO NOTHING
            RETURNING {id_column}
            """,
            (id_value, dedup_key) + extra_values + (
                json.dumps(dict(metadata or {}), sort_keys=True, default=str),
            ),
            fetch=True,
            conn=conn,
        )
        return bool(rows)
