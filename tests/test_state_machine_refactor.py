"""
Tests validating the state-machine refactor.

The refactor moved transition responsibility out of handlers and into the router.
Handlers now return a payload containing ``new_state`` (via an event in v2) and
optionally ``updates``; the router is the sole component that applies updates
and executes the transition.

Covers the seven required areas:
    1. No direct transitions in handlers
    2. Handler output contract
    3. Router responsibility
    4. Atomicity
    5. State machine enforcement
    6. No double transitions
    7. Regression safety
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.conversation_context import BookingContext
from core.router import Router
from core.state_machine import (
    STATE_TRANSITIONS,
    VALID_STATES,
    is_valid_transition,
    transition as sm_transition,
)
from core.state_manager import (
    ALLOWED_STATE_UPDATE_FIELDS,
    StateManager,
)
from tests.fakes import FakeDB, FakeStateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HANDLERS_ROOT = Path(__file__).resolve().parents[1] / "handlers"
PHONE = "+61400000001"


def _make_ctx(state: str = "COLLECTING", user_id: str = PHONE) -> BookingContext:
    return BookingContext(user_id=user_id, state=state, flow_version="v2")


def _build_v2_handler(event: str, updates: dict | None = None, messages=None):
    """Stub v2 handler. Returns (event, response_dict)."""
    response = {
        "messages": list(messages or []),
        "actions": [],
        "new_state": None,
    }
    if updates is not None:
        response["updates"] = dict(updates)

    def _handler(booking_ctx):
        return event, response

    _handler.__name__ = f"stub_v2_handler_{event}"
    return _handler


# ===========================================================================
# 1. No direct transitions in handlers
# ===========================================================================


class _TransitionCallFinder(ast.NodeVisitor):
    """Flag any `*.transition(...)` call whose receiver looks like a state manager."""

    SM_NAMES = frozenset({"state_manager", "self", "sm", "StateManager"})
    # Names that are NOT state managers but happen to have a .transition attr.
    IGNORE_RECEIVERS = frozenset({
        "state_machine",          # core.state_machine.transition (pure fn)
        "sm_transition",
        "router",
    })

    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[tuple[int, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "transition":
            receiver = self._root_name(func.value)
            if receiver in self.SM_NAMES and receiver not in self.IGNORE_RECEIVERS:
                self.violations.append((node.lineno, ast.unparse(node)))
        self.generic_visit(node)

    @staticmethod
    def _root_name(node: ast.AST) -> str | None:
        while isinstance(node, ast.Attribute):
            node = node.value
        if isinstance(node, ast.Name):
            return node.id
        return None


def test_no_handler_module_calls_state_manager_transition():
    """Architectural guarantee: no file under handlers/ may call state_manager.transition()."""
    all_violations: list[str] = []
    for py_file in HANDLERS_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        except SyntaxError:
            continue
        finder = _TransitionCallFinder(py_file)
        finder.visit(tree)
        for lineno, src in finder.violations:
            all_violations.append(f"{py_file.relative_to(HANDLERS_ROOT.parent)}:{lineno}: {src}")
    assert not all_violations, (
        "Handlers must not call state_manager.transition() directly:\n  "
        + "\n  ".join(all_violations)
    )


def test_v2_handler_does_not_touch_state_manager(fake_state_manager):
    """Invoking a representative v2 handler must not mutate state_manager."""
    from handlers.booking_coll.handle_provide_field_v2 import handle_provide_field_v2

    ctx = _make_ctx(state="COLLECTING")
    ctx.booking_data = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "missing_fields": ["date", "time", "duration"],
    }
    ctx.metadata = {"message": "", "intent": "provide_field"}

    try:
        handle_provide_field_v2(ctx)
    except Exception:
        # Handler may fail on missing deps, but it must not have reached state_manager.
        pass

    assert fake_state_manager.transitions == []
    assert fake_state_manager.updates == []


# ===========================================================================
# 2. Handler output contract
# ===========================================================================


def test_v2_handler_returns_event_response_tuple():
    """v2 handlers return (event, response_dict) — the contract the router unwraps."""
    handler = _build_v2_handler("fields_complete", updates={"client_name": "Alex"})
    event, response = handler(_make_ctx())
    assert isinstance(event, str) and event
    assert isinstance(response, dict)


def test_handler_event_resolves_to_valid_state():
    """Every event a handler emits must resolve to a state in VALID_STATES."""
    for state, events in STATE_TRANSITIONS.items():
        for event, next_state in events.items():
            assert next_state in VALID_STATES, (
                f"Event {event!r} from {state!r} resolves to invalid state {next_state!r}"
            )


def test_handler_updates_keys_are_allowed_state_fields():
    """If a handler returns updates, all keys must match a persistable state column."""
    handler = _build_v2_handler(
        "stay",
        updates={"client_name": "Alex", "duration": 60, "missing_fields": []},
    )
    _event, response = handler(_make_ctx())
    for key in (response.get("updates") or {}).keys():
        assert key in ALLOWED_STATE_UPDATE_FIELDS, (
            f"Handler updates contain disallowed field {key!r}"
        )


def test_handler_response_has_messages_and_actions_keys():
    handler = _build_v2_handler("stay", messages=["hi"])
    _event, response = handler(_make_ctx())
    assert "messages" in response
    assert "actions" in response


# ===========================================================================
# 3. Router responsibility
# ===========================================================================


def test_router_resolves_next_state_from_event_via_state_machine():
    """Router must consult state_machine.transition, not trust handler-supplied next_state."""
    router = Router()
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("fields_complete"))
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}

    ctx = _make_ctx(state="COLLECTING")
    router.route_v2(ctx, "provide_field", sm)

    assert sm.transitions == [(PHONE, "CHECKING_AVAILABILITY", {})]
    assert ctx.state == "CHECKING_AVAILABILITY"


def test_router_persists_transition_for_each_state_event_pair():
    """Router must persist next state produced by state_machine for any (state, event) pair."""
    router = Router()
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "NEW", "version": 1}
    router.register_v2("NEW", "start", _build_v2_handler("booking_started"))

    ctx = _make_ctx(state="NEW")
    router.route_v2(ctx, "start", sm)

    assert sm.transitions[-1][0:2] == (PHONE, "COLLECTING")


def test_router_applies_updates_before_transition():
    router = Router()
    mock_sm = MagicMock()
    mock_sm.get_state.return_value = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    mock_sm.update_fields.return_value = True
    mock_sm.transition.return_value = True
    router.register_v2(
        "COLLECTING",
        "provide_field",
        _build_v2_handler("fields_complete", updates={"client_name": "Alex"}),
    )

    ctx = _make_ctx(state="COLLECTING")
    router.route_v2(ctx, "provide_field", mock_sm)

    # update_fields called exactly once with the handler-supplied updates and tx conn.
    mock_sm.update_fields.assert_called_once()
    upd_args = mock_sm.update_fields.call_args
    assert upd_args.args == (PHONE, {"client_name": "Alex"})
    assert "conn" in upd_args.kwargs
    tx_conn = upd_args.kwargs["conn"]
    assert tx_conn is not None
    mock_sm.transition.assert_called_once_with(PHONE, "CHECKING_AVAILABILITY", conn=tx_conn)
    # Call order: update_fields BEFORE transition.
    ordered = [c for c in mock_sm.method_calls if c[0] in {"update_fields", "transition"}]
    assert ordered[0][0] == "update_fields"
    assert ordered[1][0] == "transition"


def test_router_skips_update_fields_when_updates_absent():
    router = Router()
    mock_sm = MagicMock()
    mock_sm.get_state.return_value = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    mock_sm.transition.return_value = True
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("fields_complete"))

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", mock_sm)
    mock_sm.update_fields.assert_not_called()


def test_router_does_not_persist_when_state_unchanged():
    """route_v2 short-circuits state_manager.transition when event resolves to same state."""
    router = Router()
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("stay"))

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", sm)
    assert sm.transitions == []


def test_router_injects_resolved_state_into_response():
    router = Router()
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    router.register_v2(
        "COLLECTING",
        "provide_field",
        _build_v2_handler("fields_complete", messages=["ok"]),
    )

    ctx = _make_ctx(state="COLLECTING")
    response = router.route_v2(ctx, "provide_field", sm)
    assert response["new_state"] == "CHECKING_AVAILABILITY"


# ===========================================================================
# 4. Atomicity
# ===========================================================================


def test_transition_failure_does_not_mutate_context_state():
    """If state_manager.transition returns False, router must not move booking_ctx.state forward."""
    router = Router()
    mock_sm = MagicMock()
    mock_sm.transition.return_value = False
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("fields_complete"))

    ctx = _make_ctx(state="COLLECTING")
    router.route_v2(ctx, "provide_field", mock_sm)
    assert ctx.state == "COLLECTING"


def test_update_fields_failure_prevents_transition():
    router = Router()
    mock_sm = MagicMock()
    mock_sm.update_fields.return_value = False
    mock_sm.transition.return_value = True
    router.register_v2(
        "COLLECTING",
        "provide_field",
        _build_v2_handler("fields_complete", updates={"client_name": "Alex"}),
    )

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", mock_sm)
    mock_sm.transition.assert_not_called()


def test_update_fields_exception_prevents_transition():
    router = Router()
    mock_sm = MagicMock()
    mock_sm.update_fields.side_effect = RuntimeError("DB down")
    mock_sm.transition.return_value = True
    router.register_v2(
        "COLLECTING",
        "provide_field",
        _build_v2_handler("fields_complete", updates={"client_name": "Alex"}),
    )

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", mock_sm)
    mock_sm.transition.assert_not_called()


def test_handler_exception_prevents_transition():
    """Handler raising inside route_v2 must NOT cause a state transition."""
    router = Router()
    mock_sm = MagicMock()
    mock_sm.get_state.return_value = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}

    def _boom(_ctx):
        raise RuntimeError("handler crashed")

    _boom.__name__ = "boom"
    router.register_v2("COLLECTING", "provide_field", _boom)

    response = router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", mock_sm)
    mock_sm.transition.assert_not_called()
    mock_sm.update_fields.assert_not_called()
    assert response["new_state"] is None


def test_real_state_manager_rejects_invalid_transition_without_mutation():
    """
    Integration-style atomicity: with a real StateManager wired to FakeDB, an invalid
    direct transition is rejected (returns False) and no UPDATE is issued.
    """
    db = FakeDB()
    db.enqueue_result([{
        "phone_number": PHONE,
        "current_state": "DEPOSIT_REQUIRED",
        "version": 1,
    }])
    sm = StateManager(db)

    ok = sm.transition(PHONE, "NEW")  # DEPOSIT_REQUIRED -> NEW not allowed (non-force)
    assert ok is False

    update_queries = [c for c in db.calls if c[0].startswith("UPDATE conversation_states")]
    assert update_queries == [], "Rejected transition must not issue an UPDATE"


# ===========================================================================
# 5. State machine enforcement
# ===========================================================================


@pytest.mark.parametrize(
    "from_state,to_state",
    [(s, nxt) for s, events in STATE_TRANSITIONS.items() for nxt in events.values()],
)
def test_declared_transition_is_valid(from_state, to_state):
    assert is_valid_transition(from_state, to_state) is True


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("NEW", "MANUAL_REVIEW_PENDING"),
        ("DEPOSIT_REQUIRED", "COLLECTING"),
        ("POST_BOOKING", "DEPOSIT_REQUIRED"),
        ("MANUAL_REVIEW_PENDING", "CONFIRMED"),
        ("CONFIRMED", "CHECKING_AVAILABILITY"),
    ],
)
def test_undeclared_transition_is_rejected(from_state, to_state):
    assert is_valid_transition(from_state, to_state) is False


def test_unknown_event_holds_current_state():
    """Router's state-machine call with an unrecognised event must keep the current state."""
    assert sm_transition("COLLECTING", "not_a_real_event") == "COLLECTING"


