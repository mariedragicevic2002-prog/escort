"""
State-machine regression tests.

Locks in the allowed transitions in core.state_manager.VALID_STATE_TRANSITIONS. If a
transition that was previously illegal becomes legal (or vice versa), these tests will
fail loudly — forcing you to decide whether you meant it.
"""

from __future__ import annotations

import pytest

from core.state_manager import VALID_STATE_TRANSITIONS, StateManager
from tests.fakes import FakeDB


# --- pure data: VALID_STATE_TRANSITIONS invariants ---------------------------------------

ALL_STATES = {
    "NEW",
    "COLLECTING",
    "CHECKING_AVAILABILITY",
    "DEPOSIT_REQUIRED",
    "CONFIRMED",
    "POST_BOOKING",
    "EXTENDED_ENQUIRY",
    "MANUAL_REVIEW_PENDING",
}


def test_all_states_have_outgoing_transitions():
    assert set(VALID_STATE_TRANSITIONS.keys()) == ALL_STATES


def test_every_state_can_self_loop():
    """Every state must allow staying in itself — the router often re-enters the same state."""
    for state, allowed in VALID_STATE_TRANSITIONS.items():
        assert state in allowed, f"{state} cannot self-loop"


def test_every_state_can_reset_to_new_except_deposit_required():
    """Resetting to NEW is how booking cancellation / clear_booking works — except from
    DEPOSIT_REQUIRED, where a silent reset would drop the pending deposit context. That
    path is deliberately excluded from the table and must go through StateManager
    .clear_booking (which uses force=True) to make the reset explicit and auditable."""
    for state, allowed in VALID_STATE_TRANSITIONS.items():
        if state == "DEPOSIT_REQUIRED":
            assert "NEW" not in allowed, (
                "DEPOSIT_REQUIRED → NEW must go through clear_booking (force=True), "
                "not the normal transition table"
            )
            continue
        assert "NEW" in allowed, f"{state} cannot reset to NEW"


def test_confirmed_cannot_go_backwards_to_collection():
    """Once the deposit lands, we never drop back into field collection — that would imply
    we lost the booking. If you need to re-collect, cancel and start over."""
    allowed = VALID_STATE_TRANSITIONS["CONFIRMED"]
    assert "COLLECTING" not in allowed
    assert "CHECKING_AVAILABILITY" not in allowed
    assert "DEPOSIT_REQUIRED" not in allowed


def test_deposit_required_cannot_reopen_collection():
    """DEPOSIT_REQUIRED should only move forward (to CONFIRMED) or be reset — never silently
    re-enter COLLECTING, which would lose the deposit request context."""
    allowed = VALID_STATE_TRANSITIONS["DEPOSIT_REQUIRED"]
    assert "COLLECTING" not in allowed
    assert "CHECKING_AVAILABILITY" not in allowed
    assert "CONFIRMED" in allowed


@pytest.mark.parametrize(
    "from_state,to_state",
    [
        ("NEW", "COLLECTING"),
        ("NEW", "CHECKING_AVAILABILITY"),
        ("COLLECTING", "CHECKING_AVAILABILITY"),
        ("CHECKING_AVAILABILITY", "DEPOSIT_REQUIRED"),
        ("DEPOSIT_REQUIRED", "CONFIRMED"),
        ("CONFIRMED", "POST_BOOKING"),
    ],
)
def test_happy_path_transitions_are_allowed(from_state, to_state):
    assert to_state in VALID_STATE_TRANSITIONS[from_state]


# --- StateManager.transition rejects invalid moves ---------------------------------------


def _seed_state(db: FakeDB, phone: str, current_state: str, version: int = 1):
    """Make FakeDB respond to StateManager's SELECT with a row in the requested state."""
    def _select_handler(query, params):
        return [{
            "phone_number": phone,
            "current_state": current_state,
            "version": version,
            "missing_fields": None,
            "offered_slot_hours": None,
            "offered_slot_minutes": None,
        }]
    # SELECT for get_state
    db.set_handler("SELECT * FROM conversation_states", _select_handler)


def test_transition_rejects_illegal_move(fake_db):
    _seed_state(fake_db, "+61400000001", "DEPOSIT_REQUIRED")
    sm = StateManager(db_service=fake_db)
    # DEPOSIT_REQUIRED cannot go back to COLLECTING
    assert sm.transition("+61400000001", "COLLECTING") is False


