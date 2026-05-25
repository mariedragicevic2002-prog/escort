"""
Core conversation engine.

ConversationEngine takes a persona + scenario + seed → ConversationLog.

The engine:
- Selects realistic utterances from dialogue_banks, shaped by persona style
- Applies persona-level typos / emoji / slang
- Follows the scenario step sequence
- Injects failures where applicable
- Tracks FSM state through transitions
- Terminates when a step is_terminal or an unrecoverable failure occurs
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .personas import Persona
from .scenarios import Scenario, ScenarioStep, Outcome
from .failure_modes import InjectedFailure, FailureMode, FailureInjector
from . import dialogue_banks as db


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    speaker: str          # "USER" or "BOT"
    text: str
    step_id: str = ""
    fsm_state: str = "NEW"
    injected_failure: Optional[str] = None


@dataclass
class ConversationLog:
    conversation_id: str
    persona_id: str
    scenario_id: str
    seed: int
    turns: list[Turn] = field(default_factory=list)
    fsm_trace: list[str] = field(default_factory=list)
    outcome: str = "unknown"
    failure_injected: Optional[str] = None
    total_turns: int = 0
    notes: str = ""

    def add_turn(self, speaker: str, text: str, step_id: str = "",
                 fsm_state: str = "NEW", failure: Optional[str] = None) -> None:
        self.turns.append(Turn(speaker, text, step_id, fsm_state, failure))
        self.total_turns += 1

    def as_transcript(self) -> str:
        """Return a human-readable transcript."""
        lines: list[str] = [
            f"=== Conversation {self.conversation_id} ===",
            f"Persona : {self.persona_id}",
            f"Scenario: {self.scenario_id}",
            f"Outcome : {self.outcome}",
            f"Seed    : {self.seed}",
        ]
        if self.failure_injected:
            lines.append(f"Failure : {self.failure_injected}")
        lines.append("-" * 60)
        for turn in self.turns:
            state_tag = f"[{turn.fsm_state}]" if turn.fsm_state else ""
            fail_tag = f" ⚠ {turn.injected_failure}" if turn.injected_failure else ""
            lines.append(f"{turn.speaker:5} {state_tag:20} {turn.text}{fail_tag}")
        lines.append("=" * 60)
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "persona_id": self.persona_id,
            "scenario_id": self.scenario_id,
            "seed": self.seed,
            "outcome": self.outcome,
            "failure_injected": self.failure_injected,
            "total_turns": self.total_turns,
            "fsm_trace": self.fsm_trace,
            "notes": self.notes,
            "turns": [
                {
                    "speaker": t.speaker,
                    "text": t.text,
                    "step_id": t.step_id,
                    "fsm_state": t.fsm_state,
                    "injected_failure": t.injected_failure,
                }
                for t in self.turns
            ],
        }


# ---------------------------------------------------------------------------
# Date / time helpers
# ---------------------------------------------------------------------------

_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_TIMES = ["1pm", "2pm", "3pm", "4pm", "5pm", "6pm", "7pm", "8pm", "9pm", "10pm"]
_ALT_TIMES = ["3pm", "4pm", "5pm", "6pm", "8pm", "9pm"]
_SUBURBS = [
    "the CBD", "Southbank", "St Kilda", "South Yarra", "Richmond",
    "Fitzroy", "Carlton", "Prahran", "Toorak", "Docklands",
]


def _rand_day(rng: random.Random) -> str:
    return rng.choice(_DAYS)


def _rand_time(rng: random.Random) -> str:
    return rng.choice(_TIMES)


def _rand_suburb(rng: random.Random) -> str:
    return rng.choice(_SUBURBS)


# ---------------------------------------------------------------------------
# Typo / style helpers
# ---------------------------------------------------------------------------

def _apply_typos(text: str, typo_rate: float, rng: random.Random) -> str:
    """Randomly corrupt individual words based on typo_rate."""
    if typo_rate == 0:
        return text
    words = text.split()
    result = []
    for word in words:
        lower = word.lower()
        if lower in db.COMMON_TYPOS and rng.random() < typo_rate:
            replacement = rng.choice(db.COMMON_TYPOS[lower])
            # Preserve original capitalisation (first letter)
            if word[0].isupper():
                replacement = replacement.capitalize()
            result.append(replacement)
        else:
            result.append(word)
    return " ".join(result)


def _maybe_add_emoji(text: str, emoji_rate: float, rng: random.Random) -> str:
    """Append a random emoji to the text based on emoji_rate."""
    emojis = ["😊", "😍", "🙏", "💕", "✨", "🥰", "👍", "🙌", "😎", "💋", "🌸", "🎉"]
    if rng.random() < emoji_rate:
        text = text + " " + rng.choice(emojis)
    return text


def _style_user_msg(text: str, persona: Persona, rng: random.Random) -> str:
    """Apply persona-level styling (typos, emoji, case) to a user message."""
    text = _apply_typos(text, persona.typo_rate, rng)
    text = _maybe_add_emoji(text, persona.emoji_rate, rng)
    if persona.style in ("terse", "casual") and rng.random() < 0.3:
        text = text.lower()
    if persona.style == "rude" and rng.random() < 0.2:
        text = text.upper()
    return text


# ---------------------------------------------------------------------------
# Template filler
# ---------------------------------------------------------------------------

def _fill(template: str, **kwargs) -> str:
    """Fill {placeholder} tokens in a string, leaving unknown ones intact."""
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError):
        return template


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class ConversationEngine:
    """
    Produces a single ConversationLog from a Persona + Scenario + integer seed.

    Parameters
    ----------
    failure_injector : FailureInjector | None
        If provided, the engine asks the injector whether to inject a failure.
    """

    def __init__(self, failure_injector: Optional[FailureInjector] = None) -> None:
        self._fi = failure_injector

    # ------------------------------------------------------------------
    def generate(self, persona: Persona, scenario: Scenario,
                 seed: int, conv_id: str) -> ConversationLog:
        rng = random.Random(seed)
        log = ConversationLog(
            conversation_id=conv_id,
            persona_id=persona.id,
            scenario_id=scenario.id,
            seed=seed,
        )

        # Canonical booking variables (vary per seed)
        day = rng.choice(["Saturday", "Friday", "Thursday", "Wednesday", "Sunday", "Tuesday"])
        time = rng.choice(["6pm", "7pm", "8pm", "9pm", "5pm"])
        duration = scenario.duration
        service = scenario.service
        suburb = _rand_suburb(rng)
        alt_time = rng.choice([t for t in _TIMES if t != time])

        ctx = dict(
            day=day, time=time, duration=duration, service=service,
            suburb=suburb, alt_time=alt_time,
            date_num=rng.randint(1, 28),
            month=rng.choice(["January", "February", "March", "April", "May", "June",
                               "July", "August", "September", "October", "November", "December"]),
            time_of_day=rng.choice(["morning", "afternoon", "evening"]),
        )

        # Decide failure injection
        injected_failure: Optional[InjectedFailure] = None
        if self._fi and scenario.failure_injection:
            injected_failure = self._fi.maybe_inject(scenario.failure_injection)
        elif self._fi:
            injected_failure = self._fi.maybe_inject()

        if injected_failure:
            log.failure_injected = injected_failure.mode.value

        fsm_state = "NEW"
        log.fsm_trace.append(fsm_state)

        for step_idx, step in enumerate(scenario.steps):
            # Apply failure at the designated step
            if (injected_failure and
                    step_idx == injected_failure.occurs_at_step and
                    injected_failure.user_visible):
                # Inject a bot failure message before the normal bot reply
                user_msg = self._build_user_msg(step, persona, scenario, ctx, rng)
                log.add_turn("USER", user_msg, step.step_id, fsm_state)
                log.add_turn("BOT", injected_failure.bot_error_message,
                             step.step_id + "_failure", fsm_state,
                             failure=injected_failure.mode.value)
                if injected_failure.recoverable and injected_failure.retry_succeeds:
                    # Bot retries — produce the normal bot reply as well
                    bot_reply = self._build_bot_reply(step, persona, scenario, ctx, rng, fsm_state)
                    log.add_turn("BOT", bot_reply, step.step_id + "_retry", fsm_state)
                else:
                    # Non-recoverable: override outcome and terminate
                    log.outcome = injected_failure.outcome_override or "failed"
                    return log
            else:
                user_msg = self._build_user_msg(step, persona, scenario, ctx, rng)
                log.add_turn("USER", user_msg, step.step_id, fsm_state)
                bot_reply = self._build_bot_reply(step, persona, scenario, ctx, rng, fsm_state)
                log.add_turn("BOT", bot_reply, step.step_id, fsm_state)

            # Advance FSM state if this step triggers a transition
            if step.fsm_transition:
                fsm_state = step.fsm_transition[1]
                if fsm_state not in log.fsm_trace:
                    log.fsm_trace.append(fsm_state)

            if step.is_terminal:
                break

        log.outcome = scenario.expected_outcome.value
        return log

    # ------------------------------------------------------------------
    # User message construction
    # ------------------------------------------------------------------

    def _build_user_msg(self, step: ScenarioStep, persona: Persona,
                        scenario: Scenario, ctx: dict, rng: random.Random) -> str:
        intent = step.user_intent
        style = persona.style
        msg = self._intent_to_user_phrase(intent, style, scenario, ctx, rng, persona)
        return _style_user_msg(msg, persona, rng)

    def _intent_to_user_phrase(self, intent: str, style: str, scenario: Scenario,
                                ctx: dict, rng: random.Random, persona: Persona) -> str:
        """Map a user intent string to a realistic phrase."""

        # Pull from persona's own phrase banks first (adds variety)
        if intent == "express_booking_intent" and persona.openers:
            return _fill(rng.choice(persona.openers), **ctx)
        if intent == "greeting_with_booking_intent" and persona.openers:
            return _fill(rng.choice(persona.openers), **ctx)
        if intent == "request_booking" and persona.openers:
            return _fill(rng.choice(persona.openers), **ctx)

        # Fallback to dialogue banks
        mapping: dict[str, list[str]] = {
            "express_booking_intent":       db.OPENERS_FRIENDLY if style == "friendly" else db.OPENERS_CASUAL,
            "greeting_with_booking_intent": db.OPENERS_FRIENDLY,
            "request_booking":              db.OPENERS_CASUAL,
            "provide_date":                 db.DATE_RESPONSES_NORMAL if style != "casual" else db.DATE_RESPONSES_CASUAL,
            "provide_date_and_time":        db.DATE_RESPONSES_NORMAL,
            "provide_date_time":            db.DATE_RESPONSES_NORMAL,  # alias
            "provide_time":                 db.TIME_RESPONSES_NORMAL,
            "provide_duration":             [db.get_duration_phrase(ctx["duration"], rng)],
            "specify_2hr":                  db.DURATION_RESPONSES.get("2hr", ["2 hours"]),
            "provide_2hr":                  db.DURATION_RESPONSES.get("2hr", ["2 hours"]),  # alias
            "specify_3hr":                  db.DURATION_RESPONSES.get("3hr", ["3 hours"]),
            "confirm_incall":               db.INCALL_RESPONSES,
            "confirm_all":                  db.CONFIRM_NEUTRAL if style != "friendly" else db.CONFIRM_EAGER,
            "confirm_all_details":          db.CONFIRM_NEUTRAL,
            "agree":                        db.CONFIRM_NEUTRAL,
            "agree_to_3hr":                 db.CONFIRM_EAGER,
            "agree_to_overnight":           db.CONFIRM_EAGER,
            "agree_doubles":                db.CONFIRM_EAGER,
            "agree_dinner_date":            db.CONFIRM_EAGER,
            "ask_for_rates":                db.OPENERS_RATES,
            "express_interest_after_rates": ["looks good, I'd like to book", "interested, how do I book?"],
            "ask_availability_today":       ["are you free tonight?", "any availability today?", "tonight?"],
            "pick_available_slot":          [f"I'll take {ctx['time']}", f"can do {ctx['time']}"],
            "request_cancellation":         db.CANCELLATION_POLITE if persona.patience in ("high", "medium") else db.CANCELLATION_ABRUPT,
            "request_reschedule":           db.RESCHEDULE_PHRASES,
            "provide_new_date":             db.DATE_RESPONSES_NORMAL,
            "ask_about_discount":           db.PUSHBACK_PRICE,
            "push_for_discount":            db.PUSHBACK_PRICE,
            "second_push":                  db.PUSHBACK_PRICE,
            "abandon_or_accept":            db.ABANDONMENT_SOFT,
            "message_intended_for_someone_else": db.OPENERS_WRONG_NUMBER,
            "realise":                      ["oh gosh sorry, wrong number!", "my bad, sorry!"],
            "attempt_jailbreak":            db.OPENERS_ADVERSARIAL,
            "escalate_jailbreak_attempt":   db.OPENERS_ADVERSARIAL,
            "third_attempt_jailbreak":      db.OPENERS_ADVERSARIAL,
            "embed_injection_in_message":   db.OPENERS_ADVERSARIAL,
            "second_injection_attempt":     db.OPENERS_ADVERSARIAL,
            "injection_fails_client_may_book": ["ok fine, can I just book normally?"],
            "send_abusive_message":         db.ABUSE_MESSAGES,
            "continue_abuse":               db.ABUSE_MESSAGES,
            "third_abusive_message":        db.ABUSE_MESSAGES,
            "send_spam_burst":              db.SPAM_MESSAGES,
            "continue_spam":                db.SPAM_MESSAGES,
            "continued_flooding":           db.SPAM_MESSAGES,
            "stop_responding":              ["…"],
            "ghost":                        ["…"],
            "disappear_for_hours":          ["…"],
            "return_with_apology":          ["hey sorry i disappeared earlier", "hi, so sorry about that"],
            "return":                       ["hey i'm back, can we continue?", "sorry, still here"],
            "very_vague_intent":            db.OPENERS_CONFUSED,
            "still_vague":                  ["yeah something like that", "not sure really", "i think so?"],
            "slightly_clearer":             ["oh ok so yeah, i want to book"],
            "enquire_overnight":            ["hi do you do overnight sessions?", "how does overnight work?"],
            "enquire_couples_experience":   ["do you do couples?", "wondering about couples sessions"],
            "confirm_couples_intent":       ["yes, a couples session please"],
            "enquire_doubles":              ["do you offer doubles?", "is doubles available?"],
            "confirm_doubles_intent":       ["yes please, doubles"],
            "enquire_dinner_date":          ["do you do dinner dates?", "how does a dinner date work?"],
            "express_interest":             ["yeah that sounds lovely!", "that works, I'm keen"],
            "enquire_future_availability":  ["hi, I'd like to book for a few weeks time"],
            "provide_future_date":          [f"how about {ctx['day']} in a couple of weeks?"],
            "request_extended_session":     ["hi, I'd like to book a longer session"],
            "request_outcall_hotel":        [f"hi, could you do outcall to my hotel in {ctx['suburb']}?"],
            "provide_hotel_suburb":         [ctx.get("suburb", "the CBD")],
            "ask_about_location":           ["where are you based?", "what's the location?"],
            "ask_specific_suburb":          ["do you know the suburb?", "are you in {suburb}?"],
            "satisfied_with_answer_or_leave": ["ok thanks!", "good to know"],
            "ask_about_all_services":       ["hi, what do you offer?", "can you tell me about all your services?"],
            "ask_about_couples":            ["what exactly are couples sessions?"],
            "ask_about_doubles":            ["what's included in doubles?"],
            "ask_about_overnight":          ["tell me more about overnight"],
            "thank_and_maybe_book_later":   ["great thanks, I'll reach out when ready"],
            "report_payment_problem":       ["something went wrong with payment", "payment isn't going through"],
            "confirm_overnight_duration":   ["yes, the full night"],
            "confirm_date":                 [ctx.get("day", "Saturday")],
            "request_3am_booking":          ["can I book at 3am?", "are you free at 3 in the morning?"],
            "ask_for_exception":            ["even just a one-off?", "could you make an exception?"],
            "accept_or_abandon":            db.ABANDONMENT_SOFT,
            "provide_invalid_date":         db.DATE_RESPONSES_INVALID,
            "provide_valid_date":           db.DATE_RESPONSES_NORMAL,
            "wait_for_retry":               ["ok", "sure, take your time"],
            "message_arrives_in_expired_session": ["hello? still there?"],
            "try_to_book_same_slot_again":  [f"actually can I also book {ctx['time']} on {ctx['day']}?"],
            "second_attempt":               [f"hmm, could I add another booking for {ctx['time']}?"],
            "give_up_or_accept":            db.ABANDONMENT_SOFT,
            "client_realises_wrong_number": ["oh gosh sorry, wrong number!", "my bad, wrong number sorry!", "oops!! sorry about that"],
            "confirm_outcall_details":      db.CONFIRM_NEUTRAL + [f"Yes, outcall to {ctx.get('suburb','the CBD')} on {ctx.get('day','Saturday')} at {ctx.get('time','7pm')} — confirmed"],
            "provide_restaurant_suburb":    [ctx.get("suburb", "Southbank"), f"we're doing dinner in {ctx.get('suburb','the CBD')}"],
            "provide_start_time":           db.TIME_RESPONSES_NORMAL,
            "push_harder":                  db.PUSHBACK_PRICE + ["seriously though, any flexibility?", "come on, even just a little?"],
        }

        bank = mapping.get(intent)
        if bank:
            choice = rng.choice(bank)
            return _fill(choice, **ctx)

        # Absolute fallback
        return f"[{intent}]"

    # ------------------------------------------------------------------
    # Bot reply construction
    # ------------------------------------------------------------------

    def _build_bot_reply(self, step: ScenarioStep, persona: Persona,
                         scenario: Scenario, ctx: dict, rng: random.Random,
                         fsm_state: str) -> str:
        intent = step.bot_intent
        return self._intent_to_bot_phrase(intent, ctx, rng)

    def _intent_to_bot_phrase(self, intent: str, ctx: dict, rng: random.Random) -> str:
        day = ctx.get("day", "Saturday")
        time = ctx.get("time", "7pm")
        duration = ctx.get("duration", "1hr")
        service = ctx.get("service", "incall")
        alt_time = ctx.get("alt_time", "6pm")
        suburb = ctx.get("suburb", "the CBD")

        mapping: dict[str, list[str]] = {
            "welcome_and_ask_date":           db.BOT_WELCOME_NEW + db.BOT_ASK_DATE,
            "welcome_ask_date":               db.BOT_WELCOME_NEW,
            "personalised_returning_welcome": db.BOT_WELCOME_RETURNING,
            "confirm_date_ask_time":          db.BOT_ASK_TIME,
            "ask_time":                       db.BOT_ASK_TIME,
            "ask_duration":                   db.BOT_ASK_DURATION,
            "ask_incall_outcall":             db.BOT_ASK_INCALL_OUTCALL,
            "ask_incall_or_outcall":          db.BOT_ASK_INCALL_OUTCALL,
            "clarify_service_ask_confirm":    ["Is that incall or outcall? And shall I confirm the booking?"],
            "ask_final_confirm":              [f"So that's {day} at {time} for {duration} ({service}) — shall I confirm that?"],
            "send_booking_confirmation":      [db.get_bot_confirmation(day, time, duration, service, rng)],
            "send_confirmed":                 [db.get_bot_confirmation(day, time, duration, service, rng)],
            "send_advance_confirmed":         [db.get_bot_confirmation(day, time, duration, service, rng)],
            "send_3hr_confirmed":             [db.get_bot_confirmation(day, time, "3hr", service, rng)],
            "send_overnight_confirmed":       [db.get_bot_confirmation(day, time, "overnight", service, rng)],
            "send_couples_confirmed":         [db.get_bot_confirmation(day, time, "2hr", "incall couples", rng)],
            "send_doubles_confirmed":         [db.get_bot_confirmation(day, time, "2hr", "incall doubles", rng)],
            "send_dinner_date_confirmed":     [db.get_bot_confirmation(day, time, "dinner date", "outcall", rng)],
            "send_outcall_confirmed":         [db.get_bot_confirmation(day, time, duration, f"outcall to {suburb}", rng)],
            "confirm_date_ask_time":          [f"Perfect! {day} it is. What time were you thinking?"],
            "confirm_and_ask_duration":       [f"Great, {day} at {time} — how long did you want?"],
            "ask_preferred_date":             db.BOT_ASK_DATE,
            "summarise_and_ask_final_confirm": [f"Okay so {day} at {time} for {duration} {service} — does that all sound right?"],
            "confirm_overnight_ask_start_time": [f"{day} overnight — what time would you like to start?"],
            "clarify_overnight_end":          ["And would that be until morning the next day?"],
            "ask_incall_or_outcall":          db.BOT_ASK_INCALL_OUTCALL,
            "confirm_overnight_summary":      [f"So overnight from {time} on {day} — {service}. Shall I confirm that?"],
            "ask_hotel_location":             [f"Of course! Which suburb or hotel are you at?"],
            "check_outcall_area":             [f"Let me check if I can get to {suburb}… yes, that works!"],
            "summarise_outcall_confirm":      [f"{day} at {time} for {duration} outcall to {suburb} — shall I confirm?"],
            "send_rates_profile_link":        db.BOT_SEND_RATES,
            "check_today_slots":              [f"Let me check… yes, I have {time} available tonight!"],
            "confirm_slot_ask_duration":      db.BOT_ASK_DURATION,
            "ask_overnight_details":          ["Sure! What date were you thinking for the overnight?"],
            "explain_overnight":              ["An overnight runs from a set time in the evening right through to morning — it's a full-night experience 💕"],
            "explain_couples_booking":        ["Couples sessions are for you and your partner together — a shared experience! 😊"],
            "ask_date":                       db.BOT_ASK_DATE,
            "confirm_ask_time":               db.BOT_ASK_TIME,
            "ask_duration_couples":           db.BOT_ASK_DURATION,
            "summarise_couples_confirm":      [f"So {day} at {time} for {duration} couples incall — does that work?"],
            "explain_doubles_availability":   ["Yes I do doubles with a selected partner — it's a duo experience!"],
            "ask_preferred_date":             db.BOT_ASK_DATE,
            "ask_duration_doubles":           db.BOT_ASK_DURATION,
            "clarify_doubles_confirm":        [f"{day} at {time} for {duration} doubles — shall I confirm?"],
            "explain_dinner_date":            ["Dinner date is a romantic evening out — dinner, drinks, and an overnight stay 🌹"],
            "ask_date_dinner_date":           db.BOT_ASK_DATE,
            "confirm_dinner_date_ask_time":   db.BOT_ASK_TIME,
            "ask_location":                   [f"Lovely! What area/suburb will we be dining in?"],
            "summarise_dinner_date":          [f"So dinner date on {day} at {time} in {suburb} — shall I lock that in?"],
            "process_cancellation":           [db.BOT_CANCEL_CONFIRM[0].format(day=day)],
            "ask_new_date":                   db.BOT_RESCHEDULE_ASK,
            "confirm_reschedule":             [db.get_bot_confirmation(day, time, duration, service, rng)],
            "send_fixed_rates_no_negotiation": db.BOT_NO_NEGOTIATION,
            "politely_decline_again":         db.BOT_NO_NEGOTIATION,
            "firm_no_negotiation":            ["I'm afraid pricing is fixed — take it or leave it! Happy to book at the standard rate though 😊"],
            "offer_standard_booking":         ["Would you like to proceed at the standard rate?"],
            "clarify_this_is_booking_service": db.BOT_WRONG_NUMBER,
            "confirm_and_offer_opt_out":      ["No worries at all! You can ignore this message. Take care! 😊"],
            "politely_decline_redirect_to_booking": db.BOT_JAILBREAK_DEFLECT,
            "maintain_policy_deflect":        db.BOT_JAILBREAK_DEFLECT,
            "trigger_escalation":             db.BOT_ESCALATION,
            "ignore_injection_respond_normally": ["Hey! Did you want to make a booking?"],
            "remain_unaffected":              ["I'm just a booking assistant 😊 Can I help you book a session?"],
            "offer_normal_booking":           db.BOT_ASK_DATE,
            "warn_professionally":            db.BOT_ABUSE_WARNING,
            "final_warning":                  ["I'm asking you to keep things respectful or I'll need to end this conversation."],
            "escalate_and_block":             db.BOT_ESCALATION,
            "rate_limit_warning":             ["Please slow down — I can only process one message at a time!"],
            "second_warning":                 ["I'm going to have to pause responses if this continues."],
            "trigger_rate_limit_block":       db.BOT_ESCALATION,
            "send_follow_up_nudge":           db.BOT_FOLLOW_UP_GHOST,
            "send_follow_up":                 db.BOT_FOLLOW_UP_GHOST,
            "context_recovery_resume":        db.BOT_CONTEXT_RECOVERY,
            "attempt_booking_api_call":       db.BOT_API_RETRY,
            "retry_booking_succeed":          [db.get_bot_confirmation(day, time, duration, service, rng)],
            "notify_session_expired_restart": db.BOT_SESSION_EXPIRED,
            "detect_duplicate_inform_client": db.BOT_DUPLICATE_BOOKING,
            "politely_flag_invalid_date_ask_again": db.BOT_INVALID_DATE,
            "explain_hours_unavailable":      db.BOT_OUTSIDE_HOURS,
            "repeat_hours_offer_alternatives": [f"I'm generally available between noon and midnight — would {alt_time} work?"],
            "offer_daytime_slot":             [f"I have {alt_time} tomorrow if that suits?"],
            "ask_clarifying_question_1":      ["What kind of session were you after?"],
            "ask_clarifying_question_2":      ["And when were you thinking of coming in?"],
            "explain_incall_location_approach": ["For incall, you'd come to me — I can share the exact address once you're booked 😊"],
            "clarify_location_policy":        ["The specific address is shared after booking is confirmed."],
            "offer_to_book":                  [f"Would you like to make a booking? 😊"],
            "send_full_info_profile":         db.BOT_SEND_RATES,
            "explain_couples":                ["Couples sessions are for two people together — shared fun! 😊"],
            "explain_doubles":                ["Doubles means two ladies at once — more the merrier! 😉"],
            "offer_booking_link":             ["Ready to book when you are! Just say the word 😊"],
            "request_deposit":                ["I do require a small deposit to lock in the booking — I'll send payment details shortly!"],
            "escalate_payment_issue":         db.BOT_ESCALATION,
            "clarify_3hr_details":            [f"Perfect, 3 hours on {day} at {time} — {service}. Shall I confirm that?"],
            "ask_overnight_details":          [f"What date were you thinking for the overnight stay?"],
            "confirm_future_date_possible":   [f"Yes, future bookings are fine! What date did you have in mind?"],
            "ask_type":                       db.BOT_ASK_INCALL_OUTCALL,
        }

        bank = mapping.get(intent)
        if bank:
            choice = rng.choice(bank)
            return _fill(choice, day=day, time=time, duration=duration,
                         service=service, alt_time=alt_time, suburb=suburb)

        # Default: ask what they need
        return "How can I help you today? Would you like to make a booking?"
