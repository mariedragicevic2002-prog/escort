"""
Phase 2b — YES-confirms golden rule tests.

Golden rule from project memory:
  "name and experience type must NEVER block a booking; plain YES always confirms"

This tests _no_experience_branch_response in handlers/booking_coll/_provide_field_stages_finish.py,
which is the critical branch entered when date+time+duration are present but experience_type is missing.

The rule: if the client sends YES and has a valid name, we MUST proceed to
CHECKING_AVAILABILITY with auto_confirm_without_experience=True — not ask about experience.
"""

from unittest.mock import MagicMock, patch
from handlers.booking_coll._provide_field_stages_finish import _no_experience_branch_response


PHONE = "+61400000002"


def _build_ctx(
    message: str,
    client_name: str = "",
    date: str = "2026-05-10",
    time: tuple = (20, 0),
    duration: int = 60,
    incall_outcall: str = "incall",
    outcall_address: str = "",
    experience_type: str = "",
):
    """Build a minimal CollectingCtx-like object for _no_experience_branch_response."""
    from handlers.booking_coll._provide_field import CollectingCtx

    updated_fields = {
        "date": date,
        "time": time,
        "duration": duration,
        "incall_outcall": incall_outcall,
        "outcall_address": outcall_address,
        "client_name": client_name,
        "experience_type": experience_type,
    }
    state = {"client_name": client_name, "experience_type": experience_type}

    sm = MagicMock()
    sm.update_fields.return_value = True
    sm.transition.return_value = True
    sm.get_state.return_value = {**state}

    ctx = MagicMock(spec=CollectingCtx)
    ctx.phone_number = PHONE
    ctx.message = message
    ctx.updated_fields = updated_fields
    ctx.state = state
    ctx.state_manager = sm
    ctx.raw_context = {
        "phone_number": PHONE,
        "state": state,
        "state_manager": sm,
        "message": message,
    }
    return ctx


# Patch calendar check to return "available" so we don't hit real calendar
_AVAIL_PATCHES = dict(
    check_conflict=MagicMock(return_value=("none", None)),
    check_outcall_conflict_with_travel=MagicMock(return_value=("none", None)),
)

# YES+name now chains into handle_check_availability (no empty messages + dead action).
# Stub it so unit tests do not need Postgres/calendar.
_STUB_AVAIL_RESULT = {
    "messages": ["[availability handler result]"],
    "new_state": "CHECKING_AVAILABILITY",
    "actions": [],
}


class TestYesConfirmsGoldenRule:
    """YES + valid name must always bypass the experience-type gate."""

    def test_yes_with_valid_name_routes_to_checking_availability(self):
        ctx = _build_ctx(message="YES", client_name="James")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result["new_state"] == "CHECKING_AVAILABILITY"
        assert result.get("messages")

    def test_yes_with_valid_name_sets_auto_confirm_flag(self):
        ctx = _build_ctx(message="YES", client_name="James")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            _no_experience_branch_response(ctx)
        # auto_confirm_without_experience is set via mark_awaiting_confirmation's
        # ``extra`` kwarg; fall back to update_fields call_args for legacy paths.
        mark_calls = ctx.state_manager.mark_awaiting_confirmation.call_args_list
        extras = [c.kwargs.get("extra") or {} for c in mark_calls]
        legacy_updates = {
            k: v
            for call in ctx.state_manager.update_fields.call_args_list
            for k, v in call[0][1].items()
        }
        merged = {**legacy_updates}
        for e in extras:
            merged.update(e)
        assert merged.get("auto_confirm_without_experience") is True

    def test_yes_case_insensitive_yep(self):
        ctx = _build_ctx(message="yep", client_name="Sarah")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result["new_state"] == "CHECKING_AVAILABILITY"

    def test_yes_case_insensitive_yeah(self):
        ctx = _build_ctx(message="yeah", client_name="Tom")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result["new_state"] == "CHECKING_AVAILABILITY"

    def test_yes_ok_is_treated_as_confirmation(self):
        ctx = _build_ctx(message="ok", client_name="Alex")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result["new_state"] == "CHECKING_AVAILABILITY"