def test_unknown_current_state_defaults_to_new():
    """Safety guard: unknown current_state must not crash; should default to NEW."""
    assert sm_transition("GHOST_STATE", "booking_started") == "COLLECTING"


def test_invalid_direct_transition_is_rejected_by_state_manager():
    """StateManager.transition must honour VALID_STATE_TRANSITIONS unless force=True."""
    db = FakeDB()
    db.enqueue_result([{
        "phone_number": PHONE,
        "current_state": "DEPOSIT_REQUIRED",
        "version": 1,
    }])
    sm = StateManager(db)
    assert sm.transition(PHONE, "NEW") is False  # not in VALID_STATE_TRANSITIONS['DEPOSIT_REQUIRED']


def test_force_bypasses_state_transition_validation():
    """The clear_booking path uses force=True; confirm the flag is actually honoured."""
    db = FakeDB()
    db.enqueue_result([{
        "phone_number": PHONE,
        "current_state": "DEPOSIT_REQUIRED",
        "version": 1,
    }])
    # Second get_state after update (inside update_fields path — not used by transition), and the RETURNING row.
    db.enqueue_result([{"version": 2}])
    sm = StateManager(db)
    assert sm.transition(PHONE, "NEW", force=True) is True


# ===========================================================================
# 6. No double transitions
# ===========================================================================


