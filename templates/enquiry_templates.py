"""
Enquiry Templates
Templates for enquiry prompts and forwarding messages to the escort.
"""

def get_enquiry_prompt_message() -> str:
    """Template-first enquiry prompt. AI fallback is handled upstream by the global fallback handler."""
    return (
        "Thanks for your message!\n\n"
        "If you have a specific question for me directly, reply with ENQUIRY followed by your question "
        "and I'll get back to you personally ASAP.\n\n"
        "Example: 'ENQUIRY Can you do doubles bookings?'\n\n"
        "Or if you'd like to make a booking, just let me know!"
    )


def get_enquiry_question_received_message(question_body: str) -> str:
    """Acknowledgement when the client already sent ENQUIRY plus question text (avoids repeating how-to)."""
    preview = (question_body or "").strip()
    if len(preview) > 200:
        preview = preview[:197].rsplit(" ", 1)[0] + "..."
    preview = preview.replace('"', "'")
    return (
        "Thanks - I've received your question:\n\n"
        f'"{preview}"\n\n'
        "I'll reply personally as soon as I can.\n\n"
        "If you'd like to book as well, send your preferred date, time, how long, and style (e.g. GFE/PSE)."
    )


def get_fifth_message_block() -> str:
    """Get message for 5th message without booking details.

    Returns:
        Fifth message block message
    """
    return (
        "I've asked for your booking details a few times now without a clear date, time, or duration, "
        "so I'm pausing here.\n\n"
        "If you'd still like to book, reply with when you'd like to meet "
        "(date, time, and how long), or message again whenever you're ready."
    )


def get_post_booking_limit_message() -> str:
    """Get message when post-booking message limit reached.

    Returns:
        Post-booking limit message
    """
    return (
        "I've noticed you have a few questions!\n\n"
        "For anything specific or detailed, feel free to text ENQUIRY followed by your question, "
        "and I'll get back to you personally.\n\n"
        "Or would you like to book again? \U0001F495"
    )