class TestNoYesDoesNotBlock:
    """Without YES, the handler shows the booking summary (not a hard block)."""

    def test_no_yes_returns_preconfirm_summary_not_none(self):
        ctx = _build_ctx(message="sounds good to me", client_name="James")
        ctx.state_manager.transition.return_value = True
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch("core.webform_security.generate_secure_token", return_value={"short_code": "abc"}),
            patch("handlers.booking_coll._provide_field_stages_finish.get_base_url", return_value="https://example.com"),
            patch("templates.booking_reconfirmation.build_incall_preconfirm_summary", return_value="[summary]"),
        ):
            result = _no_experience_branch_response(ctx)
        # Must return something (not None) — handler shows the booking summary
        assert result is not None
        # Must NOT block — messages should be present and state should not be an error state
        assert result.get("new_state") != "NEW"

    def test_no_valid_name_and_yes_shows_summary_not_confirmation(self):
        # YES without a valid name → should NOT take the name+YES fast path (no auto_confirm without name).
        # We still move to CHECKING_AVAILABILITY after showing the summary so the next reply is routed
        # to availability_check instead of COLLECTING + empty-message fallback.
        ctx = _build_ctx(message="YES", client_name="")
        ctx.state_manager.transition.return_value = True
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch("core.webform_security.generate_secure_token", return_value={"short_code": "abc"}),
            patch("handlers.booking_coll._provide_field_stages_finish.get_base_url", return_value="https://example.com"),
            patch("templates.booking_reconfirmation.build_incall_preconfirm_summary", return_value="[summary]"),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"
        all_updates = {
            k: v
            for call in ctx.state_manager.update_fields.call_args_list
            for k, v in call[0][1].items()
        }
        assert all_updates.get("auto_confirm_without_experience") is not True


class TestExperienceTypeNeverBlocksConfirmed:
    """Even with no experience type, the booking must proceed when YES is given."""

    def test_experience_missing_does_not_block_yes_with_name(self):
        ctx = _build_ctx(message="YES", client_name="Marcus", experience_type="")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        # The golden rule: experience_type="" must NOT prevent confirmation when YES + valid name
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"

    def test_yes_with_name_already_in_state_confirms(self):
        # Name was captured in a prior message; this reply is just "YES"
        ctx = _build_ctx(message="YES", client_name="James")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"

    def test_yes_james_confirmation_format_works(self):
        # Client replies "YES James" — name must be extracted even without a prior state name
        ctx = _build_ctx(message="YES James", client_name="")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"

    def test_james_yes_confirmation_format_works(self):
        # Client replies "James YES" — same rule, name first
        ctx = _build_ctx(message="James YES", client_name="")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"

    def test_james_gfe_yes_format_works(self):
        # Client replies "James GFE YES" — name + experience + yes
        ctx = _build_ctx(message="James GFE YES", client_name="")
        with (
            patch("services.calendar_service.check_conflict", return_value=("none", None)),
            patch(
                "handlers.availability_check.handle_check_availability",
                return_value=_STUB_AVAIL_RESULT,
            ),
        ):
            result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result.get("new_state") == "CHECKING_AVAILABILITY"


class TestCalendarYesDegradedEscalation:
    """After dual availability failures, next YES should offer webform instead of looping."""

    def test_calendar_yes_degraded_escalates(self):
        ctx = _build_ctx(message="yes", client_name="James")
        ctx.state["calendar_yes_degraded"] = True
        result = _no_experience_branch_response(ctx)
        assert result is not None
        assert result["new_state"] == "COLLECTING"
        assert "booking here" in (result["messages"][0] or "").lower()
        ctx.state_manager.update_fields.assert_any_call(
            ctx.phone_number, {"calendar_yes_degraded": False}
        )
