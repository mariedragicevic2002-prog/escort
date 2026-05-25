from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.conversation_context import BookingContext
from core.router import Router
from core.state_manager import ALLOWED_STATE_UPDATE_FIELDS, StateManager
from core.state_machine import FsmBridgeError, target_state_to_event
from main_v2.state_machine_bridge import dispatch_message
from tests.fakes import FakeDB

PHONE = "+61400000001"
HANDLERS_ROOT = Path(__file__).resolve().parents[1] / "handlers"


def _ctx(state: str) -> BookingContext:
    return BookingContext(user_id=PHONE, state=state, flow_version="v2")


def _legacy_handler(*, new_state: str | None, updates: dict | None = None, messages: list[str] | None = None):
    payload: dict = {
        "messages": list(messages or []),
        "actions": [],
        "new_state": new_state,
    }
    if updates is not None:
        payload["updates"] = dict(updates)

    def _handler(_legacy_context):
        return dict(payload)

    return _handler


class _TrackingStateManager:
    def __init__(self, *, state: str = "COLLECTING", updates_ok: bool = True, transition_ok: bool = True):
        self.state_row = {"phone_number": PHONE, "current_state": state, "client_name": None}
        self.calls: list[str] = []
        self.updates_ok = updates_ok
        self.transition_ok = transition_ok
        self.transition_count = 0

    def update_fields(self, _phone_number: str, updates: dict) -> bool:
        self.calls.append("update_fields")
        if not self.updates_ok:
            return False
        self.state_row.update(updates)
        return True

    def transition(self, _phone_number: str, new_state: str, updates: dict | None = None) -> bool:
        self.calls.append("transition")
        self.transition_count += 1
        if updates and not self.updates_ok:
            return False
        if not self.transition_ok:
            return False
        self.state_row["current_state"] = new_state
        if updates:
            self.state_row.update(updates)
        return True


class _TransactionalStateManager:
    class _TxConn:
        def __init__(self):
            self.pending_updates: dict = {}
            self.pending_state: str | None = None

    class _TxDB:
        def __init__(self, owner: "_TransactionalStateManager"):
            self.owner = owner

        @contextmanager
        def transaction(self):
            conn = _TransactionalStateManager._TxConn()
            try:
                yield conn
            except Exception:
                self.owner.rollback_count += 1
                raise
            self.owner.commit_count += 1
            if conn.pending_updates:
                self.owner.state_row.update(conn.pending_updates)
            if conn.pending_state is not None:
                self.owner.state_row["current_state"] = conn.pending_state

    def __init__(
        self,
        *,
        state: str = "COLLECTING",
        updates_ok: bool = True,
        transition_ok: bool = True,
        update_exc: Exception | None = None,
        transition_exc: Exception | None = None,
        update_mutates_before_false: bool = False,
    ):
        self.state_row = {"phone_number": PHONE, "current_state": state, "client_name": None}
        self.calls: list[str] = []
        self.update_conns: list[object | None] = []
        self.transition_conns: list[object | None] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.updates_ok = updates_ok
        self.transition_ok = transition_ok
        self.update_exc = update_exc
        self.transition_exc = transition_exc
        self.update_mutates_before_false = update_mutates_before_false
        self.db = _TransactionalStateManager._TxDB(self)

    def update_fields(self, _phone_number: str, updates: dict, conn=None) -> bool:
        self.calls.append("update_fields")
        self.update_conns.append(conn)
        if self.update_exc is not None:
            raise self.update_exc
        if not self.updates_ok:
            if self.update_mutates_before_false:
                if conn is None:
                    self.state_row.update(updates)
                else:
                    conn.pending_updates.update(updates)
            return False
        if conn is None:
            self.state_row.update(updates)
        else:
            conn.pending_updates.update(updates)
        return True

    def transition(self, _phone_number: str, new_state: str, conn=None) -> bool:
        self.calls.append("transition")
        self.transition_conns.append(conn)
        if self.transition_exc is not None:
            raise self.transition_exc
        if not self.transition_ok:
            return False
        if conn is None:
            self.state_row["current_state"] = new_state
        else:
            conn.pending_state = new_state
        return True


