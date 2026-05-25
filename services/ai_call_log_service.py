"""
AI call log service.

Tracks token usage and estimated cost per AI call.
Provides daily cost summaries and per-conversation cost analysis.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

logger = logging.getLogger("adella_chatbot.ai_call_log")

MODEL_COSTS = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "gemini-2.5-flash": {"input": 0.15, "output": 0.60},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
}

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS ai_call_log (
    id BIGSERIAL PRIMARY KEY,
    phone_hash VARCHAR(64),
    model VARCHAR(60) NOT NULL,
    call_type VARCHAR(40) NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    prompt_version VARCHAR(40),
    created_at TIMESTAMPTZ DEFAULT NOW()
);
"""

_CREATE_INDEX_CREATED_SQL = """
CREATE INDEX IF NOT EXISTS idx_ai_call_log_created
ON ai_call_log (created_at DESC);
"""

_CREATE_INDEX_PHONE_SQL = """
CREATE INDEX IF NOT EXISTS idx_ai_call_log_phone
ON ai_call_log (phone_hash, created_at DESC);
"""


class AICallLogService:
    def __init__(self, db_service):
        self.db = db_service
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready or not self.db:
            return
        try:
            self.db.execute_query(_CREATE_TABLE_SQL, fetch=False)
            self.db.execute_query(_CREATE_INDEX_CREATED_SQL, fetch=False)
            self.db.execute_query(_CREATE_INDEX_PHONE_SQL, fetch=False)
            self._schema_ready = True
        except Exception as e:
            logger.warning("ai call log schema ensure failed: %s", e)

    def _hash_phone(self, phone_number: str | None) -> str:
        try:
            phone = (phone_number or "").strip()
            if not phone:
                return "unknown"
            return hashlib.sha256(phone.encode("utf-8")).hexdigest()
        except Exception:
            return "unknown"

    def _compute_cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        try:
            rates = MODEL_COSTS.get((model or "").strip(), {})
            input_rate = float(rates.get("input") or 0.0)
            output_rate = float(rates.get("output") or 0.0)
            return round((max(int(input_tokens or 0), 0) / 1_000_000.0 * input_rate) + (max(int(output_tokens or 0), 0) / 1_000_000.0 * output_rate), 8)
        except Exception:
            return 0.0

    def log_call(
        self,
        *,
        phone_number: str | None,
        model: str,
        call_type: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int = 0,
        prompt_version: str | None = None,
    ) -> bool:
        if not self.db or not (model or "").strip() or not (call_type or "").strip():
            return False
        try:
            self.ensure_schema()
            self.db.execute_query(
                """
                INSERT INTO ai_call_log (
                    phone_hash, model, call_type, input_tokens, output_tokens,
                    cost_usd, latency_ms, prompt_version
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._hash_phone(phone_number),
                    (model or "").strip(),
                    (call_type or "").strip(),
                    max(int(input_tokens or 0), 0),
                    max(int(output_tokens or 0), 0),
                    self._compute_cost(model, input_tokens, output_tokens),
                    max(int(latency_ms or 0), 0),
                    (prompt_version or "").strip() or None,
                ),
                fetch=False,
            )
            return True
        except Exception as e:
            logger.warning("ai call log insert failed: %s", e)
            return False

    def get_daily_cost(self, days: int = 1) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "total_cost_usd": 0.0,
            "total_calls": 0,
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "by_model": {},
        }
        if not self.db:
            return summary
        try:
            self.ensure_schema()
            days_value = max(int(days or 1), 1)
            totals = self.db.execute_query(
                """
                SELECT
                    COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd,
                    COUNT(*) AS total_calls,
                    COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS total_output_tokens
                FROM ai_call_log
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                """,
                (days_value,),
                fetch=True,
            ) or []
            if totals:
                row = totals[0] or {}
                summary.update(
                    {
                        "total_cost_usd": float(row.get("total_cost_usd") or 0.0),
                        "total_calls": int(row.get("total_calls") or 0),
                        "total_input_tokens": int(row.get("total_input_tokens") or 0),
                        "total_output_tokens": int(row.get("total_output_tokens") or 0),
                    }
                )
            grouped = self.db.execute_query(
                """
                SELECT
                    model,
                    COUNT(*) AS calls,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                    COALESCE(SUM(input_tokens), 0) AS input_tokens,
                    COALESCE(SUM(output_tokens), 0) AS output_tokens
                FROM ai_call_log
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY model
                ORDER BY cost_usd DESC, calls DESC
                """,
                (days_value,),
                fetch=True,
            ) or []
            summary["by_model"] = {
                str((row or {}).get("model") or "unknown"): {
                    "calls": int((row or {}).get("calls") or 0),
                    "cost_usd": float((row or {}).get("cost_usd") or 0.0),
                    "input_tokens": int((row or {}).get("input_tokens") or 0),
                    "output_tokens": int((row or {}).get("output_tokens") or 0),
                }
                for row in grouped
            }
            return summary
        except Exception as e:
            logger.warning("ai call log summary failed: %s", e)
            return summary

    def get_daily_cost_by_day(self, days: int = 7) -> list[dict[str, Any]]:
        if not self.db:
            return []
        try:
            self.ensure_schema()
            rows = self.db.execute_query(
                """
                SELECT
                    DATE(created_at) AS date,
                    COALESCE(SUM(cost_usd), 0.0) AS cost_usd,
                    COUNT(*) AS calls
                FROM ai_call_log
                WHERE created_at >= NOW() - (%s * INTERVAL '1 day')
                GROUP BY DATE(created_at)
                ORDER BY DATE(created_at) DESC
                """,
                (max(int(days or 7), 1),),
                fetch=True,
            ) or []
            return [
                {
                    "date": str((row or {}).get("date") or ""),
                    "cost_usd": float((row or {}).get("cost_usd") or 0.0),
                    "calls": int((row or {}).get("calls") or 0),
                }
                for row in rows
                if row
            ]
        except Exception as e:
            logger.warning("ai call log by-day summary failed: %s", e)
            return []
