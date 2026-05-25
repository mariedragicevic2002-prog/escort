"""Backward-compatible booking collection exports and helper prompts."""

from handlers.booking_coll import *  # noqa: F401,F403
from handlers.booking_coll import __all__ as _BOOKING_COLL_ALL

# Targeted questions for each missing field
_FIELD_QUESTIONS = {
    "date": "What day were you thinking? 📅",
    "time": "What time works for you?",
    "duration": "How long would you like to book? (e.g. 1 hour, 90 mins)",
    "experience_type": "Would you prefer GFE or PSE?",
    "incall_outcall": "Would you like to come to me (incall) or shall I come to you (outcall)?",
    "outcall_address": "What address should I come to?",
}


def get_targeted_question(missing_field: str) -> str | None:
    """Return a targeted question for a missing booking field, or None if not known."""
    return _FIELD_QUESTIONS.get(missing_field)



def get_targeted_questions_for_fields(missing_fields: list[str]) -> str | None:
    """Return a combined targeted question for the first 1-2 missing fields."""
    questions = []
    for field in (missing_fields or [])[:2]:
        question = get_targeted_question(field)
        if question:
            questions.append(question)
    if not questions:
        return None
    return " ".join(questions) if len(questions) == 1 else questions[0]


__all__ = list(_BOOKING_COLL_ALL) + [
    "_FIELD_QUESTIONS",
    "get_targeted_question",
    "get_targeted_questions_for_fields",
]
