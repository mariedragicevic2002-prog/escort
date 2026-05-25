"""
Availability-check message templates - CHECKING_AVAILABILITY state client-facing SMS strings.
"""

# Appended to error message when no alternative slots are found (time-based conflict)
NO_ALTERNATIVES_SUGGEST_ANOTHER = "\n\nCould you suggest another time?"

# Sent to client when booking must be forwarded to escort for manual review.
# Includes {escort_name} by design (exception to first-person SMS copy elsewhere).
FORWARD_TO_ESCORT_NOTICE = (
    "I will need to forward this {booking_type_label} booking enquiry directly "
    "through to {escort_name} for a manual review. She will be in touch shortly."
)

# Fallback reminder when client sends an unrecognized message while awaiting confirmation
CONFIRM_BOOKING_REMINDER = (
    "Hi{name_str}! Just reply YES (or your name, or GFE/PSE/DGFE) to confirm your booking."
)