def _is_state_manager_receiver(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"state_manager", "sm"}
    if isinstance(node, ast.Attribute):
        return node.attr == "state_manager" or _is_state_manager_receiver(node.value)
    if isinstance(node, ast.Subscript):
        key = None
        if isinstance(node.slice, ast.Constant):
            key = node.slice.value
        elif isinstance(node.slice, ast.Index) and isinstance(node.slice.value, ast.Constant):  # pragma: no cover (py<3.9 compat)
            key = node.slice.value.value
        return key == "state_manager" or _is_state_manager_receiver(node.value)
    return False


def test_handlers_do_not_call_state_manager_transition_directly():
    violations: list[str] = []
    for py_file in HANDLERS_ROOT.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "transition":
                if _is_state_manager_receiver(node.func.value):
                    rel = py_file.relative_to(HANDLERS_ROOT.parent)
                    violations.append(f"{rel}:{node.lineno}")
    assert violations == []


def test_handler_output_contract_contains_new_state():
    response = _legacy_handler(new_state="CHECKING_AVAILABILITY")({})
    assert "new_state" in response
    assert response["new_state"] == "CHECKING_AVAILABILITY"


def test_handler_output_contract_allows_optional_updates():
    response = _legacy_handler(
        new_state="CHECKING_AVAILABILITY",
        updates={"client_name": "Alex"},
    )({})
    assert "updates" in response
    assert response["updates"] == {"client_name": "Alex"}


def test_missing_new_state_keeps_state_unchanged():
    router = Router()
    router.register("COLLECTING", "provide_field", _legacy_handler(new_state=None))
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == []
    assert ctx.state == "COLLECTING"
    assert result["new_state"] == "COLLECTING"


def test_router_applies_updates_then_transitions():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == ["transition"]
    assert sm.state_row["client_name"] == "Alex"
    assert sm.state_row["current_state"] == "CHECKING_AVAILABILITY"
    assert result["new_state"] == "CHECKING_AVAILABILITY"


def test_update_failure_prevents_transition():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TrackingStateManager(state="COLLECTING", updates_ok=False, transition_ok=True)
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == ["transition"]
    assert sm.state_row["current_state"] == "COLLECTING"
    assert sm.state_row["client_name"] is None
    assert result["new_state"] == "COLLECTING"


def test_transition_failure_rolls_back_updates_in_transaction():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TransactionalStateManager(state="COLLECTING", updates_ok=True, transition_ok=False)
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == ["update_fields", "transition"]
    assert sm.update_conns[0] is sm.transition_conns[0]
    assert sm.rollback_count == 1
    assert sm.commit_count == 0
    assert sm.state_row["current_state"] == "COLLECTING"
    assert sm.state_row["client_name"] is None
    assert result["new_state"] == "COLLECTING"


def test_transition_exception_rolls_back_updates_in_transaction():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TransactionalStateManager(
        state="COLLECTING",
        updates_ok=True,
        transition_ok=True,
        transition_exc=RuntimeError("transition exploded"),
    )
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == ["update_fields", "transition"]
    assert sm.rollback_count == 1
    assert sm.commit_count == 0
    assert sm.state_row["current_state"] == "COLLECTING"
    assert sm.state_row["client_name"] is None
    assert result["new_state"] == "COLLECTING"


def test_update_exception_rolls_back_and_prevents_transition():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TransactionalStateManager(
        state="COLLECTING",
        updates_ok=True,
        transition_ok=True,
        update_exc=RuntimeError("update exploded"),
    )

    result = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert sm.calls == ["update_fields"]
    assert sm.rollback_count == 1
    assert sm.commit_count == 0
    assert sm.state_row["current_state"] == "COLLECTING"
    assert sm.state_row["client_name"] is None
    assert result["new_state"] == "COLLECTING"


