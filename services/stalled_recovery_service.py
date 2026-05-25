"""
Send gentle nudges for stale conversations.
"""

import logging
from typing import Any

from services.sms_service import send_sms
from utils.structured_logging import log_quality_metric

logger = logging.getLogger("adella_chatbot.services.stalled_recovery")

STALLABLE_STATES = ("COLLECTING", "DEPOSIT_REQUIRED", "EXTENDED_ENQUIRY")


def check_and_send_stalled_nudges(
    state_manager: Any,
    db_service: Any,
    stale_minutes: int = 45,
    extended_enquiry_stale_minutes: int = 24 * 60,
) -> int:
    """
    Send one nudge to clients with stale in-progress conversations.
    Uses message history guard to avoid duplicate nudges without schema changes.
    """
    try:
        rows = db_service.execute_query(
            """
            SELECT cs.phone_number, cs.current_state
            FROM conversation_states cs
                WHERE cs.current_state = ANY(%s)
                  AND cs.last_message_at IS NOT NULL
                  AND cs.last_message_at < NOW() - (
                        CASE
                            WHEN cs.current_state = 'EXTENDED_ENQUIRY'
                                THEN (%s || ' minutes')::interval
                            ELSE (%s || ' minutes')::interval
                        END
                  )
              AND NOT EXISTS (
                  SELECT 1
                  FROM message_history mh
                    WHERE mh.phone_number = cs.phone_number
                      AND mh.direction = 'outbound'
                      AND mh.message_body ILIKE 'Just checking in%%'
                      AND mh.created_at > NOW() - INTERVAL '24 hours'
                )
            LIMIT 50
            """,
            (
                list(STALLABLE_STATES),
                int(extended_enquiry_stale_minutes),
                int(stale_minutes),
            ),
            fetch=True,
        ) or []
    except Exception as exc:
        logger.error("Failed querying stalled conversations: %s", exc)
        return 0

    sent = 0
    for row in rows:
        phone_number = row.get("phone_number")
        state = row.get("current_state") or "UNKNOWN"
        if not phone_number:
            continue
        if state == "EXTENDED_ENQUIRY":
            message = (
                "Just checking in — if you'd like to continue, ask another question, "
                "or start a booking, reply here and I'll help."
            )
        else:
            message = (
                "Just checking in — if you still want to continue your booking, "
                "reply here and I'll pick up where we left off."
            )
        try:
            ok = send_sms(phone_number, message)
            if ok:
                state_manager.log_message(phone_number, "outbound", message)
                log_quality_metric(
                    "stalled_nudge_sent",
                    phone_number=phone_number,
                    state=state,
                )
                sent += 1
        except Exception as exc:
            logger.warning("Failed stalled nudge for %s: %s", phone_number, exc)
    return sent
