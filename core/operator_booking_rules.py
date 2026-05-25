"""
Operator-provided booking rules and regression reminders.

This module keeps the human business rules in one code-native place so they can
be reused by prompts, tests, and future deterministic policy checks.
"""

from __future__ import annotations

from typing import Final

EXPERIENCE_MENU_URL: Final[str] = "https://www.adella-allure.com.au/experience"

CANONICAL_BOOKING_RULES: Final[dict[str, tuple[str, ...]]] = {
    "rule_priority": (
        "Rule priority must be explicit because one message can trigger several rules at once, such as outcall plus doubles plus profanity plus manual-review conditions.",
        "Confirmed precedence: safety and block rules first, then manual review rules, then conflicting-details clarification, then booking eligibility and deposit rules, then availability and slot messaging, then confirmation wording.",
    ),
    "conversation_defaults": (
        "Assume the client wants to book today unless they clearly mention another day or timing phrase such as tomorrow, next week, weekend, or a weekday name.",
        "Assume the booking is incall unless the client explicitly asks for outcall, travel, hotel visit, home visit, or a dinner date.",
        "For first-contact availability checks, assume the client wants a 1 hour booking unless they state another duration.",
        "The first substantive booking reply should include the configured escort profile link and the current incall hotel/address.",
        "If the client only sends a plain greeting such as hi, hello, or good morning, reply warmly and ask if they want to make a booking instead of dumping availability slots immediately.",
        "If the client asks about rates or pricing, refer them to the escort profile for the full rates and experience list instead of quoting detailed prices in chat, and include the booking webform link.",
        "If the client asks where the escort is located, answer the current incall location first, then offer three available times, then send the profile link and booking webform.",
        "If the client asks about touring a city, send the profile link for tour dates/info, mention webpage tour subscriptions for that city, and tell them to text TOURING plus the city name for SMS alerts.",
        "If the client asks about outcalls to their hotel or apartment, answer that outcall question first and do not send the incall location template; instead send the 15km CBD rule, outcall surcharge, required deposit, and booking webform.",
        "Ask at most two questions in one SMS.",
    ),
    "booking_collection": (
        "Before a reservation can be held, collect date, time, and duration.",
        f"For standard GFE, DGFE, and PSE bookings, ask what experience type the client wants at least once and include the experience menu link {EXPERIENCE_MENU_URL}.",
        "If the client has not already shared a name, ask for it when the booking is being confirmed or reserved.",
        "If the client replies with a valid single field such as '30 mins', treat that as captured information instead of asking them to repeat themselves.",
    ),
    "outcall_policy": (
        "All outcalls have a minimum duration of 1 hour.",
        "All outcalls have a minimum surcharge of $100.",
        "All outcalls require address verification within a 15km radius of the escort unless address verification has been disabled in admin settings.",
        "Standard GFE, DGFE, and PSE outcalls require at least a $100 deposit before confirmation.",
        "First-response outcall enquiries should use the policy-first hotel/apartment template: 15km CBD radius, surcharge, deposit, then booking CTA/webform.",
    ),
    "special_booking_policy": (
        "Dinner dates are a fixed $1,000 booking for 2 hours with no outcall surcharge; the restaurant and any post-dinner destination must stay within 15km.",
        "Overnight, weekend, and fly-me-to-you requests must be forwarded for manual review.",
        "Overnight/manual-review replies should explain that the automated service has forwarded the enquiry, quote the configured rate and required deposit for that booking type, and say the escort will be in touch shortly.",
        "Doubles MMF and MFF bookings require at least a $200 deposit.",
        "If the escort is sourcing the extra person for an outcall doubles booking, double the outcall surcharge to at least $200 and keep a 4-hour sourcing buffer until the client confirms they are bringing the other person themselves.",
    ),
    "deposit_and_safety": (
        "Outcall bookings are only reserved until deposit payment is verified.",
        "A standard incall booking up to 1 hour 45 minutes can be reserved without a deposit, but incall bookings of 2 hours or more require at least a $50 deposit.",
        "If the client uses configured profanity three or more times in a conversation, require at least the standard mandatory deposit.",
        "If the client uses configured unsafe words, automatically block the client.",
        "If the client negotiates or asks for a discount, refuse and tell them that if they want to barter on price they can go elsewhere.",
        "If the client uses blocked words or unsafe phrases at any time during the booking process, blocking overrides ENQUIRY or any other handoff.",
    ),
    "availability_and_slots": (
        "If the client does not name a booking time, offer the nearest three available times, aligned to 15-minute increments and spaced about one hour apart.",
        "Available-now bookings still need at least a 30-minute buffer before the first offered time.",
        "For available-now checks, always round the inbound time up to the next valid 15-minute booking increment and then add the 30-minute grace period.",
        "For available-now checks near the end of shift, still allow the booking if the assumed 1 hour incall would finish no more than 30 minutes after the escort's listed finish time.",
        "If a requested time is unavailable, open with a clear unavailable message and offer three matching alternatives unless doubles sourcing rules prevent that.",
    ),
    "calendar_status_rules": (
        "Treat basil, peacock, grape, banana, and tomato calendar entries as blocking.",
        "Treat graphite and lavender calendar entries as reservable and available to be booked over until deposit confirmation happens.",
        "Confirmed deposit-paid bookings should move to basil green, and confirmed outcall travel blocks should move to grape.",
        "Graphite and lavender temporary reservations should be cleared from the schedule every 3 months.",
    ),
    "lateness_and_access": (
        "Clients get a 10-minute leeway period; every minute they are later than that is deducted from their booked time.",
        "If the escort is late, she should notify the client and reschedule if the new time no longer suits them.",
        "If an outcall client gives the wrong address and does not answer, the deposit is forfeited.",
        "If outcall hotel access fails, the deposit is forfeited.",
        "If incall hotel access is unavailable, the escort should reschedule to a suitable time for the client.",
    ),
    "reschedule_and_cancellation": (
        "A paid deposit can be rescheduled a maximum of 2 times before it is forfeited.",
        "If the client reschedules or cancels with at least 6 hours notice, the deposit is held in trust and deducted from their next booking.",
        "If the client gives less than 6 hours notice, the deposit is forfeited.",
        "If the escort cancels after a deposit was paid, the client is entitled to a full refund.",
    ),
    "address_verification_and_conflicts": (
        "If geocoding cannot cleanly verify an outcall address with Google or OpenCage, and the client believes it is within 15km, they can text ENQUIRY so the escort can manually review it.",
        "If the client gives partial or conflicting details, clarify that conflict before moving on to other questions.",
        "If the same conflict is still unresolved on a third attempt, direct the client to ENQUIRY for manual review.",
    ),
    "reservation_and_manual_review": (
        "A client can take as long as they like to pay a deposit, but until it is paid the booking remains reserved only and can be booked over by another client.",
        "Only overnight, weekend, and fly-me-to-you bookings should auto-route to manual review as high-value jobs.",
    ),
    "handoff_and_sync": (
        "If the bot effectively repeats the same response twice in a row, send the ENQUIRY handoff template instead of repeating itself again.",
        "Apply a 3-strike policy for repeated or circular messages; on the third attempt either send the ENQUIRY template, block the sender when safety/spam rules justify it, or stop replying.",
        "If the client sends 5 consecutive spam or time-wasting messages that do not answer what the chatbot is asking, stop replying.",
        "ENQUIRY is the first point of call for unresolved manual handoff situations unless blocked words or unsafe phrases were used.",
        "Unsupported service requests should use this reply: Hi {client_name} Sorry im afraid I dont offer those type of services. Thanks for considering me but unfortunately you would need to make a booking with another escort.",
        "If the client keeps messaging after the ENQUIRY handoff without following it, stop replying automatically.",
        "Bookings should stay in sync with both the escort schedule and Google Calendar.",
    ),
}