def test_update_false_after_partial_mutation_rolls_back_in_transaction():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TransactionalStateManager(
        state="COLLECTING",
        updates_ok=False,
        transition_ok=True,
        update_mutates_before_false=True,
    )

    result = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert sm.calls == ["update_fields"]
    assert sm.rollback_count == 1
    assert sm.commit_count == 0
    assert sm.state_row["current_state"] == "COLLECTING"
    assert sm.state_row["client_name"] is None
    assert result["new_state"] == "COLLECTING"


def test_write_failure_returns_system_error_message_not_success_payload():
    from templates.errors import get_system_error_message

    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(
            new_state="CHECKING_AVAILABILITY",
            updates={"client_name": "Alex"},
            messages=["Great, your booking is now confirmed."],
        ),
    )
    sm = _TransactionalStateManager(
        state="COLLECTING",
        updates_ok=True,
        transition_ok=True,
        transition_exc=RuntimeError("db write failed"),
    )
    ctx = _ctx("COLLECTING")
    ctx.metadata["message"] = "book me in"

    result = router.route_v2(ctx, "provide_field", sm)

    assert result["messages"] == [get_system_error_message("book me in")]
    assert result["new_state"] == "COLLECTING"


def test_router_uses_single_transaction_conn_for_update_and_transition():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TransactionalStateManager(state="COLLECTING", updates_ok=True, transition_ok=True)

    router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert len(sm.update_conns) == 1
    assert len(sm.transition_conns) == 1
    assert sm.update_conns[0] is sm.transition_conns[0]
    assert sm.update_conns[0] is not None
    assert sm.commit_count == 1
    assert sm.rollback_count == 0


def test_v2_handler_event_path_applies_updates_and_transition():
    router = Router()

    def _v2_handler(_booking_ctx):
        return "fields_complete", {
            "messages": ["ok"],
            "actions": [],
            "new_state": "IGNORED",
            "updates": {"client_name": "Alex"},
        }

    router.register_v2("COLLECTING", "provide_field", _v2_handler)
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == ["transition"]
    assert sm.state_row["client_name"] == "Alex"
    assert ctx.state == "CHECKING_AVAILABILITY"
    assert result["new_state"] == "CHECKING_AVAILABILITY"


def test_v2_handler_non_dict_response_is_rejected_safely():
    from templates.errors import get_system_error_message

    router = Router()

    def _v2_bad_response(_booking_ctx):
        return "fields_complete", ["not-a-dict-response"]

    router.register_v2("COLLECTING", "provide_field", _v2_bad_response)
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")
    ctx.metadata["message"] = "hello"

    result = router.route_v2(ctx, "provide_field", sm)

    assert isinstance(result, dict)
    assert result["messages"] == [get_system_error_message("hello")]
    assert result["new_state"] == "COLLECTING"
    assert sm.calls == []


def test_v2_handler_non_string_event_is_rejected_safely():
    from templates.errors import get_system_error_message

    router = Router()

    def _v2_bad_event(_booking_ctx):
        return 123, {"messages": ["ok"], "actions": [], "new_state": None}

    router.register_v2("COLLECTING", "provide_field", _v2_bad_event)
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")
    ctx.metadata["message"] = "hello"

    result = router.route_v2(ctx, "provide_field", sm)

    assert result["messages"] == [get_system_error_message("hello")]
    assert result["new_state"] == "COLLECTING"
    assert sm.calls == []


def test_state_machine_rejects_invalid_target_when_strict(monkeypatch):
    monkeypatch.setenv("STRICT_FSM_BRIDGE", "1")
    with pytest.raises(FsmBridgeError):
        target_state_to_event("COLLECTING", "POST_BOOKING")


def test_invalid_transition_path_does_not_write_state(monkeypatch):
    monkeypatch.setenv("STRICT_FSM_BRIDGE", "1")
    router = Router()
    router.register("COLLECTING", "provide_field", _legacy_handler(new_state="POST_BOOKING", updates={"client_name": "Alex"}))
    sm = _TrackingStateManager(state="COLLECTING")

    result = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert sm.calls == []
    assert result["new_state"] is None


