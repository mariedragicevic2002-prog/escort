"""
Episodic memory service.

Retrieves the most recent completed booking for a client to handle
"same as last time" or "my usual" references in conversation.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

logger = logging.getLogger("adella_chatbot.episodic_memory")

_REPEAT_PATTERNS = (
    r"\bsame as last time\b",
    r"\bmy usual\b",
    r"\busual booking\b",
    r"\bsame as before\b",
    r"\brepeat(?: that| it)?\b",
    r"\bbook again\b",
    r"\bsame booking\b",
)


class EpisodicMemoryService:
    def __init__(self, db_service):
        self.db = db_service

    @staticmethod
    def _row_get(row: dict[str, Any] | None, *keys: str) -> Any:
        row = row or {}
        for key in keys:
            if key in row and row.get(key) not in (None, ""):
                return row.get(key)
        return None

    @staticmethod
    def _parse_duration(value: Any) -> Any:
        if value in (None, "", []):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        match = re.search(r"(\d+)", text)
        return int(match.group(1)) if match else text

    @staticmethod
    def _parse_date(value: Any) -> Any:
        if value in (None, "", []):
            return None
        if isinstance(value, datetime):
            return value.date().isoformat()
        return str(value).strip()

    @staticmethod
    def _parse_time(value: Any) -> Any:
        if value in (None, "", []):
            return None
        if isinstance(value, datetime):
            return value.strftime("%H:%M")
        return str(value).strip()

    def get_last_booking(self, phone_number: str) -> dict[str, Any] | None:
        if not self.db or not (phone_number or "").strip():
            return None
        queries = [
            "SELECT * FROM bookings WHERE phone_number = %s AND status IN ('confirmed', 'completed') ORDER BY created_at DESC LIMIT 1",
            "SELECT * FROM bookings WHERE phone = %s AND status IN ('confirmed', 'completed') ORDER BY created_at DESC LIMIT 1",
            "SELECT * FROM bookings WHERE phone = %s AND status IN ('confirmed', 'completed') ORDER BY start_time DESC LIMIT 1",
        ]
        try:
            row = None
            for query in queries:
                try:
                    rows = self.db.execute_query(query, ((phone_number or "").strip(),), fetch=True) or []
                except Exception:
                    continue
                if rows:
                    row = rows[0]
                    break
            if not row:
                return None
            start_time = self._row_get(row, "start_time")
            date_value = self._row_get(row, "event_date", "booking_date", "date")
            time_value = self._row_get(row, "event_time", "booking_time", "time")
            if date_value is None and isinstance(start_time, datetime):
                date_value = start_time
            if time_value is None and isinstance(start_time, datetime):
                time_value = start_time
            return {
                "date": self._parse_date(date_value),
                "time": self._parse_time(time_value),
                "duration": self._parse_duration(self._row_get(row, "duration_minutes", "duration")),
                "experience_type": self._row_get(row, "experience_type", "experience"),
                "incall_outcall": self._row_get(row, "incall_outcall", "booking_type", "type"),
                "outcall_address": self._row_get(row, "outcall_address", "address"),
            }
        except Exception as e:
            logger.warning("episodic lookup failed for %s: %s", phone_number, e)
            return None

    def format_last_booking(self, booking: dict[str, Any] | None) -> str:
        try:
            if not booking:
                return ""
            date_text = str(booking.get("date") or "").strip()
            time_text = str(booking.get("time") or "").strip()
            duration = booking.get("duration")
            experience = str(booking.get("experience_type") or "").strip()
            location = str(booking.get("incall_outcall") or "").strip()
            parts: list[str] = []
            if date_text:
                parts.append(date_text)
            if time_text:
                parts.append(f"at {time_text}")
            summary = " ".join(parts).strip()
            tail = " ".join(
                part for part in [f"{duration}min" if duration not in (None, "") else "", experience, location] if part
            ).strip()
            if summary and tail:
                return f"Your last booking: {summary}, {tail}."
            if summary:
                return f"Your last booking: {summary}."
            if tail:
                return f"Your last booking: {tail}."
            return ""
        except Exception as e:
            logger.warning("episodic format failed: %s", e)
            return ""

    def detect_repeat_intent(self, message: str) -> bool:
        try:
            text = (message or "").strip().lower()
            return bool(text and any(re.search(pattern, text, re.IGNORECASE) for pattern in _REPEAT_PATTERNS))
        except Exception as e:
            logger.warning("episodic repeat-intent detection failed: %s", e)
            return False

    def get_episodic_context(self, phone_number: str, message: str) -> str:
        try:
            if not self.detect_repeat_intent(message):
                return ""
            return self.format_last_booking(self.get_last_booking(phone_number))
        except Exception as e:
            logger.warning("episodic context build failed for %s: %s", phone_number, e)
            return ""
