"""
30 scenario definitions for conversational simulation.

Each scenario maps to a specific booking/conversation flow.
A scenario defines:
  - Ordered steps (user_intent + bot_intent per turn)
  - Expected FSM state sequence
  - Expected outcome
  - Whether failure injection is possible at each step
  - Canonical service/time/duration data used in the flow
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Outcome(str, Enum):
    CONFIRMED = "confirmed"
    ABANDONED = "abandoned"
    FAILED = "failed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"
    WRONG_NUMBER = "wrong_number"
    INQUIRY_ONLY = "inquiry_only"


class Category(str, Enum):
    BOOKING_SUCCESS = "booking_success"
    BOOKING_FAILURE = "booking_failure"
    INQUIRY = "inquiry"
    CANCEL_RESCHEDULE = "cancel_reschedule"
    EDGE_CASE = "edge_case"
    ADVERSARIAL = "adversarial"
    FAILURE_INJECTION = "failure_injection"


@dataclass
class ScenarioStep:
    step_id: str
    user_intent: str        # What the user is trying to communicate
    bot_intent: str         # What the bot should respond with
    fsm_transition: Optional[tuple[str, str]] = None  # (from_state, to_state) if transition occurs
    can_inject_failure: bool = False
    failure_type: Optional[str] = None  # "api_timeout"|"slot_unavailable"|"session_expiry"|etc.
    is_terminal: bool = False


@dataclass
class Scenario:
    id: str
    name: str
    category: Category
    expected_outcome: Outcome
    expected_fsm_trace: list[str]       # Ordered list of FSM states visited
    steps: list[ScenarioStep]
    # Canonical booking data (filled in by engine with variation)
    service: str = "incall"             # "incall"|"outcall"
    duration: str = "1hr"
    session_type: str = "standard"     # "standard"|"dinner_date"|"overnight"|"doubles"|"couples"
    day_of_week: str = "Saturday"
    time_of_day: str = "7pm"
    failure_injection: Optional[str] = None
    notes: str = ""


# ---------------------------------------------------------------------------
# 30 SCENARIO DEFINITIONS
# ---------------------------------------------------------------------------

SCENARIOS: list[Scenario] = [

    # -------------------------------------------------------------------------
    # 1. Simple incall 1-hour (happy path, new client)
    # -------------------------------------------------------------------------
    Scenario(
        id="incall_1hr_new_client",
        name="Simple 1-Hour Incall — New Client",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="1hr",
        session_type="standard",
        day_of_week="Saturday",
        time_of_day="7pm",
        steps=[
            ScenarioStep("open", "express_booking_intent", "welcome_and_ask_date"),
            ScenarioStep("date", "provide_date", "confirm_date_ask_time"),
            ScenarioStep("time", "provide_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "clarify_service_ask_confirm"),
            ScenarioStep("confirm", "confirm_all_details", "send_booking_confirmation",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 2. Incall 2-hour evening (returning client)
    # -------------------------------------------------------------------------
    Scenario(
        id="incall_2hr_returning",
        name="2-Hour Incall Evening — Returning Client",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="2hr",
        session_type="standard",
        day_of_week="Friday",
        time_of_day="8pm",
        steps=[
            ScenarioStep("open", "greeting_with_booking_intent", "personalised_returning_welcome"),
            ScenarioStep("date_time", "provide_date_and_time", "confirm_and_ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "specify_2hr", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "summarise_and_ask_final_confirm"),
            ScenarioStep("confirm", "confirm_all", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 3. Overnight booking
    # -------------------------------------------------------------------------
    Scenario(
        id="overnight_incall",
        name="Overnight Incall Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="overnight",
        session_type="overnight",
        day_of_week="Saturday",
        time_of_day="10pm",
        steps=[
            ScenarioStep("open", "enquire_overnight", "ask_overnight_details"),
            ScenarioStep("date", "provide_date", "confirm_overnight_ask_start_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("time", "provide_start_time", "clarify_overnight_end"),
            ScenarioStep("end", "confirm_overnight_duration", "ask_incall_or_outcall"),
            ScenarioStep("type", "confirm_incall", "confirm_overnight_summary"),
            ScenarioStep("confirm", "agree_to_overnight", "send_overnight_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 4. Outcall to hotel
    # -------------------------------------------------------------------------
    Scenario(
        id="outcall_hotel",
        name="Outcall to Hotel",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="outcall",
        duration="1hr",
        session_type="standard",
        day_of_week="Thursday",
        time_of_day="8pm",
        steps=[
            ScenarioStep("open", "request_outcall_hotel", "ask_hotel_location"),
            ScenarioStep("location", "provide_hotel_suburb", "check_outcall_area",
                         fsm_transition=("NEW", "AWAITING_DETAILS"), can_inject_failure=True,
                         failure_type="outcall_area_unavailable"),
            ScenarioStep("date", "provide_date", "ask_time"),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("duration", "provide_duration", "summarise_outcall_confirm"),
            ScenarioStep("confirm", "confirm_outcall_details", "send_outcall_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 5. Rates inquiry leading to booking
    # -------------------------------------------------------------------------
    Scenario(
        id="rates_then_booking",
        name="Rates Inquiry → Successful Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="1hr",
        session_type="standard",
        day_of_week="Wednesday",
        time_of_day="6pm",
        steps=[
            ScenarioStep("open", "ask_for_rates", "send_rates_profile_link"),
            ScenarioStep("follow_up", "express_interest_after_rates", "ask_preferred_date"),
            ScenarioStep("date", "provide_date", "confirm_date_ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("duration", "provide_duration", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "ask_final_confirm"),
            ScenarioStep("confirm", "confirm_all", "send_booking_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 6. Same-day availability check → immediate booking
    # -------------------------------------------------------------------------
    Scenario(
        id="same_day_booking",
        name="Same-Day Availability → Immediate Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="1hr",
        session_type="standard",
        day_of_week="Today",
        time_of_day="tonight",
        steps=[
            ScenarioStep("open", "ask_availability_today", "check_today_slots"),
            ScenarioStep("slot", "pick_available_slot", "confirm_slot_ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS"), can_inject_failure=True,
                         failure_type="slot_unavailable"),
            ScenarioStep("duration", "provide_duration", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "ask_final_confirm"),
            ScenarioStep("confirm", "agree", "send_booking_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 7. Future-date booking (2+ weeks ahead)
    # -------------------------------------------------------------------------
    Scenario(
        id="future_date_booking",
        name="Advance Booking (2+ Weeks Ahead)",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="2hr",
        session_type="standard",
        day_of_week="Saturday",
        time_of_day="6pm",
        steps=[
            ScenarioStep("open", "enquire_future_availability", "confirm_future_date_possible"),
            ScenarioStep("date", "provide_future_date", "confirm_date_ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("duration", "provide_2hr", "ask_type"),
            ScenarioStep("type", "confirm_incall", "ask_final_confirm"),
            ScenarioStep("confirm", "agree", "send_advance_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 8. 3-hour session booking
    # -------------------------------------------------------------------------
    Scenario(
        id="three_hour_session",
        name="3-Hour Session Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="3hr",
        session_type="standard",
        day_of_week="Friday",
        time_of_day="7pm",
        steps=[
            ScenarioStep("open", "request_extended_session", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "specify_3hr", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "clarify_3hr_details"),
            ScenarioStep("confirm", "agree_to_3hr", "send_3hr_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 9. Couples enquiry → booking
    # -------------------------------------------------------------------------
    Scenario(
        id="couples_inquiry_booking",
        name="Couples Enquiry → Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="2hr",
        session_type="couples",
        day_of_week="Saturday",
        time_of_day="8pm",
        steps=[
            ScenarioStep("open", "enquire_couples_experience", "explain_couples_booking"),
            ScenarioStep("confirm_interest", "confirm_couples_intent", "ask_date",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("date", "provide_date", "confirm_ask_time"),
            ScenarioStep("time", "provide_time", "ask_duration_couples"),
            ScenarioStep("duration", "provide_2hr", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "summarise_couples_confirm"),
            ScenarioStep("confirm", "agree", "send_couples_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 10. Doubles inquiry → booking
    # -------------------------------------------------------------------------
    Scenario(
        id="doubles_inquiry_booking",
        name="Doubles Enquiry → Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="incall",
        duration="2hr",
        session_type="doubles",
        day_of_week="Friday",
        time_of_day="9pm",
        steps=[
            ScenarioStep("open", "enquire_doubles", "explain_doubles_availability"),
            ScenarioStep("confirm_doubles", "confirm_doubles_intent", "ask_preferred_date",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("date", "provide_date", "ask_time"),
            ScenarioStep("time", "provide_time", "ask_duration_doubles"),
            ScenarioStep("duration", "provide_2hr", "clarify_doubles_confirm"),
            ScenarioStep("confirm", "agree_doubles", "send_doubles_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 11. Dinner date enquiry → booking
    # -------------------------------------------------------------------------
    Scenario(
        id="dinner_date_booking",
        name="Dinner Date Enquiry → Booking",
        category=Category.BOOKING_SUCCESS,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        service="outcall",
        duration="overnight",
        session_type="dinner_date",
        day_of_week="Saturday",
        time_of_day="7pm",
        steps=[
            ScenarioStep("open", "enquire_dinner_date", "explain_dinner_date"),
            ScenarioStep("interest", "express_interest", "ask_date_dinner_date",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("date", "provide_date", "confirm_dinner_date_ask_time"),
            ScenarioStep("time", "provide_time", "ask_location"),
            ScenarioStep("location", "provide_restaurant_suburb", "summarise_dinner_date"),
            ScenarioStep("confirm", "agree_dinner_date", "send_dinner_date_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 12. Booking then client cancels
    # -------------------------------------------------------------------------
    Scenario(
        id="booking_then_cancel",
        name="Booking Confirmed → Client Cancels",
        category=Category.CANCEL_RESCHEDULE,
        expected_outcome=Outcome.ABANDONED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED", "CANCELLED"],
        service="incall",
        duration="1hr",
        session_type="standard",
        day_of_week="Thursday",
        time_of_day="7pm",
        steps=[
            ScenarioStep("open", "express_booking_intent", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_type"),
            ScenarioStep("type", "confirm_incall", "ask_final_confirm"),
            ScenarioStep("confirm", "agree", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED")),
            ScenarioStep("cancel_request", "request_cancellation", "process_cancellation",
                         fsm_transition=("CONFIRMED", "CANCELLED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 13. Reschedule request
    # -------------------------------------------------------------------------
    Scenario(
        id="reschedule_request",
        name="Confirmed Booking → Reschedule Request",
        category=Category.CANCEL_RESCHEDULE,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED", "RESCHEDULED", "CONFIRMED"],
        service="incall",
        duration="1hr",
        session_type="standard",
        day_of_week="Tuesday",
        time_of_day="6pm",
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_type"),
            ScenarioStep("type", "confirm_incall", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED")),
            ScenarioStep("reschedule", "request_reschedule", "ask_new_date",
                         fsm_transition=("CONFIRMED", "RESCHEDULED")),
            ScenarioStep("new_date", "provide_new_date", "confirm_reschedule",
                         fsm_transition=("RESCHEDULED", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 14. Rate negotiation (rejected)
    # -------------------------------------------------------------------------
    Scenario(
        id="rate_negotiation_rejected",
        name="Rate Negotiation — Rejected → Abandon",
        category=Category.INQUIRY,
        expected_outcome=Outcome.ABANDONED,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("open", "ask_about_discount", "send_fixed_rates_no_negotiation"),
            ScenarioStep("push_back", "push_for_discount", "politely_decline_again"),
            ScenarioStep("second_push", "push_harder", "firm_no_negotiation"),
            ScenarioStep("abandon", "give_up_or_accept", "offer_standard_booking", is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 15. Completely wrong number
    # -------------------------------------------------------------------------
    Scenario(
        id="wrong_number_scenario",
        name="Wrong Number — Polite Redirect",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.WRONG_NUMBER,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("open", "message_intended_for_someone_else", "clarify_this_is_booking_service"),
            ScenarioStep("realise", "client_realises_wrong_number", "confirm_and_offer_opt_out",
                         is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 16. Jailbreak attempt
    # -------------------------------------------------------------------------
    Scenario(
        id="jailbreak_attempt",
        name="AI Jailbreak Attempt — Blocked & Recovered",
        category=Category.ADVERSARIAL,
        expected_outcome=Outcome.BLOCKED,
        expected_fsm_trace=["NEW", "ESCALATED"],
        steps=[
            ScenarioStep("jailbreak_1", "attempt_jailbreak", "politely_decline_redirect_to_booking"),
            ScenarioStep("jailbreak_2", "escalate_jailbreak_attempt", "maintain_policy_deflect"),
            ScenarioStep("jailbreak_3", "third_attempt_jailbreak", "trigger_escalation",
                         fsm_transition=("NEW", "ESCALATED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 17. Prompt injection attempt
    # -------------------------------------------------------------------------
    Scenario(
        id="prompt_injection_attempt",
        name="Prompt Injection — Detected & Deflected",
        category=Category.ADVERSARIAL,
        expected_outcome=Outcome.BLOCKED,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("inject_1", "embed_injection_in_message", "ignore_injection_respond_normally"),
            ScenarioStep("inject_2", "second_injection_attempt", "remain_unaffected"),
            ScenarioStep("redirect", "injection_fails_client_may_book", "offer_normal_booking",
                         is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 18. Abusive messages → escalation
    # -------------------------------------------------------------------------
    Scenario(
        id="abuse_to_escalation",
        name="Abusive Messages → Escalation → Block",
        category=Category.ADVERSARIAL,
        expected_outcome=Outcome.ESCALATED,
        expected_fsm_trace=["NEW", "ESCALATED"],
        steps=[
            ScenarioStep("abuse_1", "send_abusive_message", "warn_professionally"),
            ScenarioStep("abuse_2", "continue_abuse", "final_warning"),
            ScenarioStep("abuse_3", "third_abusive_message", "escalate_and_block",
                         fsm_transition=("NEW", "ESCALATED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 19. Ghost mid-conversation
    # -------------------------------------------------------------------------
    Scenario(
        id="ghost_mid_booking",
        name="Client Ghosts During Booking Flow",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.ABANDONED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS"],
        steps=[
            ScenarioStep("open", "express_booking_intent", "welcome_ask_date"),
            ScenarioStep("date", "provide_date", "ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("ghost", "stop_responding", "send_follow_up_nudge", is_terminal=True),
        ],
        notes="Client vanishes after providing partial details.",
    ),

    # -------------------------------------------------------------------------
    # 20. Return after ghosting
    # -------------------------------------------------------------------------
    Scenario(
        id="return_after_ghost",
        name="Client Returns After Ghosting Hours Later",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        steps=[
            ScenarioStep("open", "express_booking_intent", "welcome_ask_date"),
            ScenarioStep("date", "provide_date", "ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("ghost", "disappear_for_hours", "send_follow_up"),
            ScenarioStep("return", "return_with_apology", "context_recovery_resume"),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("confirm", "confirm_all", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 21. Booking API failure → retry → success
    # -------------------------------------------------------------------------
    Scenario(
        id="api_failure_retry_success",
        name="Booking API Failure → Retry → Success",
        category=Category.FAILURE_INJECTION,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        failure_injection="api_timeout",
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_type"),
            ScenarioStep("type", "confirm_incall", "attempt_booking_api_call",
                         can_inject_failure=True, failure_type="api_timeout"),
            ScenarioStep("retry", "wait_for_retry", "retry_booking_succeed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
        notes="API times out on first attempt; bot retries and succeeds.",
    ),

    # -------------------------------------------------------------------------
    # 22. Session expiry mid-conversation
    # -------------------------------------------------------------------------
    Scenario(
        id="session_expiry_mid_flow",
        name="Session Expires Mid-Booking",
        category=Category.FAILURE_INJECTION,
        expected_outcome=Outcome.FAILED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS"],
        failure_injection="session_expiry",
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("date", "provide_date", "ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("session_dies", "message_arrives_in_expired_session",
                         "notify_session_expired_restart", is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 23. Duplicate booking attempt
    # -------------------------------------------------------------------------
    Scenario(
        id="duplicate_booking_attempt",
        name="Duplicate Booking Detected",
        category=Category.FAILURE_INJECTION,
        expected_outcome=Outcome.FAILED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        failure_injection="duplicate_booking",
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_type"),
            ScenarioStep("type", "confirm_incall", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED")),
            ScenarioStep("second_attempt", "try_to_book_same_slot_again",
                         "detect_duplicate_inform_client", is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 24. Invalid date (Feb 30, past date, etc.)
    # -------------------------------------------------------------------------
    Scenario(
        id="invalid_date_request",
        name="Invalid Date Request — Recovery",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("bad_date", "provide_invalid_date", "politely_flag_invalid_date_ask_again"),
            ScenarioStep("good_date", "provide_valid_date", "confirm_date_ask_time",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("confirm", "confirm_all", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 25. Request outside available hours (3am booking)
    # -------------------------------------------------------------------------
    Scenario(
        id="outside_hours_request",
        name="Out-of-Hours Booking Request",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.ABANDONED,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("open", "request_3am_booking", "explain_hours_unavailable"),
            ScenarioStep("push_back", "ask_for_exception", "repeat_hours_offer_alternatives"),
            ScenarioStep("abandon_or_rebook", "accept_or_abandon", "offer_daytime_slot",
                         is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 26. Extremely vague request requiring many clarifications
    # -------------------------------------------------------------------------
    Scenario(
        id="vague_request_clarification",
        name="Highly Vague Request — Multiple Clarifications",
        category=Category.EDGE_CASE,
        expected_outcome=Outcome.CONFIRMED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS", "CONFIRMED"],
        steps=[
            ScenarioStep("open", "very_vague_intent", "ask_clarifying_question_1"),
            ScenarioStep("vague_1", "still_vague", "ask_clarifying_question_2"),
            ScenarioStep("clearer", "slightly_clearer", "ask_date",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("date", "provide_date", "ask_time"),
            ScenarioStep("time", "provide_time", "ask_duration"),
            ScenarioStep("duration", "provide_duration", "ask_incall_outcall"),
            ScenarioStep("type", "confirm_incall", "send_confirmed",
                         fsm_transition=("AWAITING_DETAILS", "CONFIRMED"), is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 27. Location inquiry
    # -------------------------------------------------------------------------
    Scenario(
        id="location_inquiry_only",
        name="Location Inquiry (No Booking)",
        category=Category.INQUIRY,
        expected_outcome=Outcome.INQUIRY_ONLY,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("open", "ask_about_location", "explain_incall_location_approach"),
            ScenarioStep("follow_up", "ask_specific_suburb", "clarify_location_policy"),
            ScenarioStep("end", "satisfied_with_answer_or_leave", "offer_to_book",
                         is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 28. Multi-service inquiry (asks about everything)
    # -------------------------------------------------------------------------
    Scenario(
        id="multi_service_inquiry",
        name="Multiple Service Inquiries",
        category=Category.INQUIRY,
        expected_outcome=Outcome.INQUIRY_ONLY,
        expected_fsm_trace=["NEW"],
        steps=[
            ScenarioStep("open", "ask_about_all_services", "send_full_info_profile"),
            ScenarioStep("couples", "ask_about_couples", "explain_couples"),
            ScenarioStep("doubles", "ask_about_doubles", "explain_doubles"),
            ScenarioStep("overnight", "ask_about_overnight", "explain_overnight"),
            ScenarioStep("end", "thank_and_maybe_book_later", "offer_booking_link",
                         is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 29. Deposit/payment issue during booking
    # -------------------------------------------------------------------------
    Scenario(
        id="deposit_payment_issue",
        name="Deposit/Payment Issue Mid-Booking",
        category=Category.FAILURE_INJECTION,
        expected_outcome=Outcome.FAILED,
        expected_fsm_trace=["NEW", "AWAITING_DETAILS"],
        failure_injection="payment_failure",
        steps=[
            ScenarioStep("open", "request_booking", "welcome_ask_date"),
            ScenarioStep("date_time", "provide_date_time", "ask_duration",
                         fsm_transition=("NEW", "AWAITING_DETAILS")),
            ScenarioStep("duration", "provide_duration", "ask_type"),
            ScenarioStep("type", "confirm_incall", "request_deposit"),
            ScenarioStep("deposit_issue", "report_payment_problem", "escalate_payment_issue",
                         can_inject_failure=True, failure_type="payment_failure", is_terminal=True),
        ],
    ),

    # -------------------------------------------------------------------------
    # 30. Spam bomb → rate limit → block
    # -------------------------------------------------------------------------
    Scenario(
        id="spam_rate_limit_block",
        name="Spam Flooding → Rate-Limit → Block",
        category=Category.ADVERSARIAL,
        expected_outcome=Outcome.BLOCKED,
        expected_fsm_trace=["NEW", "ESCALATED"],
        steps=[
            ScenarioStep("spam_1", "send_spam_burst", "rate_limit_warning"),
            ScenarioStep("spam_2", "continue_spam", "second_warning"),
            ScenarioStep("spam_3", "continued_flooding", "trigger_rate_limit_block",
                         fsm_transition=("NEW", "ESCALATED"), is_terminal=True),
        ],
    ),
]

SCENARIO_BY_ID: dict[str, Scenario] = {s.id: s for s in SCENARIOS}
