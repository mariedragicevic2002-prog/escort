"""
Safety message templates - unsafe request and abusive behaviour client-facing SMS strings.
"""

# Sent when client requests unsafe services; client is then blocked
UNSAFE_REQUEST_RESPONSE = "I provide a 100% safe service with condoms only. No exceptions."

# Sent when client is blocked for excessive profanity (5+ words)
BLOCKED_UNABLE_TO_ASSIST = "I'm unable to assist you further."

# Sent when cumulative profanity reaches the mandatory-deposit threshold (3+ words)
PROFANITY_WARNING_MANDATORY_DEPOSIT = (
    "Please keep our conversation respectful. "
    "A mandatory deposit will be required for your booking."
)

# Gentle first-warning for low-level profanity (1–2 words)
PROFANITY_GENTLE_WARNING = "Please keep our conversation respectful."
