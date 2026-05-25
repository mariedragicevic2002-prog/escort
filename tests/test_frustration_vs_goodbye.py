"""Frustration detection must not lose to trailing farewell phrases."""

from unittest.mock import MagicMock

from main_v2.conversation_guards import check_frustration


def test_frustration_trailing_bye_still_redirects():
    sm = MagicMock()
    state = {}
    result = check_frustration("this is stupid bye", "+61400000001", state, sm)
    assert result is not None
    assert any("webform" in (m or "").lower() for m in result.get("messages", []))
