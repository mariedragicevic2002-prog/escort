"""

Centralized retrieval for rates and booking policy snippets.
"""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


from typing import Any


import logging
logger = logging.getLogger("adella_chatbot.policy_retrieval")

def get_policy_snapshot() -> dict[str, Any]:
    """Return a compact policy snapshot with safe defaults."""
    try:
        from core.rates_from_config import (
            get_deposit_outcall,
            get_incall_pricing,
            get_outcall_pricing,
            get_surcharge,
        )

        incall = get_incall_pricing() or {}
        outcall = get_outcall_pricing() or {}
        return {
            "incall_gfe_60": int(incall.get("gfe_60") or 700),
            "incall_pse_60": int(incall.get("pse_60") or 1000),
            "outcall_gfe_60": int(outcall.get("gfe_60") or 800),
            "overnight": int(incall.get("overnight") or 5000),
            "outcall_surcharge": int(get_surcharge() or 100),
            "outcall_deposit": int(get_deposit_outcall() or 100),
        }
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=True)
        return {
            "incall_gfe_60": 700,
            "incall_pse_60": 1000,
            "outcall_gfe_60": 800,
            "overnight": 5000,
            "outcall_surcharge": 100,
            "outcall_deposit": 100,
        }


def get_rates_summary_snippet() -> str:
    """Return a short rates snippet for AI prompt context."""
    p = get_policy_snapshot()
    return (
        f"Incall GFE: ${p['incall_gfe_60']}/hr, PSE: ${p['incall_pse_60']}/hr. "
        f"Outcall: ${p['outcall_gfe_60']}/hr (includes ${p['outcall_surcharge']} travel surcharge). "
        f"Overnight: ${p['overnight']}."
    )
