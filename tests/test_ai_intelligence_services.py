from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from core.client_profile import build_client_profile_with_memory, profile_to_prompt_snippet
from services.ai_call_log_service import AICallLogService
from services.ai_service import AIService
from services.client_memory_service import ClientMemoryService
from services.episodic_memory_service import EpisodicMemoryService
from services.model_router import MessageComplexity, ModelRouter, classify_complexity, get_routed_provider


class _ClientMemoryDB:
    def __init__(self):
        self.rows = {}
        self.counter = 0

    def execute_query(self, query, params=(), fetch=False, **_kwargs):
        q = str(query)
        if "CREATE TABLE IF NOT EXISTS client_memory" in q or "idx_client_memory_phone" in q:
            return None
        if "INSERT INTO client_memory" in q:
            phone, key, value, source, confidence = params[:5]
            self.counter += 1
            self.rows[(phone, key)] = {
                "memory_key": key,
                "memory_value": value,
                "source": source,
                "confidence": confidence,
                "last_seen_at": self.counter,
            }
            return None
        if "SELECT memory_key, memory_value, source, confidence" in q:
            phone, limit = params
            rows = [
                row for (row_phone, _), row in self.rows.items() if row_phone == phone
            ]
            rows.sort(key=lambda row: row["last_seen_at"], reverse=True)
            return rows[:limit]
        if "DELETE FROM client_memory" in q:
            phone, key = params
            self.rows.pop((phone, key), None)
            return None
        return [] if fetch else None


class _BookingsDB:
    def __init__(self, rows):
        self.rows = rows

    def execute_query(self, query, params=(), fetch=False, **_kwargs):
        q = str(query)
        if "FROM bookings" in q and fetch:
            phone = params[0]
            if "phone_number" in q:
                return [row for row in self.rows if row.get("phone_number") == phone][:1]
            return [row for row in self.rows if row.get("phone") == phone][:1]
        return [] if fetch else None