def test_legacy_handler_invalid_return_shape_is_handled_safely():
    router = Router()
    router.register("COLLECTING", "provide_field", lambda _legacy_context: "bad-return-shape")
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.calls == []
    assert ctx.state == "COLLECTING"
    assert result["new_state"] == "COLLECTING"


def test_router_calls_transition_once_per_request():
    router = Router()
    router.register("COLLECTING", "provide_field", _legacy_handler(new_state="CHECKING_AVAILABILITY"))
    sm = _TrackingStateManager(state="COLLECTING")

    router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert sm.transition_count == 1


def test_booking_flow_reaches_confirmed_after_refactor():
    router = Router()
    router.register("NEW", "start", _legacy_handler(new_state="COLLECTING", updates={"client_name": "Alex"}))
    router.register("COLLECTING", "provide_field", _legacy_handler(new_state="CHECKING_AVAILABILITY"))
    router.register("CHECKING_AVAILABILITY", "confirm_booking", _legacy_handler(new_state="CONFIRMED"))

    sm = _TrackingStateManager(state="NEW")
    ctx = _ctx("NEW")

    router.route_v2(ctx, "start", sm)
    assert ctx.state == "COLLECTING"
    router.route_v2(ctx, "provide_field", sm)
    assert ctx.state == "CHECKING_AVAILABILITY"
    router.route_v2(ctx, "confirm_booking", sm)
    assert ctx.state == "CONFIRMED"


def test_router_response_does_not_include_updates_key():
    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(new_state="CHECKING_AVAILABILITY", updates={"client_name": "Alex"}),
    )
    sm = _TrackingStateManager(state="COLLECTING")

    response = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert "updates" not in response


def test_route_v2_keeps_legacy_precedence_across_v2_and_v1_tables():
    router = Router()
    router.register("*", "wrong_number_opt_out", _legacy_handler(new_state=None, messages=["wildcard intent"]))

    def _v2_state_fallback(_booking_ctx):
        return "stay", {"messages": ["v2 state fallback"], "actions": [], "new_state": None}

    router.register_v2("NEW", "*", _v2_state_fallback)
    sm = _TrackingStateManager(state="NEW")
    ctx = _ctx("NEW")

    result = router.route_v2(ctx, "wrong_number_opt_out", sm)

    assert result["messages"] == ["wildcard intent"]
    assert sm.transition_count == 0
    assert ctx.state == "NEW"


def test_unknown_event_from_v2_handler_does_not_transition():
    router = Router()

    def _v2_handler(_booking_ctx):
        return "not_a_real_event", {"messages": ["ok"], "actions": [], "new_state": None}

    router.register_v2("COLLECTING", "provide_field", _v2_handler)
    sm = _TrackingStateManager(state="COLLECTING")
    ctx = _ctx("COLLECTING")

    result = router.route_v2(ctx, "provide_field", sm)

    assert sm.transition_count == 0
    assert ctx.state == "COLLECTING"
    assert result["new_state"] == "COLLECTING"


class _BridgeRouterSpy:
    def __init__(self):
        self.route_calls = []
        self.route_v2_calls = []

    def route(self, state, intent, context):
        self.route_calls.append((state, intent, context))
        return {"messages": ["v1"], "new_state": state, "actions": []}

    def route_v2(self, booking_ctx, intent, state_manager):
        self.route_v2_calls.append((booking_ctx, intent, state_manager))
        return {"messages": ["v2"], "new_state": booking_ctx.state, "actions": []}