def test_transition_allows_legal_move(fake_db):
    """
    When the transition is legal the manager runs an UPDATE ... RETURNING version so it
    can distinguish a genuine landed write from an optimistic-lock conflict. Our fake
    has to return a row with the bumped version to mirror that contract.
    """
    phone = "+61400000002"
    state = {"current": "DEPOSIT_REQUIRED", "version": 1}

    def _select_handler(query, params):
        return [{
            "phone_number": phone,
            "current_state": state["current"],
            "version": state["version"],
            "missing_fields": None,
            "offered_slot_hours": None,
            "offered_slot_minutes": None,
        }]

    def _update_handler(query, params):
        state["current"] = "CONFIRMED"
        state["version"] += 1
        return [{"version": state["version"]}]

    fake_db.set_handler("SELECT * FROM conversation_states", _select_handler)
    fake_db.set_handler("UPDATE conversation_states", _update_handler)

    sm = StateManager(db_service=fake_db)
    assert sm.transition(phone, "CONFIRMED") is True


def test_create_state_persists_flow_version_from_env(fake_db, monkeypatch):
    phone = "+61400000999"
    monkeypatch.setenv("FLOW_VERSION_DEFAULT", "v2")
    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    inserts = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    assert inserts, "expected create_state INSERT query"
    _query, params = inserts[-1]
    assert len(params) == 4
    assert params[3] == "v2"


def test_create_state_uses_rollout_percent_when_default_unset(fake_db, monkeypatch):
    phone = "+61400000111"
    monkeypatch.delenv("FLOW_VERSION_DEFAULT", raising=False)
    monkeypatch.setenv("FLOW_VERSION_V2_ROLLOUT_PERCENT", "100")
    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    inserts = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    _query, params = inserts[-1]
    assert params[3] == "v2"


def test_create_state_falls_back_when_flow_version_column_missing(fake_db, monkeypatch):
    phone = "+61400000888"
    monkeypatch.setenv("FLOW_VERSION_DEFAULT", "v2")
    attempts = {"n": 0}

    def _insert_handler(query, params):
        if "INSERT INTO conversation_states" not in query:
            return []
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise Exception('column "flow_version" does not exist')
        return []

    fake_db.set_handler("INSERT INTO conversation_states", _insert_handler)
    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    insert_calls = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    assert len(insert_calls) == 2
    assert "flow_version" in insert_calls[0][0]
    assert "flow_version" not in insert_calls[1][0]


def test_flow_version_env_default_overrides_db_setting(fake_db, monkeypatch):
    phone = "+61400000771"
    monkeypatch.setenv("FLOW_VERSION_DEFAULT", "v1")
    monkeypatch.delenv("FLOW_VERSION_V2_ROLLOUT_PERCENT", raising=False)

    import core.settings_manager as settings_manager

    monkeypatch.setattr(
        settings_manager,
        "get_setting",
        lambda key, default=None: (
            "v2" if key == "flow_version_default" else ("100" if key == "flow_version_v2_rollout_percent" else default)
        ),
    )

    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    inserts = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    _query, params = inserts[-1]
    assert params[3] == "v1"


def test_flow_version_db_default_used_when_env_default_unset(fake_db, monkeypatch):
    phone = "+61400000772"
    monkeypatch.delenv("FLOW_VERSION_DEFAULT", raising=False)
    monkeypatch.delenv("FLOW_VERSION_V2_ROLLOUT_PERCENT", raising=False)

    import core.settings_manager as settings_manager

    monkeypatch.setattr(
        settings_manager,
        "get_setting",
        lambda key, default=None: ("v2" if key == "flow_version_default" else default),
    )

    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    inserts = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    _query, params = inserts[-1]
    assert params[3] == "v2"


def test_flow_version_env_rollout_percent_overrides_db_rollout_percent(fake_db, monkeypatch):
    phone = "+61400000773"
    monkeypatch.delenv("FLOW_VERSION_DEFAULT", raising=False)
    monkeypatch.setenv("FLOW_VERSION_V2_ROLLOUT_PERCENT", "0")

    import core.settings_manager as settings_manager

    monkeypatch.setattr(
        settings_manager,
        "get_setting",
        lambda key, default=None: (
            "rollout" if key == "flow_version_default" else ("100" if key == "flow_version_v2_rollout_percent" else default)
        ),
    )

    sm = StateManager(db_service=fake_db)
    assert sm.create_state(phone, "NEW") is True
    inserts = [c for c in fake_db.calls if "INSERT INTO conversation_states" in c[0]]
    _query, params = inserts[-1]
    assert params[3] == "v1"
