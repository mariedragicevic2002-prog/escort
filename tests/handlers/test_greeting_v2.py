"""
test_greeting_v2.py — Unit tests for handlers/new_conv/greeting_v2.py

Coverage:
  - _is_plain_greeting_only: matches plain greetings, rejects compound messages
  - _has_explicit_booking_request: original booking patterns still match
  - _has_explicit_booking_request: NEW availability patterns ("are you free", etc.)
    that were added so they route to v1 instead of the AI contextual path
  - _has_explicit_booking_request: general chat that should NOT match
  - handle_greeting_v2 decision tree via the helper functions (pure logic, no I/O)
"""
from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest

# ---------------------------------------------------------------------------
# Path setup — makes refactor2/ importable without installing it.
# ---------------------------------------------------------------------------
_REFACTOR_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REFACTOR_ROOT not in sys.path:
    sys.path.insert(0, _REFACTOR_ROOT)

# ---------------------------------------------------------------------------
# Load greeting_v2 directly to avoid the handlers.new_conv.__init__ chain
# which imports many modules that don't exist in the refactor2 standalone tree.
# ---------------------------------------------------------------------------
_GV2_PATH = os.path.join(_REFACTOR_ROOT, "handlers", "new_conv", "greeting_v2.py")
_spec = importlib.util.spec_from_file_location("handlers.new_conv.greeting_v2", _GV2_PATH)
if _spec is None or _spec.loader is None:
    raise ImportError(f"Could not load spec for {_GV2_PATH}")
_greeting_v2 = importlib.util.module_from_spec(_spec)
# Stub out imports that greeting_v2 lazily pulls in (only needed for handle_greeting_v2,
# not for the pure helper functions we're testing here).
for _stub_name in [
    "handlers.new_conv.greeting",
    "core.state_machine",
]:
    if _stub_name not in sys.modules:
        sys.modules[_stub_name] = types.ModuleType(_stub_name)
_spec.loader.exec_module(_greeting_v2)

_has_explicit_booking_request = _greeting_v2._has_explicit_booking_request
_is_plain_greeting_only = _greeting_v2._is_plain_greeting_only


# ---------------------------------------------------------------------------
# _is_plain_greeting_only
# ---------------------------------------------------------------------------

class TestIsPlainGreetingOnly(unittest.TestCase):

    # --- should match ---
    def test_hi(self):
        self.assertTrue(_is_plain_greeting_only("hi"))

    def test_hi_there(self):
        self.assertTrue(_is_plain_greeting_only("hi there"))

    def test_hello(self):
        self.assertTrue(_is_plain_greeting_only("hello"))

    def test_hello_there(self):
        self.assertTrue(_is_plain_greeting_only("hello there"))

    def test_hey(self):
        self.assertTrue(_is_plain_greeting_only("hey"))

    def test_hey_there(self):
        self.assertTrue(_is_plain_greeting_only("hey there"))

    def test_hiya(self):
        self.assertTrue(_is_plain_greeting_only("hiya"))

    def test_good_morning(self):
        self.assertTrue(_is_plain_greeting_only("good morning"))

    def test_good_afternoon(self):
        self.assertTrue(_is_plain_greeting_only("good afternoon"))

    def test_good_evening(self):
        self.assertTrue(_is_plain_greeting_only("good evening"))

    def test_greeting_with_trailing_punctuation(self):
        self.assertTrue(_is_plain_greeting_only("hi!"))
        self.assertTrue(_is_plain_greeting_only("hello?"))
        self.assertTrue(_is_plain_greeting_only("hey."))

    def test_case_insensitive(self):
        self.assertTrue(_is_plain_greeting_only("HI"))
        self.assertTrue(_is_plain_greeting_only("Hello"))

    def test_leading_trailing_whitespace(self):
        self.assertTrue(_is_plain_greeting_only("  hi  "))

    # --- should NOT match ---
    def test_greeting_with_availability_question(self):
        self.assertFalse(_is_plain_greeting_only("hi are you free later this afternoon"))

    def test_greeting_with_booking_request(self):
        self.assertFalse(_is_plain_greeting_only("hi I want to book"))

    def test_empty_string(self):
        self.assertFalse(_is_plain_greeting_only(""))

    def test_general_question(self):
        self.assertFalse(_is_plain_greeting_only("what are your rates?"))


# ---------------------------------------------------------------------------
# _has_explicit_booking_request — original patterns
# ---------------------------------------------------------------------------

class TestHasExplicitBookingRequestOriginalPatterns(unittest.TestCase):
    """Original booking-detail patterns must still match after the regex update."""

    def test_specific_clock_time_pm(self):
        self.assertTrue(_has_explicit_booking_request("Can I book at 3pm"))

    def test_specific_clock_time_am(self):
        self.assertTrue(_has_explicit_booking_request("8:30am works for me"))

    def test_at_time_no_ampm(self):
        self.assertTrue(_has_explicit_booking_request("at 3 today"))

    def test_duration_hours(self):
        self.assertTrue(_has_explicit_booking_request("I want 2 hours"))

    def test_duration_hr(self):
        self.assertTrue(_has_explicit_booking_request("just 1 hr please"))

    def test_duration_minutes(self):
        self.assertTrue(_has_explicit_booking_request("30 minutes is fine"))

    def test_book_keyword(self):
        self.assertTrue(_has_explicit_booking_request("I want to book"))

    def test_booking_keyword(self):
        self.assertTrue(_has_explicit_booking_request("making a booking"))

    def test_appointment_keyword(self):
        self.assertTrue(_has_explicit_booking_request("can I make an appointment"))

    def test_incall(self):
        self.assertTrue(_has_explicit_booking_request("is it incall?"))

    def test_outcall(self):
        self.assertTrue(_has_explicit_booking_request("do you do outcall?"))

    def test_my_place(self):
        self.assertTrue(_has_explicit_booking_request("can you come to my place"))

    def test_my_hotel(self):
        self.assertTrue(_has_explicit_booking_request("I'm at my hotel"))

    def test_my_room(self):
        self.assertTrue(_has_explicit_booking_request("come to my room"))

    def test_come_to_me(self):
        self.assertTrue(_has_explicit_booking_request("can you come to me?"))

    def test_come_over(self):
        self.assertTrue(_has_explicit_booking_request("come over tonight"))


