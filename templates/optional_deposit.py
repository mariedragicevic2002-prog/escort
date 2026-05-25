"""
Optional Deposit Templates
For requesting voluntary deposits from incall bookings after confirmation.
"""


def get_optional_deposit_declined_message() -> str:
    """Reply when client declines a non-mandatory deposit (cash on arrival, won't transfer, etc.)."""
    return "That's fine, no worries — see you soon!"