BAD_CONVERSATION_REGRESSIONS: Final[tuple[dict[str, object], ...]] = (
    {
        "key": "duration_reply_missed_and_followup_overloaded",
        "summary": "Client answered with a valid duration, but the bot still behaved as if the field was missing and later overloaded the follow-up.",
        "client_reply": "30 mins",
        "observed_failures": (
            "The bot made the client repeat the duration more than once.",
            "The bot later asked more than two questions in one SMS.",
            "The bot did not ask for the experience type when it was the next missing field.",
            "The bot did not include the experience menu link when asking about the experience type.",
        ),
        "expected_behaviour": (
            "Capture '30 mins' as the duration immediately.",
            "If experience type is still missing for a standard booking, ask for it next and include the experience menu link.",
            "Keep the follow-up to at most two questions.",
            "Do not force the client to repeat the same valid detail.",
        ),
    },
)

_PROMPT_GUARDRAILS: Final[tuple[str, ...]] = (
    "Assume today unless the client clearly names another day or timeframe.",
    "Assume incall unless the client clearly asks for outcall, travel, a hotel visit, a home visit, or a dinner date.",
    "If the client only sends a plain greeting such as hi, hello, or good morning, reply warmly and ask if they want to make a booking instead of sending slots straight away.",
    "If the client asks about rates or pricing, send the profile link for the full rates and experience list plus the booking webform link instead of quoting detailed prices in chat.",
    "If the client asks where you are located, answer with the current incall location first, then show three available times, then the profile link, then the booking webform.",
    "If the client asks about touring a city, send the profile link for tour dates/info, mention webpage tour subscriptions for that city, and tell them to text TOURING plus the city name for SMS alerts.",
    "If the client asks about outcalls to their hotel or apartment, answer with the outcall policy first: 15km CBD radius, surcharge, required deposit, then the booking webform.",
    "Ask at most two questions in one SMS.",
    "For first-contact availability checks, assume today, assume incall, and assume a 1 hour booking unless the client clearly says otherwise.",
    f"Before holding a booking, collect date, time, and duration. For standard GFE/DGFE/PSE bookings, ask for the experience type and include {EXPERIENCE_MENU_URL}.",
    "If the client has not shared a name yet, ask for it at confirmation time.",
    "The first substantive booking reply should include the profile link and current incall location.",
    "Outcalls require a verified address within 15km, a minimum 1 hour duration, at least a $100 surcharge, and at least a $100 deposit.",
    "Overnight, weekend, and fly-me-to-you requests must be handed to manual review.",
    "If the client negotiates on price, refuse the discount request.",
    "If blocked words or unsafe phrases are used, blocking overrides ENQUIRY and any other handoff.",
    "If the client gives conflicting details, resolve that conflict before asking anything else; on the third failed clarification attempt, switch to ENQUIRY manual review.",
    "When a time is unavailable or missing, offer exactly three nearby 15-minute-aligned alternatives, with at least a 30-minute buffer for available-now requests.",
    "For available-now requests, always round up to the next 15-minute increment, then add the 30-minute grace period, and near shift end allow the booking when the assumed 1 hour incall would finish no more than 30 minutes after shift end.",
    "If the client sends 5 consecutive spam or time-wasting replies that do not answer the current question, stop replying.",
    "Use a 3-strike repeat policy; if the bot is about to repeat itself again, switch to the ENQUIRY handoff instead of repeating the same answer.",
)

_REGRESSION_GUARDRAILS: Final[tuple[str, ...]] = (
    "Regression reminder: if a client replies with a valid standalone duration such as '30 mins', treat it as captured data straight away.",
    f"Regression reminder: when duration is captured but experience is still missing for a standard booking, ask for the experience type next and include {EXPERIENCE_MENU_URL}.",
    "Regression reminder: keep that follow-up to two questions maximum and do not make the client repeat the same valid detail.",
)


def get_runtime_booking_guardrails_prompt() -> str:
    """Compact operator rules for runtime AI reply generation."""
    joined = " ".join(f"- {rule}" for rule in _PROMPT_GUARDRAILS)
    return f"Operator booking rules: {joined}"


def get_runtime_booking_regression_prompt() -> str:
    """Compact reminder of the latest operator-supplied failure case."""
    joined = " ".join(f"- {rule}" for rule in _REGRESSION_GUARDRAILS)
    return f"Recent regression to avoid: {joined}"
