from typing import Any

from services.semantic_memory_service import SemanticMemoryService


class _FakeDB:
    def __init__(self):
        self.mem = []

    def execute_query(self, query, params: Any = None, fetch=False, **_kwargs):
        q = str(query)
        if "INSERT INTO semantic_memory" in q:
            self.mem.append({"phone_number": params[0], "memory_text": params[2]})
            return None
        if "FROM semantic_memory" in q and fetch:
            phone = params[0]
            rows = [m for m in self.mem if m["phone_number"] == phone]
            return [{"memory_text": r["memory_text"]} for r in rows]
        return [] if fetch else None


def test_semantic_memory_store_and_lookup():
    db = _FakeDB()
    svc = SemanticMemoryService(db)
    assert svc.store_memory(
        phone_number="+611234",
        memory_type="message_observation",
        memory_text="Client prefers late evening bookings",
        metadata={"intent": "book_appointment"},
    )
    snippets = svc.get_relevant_snippets(phone_number="+611234", query_text="evening", limit=3)
    assert snippets
    assert "evening" in snippets[0].lower()
