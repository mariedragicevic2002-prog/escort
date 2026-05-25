# ruff: noqa: E402
"""
Confirmed-booking message templates - CONFIRMED state client-facing SMS strings.
"""

# Prefix prepended to the deposit message when client says yes to optional deposit
GREAT_DEPOSIT_PREFIX = "Great! "

# Positive acknowledgement when client says thanks / great / awesome after confirmation
YOURE_WELCOME_SEE_YOU_SOON = "You're welcome! See you soon!"

# Prompt sent when client needs to send optional deposit screenshot but hasn't yet
OPTIONAL_DEPOSIT_SCREENSHOT_PROMPT = "Please send a screenshot of your payment confirmation for the optional deposit."

# IMAGE_DOWNLOAD_FAILED lives in deposit_flow_messages (single source) \u2014 import from there
from templates.deposit_flow_messages import IMAGE_DOWNLOAD_FAILED  # noqa: F401