def test_dispatch_message_uses_v1_route_for_v1_rows(monkeypatch):
    monkeypatch.setattr(
        "handlers.new_conv.booking_pivot.refresh_legacy_context_after_collecting_lane_switch",
        lambda intent, legacy_context, state_manager, phone_number: legacy_context,
    )
    router = _BridgeRouterSpy()
    sm = object()
    legacy_context = {
        "state": {"phone_number": PHONE, "current_state": "NEW", "flow_version": "v1"},
        "message": "hi",
    }

    result = dispatch_message(
        phone_number=PHONE,
        intent="greeting",
        legacy_context=legacy_context,
        router=router,
        state_manager=sm,
    )

    assert result["messages"] == ["v1"]
    assert len(router.route_calls) == 1
    assert len(router.route_v2_calls) == 0
    state, intent, context = router.route_calls[0]
    assert state == "NEW"
    assert intent == "greeting"
    assert context is legacy_context


def test_dispatch_message_uses_v2_route_for_v2_rows(monkeypatch):
    monkeypatch.setattr(
        "handlers.new_conv.booking_pivot.refresh_legacy_context_after_collecting_lane_switch",
        lambda intent, legacy_context, state_manager, phone_number: legacy_context,
    )
    router = _BridgeRouterSpy()
    sm = object()
    legacy_context = {
        "state": {"phone_number": PHONE, "current_state": "COLLECTING", "flow_version": "v2"},
        "message": "details",
        "foo": "bar",
    }

    result = dispatch_message(
        phone_number=PHONE,
        intent="provide_field",
        legacy_context=legacy_context,
        router=router,
        state_manager=sm,
    )

    assert result["messages"] == ["v2"]
    assert len(router.route_calls) == 0
    assert len(router.route_v2_calls) == 1
    booking_ctx, intent, called_sm = router.route_v2_calls[0]
    assert booking_ctx.state == "COLLECTING"
    assert booking_ctx.metadata.get("message") == "details"
    assert booking_ctx.metadata.get("foo") == "bar"
    assert intent == "provide_field"
    assert called_sm is sm


def test_dispatch_message_invalid_v2_state_falls_back_to_v1_route(monkeypatch):
    monkeypatch.setattr(
        "handlers.new_conv.booking_pivot.refresh_legacy_context_after_collecting_lane_switch",
        lambda intent, legacy_context, state_manager, phone_number: legacy_context,
    )
    router = _BridgeRouterSpy()
    sm = object()
    legacy_context = {
        "state": {"phone_number": PHONE, "current_state": "NOT_A_STATE", "flow_version": "v2"},
        "message": "hello",
    }

    result = dispatch_message(
        phone_number=PHONE,
        intent="greeting",
        legacy_context=legacy_context,
        router=router,
        state_manager=sm,
    )

    assert result["messages"] == ["v1"]
    assert len(router.route_calls) == 1
    assert len(router.route_v2_calls) == 0
    state, intent, context = router.route_calls[0]
    assert state == "NOT_A_STATE"
    assert intent == "greeting"
    assert context is legacy_context


def test_dispatch_message_flow_version_env_override_forces_v2(monkeypatch):
    monkeypatch.setenv("FLOW_VERSION_DEFAULT", "v2")
    monkeypatch.setattr(
        "handlers.new_conv.booking_pivot.refresh_legacy_context_after_collecting_lane_switch",
        lambda intent, legacy_context, state_manager, phone_number: legacy_context,
    )
    router = _BridgeRouterSpy()
    sm = object()
    legacy_context = {
        "state": {"phone_number": PHONE, "current_state": "NEW", "flow_version": "v1"},
        "message": "hi",
    }

    result = dispatch_message(
        phone_number=PHONE,
        intent="greeting",
        legacy_context=legacy_context,
        router=router,
        state_manager=sm,
    )

    assert result["messages"] == ["v2"]
    assert len(router.route_calls) == 0
    assert len(router.route_v2_calls) == 1