# ---------------------------------------------------------------------------
# _has_explicit_booking_request — NEW availability patterns
# (the change that routes "Hi are you free this afternoon" → v1)
# ---------------------------------------------------------------------------

class TestHasExplicitBookingRequestAvailabilityPatterns(unittest.TestCase):
    """
    These patterns were added so availability questions bypass the AI contextual
    path and go straight to the v1 calendar-backed handler instead.
    """

    # "are you free / available"
    def test_are_you_free(self):
        self.assertTrue(_has_explicit_booking_request("are you free?"))

    def test_are_you_available(self):
        self.assertTrue(_has_explicit_booking_request("are you available?"))

    def test_hi_are_you_free_later_this_afternoon(self):
        """The exact message that triggered the original bug report."""
        self.assertTrue(_has_explicit_booking_request("Hi are you free later this afternoon"))

    def test_are_you_free_tonight(self):
        self.assertTrue(_has_explicit_booking_request("are you free tonight?"))

    def test_are_you_available_tomorrow(self):
        self.assertTrue(_has_explicit_booking_request("are you available tomorrow?"))

    # "you free / you available" (casual shorthand)
    def test_you_free_question_mark(self):
        self.assertTrue(_has_explicit_booking_request("you free?"))

    def test_you_available(self):
        self.assertTrue(_has_explicit_booking_request("you available?"))

    # "free later / today / tonight / tomorrow / this <word>"
    def test_free_later(self):
        self.assertTrue(_has_explicit_booking_request("free later?"))

    def test_free_today(self):
        self.assertTrue(_has_explicit_booking_request("free today?"))

    def test_free_tonight(self):
        self.assertTrue(_has_explicit_booking_request("free tonight?"))

    def test_free_tomorrow(self):
        self.assertTrue(_has_explicit_booking_request("free tomorrow?"))

    def test_free_this_afternoon(self):
        self.assertTrue(_has_explicit_booking_request("free this afternoon?"))

    def test_free_this_evening(self):
        self.assertTrue(_has_explicit_booking_request("free this evening?"))

    def test_free_now(self):
        self.assertTrue(_has_explicit_booking_request("free now?"))

    # "available later / today / tonight / tomorrow / this <word>"
    def test_available_later(self):
        self.assertTrue(_has_explicit_booking_request("available later?"))

    def test_available_tonight(self):
        self.assertTrue(_has_explicit_booking_request("available tonight?"))

    def test_available_this_week(self):
        self.assertTrue(_has_explicit_booking_request("available this week?"))

    # "any openings / availability / slots"
    def test_any_openings(self):
        self.assertTrue(_has_explicit_booking_request("any openings tomorrow?"))

    def test_any_opening(self):
        self.assertTrue(_has_explicit_booking_request("any opening this week?"))

    def test_any_availability(self):
        self.assertTrue(_has_explicit_booking_request("do you have any availability?"))

    def test_any_slots(self):
        self.assertTrue(_has_explicit_booking_request("any slots today?"))

    def test_any_slot(self):
        self.assertTrue(_has_explicit_booking_request("any slot left?"))

    # case-insensitive
    def test_case_insensitive_are_you_free(self):
        self.assertTrue(_has_explicit_booking_request("ARE YOU FREE?"))

    def test_case_insensitive_available(self):
        self.assertTrue(_has_explicit_booking_request("Are You Available Tonight?"))


# ---------------------------------------------------------------------------
# _has_explicit_booking_request — messages that should NOT match
# (general chat that should still go to AI contextual path)
# ---------------------------------------------------------------------------

class TestHasExplicitBookingRequestNoMatch(unittest.TestCase):

    def test_plain_greeting_hi(self):
        self.assertFalse(_has_explicit_booking_request("Hi"))

    def test_plain_greeting_hello(self):
        self.assertFalse(_has_explicit_booking_request("Hello"))

    def test_what_time_do_you_start(self):
        """Schedule question — no availability or booking keyword, still goes to AI."""
        self.assertFalse(_has_explicit_booking_request("What time do you start?"))

    def test_where_are_you_located(self):
        self.assertFalse(_has_explicit_booking_request("Where are you located?"))

    def test_what_are_your_rates(self):
        self.assertFalse(_has_explicit_booking_request("What are your rates?"))

    def test_empty_string(self):
        self.assertFalse(_has_explicit_booking_request(""))

    def test_general_chat(self):
        self.assertFalse(_has_explicit_booking_request("I saw your profile and was interested"))

    def test_do_you_work_weekends(self):
        self.assertFalse(_has_explicit_booking_request("Do you work weekends?"))


if __name__ == "__main__":
    unittest.main()
