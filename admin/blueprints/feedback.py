"""Admin client feedback webform - /admin/feedback?pending_id=X&tok=... (escort-only, HMAC-signed)."""

import logging
import os
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from core.hmac_security import (
    FEEDBACK_TOKEN_TTL_SECONDS,
    GATEWAY_FEEDBACK,
    consume_token,
    is_token_valid,
    verify_signed_token,
)
from handlers.escort_feedback import (
    _load_booking_for_feedback,
    _save_feedback,
    clear_pending_by_id,
    get_pending_by_id,
)
from services.database_service import get_shared_db

logger = logging.getLogger("escort_chatbot.admin.feedback")

feedback_bp = Blueprint("feedback", __name__, url_prefix="/admin", template_folder="../templates")

_FEEDBACK_LINK_MAX_AGE = timedelta(seconds=FEEDBACK_TOKEN_TTL_SECONDS)


def _pending_feedback_expired(pending: dict | None) -> bool:
    """True if ``requested_at`` is older than the same window as the HMAC (24h)."""
    if not pending:
        return True
    ra = pending.get("requested_at")
    if not ra or not isinstance(ra, datetime):
        return False
    if ra.tzinfo is None:
        ra = ra.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > ra + _FEEDBACK_LINK_MAX_AGE


def _format_booking_summary_display(row) -> str:
    """Format booking for display on the feedback form."""
    if not row:
        return ""
    parts = []
    date_val = row.get("date")
    if date_val:
        if hasattr(date_val, "strftime"):
            parts.append(date_val.strftime("%A %d %B %Y"))
        else:
            parts.append(str(date_val)[:10])
    time_val = row.get("time")
    if time_val is not None:
        if isinstance(time_val, (list, tuple)) and len(time_val) >= 2:
            h, m = int(time_val[0]), int(time_val[1])
        elif hasattr(time_val, "hour"):
            h, m = time_val.hour, time_val.minute
        else:
            h, m = 12, 0
        period = "am" if h < 12 else "pm"
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        parts.append(f"{display_h}:{m:02d}{period}")
    duration = row.get("duration")
    if duration is not None:
        if duration >= 60:
            hrs = duration // 60
            mins = duration % 60
            parts.append(f"{hrs}h {mins}min" if mins else f"{hrs}h")
        else:
            parts.append(f"{duration} min")
    exp = (row.get("experience_type") or "GFE").strip() or "GFE"
    parts.append(exp.upper())
    loc = (row.get("incall_outcall") or "incall").strip().lower() or "incall"
    parts.append(loc)
    return " ".join(parts)


@feedback_bp.route("/feedback", methods=["GET", "POST"])
def feedback_form():
    """Show client feedback form (GET) or process submission (POST). HMAC-signed link required."""
    pending_id = request.args.get("pending_id") if request.method == "GET" else request.form.get("pending_id")
    tok = request.args.get("tok") if request.method == "GET" else request.form.get("tok")

    try:
        pending_id = int(pending_id) if pending_id else None
    except (TypeError, ValueError):
        pending_id = None

    _err_tpl = dict(client_name="", booking_summary="", pending_id=None, tok="")

    db = get_shared_db(os.getenv('DATABASE_URL', ''))
    if not db:
        return render_template("feedback.html", error="Database unavailable.", **_err_tpl)

    if not pending_id or not tok:
        return render_template("feedback.html", error="Invalid or expired feedback link.", **_err_tpl)

    if not verify_signed_token(tok, str(pending_id), GATEWAY_FEEDBACK):
        logger.warning("Feedback HMAC verification failed for pending_id=%s", pending_id)
        return render_template("feedback.html", error="This feedback link is invalid or has expired.", **_err_tpl)

    if not is_token_valid(db, tok):
        logger.warning("Feedback token already consumed for pending_id=%s", pending_id)
        return render_template("feedback.html", error="This feedback link has already been used.", **_err_tpl)

    pending = get_pending_by_id(db, pending_id)
    if not pending:
        return render_template("feedback.html", error="This feedback link has expired or already been used.", **_err_tpl)
    if _pending_feedback_expired(pending):
        return render_template(
            "feedback.html", error="This feedback link has expired. Request a new one if still needed.", **_err_tpl
        )

    client_phone = pending.get("client_phone_number") or ""
    booking_row = _load_booking_for_feedback(db, client_phone)
    client_name = (booking_row.get("client_name") or "Client").strip() if booking_row else "Client"
    booking_summary = _format_booking_summary_display(booking_row)

    if request.method == "POST":
        if not consume_token(db, tok):
            return render_template("feedback.html", error="This feedback link has already been used.", **_err_tpl)

        action = (request.form.get("action") or "").strip().lower()
        comments = (request.form.get("comments") or "").strip() or None

        if action == "3star":
            _save_feedback(
                db, client_phone,
                arrived_on_time=True, was_respectful=True, would_see_again=True,
                star_rating=3, booking_row=booking_row, comments=comments,
            )
            clear_pending_by_id(db, pending_id)
            flash("Thank you. Feedback saved (3 stars).", "success")
            return redirect(url_for("feedback.feedback_thanks"))

        if action == "block":
            state_manager = current_app.config.get("STATE_MANAGER")
            if state_manager:
                state_manager.block_client(
                    client_phone,
                    reason="client_feedback_block",
                    notes="Escort requested block via feedback webform",
                )
            _save_feedback(
                db, client_phone,
                arrived_on_time=False, was_respectful=False, would_see_again=False,
                star_rating=None, booking_row=booking_row, comments=comments,
            )
            clear_pending_by_id(db, pending_id)
            flash("Feedback saved. Client has been blocked.", "success")
            return redirect(url_for("feedback.feedback_thanks"))

        q1 = (request.form.get("q1") or "").strip().lower() == "yes"
        q2 = (request.form.get("q2") or "").strip().lower() == "yes"
        q3 = (request.form.get("q3") or "").strip().lower() == "yes"
        stars = sum([q1, q2, q3])
        if not q3:
            state_manager = current_app.config.get("STATE_MANAGER")
            if state_manager:
                state_manager.block_client(
                    client_phone,
                    reason="client_feedback_block",
                    notes="Escort would not see client again (post-booking feedback Q3=No)",
                )
        _save_feedback(
            db, client_phone,
            arrived_on_time=q1, was_respectful=q2, would_see_again=q3,
            star_rating=stars if stars else None, booking_row=booking_row, comments=comments,
        )
        clear_pending_by_id(db, pending_id)
        if not q3:
            flash("Thank you. Feedback saved. This client has been blocked (you would not see them again).", "success")
        else:
            flash("Thank you. Feedback saved.", "success")
        return redirect(url_for("feedback.feedback_thanks"))

    return render_template(
        "feedback.html",
        error=None,
        client_name=client_name,
        booking_summary=booking_summary,
        pending_id=pending_id,
        tok=tok,
    )


@feedback_bp.route("/feedback/thanks", methods=["GET"])
def feedback_thanks():
    """Thank you page after feedback submission."""
    return render_template("feedback_thanks.html")
