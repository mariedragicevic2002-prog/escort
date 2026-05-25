"""
Pending Deposit Service
GRAPHITE events (pending deposits) are permanent soft-holds \u2014 they are never
auto-cancelled. Clients booking well in advance may pay hours or days later.
"""

import logging

logger = logging.getLogger("adella_chatbot.pending_deposit_service")


def check_and_cancel_expired_pending_deposits(_state_manager, _db_service) -> int:
    """
    Previously auto-cancelled GRAPHITE events after 30 minutes.

    DISABLED: GRAPHITE events now persist indefinitely. A GRAPHITE slot is a
    soft-hold \u2014 the time appears available to other clients but any booking
    over it requires a mandatory deposit. Whichever client pays first gets
    the confirmed (BASIL) slot.

    Returns 0 (no cancellations performed).
    """
    return 0
