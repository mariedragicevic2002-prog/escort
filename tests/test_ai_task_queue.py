from typing import Any

from services.ai_task_queue import enqueue_ai_task, process_pending_ai_tasks


class _FakeDB:
    def __init__(self):
        self.rows = []
        self.next_id = 1

    def execute_query(self, query, params: Any = None, fetch=False, **_kwargs):
        q = str(query)
        if "INSERT INTO ai_task_queue" in q:
            self.rows.append(
                {
                    "id": self.next_id,
                    "task_type": params[0],
                    "payload": {"x": 1},
                    "status": "pending",
                    "attempts": 0,
                }
            )
            self.next_id += 1
            return None
        if "SELECT id, task_type, payload, attempts" in q:
            pending = [r for r in self.rows if r["status"] == "pending"]
            return pending[: params[0]] if fetch else None
        if "SET status = 'done'" in q:
            task_id = params[0]
            for r in self.rows:
                if r["id"] == task_id:
                    r["status"] = "done"
            return None
        if "SET status = 'failed'" in q:
            task_id = params[-1]
            for r in self.rows:
                if r["id"] == task_id:
                    r["status"] = "failed"
            return None
        return [] if fetch else None


def test_ai_task_queue_enqueue_and_process():
    db = _FakeDB()
    assert enqueue_ai_task(db, task_type="semantic_memory_capture", payload={"foo": "bar"}) is True

    captured = []

    def _handler(payload, _db):
        captured.append(payload)

    done = process_pending_ai_tasks(
        db,
        handlers={"semantic_memory_capture": _handler},
        batch_size=5,
    )
    assert done == 1
    assert db.rows[0]["status"] == "done"