def test_dispatch_message_flow_version_db_override_forces_v1(monkeypatch):
    monkeypatch.delenv("FLOW_VERSION_DEFAULT", raising=False)
    monkeypatch.setattr("core.settings_manager.get_setting", lambda key, default=None: "v1" if key == "flow_version_default" else default)
    monkeypatch.setattr(
        "handlers.new_conv.booking_pivot.refresh_legacy_context_after_collecting_lane_switch",
        lambda intent, legacy_context, state_manager, phone_number: legacy_context,
    )
    router = _BridgeRouterSpy()
    sm = object()
    legacy_context = {
        "state": {"phone_number": PHONE, "current_state": "COLLECTING", "flow_version": "v2"},
        "message": "details",
    }

    result = dispatch_message(
        phone_number=PHONE,
        intent="provide_field",
        legacy_context=legacy_context,
        router=router,
        state_manager=sm,
    )

    assert result["messages"] == ["v1"]
    assert len(router.route_calls) == 1
    assert len(router.route_v2_calls) == 0


def test_route_v2_applies_copy_variant_for_webhook_new_state():
    router = Router()
    router.register_v2(
        "NEW",
        "greeting",
        lambda _ctx: (
            "stay",
            {
                "messages": [
                    "Hi there\n\nI STRONGLY recommend booking through my webform:\nhttps://example.com\n\nWhat time works for you, and how long would you like to book for?"
                ],
                "actions": [],
                "new_state": None,
            },
        ),
    )
    sm = _TrackingStateManager(state="NEW")
    ctx = BookingContext(
        user_id=PHONE,
        state="NEW",
        flow_version="v2",
        booking_data={"first_contact_sent": False},
        metadata={"apply_v2_copy_variant": True},
    )

    result = router.route_v2(ctx, "greeting", sm)
    assert result["messages"][0].startswith("Hey ")
    assert "Fastest way to lock this in is my booking form:" in result["messages"][0]
    assert "What time suits you and how long did you want to book for?" in result["messages"][0]


def test_route_v2_injects_hierarchical_booking_phase_for_webhook():
    router = Router()
    router.register_v2(
        "NEW",
        "greeting",
        lambda _ctx: ("booking_started", {"messages": ["Hi there"], "actions": [], "new_state": None}),
    )
    sm = _TrackingStateManager(state="NEW")
    ctx = BookingContext(
        user_id=PHONE,
        state="NEW",
        flow_version="v2",
        booking_data={"first_contact_sent": False},
        metadata={"apply_hierarchical_booking_phase": True},
    )

    result = router.route_v2(ctx, "greeting", sm)
    assert result["new_state"] == "COLLECTING"
    assert sm.state_row.get("booking_status") == "phase_availability"


def test_route_v2_preserves_protected_booking_status_for_webhook():
    router = Router()
    router.register_v2(
        "COLLECTING",
        "provide_field",
        lambda _ctx: ("fields_complete", {"messages": ["ok"], "actions": [], "new_state": None}),
    )
    sm = _TrackingStateManager(state="COLLECTING")
    sm.state_row["booking_status"] = "doubles_supply_escort"
    ctx = BookingContext(
        user_id=PHONE,
        state="COLLECTING",
        flow_version="v2",
        booking_data={"booking_status": "doubles_supply_escort"},
        metadata={"apply_hierarchical_booking_phase": True},
    )

    result = router.route_v2(ctx, "provide_field", sm)
    assert result["new_state"] == "CHECKING_AVAILABILITY"
    assert sm.state_row.get("booking_status") == "doubles_supply_escort"


