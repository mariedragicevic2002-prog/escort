"""
Long-term client memory service.

Stores persistent per-client preferences and facts extracted from bookings.
Used to personalize AI responses for returning clients.

Phone numbers are stored as SHA-256 hashes for privacy, consistent with
the ai_call_log_service pattern.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

logger = logging.getLogger("adella_chatbot.client_memory")

# Maximum length for stored memory values and client names (prevents prompt injection payloads)
_MAX_MEMORY_VALUE_LEN = 200
_MAX_CLIENT_NAME_LEN = 80
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS client_memory (
    id BIGSERIAL PRIMARY KEY,
    phone_number VARCHAR(64) NOT NULL,
    memory_key VARCHAR(80) NOT NULL,
    memory_value TEXT NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT 'system',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (phone_number, memory_key)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_client_memory_phone
ON client_memory (phone_number, last_seen_at DESC);
"""


def _hash_phone(phone_number: str) -> str:
    """Return SHA-256 hex digest of the phone number for private storage."""
    phone = (phone_number or "").strip()
    if not phone:
        return "unknown"
    return hashlib.sha256(phone.encode("utf-8")).hexdigest()


def _sanitize_value(value: str, max_len: int = _MAX_MEMORY_VALUE_LEN) -> str:
    """Strip control characters and truncate to prevent prompt injection."""
    cleaned = _CONTROL_CHARS.sub(" ", value).strip()
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip()
    return cleaned


class ClientMemoryService:
    def __init__(self, db_service):
        self.db = db_service
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready or not self.db:
            return
        try:
            self.db.execute_query(_CREATE_TABLE_SQL, fetch=False)
            self.db.execute_query(_CREATE_INDEX_SQL, fetch=False)
            self._schema_ready = True
        except Exception as e:
            logger.warning("client memory schema ensure failed: %s", e)

    def get_memories(self, phone_number: str, limit: int = 5) -> list[dict[str, Any]]:
        if not self.db or not (phone_number or "").strip():
            return []
        self.ensure_schema()
        phone_hash = _hash_phone(phone_number)
        try:
            rows = self.db.execute_query(
                """
                SELECT memory_key, memory_value, source, confidence
                FROM client_memory
                WHERE phone_number = %s
                ORDER BY last_seen_at DESC
                LIMIT %s
                """,
                (phone_hash, max(1, min(int(limit or 5), 20))),
                fetch=True,
            ) or []
            return [
                {
                    "key": str((row or {}).get("memory_key") or "").strip(),
                    "value": str((row or {}).get("memory_value") or "").strip(),
                    "source": str((row or {}).get("source") or "system").strip() or "system",
                    "confidence": float((row or {}).get("confidence") or 0.0),
                }
                for row in rows
                if str((row or {}).get("memory_key") or "").strip()
                and str((row or {}).get("memory_value") or "").strip()
            ]
        except Exception as e:
            logger.warning("client memory lookup failed: %s", e)
            return []

    def upsert_memory(
        self,
        phone_number: str,
        key: str,
        value: str,
        source: str = "system",
        confidence: float = 1.0,
    ) -> bool:
        if not self.db:
            return False
        phone_hash = _hash_phone(phone_number)
        memory_key = (key or "").strip()
        memory_value = _sanitize_value((value or "").strip())
        if phone_hash == "unknown" or not memory_key or not memory_value:
            return False
        self.ensure_schema()
        try:
            conf = max(0.0, min(float(confidence), 1.0))
            self.db.execute_query(
                """
                INSERT INTO client_memory (phone_number, memory_key, memory_value, source, confidence)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (phone_number, memory_key)
                DO UPDATE SET memory_value = %s, last_seen_at = NOW(), confidence = %s
                """,
                (phone_hash, memory_key, memory_value, (source or "system").strip() or "system", conf, memory_value, conf),
                fetch=False,
            )
            return True
        except Exception as e:
            logger.warning("client memory upsert failed for key=%s: %s", key, e)
            return False

    def delete_memory(self, phone_number: str, key: str) -> bool:
        if not self.db:
            return False
        phone_hash = _hash_phone(phone_number)
        memory_key = (key or "").strip()
        if phone_hash == "unknown" or not memory_key:
            return False
        self.ensure_schema()
        try:
            self.db.execute_query(
                "DELETE FROM client_memory WHERE phone_number = %s AND memory_key = %s",
                (phone_hash, memory_key),
                fetch=False,
            )
            return True
        except Exception as e:
            logger.warning("client memory delete failed for key=%s: %s", key, e)
            return False

    def format_for_prompt(self, phone_number: str) -> str:
        try:
            memories = self.get_memories(phone_number, limit=5)
            if not memories:
                return ""
            values = {m.get("key"): _sanitize_value(m.get("value") or "") for m in memories if m.get("key") and m.get("value")}
            preference_keys = ["preferred_duration", "preferred_experience", "preferred_location"]
            preference_parts = [f"{key}={values[key]}" for key in preference_keys if values.get(key)]
            parts: list[str] = []
            if preference_parts:
                parts.append(f"Client preferences: {', '.join(preference_parts)}.")
            name = values.get("client_name")
            if name:
                parts.append(f"Name: {name}.")
            extras = [
                f"{m['key']}={_sanitize_value(m['value'])}"
                for m in memories
                if m.get("key") not in set(preference_keys + ["client_name"])
            ]
            if extras:
                parts.append(f"Known details: {', '.join(extras[:2])}.")
            return " ".join(parts).strip()
        except Exception as e:
            logger.warning("client memory prompt formatting failed: %s", e)
            return ""

    def extract_from_booking(self, phone_number: str, booking_data: dict[str, Any]) -> int:
        if not self.db or not (phone_number or "").strip() or not isinstance(booking_data, dict):
            return 0
        try:
            extracted: list[tuple[str, str, float]] = []
            duration = booking_data.get("duration") or booking_data.get("duration_minutes")
            if duration not in (None, "", []):
                duration_text = str(duration).strip()
                if duration_text.isdigit():
                    duration_text = f"{duration_text}min"
                extracted.append(("preferred_duration", duration_text, 0.95))
            experience = booking_data.get("experience_type") or booking_data.get("experience")
            if experience:
                extracted.append(("preferred_experience", str(experience).strip().upper(), 0.9))
            location = booking_data.get("incall_outcall") or booking_data.get("booking_type") or booking_data.get("type")
            if location:
                location_text = str(location).strip().lower()
                if location_text in {"incall", "outcall"}:
                    extracted.append(("preferred_location", location_text, 0.9))
            client_name = booking_data.get("client_name") or booking_data.get("name")
            if client_name:
                # Sanitize and cap length to prevent prompt injection via stored names
                safe_name = _sanitize_value(str(client_name).strip(), max_len=_MAX_CLIENT_NAME_LEN)
                if safe_name:
                    extracted.append(("client_name", safe_name, 1.0))
            stored = 0
            for key, value, conf in extracted:
                if value and self.upsert_memory(phone_number, key, value, source="booking", confidence=conf):
                    stored += 1
            return stored
        except Exception as e:
            logger.warning("client memory booking extraction failed: %s", e)
            return 0
