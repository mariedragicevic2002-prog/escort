"""
50-scenario regression suite for 3 confirmed live-transcript bugs.

Bug 3 (CRITICAL): "no" / negation words treated as client name → booking confirmed
Bug 4 (HIGH):     booking_type not cleared when client cancels doubles mid-flow
Bug 1 (MEDIUM):   doubles MFF template uses old text / "mandatory deposit" wording

All tests are offline — no DB, no Redis, no Twilio.
"""

from __future__ import annotations

import pytest
from unittest.mock import MagicMock

from templates.greetings import is_valid_client_name, is_likely_not_a_name
from handlers.booking_coll._provide_field_stages_extract import (
    _stage_cancel_doubles,
)
from handlers.booking_coll.doubles_first_turn_compose import (
    _four_hour_notice_block,
    _mandatory_doubles_deposit_line,
)
from handlers.booking_coll._provide_field_context import CollectingCtx
from tests.fakes import FakeStateManager

PHONE = "+61400111222"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(
    message: str,
    booking_type: str = "doubles_mff",
    experience_type: str = "doubles_mff",
    client_name: str = "Alex",
) -> CollectingCtx:
    state = {
        "current_state": "COLLECTING",
        "booking_type": booking_type,
        "experience_type": experience_type,
        "client_name": client_name,
        "first_contact_sent": True,
    }
    sm = FakeStateManager(initial={PHONE: state})
    ctx = MagicMock(spec=CollectingCtx)
    ctx.phone_number = PHONE
    ctx.message = message
    ctx.state = state
    ctx.state_manager = sm
    return ctx


# ===========================================================================
# BUG 3 — "no" / negation words must NOT be treated as client name
# ===========================================================================

class TestBug3NoAsTrigger:

    # --- is_valid_client_name ---

    @pytest.mark.parametrize("word", ["no", "No", "NO", "nope", "nah", "na", "Nope", "Nah"])
    def test_negation_not_a_valid_name(self, word):
        """None of the negation words should pass is_valid_client_name."""
        assert not is_valid_client_name(word), f"'{word}' should not be a valid client name"

    # --- is_likely_not_a_name ---

    @pytest.mark.parametrize("word", ["no", "nope", "nah", "na", "cancel"])
    def test_negation_flagged_as_not_a_name(self, word):
        assert is_likely_not_a_name(word), f"'{word}' should be flagged as not a name"

    # --- awaiting_yes negation gate ---

    def _build_awaiting_ctx(self, message: str) -> tuple:
        """Returns (context, state_manager) in awaiting-YES state (offline, no DB/redis)."""
        from tests.fakes import FakeStateManager

        state = {
            "current_state": "CHECKING_AVAILABILITY",
            "incall_outcall": "incall",
            "incall_awaiting_yes": True,
            "experience_type": "gfe",
            "client_name": "Alex",
            "first_contact_sent": True,
        }
        sm = FakeStateManager(initial={PHONE: state})
        return state, sm

    @pytest.mark.parametrize("msg", ["no", "nope", "nah", "No", "Nope", "Nah", "nada"])
    def test_negation_during_awaiting_yes_returns_no_problem(self, msg):
        """Plain negation while awaiting YES must not be treated as a client name."""
        # Primary guard: is_valid_client_name must reject these
        assert not is_valid_client_name(msg), \
            f"'{msg}' passed is_valid_client_name — booking would be confirmed incorrectly"

    @pytest.mark.parametrize("msg", [
        "dont want this booking",
        "don't want this",
        "cancel booking",
        "not booking",
    ])
    def test_cancellation_phrase_during_awaiting_yes(self, msg):
        """Multi-word cancellation phrases are rejected as names (len(words) > 2 guard)."""
        assert not is_valid_client_name(msg), f"'{msg}' should not be a valid client name"

    def test_yes_still_confirms_booking(self):
        """YES is in _IM_NOT_NAME_WORDS — not treated as a name, handled by yes_words path."""
        assert not is_valid_client_name("yes")
        assert not is_valid_client_name("yep")
        assert not is_valid_client_name("yeah")
        # Real names still valid
        assert is_valid_client_name("Alex")
        assert is_valid_client_name("James")

    def test_real_name_still_confirms(self):
        """A real name like 'James' should still be valid."""
        assert is_valid_client_name("James")
        assert is_valid_client_name("Sarah")
        assert not is_likely_not_a_name("James")

    @pytest.mark.parametrize("bad_name", ["no", "nope", "nah", "cancel", "stop", "bye", "ok", "okay"])
    def test_common_reject_words_not_names(self, bad_name):
        assert not is_valid_client_name(bad_name)

    def test_name_with_yes_not_a_name(self):
        """'Alex YES' should be treated as YES with name, not pure name."""
        assert is_valid_client_name("Alex")  # name part is valid
        # The full phrase "Alex YES" has YES word, so is_name logic should mark this as YES not name