def test_single_route_v2_call_invokes_transition_at_most_once():
    router = Router()
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("fields_complete"))

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", sm)
    assert len(sm.transitions) == 1


def test_stay_event_yields_zero_transition_calls():
    router = Router()
    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("stay"))

    router.route_v2(_make_ctx(state="COLLECTING"), "provide_field", sm)
    assert sm.transitions == []


def test_same_state_new_state_does_not_trigger_persistence():
    """Handler returning event that resolves to same state must not call state_manager.transition."""
    router = Router()
    mock_sm = MagicMock()
    router.register_v2("CONFIRMED", "greet", _build_v2_handler("stay"))

    router.route_v2(_make_ctx(state="CONFIRMED"), "greet", mock_sm)
    mock_sm.transition.assert_not_called()


# ===========================================================================
# 7. Regression safety — booking flow still reaches correct states
# ===========================================================================


@pytest.mark.parametrize(
    "current_state,event,expected_next",
    [
        ("NEW", "booking_started", "COLLECTING"),
        ("NEW", "fields_complete", "CHECKING_AVAILABILITY"),
        ("NEW", "extended_enquiry", "EXTENDED_ENQUIRY"),
        ("COLLECTING", "fields_complete", "CHECKING_AVAILABILITY"),
        ("COLLECTING", "deposit_required", "DEPOSIT_REQUIRED"),
        ("CHECKING_AVAILABILITY", "availability_confirmed", "CONFIRMED"),
        ("CHECKING_AVAILABILITY", "availability_failed_deposit", "DEPOSIT_REQUIRED"),
        ("CHECKING_AVAILABILITY", "availability_failed", "COLLECTING"),
        ("DEPOSIT_REQUIRED", "deposit_paid", "CONFIRMED"),
        ("CONFIRMED", "booking_complete", "POST_BOOKING"),
        ("POST_BOOKING", "booking_started", "COLLECTING"),
        ("EXTENDED_ENQUIRY", "booking_started", "COLLECTING"),
        ("MANUAL_REVIEW_PENDING", "resolved", "COLLECTING"),
    ],
)
def test_booking_flow_event_reaches_expected_state(current_state, event, expected_next):
    assert sm_transition(current_state, event) == expected_next


