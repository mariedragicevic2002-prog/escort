from __future__ import annotations

from handlers import deposit_flow
from tests.fakes import FakeStateManager


def test_refuse_deposit_keeps_flow_open_for_recovery():
    phone = "+61400000999"
    sm = FakeStateManager(initial={phone: {"current_state": "DEPOSIT_REQUIRED", "deposit_required": True}})
    ctx = {"phone_number": phone, "state_manager": sm, "state": sm.get_state(phone)}

    result = deposit_flow.handle_refuse_deposit(ctx)

    assert result["new_state"] is None
    assert "deposit" in result["messages"][0].lower()
    state = sm.get_state(phone)
    assert state is not None
    assert state["current_state"] == "DEPOSIT_REQUIRED"