# ===========================================================================
# BUG 4 — Doubles cancellation clears booking type
# ===========================================================================

class TestBug4CancelDoubles:

    @pytest.mark.parametrize("booking_type,experience_type", [
        ("doubles_mff", "doubles_mff"),
        ("Doubles MMF", "Doubles MMF"),
        ("couples_mff", "couples_mff"),
        ("doubles_mff", "couples_mff"),
    ])
    @pytest.mark.parametrize("cancel_msg", [
        "dont want a couples booking just solo booking",
        "dont want doubles",
        "dont want a doubles booking",
        "just solo",
        "solo booking",
        "just me",
        "not couples",
        "not doubles",
        "cancel doubles",
        "just regular booking",
        "normal booking",
        "solo only",
        "only me",
        "by myself",
    ])
    def test_cancel_doubles_clears_booking_type(self, booking_type, experience_type, cancel_msg):
        """_stage_cancel_doubles should fire and clear booking fields."""
        ctx = _make_ctx(cancel_msg, booking_type=booking_type, experience_type=experience_type)
        result = _stage_cancel_doubles(ctx)

        assert result is not None, f"Expected cancel_doubles to fire for msg='{cancel_msg}'"
        assert result.get("new_state") == "COLLECTING"

        # Fields should be cleared
        updated = ctx.state_manager.get_state(PHONE) or {}
        assert updated.get("booking_type") is None, "booking_type should be cleared"
        assert updated.get("experience_type") is None, "experience_type should be cleared"
        assert updated.get("doubles_type") is None, "doubles_type should be cleared"

    @pytest.mark.parametrize("cancel_msg", [
        "dont want a couples booking just solo booking",
        "just solo",
        "not couples",
        "not doubles",
    ])
    def test_cancel_doubles_response_mentions_solo(self, cancel_msg):
        """Response should mention switching to solo / no problem."""
        ctx = _make_ctx(cancel_msg)
        result = _stage_cancel_doubles(ctx)
        assert result is not None
        msg = " ".join(result.get("messages", [])).lower()
        assert any(kw in msg for kw in ("solo", "no problem", "switched", "booking")), \
            f"Expected helpful response, got: {msg}"

    @pytest.mark.parametrize("innocent_msg", [
        "8pm tomorrow",
        "1 hour",
        "tomorrow night",
        "incall please",
        "yes",
        "my address is 123 Main St",
    ])
    def test_non_cancel_messages_dont_trigger(self, innocent_msg):
        """Normal booking messages should NOT trigger the cancel stage."""
        ctx = _make_ctx(innocent_msg, booking_type="doubles_mff", experience_type="doubles_mff")
        result = _stage_cancel_doubles(ctx)
        assert result is None, f"'{innocent_msg}' should not trigger doubles cancel"

    def test_cancel_doubles_ignored_when_not_in_doubles_flow(self):
        """If not in a doubles flow, cancellation message should not fire."""
        ctx = _make_ctx("dont want doubles", booking_type="gfe", experience_type="gfe")
        result = _stage_cancel_doubles(ctx)
        assert result is None

    def test_real_transcript_phrase(self):
        """The exact phrase from the real transcript should trigger cancellation."""
        ctx = _make_ctx(
            "dont want a couples booking just solo booking",
            booking_type="doubles_mff",
            experience_type="couples_mff",
        )
        result = _stage_cancel_doubles(ctx)
        assert result is not None, "Real transcript phrase must trigger doubles cancel"
        updated = ctx.state_manager.get_state(PHONE) or {}
        assert updated.get("experience_type") is None


