"""
Deposit-flow message templates - DEPOSIT_REQUIRED state client-facing SMS strings.
"""

# Prompt sent when client needs to upload their deposit screenshot but hasn't sent one yet
DEPOSIT_SCREENSHOT_PROMPT = (
    "Thanks — I still need a screenshot of your payment confirmation to verify the deposit. "
    "A text message alone doesn't confirm it."
)

# Error when the MMS image cannot be downloaded
IMAGE_DOWNLOAD_FAILED = "I couldn't download your image. Please try uploading again."

# Sent when client cancels / does not want to proceed (shared with COLLECTING state)
BOOKING_CANCELLED_NO_PROBLEM = "No worries! Let me know if you'd like to book another time."