class _AICallLogDB:
    def __init__(self):
        self.rows = []

    def execute_query(self, query, params=(), fetch=False, **_kwargs):
        q = str(query)
        if "CREATE TABLE IF NOT EXISTS ai_call_log" in q or "idx_ai_call_log_" in q:
            return None
        if "INSERT INTO ai_call_log" in q:
            self.rows.append(
                {
                    "phone_hash": params[0],
                    "model": params[1],
                    "call_type": params[2],
                    "input_tokens": params[3],
                    "output_tokens": params[4],
                    "cost_usd": params[5],
                    "latency_ms": params[6],
                    "prompt_version": params[7],
                    "created_at": datetime.now(timezone.utc),
                }
            )
            return None
        if "COUNT(*) AS total_calls" in q:
            return [
                {
                    "total_cost_usd": sum(row["cost_usd"] for row in self.rows),
                    "total_calls": len(self.rows),
                    "total_input_tokens": sum(row["input_tokens"] for row in self.rows),
                    "total_output_tokens": sum(row["output_tokens"] for row in self.rows),
                }
            ]
        if "GROUP BY model" in q:
            grouped = {}
            for row in self.rows:
                agg = grouped.setdefault(
                    row["model"],
                    {"model": row["model"], "calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0},
                )
                agg["calls"] += 1
                agg["cost_usd"] += row["cost_usd"]
                agg["input_tokens"] += row["input_tokens"]
                agg["output_tokens"] += row["output_tokens"]
            return list(grouped.values())
        if "GROUP BY DATE(created_at)" in q:
            today = datetime.now(timezone.utc).date().isoformat()
            return [{"date": today, "cost_usd": sum(row["cost_usd"] for row in self.rows), "calls": len(self.rows)}]
        return [] if fetch else None


class _StubClientMemoryService:
    def get_memories(self, phone_number: str, limit: int = 5):
        _ = (phone_number, limit)
        return [{"key": "preferred_duration", "value": "60min", "source": "booking", "confidence": 0.95}]

    def format_for_prompt(self, phone_number: str) -> str:
        _ = phone_number
        return "Client preferences: preferred_duration=60min."


def test_client_memory_service_round_trip_and_prompt_formatting():
    db = _ClientMemoryDB()
    svc = ClientMemoryService(db)

    assert svc.upsert_memory("+611234", "preferred_duration", "60min", source="booking", confidence=0.9)
    assert svc.upsert_memory("+611234", "preferred_experience", "GFE")
    assert svc.upsert_memory("+611234", "client_name", "Jane")

    memories = svc.get_memories("+611234")
    assert len(memories) == 3
    assert svc.extract_from_booking(
        "+611234",
        {"duration": 90, "experience_type": "pse", "incall_outcall": "incall", "client_name": "Jane"},
    ) == 4
    prompt = svc.format_for_prompt("+611234")
    assert "Client preferences:" in prompt
    assert "Name: Jane." in prompt
    assert svc.delete_memory("+611234", "client_name")


def test_episodic_memory_service_detects_repeat_intent_and_formats_last_booking():
    svc = EpisodicMemoryService(
        _BookingsDB(
            [
                {
                    "phone_number": "+611111",
                    "event_date": "2026-05-10",
                    "event_time": "19:30",
                    "duration_minutes": 60,
                    "experience_type": "GFE",
                    "booking_type": "incall",
                    "outcall_address": None,
                    "status": "confirmed",
                    "created_at": datetime.now(timezone.utc) - timedelta(days=1),
                }
            ]
        )
    )

    assert svc.detect_repeat_intent("same as last time please") is True
    context = svc.get_episodic_context("+611111", "book again, same as last time")
    assert "Your last booking:" in context
    assert "60min" in context


def test_model_router_classifies_and_respects_shadow_mode(monkeypatch):
    assert classify_complexity("yes") == MessageComplexity.TRIVIAL
    assert classify_complexity("I FEEL UNSAFE!!") == MessageComplexity.COMPLEX
    assert get_routed_provider(MessageComplexity.TRIVIAL, "claude") == ("gemini", "gemini-2.5-flash")

    shadow_router = ModelRouter(shadow_mode=True)
    assert shadow_router.route("yes", configured_provider="claude") == ("claude", "")

    monkeypatch.setenv("MODEL_ROUTING_SHADOW", "false")
    live_router = ModelRouter()
    assert live_router.route("yes", configured_provider="claude") == ("gemini", "gemini-2.5-flash")


def test_ai_call_log_service_records_cost_summaries():
    db = _AICallLogDB()
    svc = AICallLogService(db)

    assert svc.log_call(
        phone_number="+611234",
        model="gemini-2.5-flash",
        call_type="chat",
        input_tokens=1000,
        output_tokens=500,
        latency_ms=42,
    )
    summary = svc.get_daily_cost()
    assert summary["total_calls"] == 1
    assert summary["total_input_tokens"] == 1000
    assert summary["by_model"]["gemini-2.5-flash"]["calls"] == 1
    assert summary["total_cost_usd"] > 0
    assert svc.get_daily_cost_by_day(days=7)[0]["calls"] == 1


def test_client_profile_with_memory_and_episodic_snippets_are_prompted():
    profile = build_client_profile_with_memory(
        {"client_name": "Jane", "current_state": "NEW", "phone_number": "+611234"},
        {"total_bookings": 2},
        client_memory_service=_StubClientMemoryService(),
        phone_number="+611234",
    )
    profile["episodic_prompt_snippet"] = "Your last booking: 2026-05-10 at 19:30, 60min GFE incall."
    snippet = profile_to_prompt_snippet(profile)
    assert "Client preferences: preferred_duration=60min." in snippet
    assert "Your last booking:" in snippet


def test_ai_service_extraction_parser_adds_confidence_metadata():
    svc = AIService()
    parsed = svc._parse_extraction_result(  # noqa: SLF001
        json.dumps(
            {
                "date": "2026-05-10",
                "time": [19, 30],
                "duration": 60,
                "experience_type": "GFE",
                "incall_outcall": "incall",
                "outcall_address": None,
                "date_confidence": 0.92,
                "time_confidence": 0.55,
                "duration_confidence": 0.88,
                "experience_type_confidence": 0.95,
                "incall_outcall_confidence": 0.91,
                "outcall_address_confidence": 0.1,
            }
        ),
        datetime.now(timezone.utc),
    )
    assert parsed["_confidence"]["date"] == 0.92
    assert "time" in parsed["_low_confidence_fields"]
    assert parsed["experience_type"] == "GFE"