def test_v1_fallback_and_v2_native_handlers_produce_same_outcome():
    v1_router = Router()
    v1_router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(
            new_state="CHECKING_AVAILABILITY",
            updates={"client_name": "Alex"},
            messages=["ok"],
        ),
    )
    v1_sm = _TrackingStateManager(state="COLLECTING")
    v1_ctx = _ctx("COLLECTING")
    v1_result = v1_router.route_v2(v1_ctx, "provide_field", v1_sm)

    v2_router = Router()

    def _v2_handler(_booking_ctx):
        return "fields_complete", {
            "messages": ["ok"],
            "actions": [],
            "new_state": None,
            "updates": {"client_name": "Alex"},
        }

    v2_router.register_v2("COLLECTING", "provide_field", _v2_handler)
    v2_sm = _TrackingStateManager(state="COLLECTING")
    v2_ctx = _ctx("COLLECTING")
    v2_result = v2_router.route_v2(v2_ctx, "provide_field", v2_sm)

    assert v1_result == v2_result
    assert v1_ctx.state == v2_ctx.state == "CHECKING_AVAILABILITY"
    assert v1_sm.state_row["client_name"] == v2_sm.state_row["client_name"] == "Alex"
    assert v1_sm.state_row["current_state"] == v2_sm.state_row["current_state"] == "CHECKING_AVAILABILITY"


def test_router_with_real_state_manager_ignores_invalid_update_keys_and_transitions():
    db = FakeDB()
    db.enqueue_result([{"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}])
    db.enqueue_result([{"version": 2}])
    db.enqueue_result([{"phone_number": PHONE, "current_state": "COLLECTING", "version": 2}])
    db.enqueue_result([{"version": 3}])
    sm = StateManager(db)

    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(
            new_state="CHECKING_AVAILABILITY",
            updates={"client_name": "Alex", "__invalid_update_field__": "x"},
        ),
    )

    response = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    all_queries = [query for query, _params in db.calls]
    assert not any("__invalid_update_field__" in query for query in all_queries)
    assert any("client_name" in query for query in all_queries)
    assert response["new_state"] == "CHECKING_AVAILABILITY"


def test_wrong_city_abort_updates_are_persistable_and_applied():
    from handlers.booking_coll._provide_field_context import CollectingCtx
    from handlers.booking_coll._provide_field_stages_geo_guard import _stage_outcall_wrong_city_correction

    ctx = CollectingCtx(
        phone_number=PHONE,
        message="actually i'm in sydney",
        raw_context={},
        state_manager=None,
        field_collector=None,
        field_validator=None,
        ai_service=None,
        db_service=None,
        state={"client_name": "Alex"},
        current_fields={
            "incall_outcall": "outcall",
            "outcall_address": "123 Collins St",
            "client_name": "Alex",
        },
    )

    import config
    import handlers.touring_inquiry as touring_inquiry

    original_city = config.get_effective_booking_city
    original_extract = touring_inquiry.extract_australian_city_from_message
    config.get_effective_booking_city = lambda: "Perth"
    touring_inquiry.extract_australian_city_from_message = lambda _msg: "Sydney"
    try:
        stage_result = _stage_outcall_wrong_city_correction(ctx)
    finally:
        config.get_effective_booking_city = original_city
        touring_inquiry.extract_australian_city_from_message = original_extract

    assert stage_result is not None
    updates = stage_result["updates"]
    assert updates
    assert set(updates.keys()).issubset(ALLOWED_STATE_UPDATE_FIELDS)

    db = FakeDB()
    db.enqueue_result([{"phone_number": PHONE, "current_state": "COLLECTING", "version": 1}])  # update_fields get_state
    db.enqueue_result([{"version": 2}])  # update_fields UPDATE ... RETURNING
    db.enqueue_result([{"phone_number": PHONE, "current_state": "COLLECTING", "version": 2}])  # transition get_state
    db.enqueue_result([{"version": 3}])  # transition UPDATE ... RETURNING
    sm = StateManager(db)

    router = Router()
    router.register(
        "COLLECTING",
        "provide_field",
        _legacy_handler(
            new_state=stage_result["new_state"],
            updates=updates,
            messages=stage_result["messages"],
        ),
    )

    result = router.route_v2(_ctx("COLLECTING"), "provide_field", sm)

    assert result["new_state"] == "NEW"
    all_queries = [query for query, _params in db.calls]
    assert any("last_touring_inquiry_city" in query for query in all_queries)
    assert any("outcall_address" in query for query in all_queries)
    assert any("booking_status" in query for query in all_queries)

