"""Tests for post-booking escort feedback (SMS parser, HMAC window, job query guards)."""

from __future__ import annotations

import time
from unittest.mock import patch

from core.hmac_security import (
    FEEDBACK_TOKEN_TTL_SECONDS,
    GATEWAY_FEEDBACK,
    generate_signed_token,
)
from handlers.escort_feedback import handle_escort_feedback_reply
from tests.fakes import FakeDB, FakeStateManager


def test_sms_ynn_blocks_on_third_no():
    """Q3 = N (e.g. Y Y N) must block, matching webform when would_see_again is No."""
    db = FakeDB()

    def _pending(q, _p):
        qu = (q or "").upper()
        if "DELETE" in qu and "FEEDBACK_PENDING" in qu:
            return None
        if "INSERT" in qu and "CLIENT_FEEDBACK" in qu:
            return None
        if "SELECT" in qu and "FROM FEEDBACK_PENDING" in qu and "CLIENT_PHONE_NUMBER" in qu:
            return [{"client_phone_number": "+61400111222"}]
        if "SELECT" in qu and "FROM CONVERSATION_STATES" in qu:
            return [
                {
                    "client_name": "Alex",
                    "date": "2025-01-15",
                    "time": [9, 0],
                    "duration": 60,
                    "experience_type": "GFE",
                    "incall_outcall": "incall",
                }
            ]
        return []

    # FakeDB matches substrings on the query string; SQL is lowercase in code.
    db.set_handler("feedback_pending", _pending)
    db.set_handler("conversation_states", _pending)

    sm = FakeStateManager()
    ok, msg = handle_escort_feedback_reply("Y Y N", db, sm)
    assert ok is True
    assert "blocked" in msg.lower()
    assert any(
        t[0] == "+61400111222" and t[1] == "client_feedback_block" for t in sm.blocks
    ), sm.blocks


def test_feedback_signed_token_uses_24h_ttl():
    """Links must match FEEDBACK_TOKEN_TTL_SECONDS (24h)."""
    assert FEEDBACK_TOKEN_TTL_SECONDS == 86400
    tok = generate_signed_token("99", GATEWAY_FEEDBACK, ttl_seconds=FEEDBACK_TOKEN_TTL_SECONDS)
    parts = tok.split(":", 2)
    assert len(parts) == 3
    _val, exp_s, _sig = parts
    assert _val == "99"
    exp = int(exp_s)
    now = int(time.time())
    assert now <= exp <= now + 86400 + 2


def test_client_feedback_query_has_state_and_cancel_guards():
    """Ensure job SQL filters active confirmed states and non-cancelled bookings."""
    import services.client_feedback_service as cfs

    import inspect

    src = inspect.getsource(cfs.check_and_send_feedback_requests)
    assert "current_state IN" in src and "CONFIRMED" in src
    assert "cancelled" in src.lower() or "canceled" in src


@patch("services.sms_service.send_sms", return_value=True)
def test_send_escort_sms_respects_client_rating_toggle(mock_sms):
    """When escort_sms_client_rating is off, no SMS is sent and caller gets False."""
    with patch("core.settings_manager.get_setting") as gs:

        def _gs(key, *args, **kwargs):
            m = {
                "escort_sms_enabled": "true",
                "escort_sms_client_rating": "false",
            }
            if key in m:
                return m[key]
            return "true"

        gs.side_effect = _gs
        from services.sms_service import send_escort_sms

        out = send_escort_sms("+61000", "hi", category="client_rating")
    assert out is False
    mock_sms.assert_not_called()
