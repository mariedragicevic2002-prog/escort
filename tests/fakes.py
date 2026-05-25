"""
In-memory fakes for the booking test suite.

These are deliberately narrow: each one implements only the surface area the code-under-test
actually calls. If a test needs a method that isn't here, add it — don't reach for a real
library.

Nothing here touches the network, Google, or Postgres. If a fake is calling out to a service,
that's a bug in the fake, not a fact about production.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any


class FakeDB:
    """
    Tiny stub of services.database_service — only .execute_query is implemented because that
    is the one method the booking code actually uses at runtime.

    Tests either:
      - push a canned result with `enqueue_result(...)` before the call, or
      - register a handler with `set_handler(substring, fn)` that matches on a query fragment
        and returns (or raises) whatever the test needs.
    """

    def __init__(self) -> None:
        self._results: list[Any] = []
        self._handlers: list[tuple[str, Any]] = []
        self.calls: list[tuple[str, tuple]] = []

    def enqueue_result(self, result: Any) -> None:
        self._results.append(result)

    @contextmanager
    def transaction(self):
        """No-op transactional context (tests ignore ``conn`` on execute_query)."""
        yield self

    def set_handler(self, query_fragment: str, handler) -> None:
        """handler is called with (query, params) and its return value is returned to caller."""
        self._handlers.append((query_fragment, handler))

    def execute_query(self, query, params=(), fetch=None, conn=None, **kwargs):
        _ = (fetch, conn, kwargs)
        query_str = str(query)
        self.calls.append((query_str, tuple(params) if params else ()))
        for fragment, handler in self._handlers:
            if fragment in query_str:
                return handler(query_str, params)
        if self._results:
            return self._results.pop(0)
        return []


class FakeStateManager:
    """
    Drop-in for core.state_manager.StateManager that stores state in a dict.

    Use this when the test needs to assert what was written, without exercising StateManager's
    SQL-composition path. For tests that *do* want to exercise real transition logic, use the
    real StateManager wired to a FakeDB.
    """

    def __init__(self, initial: dict | None = None) -> None:
        self.states: dict[str, dict] = {}
        if initial:
            for phone, state in initial.items():
                self.states[phone] = {"version": 1, "current_state": "NEW", **state}
        self.updates: list[tuple[str, dict]] = []
        self.transitions: list[tuple[str, str, dict]] = []
        self.blocks: list[tuple[str, str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self._confirmation_claims: set[tuple[str, str]] = set()

    def get_state(self, phone_number: str, conn=None) -> dict | None:
        _ = conn
        state = self.states.get(phone_number)
        return dict(state) if state else None

    def get_or_create_context(self, phone_number: str) -> dict:
        if phone_number not in self.states:
            self.states[phone_number] = {
                "phone_number": phone_number,
                "current_state": "NEW",
                "version": 1,
            }
        return dict(self.states[phone_number])

    def update_fields(self, phone_number: str, updates: dict, conn=None) -> bool:
        _ = conn
        self.updates.append((phone_number, dict(updates)))
        self.states.setdefault(phone_number, {"phone_number": phone_number, "current_state": "NEW", "version": 1})
        self.states[phone_number].update(updates)
        self.states[phone_number]["version"] = self.states[phone_number].get("version", 1) + 1
        return True

    def transition(self, phone_number: str, new_state: str, updates: dict | None = None, conn=None, force: bool = False) -> bool:
        _ = (conn, force)
        self.transitions.append((phone_number, new_state, dict(updates or {})))
        self.states.setdefault(phone_number, {"phone_number": phone_number, "current_state": "NEW", "version": 1})
        self.states[phone_number]["current_state"] = new_state
        if updates:
            self.states[phone_number].update(updates)
        self.states[phone_number]["version"] = self.states[phone_number].get("version", 1) + 1
        return True

    def claim_confirmation_token_status(self, phone_number: str, token: str) -> str:
        key = (phone_number, token)
        if key in self._confirmation_claims:
            return "duplicate"
        self._confirmation_claims.add(key)
        return "claimed"

    def claim_confirmation_token(self, phone_number: str, token: str) -> bool:
        return self.claim_confirmation_token_status(phone_number, token) == "claimed"

    def release_confirmation_token(self, phone_number: str, token: str) -> bool:
        key = (phone_number, token)
        if key in self._confirmation_claims:
            self._confirmation_claims.remove(key)
            return True
        return False

    def clear_booking(self, phone_number: str) -> bool:
        """Mirror StateManager.clear_booking reset paths exercised by cancel/refuse handlers."""
        self.states.setdefault(phone_number, {"phone_number": phone_number, "current_state": "NEW", "version": 1})
        st = self.states[phone_number]
        st["current_state"] = "NEW"
        wipe = {
            "date": None,
            "time": None,
            "duration": None,
            "experience_type": None,
            "incall_outcall": None,
            "outcall_address": None,
            "client_name": None,
            "missing_fields": ["date", "time", "duration"],
            "deposit_required": False,
            "deposit_amount": None,
            "deposit_reason": None,
            "deposit_requested_at": None,
            "deposit_payment_reference": None,
            "deposit_screenshot_attempts": 0,
            "deposit_paid": False,
            "peacock_event_id": None,
            "graphite_event_id": None,
            "confirmed_event_id": None,
            "travel_outbound_event_id": None,
            "travel_return_event_id": None,
            "confirmed_at": None,
            "total_booking_cost": None,
            "available_now_requested": False,
            "arrival_time_minutes": None,
            "outcall_awaiting_yes": False,
            "incall_awaiting_yes": False,
            "awaiting_booking_change_cancel_choice": False,
            "awaiting_name": False,
            "message_count": 0,
            "booking_status": None,
            "booking_type": None,
            "first_contact_sent": False,
            "feedback_request_sent": False,
            "optional_deposit_requested": False,
            "optional_deposit_paid": False,
            "calendar_yes_degraded": False,
        }
        st.update(wipe)
        st["version"] = st.get("version", 1) + 1
        return True

    def block_client(self, phone_number: str, reason: str, notes: str = "") -> bool:
        self.blocks.append((phone_number, reason, notes))
        return True

    def touch(self, phone_number: str, conn=None) -> bool:
        _ = (phone_number, conn)
        return True

    def log_message(self, phone_number, direction, message_body, media_urls=None, intent=None, conn=None):
        _ = (media_urls, intent, conn)
        self.messages.append((phone_number, direction, message_body))
        return True

    def log_inbound_and_touch(self, phone_number, message_body, media_urls=None, intent=None):
        self.log_message(phone_number, "inbound", message_body, media_urls, intent=intent)
        return self.touch(phone_number)

    def get_booking_fields(self, phone_number: str) -> dict[str, Any]:
        """Mirror core.state_manager.StateManager.get_booking_fields for handler parity in tests/sim."""
        state = self.get_state(phone_number)
        if not state:
            return {}
        return {
            "date": state.get("date"),
            "time": state.get("time"),
            "duration": state.get("duration"),
            "experience_type": state.get("experience_type"),
            "incall_outcall": state.get("incall_outcall"),
            "outcall_address": state.get("outcall_address"),
            "client_name": state.get("client_name"),
            "booking_type": state.get("booking_type"),
            "booking_status": state.get("booking_status"),
            "escort_supply_source": state.get("escort_supply_source"),
            "mmf_exploration_tags": state.get("mmf_exploration_tags"),
            "dinner_restaurant": state.get("dinner_restaurant"),
            "dinner_after_preference": state.get("dinner_after_preference"),
            "dinner_client_address": state.get("dinner_client_address"),
            "dinner_client_outside_15km": state.get("dinner_client_outside_15km"),
        }


def make_calendar_event(
    color_id: str | None = None,
    summary: str = "Booking",
    description: str = "",
    start: str = "2026-05-01T10:00:00+10:00",
    end: str = "2026-05-01T11:00:00+10:00",
) -> dict:
    """Build a Google Calendar event dict in the shape services.calendar.* expects."""
    ev: dict[str, Any] = {
        "summary": summary,
        "description": description,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if color_id is not None:
        ev["colorId"] = color_id
    return ev


class FakeCalendarService:
    """
    Quacks like a googleapiclient Calendar service for the subset check_conflict calls:
        service.events().list(...).execute() -> {"items": [...]}
    """

    def __init__(self, events: list[dict] | None = None) -> None:
        self._events = list(events or [])

    def set_events(self, events: list[dict]) -> None:
        self._events = list(events)

    def events(self):
        return self

    def list(self, **_kwargs):
        return self

    def execute(self):
        return {"items": list(self._events)}
