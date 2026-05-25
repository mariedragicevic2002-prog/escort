"""
Handle SMS replies from the escort in response to post-booking feedback requests.
Parses 3 STAR / N Y Y / BLOCK and saves to client_feedback; BLOCK blacklists the client.
"""

import logging

logger = logging.getLogger("adella_chatbot.escort_feedback")


def _get_pending_client(db_service) -> str | None:
    """Get client_phone_number from the current feedback_pending row (most recent)."""
    try:
        rows = db_service.execute_query(
            "SELECT client_phone_number FROM feedback_pending ORDER BY requested_at DESC LIMIT 1",
            (),
            fetch=True
        )
        if rows:
            from utils.row_utils import row_get
            row = rows[0]
            if isinstance(row, dict):
                return row.get("client_phone_number")
            return row_get(row, 0, None)
    except Exception as e:
        logger.warning("Failed to get feedback_pending: %s", e)
    return None


def get_pending_by_id(db_service, pending_id: int) -> dict | None:
    """Get feedback_pending row by id. Returns dict with client_phone_number or None."""
    try:
        rows = db_service.execute_query(
            "SELECT id, client_phone_number, requested_at FROM feedback_pending WHERE id = %s",
            (pending_id,),
            fetch=True
        )
        if rows:
            return rows[0] if isinstance(rows[0], dict) else None
    except Exception as e:
        logger.warning("Failed to get feedback_pending by id: %s", e)
    return None


def clear_pending_by_id(db_service, pending_id: int) -> None:
    """Delete the feedback_pending row with the given id."""
    try:
        db_service.execute_query(
            "DELETE FROM feedback_pending WHERE id = %s",
            (pending_id,),
            fetch=False
        )
    except Exception as e:
        logger.warning("Failed to clear feedback_pending by id: %s", e)


def _clear_feedback_pending(db_service) -> None:
    """Clear all feedback_pending rows."""
    try:
        db_service.execute_query("DELETE FROM feedback_pending", (), fetch=False)
    except Exception as e:
        logger.warning("Failed to clear feedback_pending: %s", e)


def _load_booking_for_feedback(db_service, client_phone_number: str) -> dict | None:
    """Load current conversation_states row for client to get booking details for client_feedback row."""
    try:
        rows = db_service.execute_query(
            """SELECT client_name, date, time, duration, experience_type, incall_outcall
               FROM conversation_states WHERE phone_number = %s""",
            (client_phone_number,),
            fetch=True
        )
        if rows:
            return rows[0] if isinstance(rows[0], dict) else None
    except Exception as e:
        logger.warning("Failed to load booking for feedback: %s", e)
    return None


def _save_feedback(
    db_service,
    client_phone_number: str,
    arrived_on_time: bool,
    was_respectful: bool,
    would_see_again: bool,
    star_rating: int | None = None,
    booking_row: dict | None = None,
    comments: str | None = None,
) -> bool:
    """Insert a row into client_feedback."""
    try:
        if booking_row:
            client_name = booking_row.get("client_name")
            booking_date = booking_row.get("date")
            booking_time = booking_row.get("time")
            duration = booking_row.get("duration")
            experience_type = booking_row.get("experience_type")
            incall_outcall = booking_row.get("incall_outcall")
        else:
            client_name = booking_date = booking_time = duration = experience_type = incall_outcall = None

        comment_val = (comments or "").strip() or None

        db_service.execute_query(
            """INSERT INTO client_feedback (
                   client_phone_number, client_name, booking_date, booking_time, duration,
                   experience_type, incall_outcall,
                   arrived_on_time, was_respectful, would_see_again, star_rating, comments, feedback_received_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
            (
                client_phone_number,
                client_name,
                booking_date,
                booking_time,
                duration,
                experience_type,
                incall_outcall,
                arrived_on_time,
                was_respectful,
                would_see_again,
                star_rating,
                comment_val,
            ),
            fetch=False
        )
        return True
    except Exception as e:
        logger.error("Failed to save client_feedback: %s", e)
        return False


def handle_escort_feedback_reply(
    message_body: str,
    db_service,
    state_manager,
) -> tuple[bool, str]:
    """
    Parse escort's feedback reply (3 STAR / N Y Y / BLOCK), save to client_feedback, block if BLOCK.

    Returns:
        (success, reply_message) - reply_message can be sent back to escort.
    """
    body = (message_body or "").strip().upper()
    if not body:
        return False, "Please reply with 3 STAR, or N Y Y (for the 3 questions), or BLOCK."

    client_phone = _get_pending_client(db_service)
    if not client_phone:
        return False, ""

    booking_row = _load_booking_for_feedback(db_service, client_phone)

    if body == "BLOCK":
        state_manager.block_client(
            client_phone,
            reason="client_feedback_block",
            notes="Escort requested block after booking feedback"
        )
        _save_feedback(
            db_service,
            client_phone,
            arrived_on_time=False,
            was_respectful=False,
            would_see_again=False,
            star_rating=None,
            booking_row=booking_row,
        )
        _clear_feedback_pending(db_service)
        logger.info("Client %s blocked from escort feedback reply (BLOCK)", client_phone)
        return True, "Client blocked. Thank you."

    if body == "3 STAR":
        _save_feedback(
            db_service,
            client_phone,
            arrived_on_time=True,
            was_respectful=True,
            would_see_again=True,
            star_rating=3,
            booking_row=booking_row,
        )
        _clear_feedback_pending(db_service)
        logger.info("Saved 3 STAR feedback for client %s", client_phone)
        return True, "Feedback received. Thank you."

    # Parse N Y Y style (three tokens, each must be Y or N)
    tokens = body.split()
    if len(tokens) >= 3 and all(t in ('Y', 'N') for t in tokens[:3]):
        def to_bool(c: str) -> bool:
            return (c or "").strip().upper() == "Y"
        q1 = to_bool(tokens[0])
        q2 = to_bool(tokens[1])
        q3 = to_bool(tokens[2])
        if not q3:
            state_manager.block_client(
                client_phone,
                reason="client_feedback_block",
                notes="Escort would not see client again (post-booking SMS Q3=N)",
            )
        stars = sum([q1, q2, q3])
        _save_feedback(
            db_service,
            client_phone,
            arrived_on_time=q1,
            was_respectful=q2,
            would_see_again=q3,
            star_rating=stars if stars else None,
            booking_row=booking_row,
        )
        _clear_feedback_pending(db_service)
        logger.info("Saved Y/N feedback for client %s: %s %s %s", client_phone, q1, q2, q3)
        if not q3:
            return True, "Client blocked (would not see again). Feedback saved. Thank you."
        return True, "Feedback received. Thank you."

    return False, "Please reply with 3 STAR, or N Y Y (e.g. N Y Y for no to Q1), or BLOCK."
