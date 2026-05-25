"""Classifier routing fixes validated against offline stress simulation findings."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from core.classifier import Classifier


def _cls():
    return Classifier(ai_service=Mock())


def test_new_bare_yes_routes_to_greeting_not_confirm():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("yes", [], {"state": st}) == "greeting"


def test_new_bare_ok_routes_to_greeting():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("ok", [], {"state": st}) == "greeting"


def test_new_bare_no_routes_to_greeting():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("no", [], {"state": st}) == "greeting"


def test_new_with_booking_skeleton_bare_yes_still_confirm_booking():
    c = _cls()
    st = {
        "current_state": "NEW",
        "date": "2026-06-10",
        "time": (15, 0),
        "duration": 60,
    }
    assert c.classify("yes", [], {"state": st}) == "confirm_booking"


def test_enquiry_prefix_routes_to_enquiry_keyword():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert (
        c.classify("ENQUIRY do you tour Melbourne", [], {"state": st})
        == "enquiry_keyword"
    )


def test_wrong_number_routes_to_opt_out():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("wrong number sorry", [], {"state": st}) == "wrong_number_opt_out"


def test_new_opt_out_phrase_routes_to_opt_out():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("please stop texting me", [], {"state": st}) == "wrong_number_opt_out"


def test_new_punctuation_only_routes_to_other():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("...", [], {"state": st}) == "other"


def test_you_suck_routes_to_rude_abusive():
    c = _cls()
    st = {"current_state": "COLLECTING", "first_contact_sent": True, "version": 1}
    assert c.classify("you suck", [], {"state": st}) == "rude_abusive"


def test_collecting_bare_yes_still_provide_field_when_missing_fields():
    """COLLECTING short confirmations defer to provide_field (field collection), unchanged."""
    c = _cls()
    st = {
        "current_state": "COLLECTING",
        "first_contact_sent": True,
        "date": None,
        "time": None,
        "duration": 60,
    }
    assert c.classify("yes", [], {"state": st}) == "provide_field"


def test_doubles_booking_plus_when_available_routes_doubles_enquiry_not_ask_availability():
    """Regression: 'doubles booking' + 'when … available' must not lose to ask_availability."""
    from core.classifier import classify_doubles_signal

    msg = (
        "Hi mt name is Tony im keen to book you for a doubles booking "
        "let me know when your available?"
    )
    assert classify_doubles_signal(msg.lower()) == "ambiguous_threesome"
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify(msg, [], {"state": st}) == "doubles_enquiry"


def test_doubles_session_without_mmf_mff_is_ambiguous_signal():
    from core.classifier import classify_doubles_signal

    assert classify_doubles_signal("interested in a doubles session this weekend") == "ambiguous_threesome"


def test_mmf_still_beats_contextual_doubles_phrase():
    from core.classifier import classify_doubles_signal

    assert classify_doubles_signal("mmf doubles booking tomorrow") == "mmf_explicit"


def test_same_time_as_last_booking_routes_quick_booking():
    c = _cls()
    st = {"current_state": "POST_BOOKING", "version": 1}
    assert c.classify("same time as my last booking", [], {"state": st}) == "quick_booking"


def test_double_booking_same_slot_routes_book_appointment():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("can i double book the same slot tonight", [], {"state": st}) == "book_appointment"


def test_collecting_no_worries_routes_to_provide_field():
    c = _cls()
    st = {"current_state": "COLLECTING", "date": None, "time": None, "duration": None}
    assert c.classify("no worries", [], {"state": st}) == "provide_field"


def test_farewell_catch_you_later_routes_goodbye():
    c = _cls()
    st = {"current_state": "NEW", "version": 1}
    assert c.classify("catch you later", [], {"state": st}) == "goodbye"


@pytest.mark.parametrize(
    ("handler_name",),
    [
        ("handle_enquiry_keyword",),
        ("handle_wrong_number_opt_out",),
        ("handle_new_ambiguous",),
        ("handle_cancel_booking_new",),
    ],
)
def test_handlers_exported_on_new_conversation_module(handler_name: str):
    import handlers.new_conversation as nc

    assert callable(getattr(nc, handler_name))