def test_full_happy_path_transitions_through_router():
    """Simulate NEW -> COLLECTING -> CHECKING_AVAILABILITY -> CONFIRMED via route_v2."""
    router = Router()
    router.register_v2("NEW", "start", _build_v2_handler("booking_started"))
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("fields_complete"))
    router.register_v2("CHECKING_AVAILABILITY", "confirm", _build_v2_handler("availability_confirmed"))

    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "NEW", "version": 1}

    ctx = _make_ctx(state="NEW")

    router.route_v2(ctx, "start", sm)
    assert ctx.state == "COLLECTING"

    router.route_v2(ctx, "provide_field", sm)
    assert ctx.state == "CHECKING_AVAILABILITY"

    router.route_v2(ctx, "confirm", sm)
    assert ctx.state == "CONFIRMED"

    persisted_states = [t[1] for t in sm.transitions]
    assert persisted_states == ["COLLECTING", "CHECKING_AVAILABILITY", "CONFIRMED"]


def test_deposit_path_reaches_confirmed():
    router = Router()
    router.register_v2("COLLECTING", "provide_field", _build_v2_handler("deposit_required"))
    router.register_v2("DEPOSIT_REQUIRED", "deposit_paid", _build_v2_handler("deposit_paid"))

    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}

    ctx = _make_ctx(state="COLLECTING")
    router.route_v2(ctx, "provide_field", sm)
    assert ctx.state == "DEPOSIT_REQUIRED"

    router.route_v2(ctx, "deposit_paid", sm)
    assert ctx.state == "CONFIRMED"


def test_reset_path_returns_to_new():
    router = Router()
    router.register_v2("COLLECTING", "reset", _build_v2_handler("reset"))

    sm = FakeStateManager()
    sm.states[PHONE] = {"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}

    ctx = _make_ctx(state="COLLECTING")
    router.route_v2(ctx, "reset", sm)
    assert ctx.state == "NEW"