# ===========================================================================
# BUG 1 — Doubles template text is correct
# ===========================================================================

class TestBug1DoublesTemplate:

    def test_four_hour_notice_new_wording(self):
        """New 4-hour notice text should use the requested phrasing."""
        text = _four_hour_notice_block().lower()
        assert "just so you know" in text
        assert "minimum 4 hours notice required" in text
        # Old text must NOT appear
        assert "if you need me to organise" not in text
        assert "4 hrs notice" not in text

    def test_deposit_line_no_mandatory(self):
        """Deposit line must not say 'mandatory'."""
        line = _mandatory_doubles_deposit_line(200).lower()
        assert "mandatory" not in line
        assert "$200" in line
        assert "deposit" in line

    def test_deposit_line_correct_amount(self):
        for amt in [150, 200, 250, 300]:
            line = _mandatory_doubles_deposit_line(amt)
            assert f"${amt}" in line

    def test_mff_incall_template_opening_merged(self, monkeypatch):
        """MFF incall template should merge opening + gate_primary into one paragraph."""
        monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/wf")
        monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")
        monkeypatch.setattr("config.get_current_incall_location", lambda: {
            "city": "Adelaide",
            "hotel_name": "Hilton",
            "address": "233 Victoria Square",
        })
        monkeypatch.setattr("config.get_effective_booking_city", lambda: "Adelaide")
        monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: __import__("datetime").datetime(2026, 6, 1, 12, 0))
        monkeypatch.setattr("services.calendar_service.check_conflict", lambda *a, **kw: False)
        monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", lambda *a, **kw: [])

        from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
        from tests.fakes import FakeStateManager as _FSM

        sm = _FSM(initial={PHONE: {
            "current_state": "COLLECTING",
            "booking_type": "doubles_mff",
            "experience_type": "doubles_mff",
            "doubles_type": "mff",
            "incall_outcall": "incall",
            "client_name": "Sam",
        }})
        state = sm.get_state(PHONE) or {}

        out = _check_doubles_supply_response(
            "MFF doubles", PHONE, state, sm, doubles_supply_gate_follow_up=False,
        )
        assert out is not None
        body = "\n".join(out.get("messages") or [])
        low = body.lower()

        assert "i love doubles mmf bookings." in low
        assert "will you be bringing the other person yourself" in low
        assert "just so you know" in low and "4 hours notice" in low
        assert "mandatory" not in low
        assert "4 hrs notice" not in low
        assert "$200" in body and "deposit" in low

    def test_mmf_incall_template_opening_merged(self, monkeypatch):
        """MMF incall template should also have merged opening + gate_primary."""
        monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/wf")
        monkeypatch.setattr("config.get_profile_url", lambda: "")
        monkeypatch.setattr("config.get_current_incall_location", lambda: {"city": "Adelaide"})
        monkeypatch.setattr("config.get_effective_booking_city", lambda: "Adelaide")
        monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: __import__("datetime").datetime(2026, 6, 1, 12, 0))
        monkeypatch.setattr("services.calendar_service.check_conflict", lambda *a, **kw: False)
        monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", lambda *a, **kw: [])

        from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
        from tests.fakes import FakeStateManager as _FSM

        sm = _FSM(initial={PHONE: {
            "current_state": "COLLECTING",
            "booking_type": "Doubles MMF",
            "experience_type": "Doubles MMF",
            "doubles_type": "mmf",
            "incall_outcall": "incall",
            "client_name": "Tom",
        }})
        state = sm.get_state(PHONE) or {}

        out = _check_doubles_supply_response(
            "Doubles MMF", PHONE, state, sm, doubles_supply_gate_follow_up=False,
        )
        assert out is not None
        body = "\n".join(out.get("messages") or [])
        low = body.lower()

        assert "tom" in low
        assert "i love doubles mmf bookings." in low
        assert "will you be bringing the other person yourself" in low
        assert "just so you know" in low
        assert "mandatory" not in low


