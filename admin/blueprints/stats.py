"""Statistics blueprint - /stats route."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from flask import Blueprint, flash, redirect, render_template, request, session, url_for

import config
from admin.auth import get_admin_login_block_reason, verify_password
from core.settings_manager import get_setting, set_setting
from services.database_service import get_shared_db
from utils.row_utils import row_get

logger = logging.getLogger("escort_chatbot.admin.stats")

stats_bp = Blueprint('stats', __name__, template_folder='../templates')


def _is_stats_authenticated():
    """Check if user is authenticated for stats."""
    return session.get("admin_authenticated", False) or session.get("stats_authenticated", False)


def _parse_stats_filters(source) -> tuple[int, str, str]:
    days_raw = source.get("days", "30")
    location_filter = (source.get("location") or "all").strip().lower()
    experience_filter = (source.get("experience") or "all").strip()
    try:
        days = int(days_raw)
    except (TypeError, ValueError):
        days = 30
    if days not in (7, 30, 90):
        days = 30
    if location_filter not in ("all", "incall", "outcall"):
        location_filter = "all"
    if not experience_filter:
        experience_filter = "all"
    return days, location_filter, experience_filter


def _stats_redirect_with_filters(days: int, location_filter: str, experience_filter: str):
    return redirect(
        url_for(
            "stats.stats_page",
            days=days,
            location=location_filter,
            experience=experience_filter,
        )
    )


@stats_bp.route('/stats', methods=['GET', 'POST'])
def stats_page():
    """Statistics dashboard with authentication - shows all analytics and graphs."""
    authenticated = _is_stats_authenticated()
    error = None

    if not authenticated:
        block = get_admin_login_block_reason()
        if block:
            return render_template("stats.html", authenticated=False, error=block)
        if request.method == 'POST':
            password = (request.form.get("password") or "").strip()
            if verify_password(password):
                session["stats_authenticated"] = True
                session.modified = True
                authenticated = True
                logger.info("Successful stats dashboard login")
            else:
                error = "Invalid password"
                logger.warning("Failed stats login attempt")
        if not authenticated:
            return render_template("stats.html", authenticated=False, error=error)

    # Optional: show sample data for preview (e.g. /stats?demo=1)
    if request.args.get("demo") == "1":
        return render_template("stats.html", authenticated=True, stats=_demo_stats())

    try:
        days, location_filter, experience_filter = _parse_stats_filters(request.args)

        stats = _collect_all_stats(days=days, location_filter=location_filter, experience_filter=experience_filter)
        return render_template("stats.html", authenticated=True, stats=stats)
    except Exception as e:
        logger.exception("Error loading statistics")
        return render_template("stats.html", authenticated=True, stats=_empty_stats(), error=f"Error loading statistics: {str(e)}")


@stats_bp.route('/stats/apply-threshold-optimizer', methods=['POST'])
def apply_threshold_optimizer():
    """Apply recommended per-step fallback thresholds from current stats telemetry."""
    if not _is_stats_authenticated():
        return redirect(url_for('stats.stats_page'))
    days, location_filter, experience_filter = _parse_stats_filters(request.form)
    try:
        stats = _collect_all_stats(days=days, location_filter=location_filter, experience_filter=experience_filter)
        recommended = ((stats.get("threshold_optimizer") or {}).get("suggested_thresholds") or {})
        if not recommended:
            flash("No threshold optimization suggestions available for the selected range.", "warning")
            return _stats_redirect_with_filters(days, location_filter, experience_filter)

        key_map = {
            "global": "ai_fallback_confidence_threshold",
            "qualification": "ai_fallback_confidence_threshold_qualification",
            "availability": "ai_fallback_confidence_threshold_availability",
            "screening": "ai_fallback_confidence_threshold_screening",
            "deposit": "ai_fallback_confidence_threshold_deposit",
            "confirmation": "ai_fallback_confidence_threshold_confirmation",
            "follow_up": "ai_fallback_confidence_threshold_follow_up",
        }
        failed = []
        for step, value in recommended.items():
            setting_key = key_map.get(step)
            if not setting_key:
                continue
            try:
                threshold = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                continue
            if not set_setting(setting_key, f"{threshold:.2f}"):
                failed.append(setting_key)
        if failed:
            flash("Threshold optimizer partially failed: " + ", ".join(failed), "error")
            logger.error("Threshold optimizer failed for keys: %s", failed)
        else:
            flash("Threshold optimizer applied to global and per-step fallback thresholds.", "success")
            logger.info("Threshold optimizer applied from stats dashboard")
    except Exception as e:
        logger.exception("Threshold optimizer apply failed")
        flash(f"Could not apply threshold optimizer: {e}", "error")
    return _stats_redirect_with_filters(days, location_filter, experience_filter)


@stats_bp.route('/stats/apply-rollout-guardrail', methods=['POST'])
def apply_rollout_guardrail():
    """Apply one-click rollout guardrail action based on v1/v2 KPI comparison."""
    if not _is_stats_authenticated():
        return redirect(url_for('stats.stats_page'))
    days, location_filter, experience_filter = _parse_stats_filters(request.form)
    try:
        stats = _collect_all_stats(days=days, location_filter=location_filter, experience_filter=experience_filter)
        guardrail = stats.get("rollout_guardrail") or {}
        action = str(guardrail.get("recommended_action") or "none").strip().lower()
        if action == "none":
            flash("Guardrail check is healthy. No rollout override applied.", "success")
            return _stats_redirect_with_filters(days, location_filter, experience_filter)

        failed = []
        if action == "force_v1":
            if not set_setting("flow_version_default", "v1"):
                failed.append("flow_version_default")
            if not set_setting("flow_version_v2_rollout_percent", "0"):
                failed.append("flow_version_v2_rollout_percent")
        elif action == "reduce_v2":
            suggested_percent = int(guardrail.get("suggested_rollout_percent") or 0)
            suggested_percent = max(0, min(100, suggested_percent))
            if not set_setting("flow_version_default", "rollout"):
                failed.append("flow_version_default")
            if not set_setting("flow_version_v2_rollout_percent", str(suggested_percent)):
                failed.append("flow_version_v2_rollout_percent")
        else:
            flash("Guardrail recommendation is unavailable for this range.", "warning")
            return _stats_redirect_with_filters(days, location_filter, experience_filter)

        if failed:
            flash("Guardrail action failed: " + ", ".join(failed), "error")
            logger.error("Guardrail apply failed for keys: %s", failed)
        else:
            if action == "force_v1":
                flash("Guardrail applied: flow forced to v1 and rollout set to 0%.", "success")
            else:
                flash(
                    f"Guardrail applied: rollout mode enabled and v2 rollout reduced to {suggested_percent}%.",
                    "success",
                )
            logger.info("Rollout guardrail applied: action=%s", action)
    except Exception as e:
        logger.exception("Rollout guardrail apply failed")
        flash(f"Could not apply rollout guardrail: {e}", "error")
    return _stats_redirect_with_filters(days, location_filter, experience_filter)


@stats_bp.route('/stats/logout', methods=['GET', 'POST'])
def stats_logout():
    """Logout from stats page."""
    session.pop("stats_authenticated", None)
    return redirect(url_for('stats.stats_page'))


def _safe_date_str(val):
    """Convert a date value to YYYY-MM-DD string for charts."""
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%Y-%m-%d")
    return str(val)[:10]


def _float_from_setting(
    setting_key: str,
    default: float,
    min_value: float,
    max_value: float,
) -> float:
    """Parse float from admin settings, clamped to a safe range."""
    raw = get_setting(setting_key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, value))


def _build_threshold_optimizer(
    *,
    ai_fallback_confidence_threshold: float,
    confidence_rows: list[Any],
    flow_version_comparison: dict[str, list[float] | list[int] | list[str]],
) -> dict:
    """Compute suggested global/per-step thresholds from low-confidence band + confirmation lift."""
    current_thresholds = {
        "global": ai_fallback_confidence_threshold,
        "qualification": _float_from_setting("ai_fallback_confidence_threshold_qualification", ai_fallback_confidence_threshold, 0.0, 1.0),
        "availability": _float_from_setting("ai_fallback_confidence_threshold_availability", ai_fallback_confidence_threshold, 0.0, 1.0),
        "screening": _float_from_setting("ai_fallback_confidence_threshold_screening", ai_fallback_confidence_threshold, 0.0, 1.0),
        "deposit": _float_from_setting("ai_fallback_confidence_threshold_deposit", ai_fallback_confidence_threshold, 0.0, 1.0),
        "confirmation": _float_from_setting("ai_fallback_confidence_threshold_confirmation", ai_fallback_confidence_threshold, 0.0, 1.0),
        "follow_up": _float_from_setting("ai_fallback_confidence_threshold_follow_up", ai_fallback_confidence_threshold, 0.0, 1.0),
    }
    step_metrics = {
        str((row or {}).get("funnel_step") or "unknown"): {
            "turn_count": int((row or {}).get("turn_count") or 0),
            "low_confidence_count": int((row or {}).get("low_confidence_count") or 0),
        }
        for row in confidence_rows
    }
    suggestions = dict(current_thresholds)
    reasons = []
    step_order = ("qualification", "availability", "screening", "deposit", "confirmation", "follow_up")
    for step in step_order:
        metrics = step_metrics.get(step, {})
        turn_count = int(metrics.get("turn_count") or 0)
        low_count = int(metrics.get("low_confidence_count") or 0)
        current = float(current_thresholds.get(step, ai_fallback_confidence_threshold))
        if turn_count < 10:
            suggestions[step] = current
            continue
        low_rate = (low_count / turn_count * 100.0) if turn_count > 0 else 0.0
        adjusted = current
        if low_rate > 30.0:
            adjusted -= 0.05
        elif low_rate > 22.0:
            adjusted -= 0.02
        elif low_rate < 10.0:
            adjusted += 0.04
        elif low_rate < 15.0:
            adjusted += 0.02
        adjusted = max(0.0, min(1.0, adjusted))
        suggestions[step] = round(adjusted, 2)
        if round(adjusted, 2) != round(current, 2):
            reasons.append(f"{step}: low-confidence rate {round(low_rate, 1)}% adjusted threshold {round(current, 2)}→{round(adjusted, 2)}")

    confirmation_rates = flow_version_comparison.get("confirmation_rate") or [0.0, 0.0]
    if isinstance(confirmation_rates, list) and len(confirmation_rates) >= 2:
        try:
            v1_confirmation = float(confirmation_rates[0] or 0.0)
            v2_confirmation = float(confirmation_rates[1] or 0.0)
            confirmation_lift = v2_confirmation - v1_confirmation
        except (TypeError, ValueError):
            confirmation_lift = 0.0
    else:
        confirmation_lift = 0.0

    if confirmation_lift < 0:
        for step in ("qualification", "availability", "screening"):
            suggestions[step] = round(max(0.0, min(1.0, float(suggestions[step]) - 0.03)), 2)
        reasons.append(f"v2 confirmation lift is {round(confirmation_lift, 1)}pp; relaxed early-stage thresholds for conversion recovery")
    elif confirmation_lift > 5:
        for step in ("deposit", "confirmation", "follow_up"):
            suggestions[step] = round(max(0.0, min(1.0, float(suggestions[step]) + 0.02)), 2)
        reasons.append(f"v2 confirmation lift is +{round(confirmation_lift, 1)}pp; tightened late-stage thresholds for safer quality")

    suggestions["global"] = round(
        sum(float(suggestions[s]) for s in step_order) / len(step_order),
        2,
    )
    would_adjust = any(round(float(suggestions[k]), 2) != round(float(current_thresholds.get(k, 0.0)), 2) for k in current_thresholds)
    return {
        "target_low_confidence_band": [15.0, 25.0],
        "current_thresholds": {k: round(float(v), 2) for k, v in current_thresholds.items()},
        "suggested_thresholds": {k: round(float(v), 2) for k, v in suggestions.items()},
        "estimated_confirmation_lift_pp": round(confirmation_lift, 1),
        "reasons": reasons,
        "would_adjust": bool(would_adjust),
    }


def _build_rollout_guardrail(
    *,
    flow_version_comparison: dict[str, list[float] | list[int] | list[str]],
) -> dict:
    """Compute guardrail status and recommended one-click rollout action."""
    confirmation_rate = flow_version_comparison.get("confirmation_rate") or [0.0, 0.0]
    low_confidence_rate = flow_version_comparison.get("low_confidence_rate") or [0.0, 0.0]
    v1_confirmation = float(confirmation_rate[0] or 0.0) if isinstance(confirmation_rate, list) and len(confirmation_rate) > 0 else 0.0
    v2_confirmation = float(confirmation_rate[1] or 0.0) if isinstance(confirmation_rate, list) and len(confirmation_rate) > 1 else 0.0
    v1_low = float(low_confidence_rate[0] or 0.0) if isinstance(low_confidence_rate, list) and len(low_confidence_rate) > 0 else 0.0
    v2_low = float(low_confidence_rate[1] or 0.0) if isinstance(low_confidence_rate, list) and len(low_confidence_rate) > 1 else 0.0

    confirmation_gap_pp = round(v2_confirmation - v1_confirmation, 1)
    low_conf_gap_pp = round(v2_low - v1_low, 1)

    try:
        current_rollout_percent = int((get_setting("flow_version_v2_rollout_percent") or "0").strip())
    except (TypeError, ValueError):
        current_rollout_percent = 0
    current_rollout_percent = max(0, min(100, current_rollout_percent))

    status = "healthy"
    recommended_action = "none"
    suggested_rollout_percent = current_rollout_percent
    summary = "v2 KPIs are within guardrail thresholds."

    confirmation_breach = confirmation_gap_pp <= -3.0
    low_conf_breach = low_conf_gap_pp >= 12.0
    if confirmation_breach or low_conf_breach:
        status = "breach"
        severe_confirmation = confirmation_gap_pp <= -8.0
        severe_low_conf = low_conf_gap_pp >= 20.0
        if severe_confirmation or severe_low_conf:
            recommended_action = "force_v1"
            suggested_rollout_percent = 0
            summary = "Guardrail breach is severe; recommend immediate force-v1 rollback."
        else:
            recommended_action = "reduce_v2"
            suggested_rollout_percent = max(0, current_rollout_percent - 20)
            summary = "Guardrail breach detected; recommend reducing v2 rollout by 20 percentage points."
    elif confirmation_gap_pp <= -1.0 or low_conf_gap_pp >= 6.0:
        status = "warning"
        summary = "v2 is trending worse than v1; monitor closely or run a preventive rollout reduction."

    return {
        "status": status,
        "summary": summary,
        "confirmation_gap_pp": confirmation_gap_pp,
        "low_confidence_gap_pp": low_conf_gap_pp,
        "current_rollout_percent": current_rollout_percent,
        "recommended_action": recommended_action,
        "suggested_rollout_percent": suggested_rollout_percent,
    }


def _collect_all_stats(days: int = 30, location_filter: str = "all", experience_filter: str = "all"):
    """Collect all statistics for the dashboard."""
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        out = _empty_stats()
        out["stats_load_error"] = (
            "Database is not available (connection pool failed). "
            "Check DATABASE_URL and that the server can reach Postgres (e.g. PythonAnywhere → hosted DB only from web workers)."
        )
        return out
    cutoff_days_sql = f"{days} days"
    location_filter = (location_filter or "all").lower()
    experience_filter = (experience_filter or "all").strip()

    base_confirmed_where = ["confirmed_at IS NOT NULL"]
    base_params = []
    if location_filter in ("incall", "outcall"):
        base_confirmed_where.append("LOWER(COALESCE(incall_outcall, '')) = %s")
        base_params.append(location_filter)
    if experience_filter and experience_filter != "all":
        base_confirmed_where.append("LOWER(COALESCE(experience_type, '')) = LOWER(%s)")
        base_params.append(experience_filter)
    confirmed_where_sql = " AND ".join(base_confirmed_where)
    confirmed_where_with_cutoff_sql = f"{confirmed_where_sql} AND confirmed_at >= NOW() - INTERVAL '{cutoff_days_sql}'"

    # Basic booking stats
    total_bookings = _get_count(
        db,
        f"SELECT COUNT(*) as count FROM conversation_states WHERE {confirmed_where_sql}",  # nosec B608
        tuple(base_params),
    )
    try:
        bookings_this_month = _get_count(
            db,
            f"SELECT COUNT(*) as count FROM conversation_states WHERE {confirmed_where_sql} "  # nosec B608
            "AND confirmed_at >= DATE_TRUNC('month', NOW())",
            tuple(base_params),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        bookings_this_month = 0
    try:
        bookings_last_month = _get_count(
            db,
            f"""SELECT COUNT(*) as count FROM conversation_states
               WHERE {confirmed_where_sql}
               AND confirmed_at >= DATE_TRUNC('month', NOW()) - INTERVAL '1 month'
               AND confirmed_at < DATE_TRUNC('month', NOW())""",  # nosec B608
            tuple(base_params),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        bookings_last_month = 0
    total_enquiries = _get_count(
        db,
        f"SELECT COUNT(DISTINCT phone_number) as count FROM message_history WHERE created_at >= NOW() - INTERVAL '{cutoff_days_sql}'",  # nosec B608
    )
    # Conversion % = (bookings / enquiries * 100). Use 2 decimals so small rates (e.g. 0.15%) don't show as 0%.
    conversion_rate = (total_bookings / total_enquiries * 100) if total_enquiries > 0 else 0

    # Enquiries by day — fixed 7-day window regardless of the selected days filter
    try:
        enquiries_7d_rows = db.execute_query(
            """SELECT DATE(created_at) as date, COUNT(DISTINCT phone_number) as count
               FROM message_history
               WHERE created_at >= NOW() - INTERVAL '7 days'
               GROUP BY DATE(created_at) ORDER BY DATE(created_at)""",
            fetch=True,
        ) or []
        today = datetime.now(UTC).date()
        enq_by_day = {_safe_date_str(r.get("date")): int(r.get("count") or 0) for r in enquiries_7d_rows}
        enquiries_7d_labels = [_safe_date_str(today - timedelta(days=6 - i)) for i in range(7)]
        enquiries_7d_values = [enq_by_day.get(lbl, 0) for lbl in enquiries_7d_labels]
    except Exception as e:
        logger.warning("Enquiries 7d chart query failed: %s", e)
        enquiries_7d_labels = []
        enquiries_7d_values = []

    # Deposit stats
    try:
        deposits_requested = _get_count(
            db,
            f"SELECT COUNT(*) as count FROM conversation_states WHERE deposit_required = TRUE AND {confirmed_where_with_cutoff_sql}",  # nosec B608
            tuple(base_params),
        )
        deposits_paid = _get_count(
            db,
            f"SELECT COUNT(*) as count FROM conversation_states "
            f"WHERE deposit_required = TRUE AND deposit_paid = TRUE AND {confirmed_where_with_cutoff_sql}",  # nosec B608
            tuple(base_params),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        deposits_requested = 0
        deposits_paid = 0
    deposit_rate = (deposits_paid / deposits_requested * 100) if deposits_requested > 0 else 0

    # Block stats
    try:
        profanity_blocks = _get_count(db, "SELECT COUNT(*) as count FROM blocked_clients WHERE reason LIKE '%%profanity%%'")
        blocked_count = _get_count(db, "SELECT COUNT(*) as count FROM blocked_clients")
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        profanity_blocks = 0
        blocked_count = 0

    # Message stats
    try:
        total_messages_30d = _get_count(
            db, f"SELECT COUNT(*) as count FROM message_history WHERE created_at >= NOW() - INTERVAL '{cutoff_days_sql}'"  # nosec B608
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        total_messages_30d = 0

    try:
        messages_sent_30d = _get_count(
            db,
            f"SELECT COUNT(*) as count FROM message_history "
            f"WHERE direction = 'outbound' AND created_at >= NOW() - INTERVAL '{cutoff_days_sql}'",  # nosec B608
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        messages_sent_30d = 0

    try:
        ai_fallback_count = _get_count(
            db,
            f"SELECT COUNT(*) as count FROM message_history "
            f"WHERE created_at >= NOW() - INTERVAL '{cutoff_days_sql}' AND message_body ILIKE %s",  # nosec B608
            ("%AI fallback%",),
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        ai_fallback_count = 0

    # Bookings over time (chart)
    try:
        bookings_by_day = db.execute_query(
            f"""SELECT DATE(confirmed_at) as date, COUNT(*) as count
               FROM conversation_states
               WHERE {confirmed_where_with_cutoff_sql}
               GROUP BY DATE(confirmed_at) ORDER BY DATE(confirmed_at)""",  # nosec B608
            tuple(base_params),
            fetch=True,
        ) or []
        bookings_labels = [_safe_date_str(row.get("date")) for row in bookings_by_day]
        bookings_values = [int(row.get("count") or 0) for row in bookings_by_day]
    except Exception as e:
        logger.warning(f"Bookings chart query failed: {e}")
        bookings_labels = []
        bookings_values = []

    # Message traffic (chart)
    try:
        message_traffic = db.execute_query(
            f"""SELECT DATE(created_at) as date, COUNT(*) as total
               FROM message_history
               WHERE created_at >= NOW() - INTERVAL '{cutoff_days_sql}'
               GROUP BY DATE(created_at) ORDER BY DATE(created_at)""",  # nosec B608
            fetch=True,
        ) or []
        message_traffic_labels = [_safe_date_str(row.get("date")) for row in message_traffic]
        inbound_traffic = [int(row.get("total") or 0) for row in message_traffic]
        try:
            outbound_rows = db.execute_query(
                f"""SELECT DATE(created_at) as date, COUNT(*) as total
                   FROM message_history
                   WHERE direction = 'outbound' AND created_at >= NOW() - INTERVAL '{cutoff_days_sql}'
                   GROUP BY DATE(created_at) ORDER BY DATE(created_at)""",  # nosec B608
                fetch=True,
            ) or []

            def _dk(d):
                if d is None:
                    return ""
                return _safe_date_str(d)

            out_by = {_dk(r.get("date")): int(r.get("total") or 0) for r in outbound_rows}
            outbound_traffic = [out_by.get(lbl, 0) for lbl in message_traffic_labels]
        except Exception as oe:
            logger.warning(f"Outbound message traffic query failed: {oe}")
            outbound_traffic = [0] * len(inbound_traffic)
    except Exception as e:
        logger.warning(f"Message traffic query failed: {e}")
        message_traffic_labels = []
        inbound_traffic = []
        outbound_traffic = []

    # Earnings and top clients from Rates page pricing
    (
        total_earnings_30d,
        total_earnings_all,
        avg_booking_value,
        daily_earnings_labels,
        daily_earnings_values,
        weekly_earnings_labels,
        weekly_earnings_values,
        top_clients,
    ) = _compute_earnings(
        db,
        days=days,
        location_filter=location_filter,
        experience_filter=experience_filter,
    )

    # Experience breakdown — fixed display order
    _EXPERIENCE_ORDER = ["GFE", "DGFE", "PSE", "Couples MFF", "Doubles MFF", "Doubles MMF", "Dinner Date"]
    try:
        experience_breakdown = db.execute_query(
            f"""SELECT COALESCE(experience_type, 'Unknown') AS experience_type, COUNT(*) AS count
               FROM conversation_states WHERE {confirmed_where_with_cutoff_sql}
               GROUP BY COALESCE(experience_type, 'Unknown')""",  # nosec B608
            tuple(base_params),
            fetch=True,
        ) or []
        exp_map = {str(row.get("experience_type") or "Unknown"): int(row.get("count") or 0) for row in experience_breakdown}
        experience_labels: list = []
        experience_values: list = []
        seen: set = set()
        for exp in _EXPERIENCE_ORDER:
            matched = next((k for k in exp_map if k.lower() == exp.lower()), None)
            if matched is not None:
                experience_labels.append(matched)
                experience_values.append(exp_map[matched])
                seen.add(matched)
        for exp_type, count in exp_map.items():
            if exp_type not in seen:
                experience_labels.append(exp_type)
                experience_values.append(count)
    except Exception as e:
        logger.warning(f"Experience breakdown failed: {e}")
        experience_labels = []
        experience_values = []

    # Location breakdown (include NULL as Unknown)
    try:
        location_breakdown = db.execute_query(
            f"""SELECT COALESCE(incall_outcall, 'Unknown') AS incall_outcall, COUNT(*) AS count
               FROM conversation_states WHERE {confirmed_where_with_cutoff_sql}
               GROUP BY COALESCE(incall_outcall, 'Unknown')""",  # nosec B608
            tuple(base_params),
            fetch=True,
        ) or []
        location_labels = [str(row.get("incall_outcall") or "Unknown") for row in location_breakdown]
        location_values = [int(row.get("count") or 0) for row in location_breakdown]
    except Exception as e:
        logger.warning(f"Location breakdown failed: {e}")
        location_labels = []
        location_values = []

    city_labels = []
    city_values = []

    # Funnel metrics for selected period
    try:
        funnel_rows = db.execute_query(
            f"""SELECT current_state, COUNT(*) AS count
                FROM conversation_states
                WHERE updated_at >= NOW() - INTERVAL '{cutoff_days_sql}'
                GROUP BY current_state""",  # nosec B608
            fetch=True,
        ) or []
        funnel = {row.get("current_state"): int(row.get("count") or 0) for row in funnel_rows}
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        funnel = {}

    ai_fallback_confidence_threshold = _float_from_setting(
        "ai_fallback_confidence_threshold",
        default=0.45,
        min_value=0.0,
        max_value=1.0,
    )
    confidence_rows = []
    try:
        confidence_rows = db.execute_query(
            f"""
            SELECT
                COALESCE(NULLIF(metadata->>'funnel_step', ''), 'unknown') AS funnel_step,
                COUNT(*) AS turn_count,
                AVG(
                    CASE
                        WHEN (metadata->>'confidence_score') ~ '^[0-9]*\\.?[0-9]+$'
                            THEN (metadata->>'confidence_score')::double precision
                        ELSE NULL
                    END
                ) AS avg_confidence,
                SUM(
                    CASE
                        WHEN (metadata->>'confidence_score') ~ '^[0-9]*\\.?[0-9]+$'
                             AND (metadata->>'confidence_score')::double precision < %s
                            THEN 1
                        ELSE 0
                    END
                ) AS low_confidence_count
            FROM conversation_events
            WHERE event_type = 'turn_quality'
              AND created_at >= NOW() - INTERVAL '{cutoff_days_sql}'
            GROUP BY COALESCE(NULLIF(metadata->>'funnel_step', ''), 'unknown')
            """,  # nosec B608
            (ai_fallback_confidence_threshold,),
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning("Turn confidence query failed: %s", e)
        confidence_rows = []

    fallback_path_rows = []
    try:
        fallback_path_rows = db.execute_query(
            f"""
            SELECT metadata->>'action_tag' AS action_tag, COUNT(*) AS count
            FROM conversation_events
            WHERE event_type = 'action_tag'
              AND created_at >= NOW() - INTERVAL '{cutoff_days_sql}'
              AND metadata->>'action_tag' IN (
                  'retrieval_policy_used',
                  'ai_fallback_used',
                  'fallback_template_low_confidence',
                  'fallback_template_used'
              )
            GROUP BY metadata->>'action_tag'
            """,  # nosec B608
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning("Fallback path query failed: %s", e)
        fallback_path_rows = []

    confidence_by_step_raw = {
        str((row or {}).get("funnel_step") or "unknown"): float((row or {}).get("avg_confidence") or 0.0)
        for row in confidence_rows
    }
    total_turn_quality = sum(int((row or {}).get("turn_count") or 0) for row in confidence_rows)
    low_confidence_turns = sum(int((row or {}).get("low_confidence_count") or 0) for row in confidence_rows)

    avg_turn_confidence = 0.0
    if total_turn_quality > 0:
        weighted_sum = 0.0
        for row in confidence_rows:
            turn_count = int((row or {}).get("turn_count") or 0)
            avg_conf = float((row or {}).get("avg_confidence") or 0.0)
            weighted_sum += (avg_conf * turn_count)
        avg_turn_confidence = weighted_sum / total_turn_quality

    low_confidence_rate = (low_confidence_turns / total_turn_quality * 100.0) if total_turn_quality > 0 else 0.0

    confidence_step_order = [
        ("qualification", "Qualification"),
        ("availability", "Availability"),
        ("screening", "Screening"),
        ("deposit", "Deposit"),
        ("confirmation", "Confirmation"),
        ("follow_up", "Follow-up"),
    ]
    confidence_by_step_labels = [label for _, label in confidence_step_order]
    confidence_by_step_values = [
        round(float(confidence_by_step_raw.get(key, 0.0)) * 100.0, 1) for key, _ in confidence_step_order
    ]

    fallback_counts_raw = {
        str((row or {}).get("action_tag") or ""): int((row or {}).get("count") or 0)
        for row in fallback_path_rows
    }
    fallback_path_counts = {
        "retrieval_policy": fallback_counts_raw.get("retrieval_policy_used", 0),
        "ai_fallback": fallback_counts_raw.get("ai_fallback_used", 0),
        "template": (
            fallback_counts_raw.get("fallback_template_low_confidence", 0)
            + fallback_counts_raw.get("fallback_template_used", 0)
        ),
    }
    fallback_total = sum(fallback_path_counts.values())
    fallback_path_percentages = {
        key: round((value / fallback_total * 100.0), 1) if fallback_total > 0 else 0.0
        for key, value in fallback_path_counts.items()
    }
    flow_version_comparison = {
        "labels": ["v1", "v2"],
        "qualification_rate": [0.0, 0.0],
        "deposit_reach_rate": [0.0, 0.0],
        "deposit_paid_rate": [0.0, 0.0],
        "completed_booking_rate": [0.0, 0.0],
        "confirmation_rate": [0.0, 0.0],
        "low_confidence_rate": [0.0, 0.0],
        "conversation_totals": [0, 0],
        "turn_quality_totals": [0, 0],
    }
    flow_compare_where = [f"updated_at >= NOW() - INTERVAL '{cutoff_days_sql}'"]
    flow_compare_params: list[str] = []
    if location_filter in ("incall", "outcall"):
        flow_compare_where.append("LOWER(COALESCE(incall_outcall, '')) = %s")
        flow_compare_params.append(location_filter)
    if experience_filter and experience_filter != "all":
        flow_compare_where.append("LOWER(COALESCE(experience_type, '')) = LOWER(%s)")
        flow_compare_params.append(experience_filter)
    flow_compare_where_sql = " AND ".join(flow_compare_where)
    try:
        flow_rows = db.execute_query(
            f"""
            SELECT
                COALESCE(NULLIF(LOWER(flow_version), ''), 'v1') AS flow_version,
                COUNT(*) AS total_conversations,
                SUM(CASE WHEN COALESCE(current_state, 'NEW') <> 'NEW' THEN 1 ELSE 0 END) AS qualified_count,
                SUM(CASE WHEN COALESCE(current_state, '') IN ('DEPOSIT_REQUIRED', 'CONFIRMED') THEN 1 ELSE 0 END) AS deposit_reached_count,
                SUM(CASE WHEN deposit_paid = TRUE THEN 1 ELSE 0 END) AS deposit_paid,
                SUM(CASE WHEN COALESCE(current_state, '') = 'CONFIRMED' THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN COALESCE(current_state, '') = 'POST_BOOKING' THEN 1 ELSE 0 END) AS completed_booking
            FROM conversation_states
            WHERE {flow_compare_where_sql}
            GROUP BY COALESCE(NULLIF(LOWER(flow_version), ''), 'v1')
            """,  # nosec B608
            tuple(flow_compare_params),
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning("Flow comparison query failed: %s", e)
        flow_rows = []
    try:
        flow_low_conf_rows = db.execute_query(
            f"""
            SELECT
                COALESCE(NULLIF(LOWER(cs.flow_version), ''), 'v1') AS flow_version,
                COUNT(*) AS total_turns,
                SUM(
                    CASE
                        WHEN (ce.metadata->>'confidence_score') ~ '^[0-9]*\\.?[0-9]+$'
                             AND (ce.metadata->>'confidence_score')::double precision < %s
                            THEN 1
                        ELSE 0
                    END
                ) AS low_confidence_count
            FROM conversation_events ce
            JOIN conversation_states cs ON cs.phone_number = ce.phone_number
            WHERE ce.event_type = 'turn_quality'
              AND ce.created_at >= NOW() - INTERVAL '{cutoff_days_sql}'
              {"AND LOWER(COALESCE(cs.incall_outcall, '')) = %s" if location_filter in ("incall", "outcall") else ""}
              {"AND LOWER(COALESCE(cs.experience_type, '')) = LOWER(%s)" if experience_filter and experience_filter != "all" else ""}
            GROUP BY COALESCE(NULLIF(LOWER(cs.flow_version), ''), 'v1')
            """,  # nosec B608
            tuple(
                [ai_fallback_confidence_threshold]
                + ([location_filter] if location_filter in ("incall", "outcall") else [])
                + ([experience_filter] if experience_filter and experience_filter != "all" else [])
            ),
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning("Flow low-confidence comparison query failed: %s", e)
        flow_low_conf_rows = []

    flow_snapshot = {
        str((row or {}).get("flow_version") or "v1"): {
            "total": int((row or {}).get("total_conversations") or 0),
            "qualified": int((row or {}).get("qualified_count") or 0),
            "deposit_reached": int((row or {}).get("deposit_reached_count") or 0),
            "confirmed": int((row or {}).get("confirmed_count") or 0),
        }
        for row in flow_rows
    }
    flow_low_conf = {
        str((row or {}).get("flow_version") or "v1"): {
            "total_turns": int((row or {}).get("total_turns") or 0),
            "low_confidence": int((row or {}).get("low_confidence_count") or 0),
        }
        for row in flow_low_conf_rows
    }
    for idx, flow_name in enumerate(("v1", "v2")):
        snap = flow_snapshot.get(flow_name, {})
        flow_total = int(snap.get("total", 0))
        flow_version_comparison["conversation_totals"][idx] = flow_total
        flow_version_comparison["qualification_rate"][idx] = round(
            (int(snap.get("qualified", 0)) / flow_total * 100.0) if flow_total > 0 else 0.0,
            1,
        )
        flow_version_comparison["deposit_reach_rate"][idx] = round(
            (int(snap.get("deposit_reached", 0)) / flow_total * 100.0) if flow_total > 0 else 0.0,
            1,
        )
        flow_version_comparison["deposit_paid_rate"][idx] = round(
            (int(snap.get("deposit_paid", 0)) / flow_total * 100.0) if flow_total > 0 else 0.0,
            1,
        )
        flow_version_comparison["confirmation_rate"][idx] = round(
            (int(snap.get("confirmed", 0)) / flow_total * 100.0) if flow_total > 0 else 0.0,
            1,
        )
        flow_version_comparison["completed_booking_rate"][idx] = round(
            (int(snap.get("completed_booking", 0)) / flow_total * 100.0) if flow_total > 0 else 0.0,
            1,
        )
        lc = flow_low_conf.get(flow_name, {})
        lc_total = int(lc.get("total_turns", 0))
        flow_version_comparison["turn_quality_totals"][idx] = lc_total
        flow_version_comparison["low_confidence_rate"][idx] = round(
            (int(lc.get("low_confidence", 0)) / lc_total * 100.0) if lc_total > 0 else 0.0,
            1,
        )
    threshold_optimizer = _build_threshold_optimizer(
        ai_fallback_confidence_threshold=ai_fallback_confidence_threshold,
        confidence_rows=confidence_rows,
        flow_version_comparison=flow_version_comparison,
    )
    rollout_guardrail = _build_rollout_guardrail(
        flow_version_comparison=flow_version_comparison,
    )

    alerts = []
    if bookings_last_month and bookings_this_month < bookings_last_month:
        pct_drop = round(((bookings_last_month - bookings_this_month) / bookings_last_month) * 100, 1)
        alerts.append({"severity": "warning", "text": f"Bookings are down {pct_drop}% vs last month."})
    if conversion_rate and conversion_rate < 10:
        alerts.append({"severity": "warning", "text": "Conversion is below 10%. Review first-contact prompts and follow-up speed."})
    if deposit_rate and deposit_rate < 60:
        alerts.append({"severity": "warning", "text": "Deposit completion is below 60%. Check deposit friction and validation flow."})
    if low_confidence_rate >= 35:
        alerts.append({
            "severity": "warning",
            "text": (
                f"Low-confidence turns are {round(low_confidence_rate, 1)}%. "
                "Review fallback threshold and first-response copy."
            ),
        })
    if rollout_guardrail.get("status") == "breach":
        alerts.append({
            "severity": "warning",
            "text": (
                f"Guardrail breach: v2 confirmation gap {rollout_guardrail.get('confirmation_gap_pp')}pp, "
                f"low-confidence gap {rollout_guardrail.get('low_confidence_gap_pp')}pp."
            ),
        })
    elif rollout_guardrail.get("status") == "warning":
        alerts.append({
            "severity": "warning",
            "text": (
                f"Guardrail warning: v2 confirmation gap {rollout_guardrail.get('confirmation_gap_pp')}pp, "
                f"low-confidence gap {rollout_guardrail.get('low_confidence_gap_pp')}pp."
            ),
        })

    return {
        "selected_days": days,
        "selected_location": location_filter,
        "selected_experience": experience_filter,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "total_bookings": total_bookings,
        "bookings_this_month": bookings_this_month,
        "bookings_last_month": bookings_last_month,
        "total_enquiries": total_enquiries,
        "conversion_rate": round(conversion_rate, 2),
        "enquiries_7d_labels": enquiries_7d_labels,
        "enquiries_7d_values": enquiries_7d_values,
        "deposits_requested": deposits_requested,
        "deposits_paid": deposits_paid,
        "deposit_rate": round(deposit_rate, 1),
        "profanity_blocks": profanity_blocks,
        "blocked_count": blocked_count,
        "total_messages_30d": total_messages_30d,
        "inbound_messages_30d": total_messages_30d,
        "outbound_messages_30d": messages_sent_30d,
        "messages_sent_30d": messages_sent_30d,
        "ai_fallback_count": ai_fallback_count,
        "ai_fallback_confidence_threshold": round(ai_fallback_confidence_threshold, 2),
        "avg_turn_confidence": round(avg_turn_confidence * 100.0, 1),
        "total_turn_quality_events": total_turn_quality,
        "low_confidence_turns": low_confidence_turns,
        "low_confidence_rate": round(low_confidence_rate, 1),
        "confidence_by_step_labels": confidence_by_step_labels,
        "confidence_by_step_values": confidence_by_step_values,
        "fallback_path_counts": fallback_path_counts,
        "fallback_path_percentages": fallback_path_percentages,
        "flow_version_comparison": flow_version_comparison,
        "threshold_optimizer": threshold_optimizer,
        "rollout_guardrail": rollout_guardrail,
        "bookings_labels": bookings_labels,
        "bookings_values": bookings_values,
        "message_traffic_labels": message_traffic_labels,
        "inbound_traffic": inbound_traffic,
        "outbound_traffic": outbound_traffic,
        "total_earnings_30d": float(total_earnings_30d or 0),
        "total_earnings_all": float(total_earnings_all or 0),
        "daily_earnings_labels": daily_earnings_labels,
        "daily_earnings_values": daily_earnings_values,
        "weekly_earnings_labels": weekly_earnings_labels,
        "weekly_earnings_values": weekly_earnings_values,
        "avg_booking_value": float(avg_booking_value or 0),
        "experience_labels": experience_labels,
        "experience_values": experience_values,
        "location_labels": location_labels,
        "location_values": location_values,
        "city_labels": city_labels,
        "city_values": city_values,
        "top_clients": top_clients or [],
        "funnel": {
            "NEW": funnel.get("NEW", 0),
            "COLLECTING": funnel.get("COLLECTING", 0),
            "CHECKING_AVAILABILITY": funnel.get("CHECKING_AVAILABILITY", 0),
            "DEPOSIT_REQUIRED": funnel.get("DEPOSIT_REQUIRED", 0),
            "CONFIRMED": funnel.get("CONFIRMED", 0),
        },
        "alerts": alerts,
    }


def _confirmed_at_to_date(confirmed_at) -> date | None:
    """Normalize confirmed_at from DB (datetime, date, or string) to a date for comparison."""
    if confirmed_at is None:
        return None
    if hasattr(confirmed_at, "date") and callable(confirmed_at.date):
        result = confirmed_at.date()
        if isinstance(result, date):
            return result
    if hasattr(confirmed_at, "year") and hasattr(confirmed_at, "month") and hasattr(confirmed_at, "day"):
        if isinstance(confirmed_at, date):
            return confirmed_at
    if isinstance(confirmed_at, str):
        try:
            return datetime.strptime(confirmed_at[:10], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            pass
    return None


def _compute_earnings(db, days: int = 30, location_filter: str = "all", experience_filter: str = "all"):
    """Compute earnings from confirmed bookings using Rates page pricing.
    Returns (total_earnings_30d, total_earnings_all, avg_booking_value,
             daily_earnings_labels, daily_earnings_values,
             weekly_earnings_labels, weekly_earnings_values, top_clients).
    """
    _allowed_spans = frozenset({7, 30, 90})
    span_days = days if days in _allowed_spans else 30

    try:
        from templates.confirmations import calculate_price
    except Exception as e:
        logger.warning(f"Could not import calculate_price: {e}")
        return (0.0, 0.0, 0.0, [], [], [], [], [])

    params = []
    where = ["confirmed_at IS NOT NULL", f"confirmed_at >= NOW() - INTERVAL '{span_days} days'"]
    if location_filter in ("incall", "outcall"):
        where.append("LOWER(COALESCE(incall_outcall, '')) = %s")
        params.append(location_filter)
    if experience_filter and experience_filter != "all":
        where.append("LOWER(COALESCE(experience_type, '')) = LOWER(%s)")
        params.append(experience_filter)
    where_sql = " AND ".join(where)

    try:
        rows = db.execute_query(
            f"""SELECT confirmed_at, duration, experience_type, incall_outcall, phone_number, client_name
               FROM conversation_states WHERE {where_sql}""",  # nosec B608
            tuple(params),
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning(f"Earnings query failed: {e}")
        return (0.0, 0.0, 0.0, [], [], [], [], [])

    # Use app timezone so "last 30 days" matches business location (e.g. Australia)
    try:
        from utils.timezone import get_current_datetime
        now = get_current_datetime()
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        now = datetime.now(UTC)
    # Ensure we have a date for cutoff (handle timezone-aware datetime)
    if hasattr(now, "date"):
        now_date = now.date()
    else:
        now_date = now
    cutoff_period = now_date - timedelta(days=span_days)
    cutoff_12w = now_date - timedelta(weeks=12)

    daily_totals = defaultdict(float)
    weekly_totals = defaultdict(float)
    client_totals = defaultdict(lambda: {"bookings": 0, "total_spent": 0.0})
    total_earnings_all = 0.0
    total_earnings_30d = 0.0

    for row in rows:
        confirmed_at = row.get("confirmed_at")
        duration = int(row.get("duration") or 60)
        phone_number = str(row.get("phone_number") or "")
        client_name = (row.get("client_name") or "").strip() or None

        try:
            price = calculate_price(duration)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            price = 0
        price = float(price) if price is not None else 0.0

        total_earnings_all += price
        dt_date = _confirmed_at_to_date(confirmed_at)
        if dt_date is not None:
            if dt_date >= cutoff_period:
                total_earnings_30d += price
                daily_totals[dt_date] += price
            if dt_date >= cutoff_12w:
                if hasattr(dt_date, "isocalendar"):
                    iso = dt_date.isocalendar()
                    week_key = (iso[0], iso[1])
                else:
                    week_key = (getattr(confirmed_at, "year", now_date.year), getattr(confirmed_at, "month", now_date.month))
                weekly_totals[week_key] += price

        if dt_date is not None and dt_date >= cutoff_period:
            key = (phone_number, client_name or "")
            client_totals[key]["bookings"] += 1
            client_totals[key]["total_spent"] += price

    daily_earnings_labels = []
    daily_earnings_values = []
    for i in range(span_days):
        d = now_date - timedelta(days=(span_days - 1) - i)
        daily_earnings_labels.append(_safe_date_str(d))
        daily_earnings_values.append(round(daily_totals.get(d, 0), 2))

    # Weekly earnings: this week Sun–Sat, one column per day
    # Python weekday(): Mon=0 … Sun=6. Days since Sunday = (weekday + 1) % 7
    days_since_sunday = (now_date.weekday() + 1) % 7
    week_start_sunday = now_date - timedelta(days=days_since_sunday)
    day_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    weekly_earnings_labels = []
    weekly_earnings_values = []
    for i in range(7):
        d = week_start_sunday + timedelta(days=i)
        weekly_earnings_labels.append(day_names[i])
        weekly_earnings_values.append(round(daily_totals.get(d, 0), 2))

    booking_count = len(rows)
    avg_booking_value = round(total_earnings_all / booking_count, 2) if booking_count else 0.0
    total_earnings_30d = round(total_earnings_30d, 2)
    total_earnings_all = round(total_earnings_all, 2)

    top_clients = []
    for (phone_number, client_name), data in sorted(
        client_totals.items(), key=lambda x: -x[1]["total_spent"]
    )[:5]:
        top_clients.append({
            "phone_number": phone_number,
            "client_name": client_name or "\u2014",
            "bookings": data["bookings"],
            "total_spent": round(data["total_spent"], 2),
        })

    return (
        total_earnings_30d,
        total_earnings_all,
        avg_booking_value,
        daily_earnings_labels,
        daily_earnings_values,
        weekly_earnings_labels,
        weekly_earnings_values,
        top_clients,
    )


def _get_count(db, query, params=None):
    """Execute a count query and return the result as int."""
    result = db.execute_query(query, params or (), fetch=True)
    if result and isinstance(result[0], dict):
        val = result[0].get("count", 0) or 0
        return int(val)
    elif result:
        # tuple-like result: use row_get to access first column safely
        return int(row_get(result[0], 0, 0) or 0)
    return 0


def _get_sum(db, query):
    """Execute a sum query and return the result."""
    result = db.execute_query(query, fetch=True)
    if result and isinstance(result[0], dict):
        return result[0].get("total", 0) or 0
    elif result:
        return row_get(result[0], 0, 0) or 0
    return 0


def _demo_stats():
    """Return sample stats so the stats page can be previewed without DB (e.g. /stats?demo=1)."""
    from datetime import datetime, timedelta
    now = datetime.now(UTC)
    # Last 30 days labels
    daily_labels = [(now - timedelta(days=29 - i)).strftime("%Y-%m-%d") for i in range(30)]
    # Sample chart data
    bookings_values = [0, 1, 0, 2, 1, 0, 3, 2, 1, 0, 2, 1, 2, 0, 1, 3, 2, 1, 0, 2, 1, 2, 0, 1, 2, 1, 0, 2, 1, 1]
    daily_earnings = [0, 350, 0, 700, 350, 0, 1050, 700, 350, 0, 700, 350, 700, 0, 350, 1050, 700, 350, 0, 700, 350, 700, 0, 350, 700, 350, 0, 700, 350, 350]

    # This week Sun–Sat labels for demo
    weekly_labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    return {
        "selected_days": 30,
        "selected_location": "all",
        "selected_experience": "all",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "total_bookings": 42,
        "bookings_this_month": 12,
        "bookings_last_month": 15,
        "total_enquiries": 186,
        "enquiries_7d_labels": [(datetime.now(UTC) - timedelta(days=6 - i)).strftime("%Y-%m-%d") for i in range(7)],
        "enquiries_7d_values": [8, 12, 6, 15, 11, 9, 14],
        "conversion_rate": 22.58,
        "deposits_requested": 8,
        "deposits_paid": 6,
        "deposit_rate": 75.0,
        "profanity_blocks": 1,
        "blocked_count": 2,
        "total_messages_30d": 412,
        "inbound_messages_30d": 412,
        "outbound_messages_30d": 0,
        "messages_sent_30d": 388,
        "ai_fallback_count": 9,
        "ai_fallback_confidence_threshold": 0.45,
        "avg_turn_confidence": 72.5,
        "total_turn_quality_events": 160,
        "low_confidence_turns": 28,
        "low_confidence_rate": 17.5,
        "confidence_by_step_labels": ["Qualification", "Availability", "Screening", "Deposit", "Confirmation", "Follow-up"],
        "confidence_by_step_values": [74.0, 71.0, 66.0, 63.0, 79.0, 76.0],
        "fallback_path_counts": {"retrieval_policy": 48, "ai_fallback": 31, "template": 21},
        "fallback_path_percentages": {"retrieval_policy": 48.0, "ai_fallback": 31.0, "template": 21.0},
        "flow_version_comparison": {
            "labels": ["v1", "v2"],
            "qualification_rate": [63.0, 72.0],
            "deposit_reach_rate": [24.0, 31.0],
            "confirmation_rate": [18.0, 27.0],
            "low_confidence_rate": [24.0, 16.0],
            "conversation_totals": [82, 78],
            "turn_quality_totals": [106, 98],
        },
        "threshold_optimizer": {
            "target_low_confidence_band": [15.0, 25.0],
            "current_thresholds": {
                "global": 0.45,
                "qualification": 0.45,
                "availability": 0.45,
                "screening": 0.45,
                "deposit": 0.45,
                "confirmation": 0.45,
                "follow_up": 0.45,
            },
            "suggested_thresholds": {
                "global": 0.42,
                "qualification": 0.40,
                "availability": 0.41,
                "screening": 0.42,
                "deposit": 0.44,
                "confirmation": 0.43,
                "follow_up": 0.42,
            },
            "estimated_confirmation_lift_pp": 9.0,
            "reasons": ["Demo dataset indicates strong v2 confirmation lift."],
            "would_adjust": True,
        },
        "rollout_guardrail": {
            "status": "healthy",
            "summary": "v2 KPIs are within guardrail thresholds.",
            "confirmation_gap_pp": 9.0,
            "low_confidence_gap_pp": -8.0,
            "current_rollout_percent": 35,
            "recommended_action": "none",
            "suggested_rollout_percent": 35,
        },
        "bookings_labels": daily_labels,
        "bookings_values": bookings_values,
        "message_traffic_labels": daily_labels,
        "inbound_traffic": [12, 18, 10, 22, 14, 8, 20, 16, 12, 9, 15, 11, 19, 7, 13, 21, 17, 14, 10, 16, 12, 18, 8, 11, 15, 13, 9, 17, 12, 14],
        "outbound_traffic": [0] * 30,
        "total_earnings_30d": 10850.0,
        "total_earnings_all": 18900.0,
        "daily_earnings_labels": daily_labels,
        "daily_earnings_values": daily_earnings,
        "weekly_earnings_labels": weekly_labels,
        "weekly_earnings_values": [0.0, 350.0, 700.0, 1050.0, 700.0, 350.0, 0.0],
        "avg_booking_value": 450.0,
        "experience_labels": ["GFE", "PSE", "Dinner date", "Social", "Unknown"],
        "experience_values": [18, 12, 5, 4, 3],
        "location_labels": ["incall", "outcall"],
        "location_values": [28, 14],
        "city_labels": [],
        "city_values": [],
        "top_clients": [
            {"phone_number": "+614****1234", "client_name": "James", "bookings": 5, "total_spent": 2250.0},
            {"phone_number": "+614****5678", "client_name": "Mike", "bookings": 4, "total_spent": 1800.0},
            {"phone_number": "+614****9012", "client_name": "Alex", "bookings": 3, "total_spent": 1350.0},
            {"phone_number": "+614****3456", "client_name": "Tom", "bookings": 3, "total_spent": 1200.0},
            {"phone_number": "+614****7890", "client_name": "David", "bookings": 2, "total_spent": 900.0},
        ],
        "funnel": {
            "NEW": 37,
            "COLLECTING": 24,
            "CHECKING_AVAILABILITY": 18,
            "DEPOSIT_REQUIRED": 9,
            "CONFIRMED": 12,
        },
        "alerts": [{"severity": "warning", "text": "Bookings are down 20.0% vs last month."}],
    }


def _empty_stats():
    """Return empty stats structure for error cases."""
    return {
        "selected_days": 30,
        "selected_location": "all",
        "selected_experience": "all",
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M"),
        "total_bookings": 0,
        "bookings_this_month": 0,
        "bookings_last_month": 0,
        "total_enquiries": 0,
        "enquiries_7d_labels": [],
        "enquiries_7d_values": [],
        "conversion_rate": 0,
        "deposits_requested": 0,
        "deposits_paid": 0,
        "deposit_rate": 0,
        "profanity_blocks": 0,
        "blocked_count": 0,
        "total_messages_30d": 0,
        "inbound_messages_30d": 0,
        "outbound_messages_30d": 0,
        "messages_sent_30d": 0,
        "ai_fallback_count": 0,
        "ai_fallback_confidence_threshold": 0.45,
        "avg_turn_confidence": 0,
        "total_turn_quality_events": 0,
        "low_confidence_turns": 0,
        "low_confidence_rate": 0,
        "confidence_by_step_labels": [],
        "confidence_by_step_values": [],
        "fallback_path_counts": {"retrieval_policy": 0, "ai_fallback": 0, "template": 0},
        "fallback_path_percentages": {"retrieval_policy": 0, "ai_fallback": 0, "template": 0},
        "flow_version_comparison": {
            "labels": ["v1", "v2"],
            "qualification_rate": [0.0, 0.0],
            "deposit_reach_rate": [0.0, 0.0],
            "confirmation_rate": [0.0, 0.0],
            "low_confidence_rate": [0.0, 0.0],
            "conversation_totals": [0, 0],
            "turn_quality_totals": [0, 0],
        },
        "threshold_optimizer": {
            "target_low_confidence_band": [15.0, 25.0],
            "current_thresholds": {
                "global": 0.45,
                "qualification": 0.45,
                "availability": 0.45,
                "screening": 0.45,
                "deposit": 0.45,
                "confirmation": 0.45,
                "follow_up": 0.45,
            },
            "suggested_thresholds": {
                "global": 0.45,
                "qualification": 0.45,
                "availability": 0.45,
                "screening": 0.45,
                "deposit": 0.45,
                "confirmation": 0.45,
                "follow_up": 0.45,
            },
            "estimated_confirmation_lift_pp": 0.0,
            "reasons": [],
            "would_adjust": False,
        },
        "rollout_guardrail": {
            "status": "healthy",
            "summary": "v2 KPIs are within guardrail thresholds.",
            "confirmation_gap_pp": 0.0,
            "low_confidence_gap_pp": 0.0,
            "current_rollout_percent": 0,
            "recommended_action": "none",
            "suggested_rollout_percent": 0,
        },
        "bookings_labels": [],
        "bookings_values": [],
        "message_traffic_labels": [],
        "inbound_traffic": [],
        "outbound_traffic": [],
        "total_earnings_30d": 0,
        "total_earnings_all": 0,
        "daily_earnings_labels": [],
        "daily_earnings_values": [],
        "weekly_earnings_labels": [],
        "weekly_earnings_values": [],
        "avg_booking_value": 0,
        "experience_labels": [],
        "experience_values": [],
        "location_labels": [],
        "location_values": [],
        "city_labels": [],
        "city_values": [],
        "top_clients": [],
        "funnel": {
            "NEW": 0,
            "COLLECTING": 0,
            "CHECKING_AVAILABILITY": 0,
            "DEPOSIT_REQUIRED": 0,
            "CONFIRMED": 0,
        },
        "alerts": [],
    }
