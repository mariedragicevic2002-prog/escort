"""
Repeat-detection regression tests.

The rule (feedback_repeat_detection_golden_rule):
  The bot must never send the same outbound message 3 or more times in a single conversation.
  After the 2nd identical outbound, the 3rd attempt is replaced with the "enquiry" template.

This module tests that contract directly against _check_repeat_response.

Note: importing main_v2.application instantiates Flask + registers blueprints (heavy). That
happens once per test session, not per test. If you're adding more repeat-detection tests,
put them here so we don't pay the import cost twice.
"""

from __future__ import annotations


from main_v2.conversation_guards import check_repeat_response as _check_repeat_response
from tests.fakes import FakeDB, FakeStateManager


# --- fixtures -----------------------------------------------------------------------------


def _db_with_outbound_history(messages: list[str]) -> FakeDB:
    """FakeDB where the outbound-history SELECT returns the given messages (newest first)."""
    db = FakeDB()

    def _select_handler(query, params):
        return [{"message_body": m} for m in messages]

    db.set_handler("FROM message_history", _select_handler)
    return db


# --- the contract -------------------------------------------------------------------------


def test_no_prior_sends_returns_none():
    db = _db_with_outbound_history([])
    result = _check_repeat_response("Hi, when would you like to book?", "+61400000001", db)
    assert result is None


def test_one_prior_identical_send_returns_none():
    db = _db_with_outbound_history(["Hi, when would you like to book?"])
    result = _check_repeat_response("Hi, when would you like to book?", "+61400000001", db)
    assert result is None


def test_two_prior_identical_sends_triggers_guard():
    """The 3rd attempt (2 prior identical) is replaced with the enquiry template."""
    msg = "Hi, when would you like to book?"
    db = _db_with_outbound_history([msg, msg])
    result = _check_repeat_response(msg, "+61400000001", db)
    assert result is not None
    assert "ENQUIRY" in result  # the template invites the client to text ENQUIRY back


def test_tuple_rows_from_db_still_triggers_guard():
    """Defence-in-depth: some drivers/legacy code paths return tuple rows, not dicts."""
    msg = "Hi, when would you like to book?"
    db = FakeDB()

    def _tuple_rows(_q, _p):
        return [(msg,), (msg,)]

    db.set_handler("FROM message_history", _tuple_rows)
    result = _check_repeat_response(msg, "+61400000001", db)
    assert result is not None
    assert "ENQUIRY" in result


def test_three_prior_identical_sends_still_triggers_guard():
    msg = "Hi, when would you like to book?"
    db = _db_with_outbound_history([msg, msg, msg])
    result = _check_repeat_response(msg, "+61400000001", db)
    assert result is not None


def test_different_recent_messages_do_not_count():
    db = _db_with_outbound_history([
        "Please send a screenshot of your deposit.",
        "Booking confirmed — see you then.",
    ])
    result = _check_repeat_response("Hi, when would you like to book?", "+61400000001", db)
    assert result is None


def test_partial_match_does_not_count_as_repeat():
    """Repeat detection is exact-match; near-misses shouldn't trigger it."""
    db = _db_with_outbound_history([
        "Hi, when would you like to book? (morning?)",
        "Hi, when would you like to book? (afternoon?)",
    ])
    result = _check_repeat_response("Hi, when would you like to book?", "+61400000001", db)
    assert result is None


def test_long_message_triggers_repeat_guard_when_prior_sends_match():
    """Long outbound templates are counted like any other message (golden rule)."""
    long_msg = "x" * 700
    db = _db_with_outbound_history([long_msg, long_msg])
    result = _check_repeat_response(long_msg, "+61400000001", db)
    assert result is not None
    assert "ENQUIRY" in result


def test_heres_what_you_need_to_know_summary_triggers_guard_when_repeated():
    summary = "Here's what you need to know: your booking is confirmed for 14:00."
    db = _db_with_outbound_history([summary, summary])
    result = _check_repeat_response(summary, "+61400000001", db)
    assert result is not None
    assert "ENQUIRY" in result


def test_collecting_long_message_still_triggers_guard():
    """COLLECTING must not bypass repeat detection based on message length."""
    long_msg = "y" * 250
    db = _db_with_outbound_history([long_msg, long_msg])
    sm = FakeStateManager(initial={"+61400000001": {"current_state": "COLLECTING"}})
    result = _check_repeat_response(long_msg, "+61400000001", db, state_manager=sm)
    assert result is not None
def test_empty_message_returns_none():
    db = _db_with_outbound_history([])
    assert _check_repeat_response("", "+61400000001", db) is None
    assert _check_repeat_response("   ", "+61400000001", db) is None


def test_db_query_failure_does_not_raise():
    """Repeat detection is a safety net; if the DB fails, fall through silently — the main
    flow should still send the message. An exception here would take the whole bot down."""
    db = FakeDB()

    def _raise(_q, _p):
        raise RuntimeError("simulated DB outage")

    db.set_handler("FROM message_history", _raise)
    result = _check_repeat_response("any message", "+61400000001", db)
    assert result is None


def test_guard_trigger_marks_booking_status():
    """When the guard fires, we persist booking_status='repeat_guard_prompt_sent' so the next
    reply can escalate to the final cut-off message."""
    msg = "Hi, when would you like to book?"
    db = _db_with_outbound_history([msg, msg])
    sm = FakeStateManager(initial={"+61400000001": {}})

    result = _check_repeat_response(msg, "+61400000001", db, state_manager=sm)

    assert result is not None
    assert any(
        u.get("booking_status") == "repeat_guard_prompt_sent"
        for _phone, u in sm.updates
    ), f"expected booking_status update, got {sm.updates}"