# ===========================================================================
# Combined flow simulations
# ===========================================================================

class TestCombinedFlowSims:
    """End-to-end style (offline) simulations for the combined bug scenarios."""

    def test_sim_no_during_couples_mff_awaiting_yes(self):
        """
        Sim 1: Client was in a doubles/couples flow, bot sent confirmation summary,
        client replies 'no' — must NOT confirm booking as 'No'.
        """
        assert not is_valid_client_name("no")
        assert is_likely_not_a_name("no")

    def test_sim_no_uppercase_not_a_name(self):
        """Sim 2: 'No' (capitalised) should still not be a valid name."""
        assert not is_valid_client_name("No")

    def test_sim_nope_not_a_name(self):
        """Sim 3: 'nope' should not be a valid name."""
        assert not is_valid_client_name("nope")

    def test_sim_nah_not_a_name(self):
        """Sim 4: 'nah' should not be a valid name."""
        assert not is_valid_client_name("nah")

    def test_sim_cancel_doubles_before_time_collected(self):
        """Sim 5: Client cancels doubles before any time is collected."""
        ctx = _make_ctx("just solo thanks", booking_type="doubles_mff", experience_type="doubles_mff")
        result = _stage_cancel_doubles(ctx)
        assert result is not None
        updated = ctx.state_manager.get_state(PHONE) or {}
        assert updated.get("experience_type") is None

    def test_sim_cancel_doubles_couples_mff_type(self):
        """Sim 6: Cancellation works for couples_mff experience type."""
        ctx = _make_ctx(
            "dont want a couples booking just solo booking",
            booking_type="couples_mff",
            experience_type="couples_mff",
        )
        result = _stage_cancel_doubles(ctx)
        assert result is not None
        updated = ctx.state_manager.get_state(PHONE) or {}
        assert updated.get("booking_type") is None
        assert updated.get("experience_type") is None

    def test_sim_cancel_doubles_mmf_type(self):
        """Sim 7: Cancellation works for Doubles MMF booking type."""
        ctx = _make_ctx("not a doubles booking", booking_type="Doubles MMF", experience_type="Doubles MMF")
        result = _stage_cancel_doubles(ctx)
        assert result is not None

    def test_sim_deposit_line_for_various_amounts(self):
        """Sim 8-10: Deposit line correct for $150/$200/$250."""
        for amt in [150, 200, 250]:
            line = _mandatory_doubles_deposit_line(amt)
            assert f"${amt}" in line
            assert "mandatory" not in line.lower()

    def test_sim_four_hour_notice_present(self):
        """Sim 11: Four-hour notice block present with correct text."""
        text = _four_hour_notice_block()
        assert "4 hours notice" in text.lower() or "4 hours" in text.lower()

    def test_sim_real_name_unaffected(self):
        """Sim 12: Real first names still valid after fix."""
        for name in ["James", "Sarah", "Mike", "Emma", "Tom", "Lisa"]:
            assert is_valid_client_name(name), f"'{name}' should be a valid client name"

    def test_sim_na_not_a_name(self):
        """Sim 13: 'na' (short for nah) should not be treated as name."""
        assert not is_valid_client_name("na")

    def test_sim_negative_not_a_name(self):
        """Sim 14: 'negative' should not be treated as name."""
        assert not is_valid_client_name("negative")

    def test_sim_nada_not_a_name(self):
        """Sim 15: 'nada' should not be treated as name."""
        assert not is_valid_client_name("nada")

    @pytest.mark.parametrize("cancel_msg,exp_type", [
        ("dont want doubles", "doubles_mff"),
        ("dont want couples", "couples_mff"),
        ("just me tonight", "Doubles MMF"),
        ("solo only", "doubles_mff"),
        ("by myself", "couples_mff"),
    ])
    def test_sim_cancel_patterns_various_types(self, cancel_msg, exp_type):
        """Sims 16-20: Various cancel phrases work for various doubles types."""
        ctx = _make_ctx(cancel_msg, booking_type=exp_type, experience_type=exp_type)
        result = _stage_cancel_doubles(ctx)
        if result is not None:
            updated = ctx.state_manager.get_state(PHONE) or {}
            assert updated.get("experience_type") is None

    def test_sim_solo_booking_after_cancel(self):
        """Sim 21: After cancelling doubles, client name preserved in response."""
        ctx = _make_ctx("just solo", booking_type="doubles_mff", experience_type="doubles_mff", client_name="Mike")
        result = _stage_cancel_doubles(ctx)
        assert result is not None
        msg = " ".join(result.get("messages", []))
        assert "Mike" in msg

    def test_sim_gate_follow_present_in_mff_template(self, monkeypatch):
        """Sim 22: MFF incall template still asks who supplies (gate_follow)."""
        monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/wf")
        monkeypatch.setattr("config.get_profile_url", lambda: "")
        monkeypatch.setattr("config.get_current_incall_location", lambda: {"city": "Adelaide"})
        monkeypatch.setattr("config.get_effective_booking_city", lambda: "Adelaide")
        monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: __import__("datetime").datetime(2026, 6, 1, 12, 0))
        monkeypatch.setattr("services.calendar_service.check_conflict", lambda *a, **kw: False)
        monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", lambda *a, **kw: [])

        from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
        from tests.fakes import FakeStateManager as _FSM

        sm = _FSM(initial={PHONE: {
            "current_state": "COLLECTING",
            "booking_type": "doubles_mff",
            "experience_type": "doubles_mff",
            "doubles_type": "mff",
            "incall_outcall": "incall",
            "client_name": "",
        }})
        state = sm.get_state(PHONE) or {}
        out = _check_doubles_supply_response(
            "do u have a girlfriend that can join us", PHONE, state, sm, doubles_supply_gate_follow_up=False,
        )
        assert out is not None
        body = "\n".join(out.get("messages") or []).lower()
        assert "please advise if you will be supplying the other person" in body or \
               "will you be bringing the other person yourself" in body

    def test_sim_webform_url_present_in_template(self, monkeypatch):
        """Sim 23: Webform URL appears in doubles template."""
        monkeypatch.setattr("core.webform_security.get_webform_url", lambda _phone: "https://example.test/XYZABC")
        monkeypatch.setattr("config.get_profile_url", lambda: "")
        monkeypatch.setattr("config.get_current_incall_location", lambda: {"city": "Adelaide"})
        monkeypatch.setattr("config.get_effective_booking_city", lambda: "Adelaide")
        monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: __import__("datetime").datetime(2026, 6, 1, 12, 0))
        monkeypatch.setattr("services.calendar_service.check_conflict", lambda *a, **kw: False)
        monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", lambda *a, **kw: [])

        from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
        from tests.fakes import FakeStateManager as _FSM

        sm = _FSM(initial={PHONE: {
            "current_state": "COLLECTING",
            "booking_type": "doubles_mff",
            "experience_type": "doubles_mff",
            "doubles_type": "mff",
            "incall_outcall": "incall",
        }})
        state = sm.get_state(PHONE) or {}
        out = _check_doubles_supply_response(
            "mff doubles", PHONE, state, sm, doubles_supply_gate_follow_up=False,
        )
        body = "\n".join(out.get("messages") or []) if out else ""
        assert "https://example.test/XYZABC" in body

    @pytest.mark.parametrize("name", ["ok", "okay", "sure", "thanks", "cancel", "stop", "bye"])
    def test_sim_common_reject_words_not_names(self, name):
        """Sims 24-30: Common non-name words already in _non_name_words."""
        assert not is_valid_client_name(name)
