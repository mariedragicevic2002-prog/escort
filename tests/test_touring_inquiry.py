from __future__ import annotations

from handlers.touring_inquiry import handle_touring_inquiry
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999335"


def test_handle_touring_inquiry_points_to_profile_and_touring_keyword(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW", client_name="Alex")
    ctx = build_context(
        phone_number=PHONE,
        message="when are you next in perth",
        state_manager=sm,
    )

    monkeypatch.setattr("handlers.touring_inquiry._get_profile_url", lambda: "https://example.test/profile")

    result = handle_touring_inquiry(ctx)

    assert result["new_state"] is None
    assert result["messages"] == [
        "Hi Alex\n\n"
        "All my tour dates and info can be seen by visiting my profile\n\n"
        "https://example.test/profile\n\n"
        "You can also subscribe to my tours on the webpage so you will know the next time I'm in Perth.\n\n"
        "Alternatively if you text back the word TOURING Perth I'll send you a text the next time I'm in Perth."
    ]
    state = sm.get_state(PHONE) or {}
    assert state.get("last_touring_inquiry_city") == "Perth"
