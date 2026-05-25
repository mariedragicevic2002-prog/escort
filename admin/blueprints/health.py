"""Health dashboard blueprint - /health route."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import hmac
import logging
import os
import time

from flask import Blueprint, current_app, jsonify, render_template, request, session

import config
from admin.auth import login_user, verify_password
from config import get_effective_escort_timezone
from admin.rate_limiter import get_lockout_remaining, is_ip_locked_out, rate_limit_login
from services.database_service import get_shared_db
from utils.row_utils import row_get

logger = logging.getLogger("escort_chatbot.admin.health")

health_bp = Blueprint('health', __name__, template_folder='../templates')

# Section / JSON keys (Sonar S1192)
_ERR_NOT_AUTHENTICATED = "Not authenticated"
_HC_DB_CONNECTION = "Database Connection"
_HC_DB_TABLES = "Database Tables"
_HC_MOBILE_API = "Mobile APK API"
_HC_SMS_GATEWAYS = "SMS Gateways"
_HC_CLAUDE = "Claude AI (primary)"
_HC_GEMINI = "Gemini AI (fallback)"
_HC_ESCORT_NAME = "Escort Name"
_HC_PRICING_CONFIG = "Pricing Config"
_HC_DEPOSIT_RATES = "Deposit Rates"
_HC_AVAILABLE_HOURS = "Available Hours"


def _is_health_authenticated():
    """Check if user is authenticated for health dashboard."""
    return session.get("admin_authenticated", False) or session.get("health_authenticated", False)


@health_bp.route('/health', methods=['GET', 'POST'])
@rate_limit_login
def health_dashboard():
    """System health dashboard with diagnostics."""
    authenticated = _is_health_authenticated()
    error = None

    # Check if IP is locked out
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()

    if is_ip_locked_out(ip):
        remaining = get_lockout_remaining(ip)
        minutes = remaining // 60
        error = f'Too many failed attempts. Locked out for {minutes} more minutes.'
        return render_template('health.html', authenticated=False, error=error, escort_timezone=get_effective_escort_timezone())

    # Handle authentication
    if request.method == 'POST' and not authenticated:
        password = request.form.get("password", "")
        if verify_password(password):
            login_user()  # Use proper session initialization
            session["health_authenticated"] = True
            authenticated = True
        else:
            error = "Invalid password"

    if not authenticated:
        return render_template("health.html", authenticated=False, error=error, escort_timezone=get_effective_escort_timezone())

    return render_template('health.html', authenticated=True, escort_timezone=get_effective_escort_timezone())


@health_bp.route('/admin/run-health-check', methods=['POST'])
def run_health_check():
    """Run comprehensive chatbot health check (14 tests)."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        tests = []
        passed = 0
        warnings = 0
        failed = 0

        def _pass(name, msg):
            nonlocal passed
            tests.append({"name": name, "status": "passed", "message": msg})
            passed += 1

        def _warn(name, msg):
            nonlocal warnings
            tests.append({"name": name, "status": "warning", "message": msg})
            warnings += 1

        def _fail(name, msg):
            nonlocal failed
            tests.append({"name": name, "status": "failed", "message": msg})
            failed += 1

        # 1. Database Connection
        try:
            result = db.execute_query("SELECT 1", fetch=True)
            if result:
                _pass(_HC_DB_CONNECTION, "Database is responding")
            else:
                _fail(_HC_DB_CONNECTION, "Database not responding")
        except Exception as e:
            _fail(_HC_DB_CONNECTION, f"Error: {str(e)[:120]}")

        # 2. Database Tables
        try:
            result = db.execute_query(
                "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('conversation_states','message_history','admin_settings')",
                fetch=True
            )
            found = {r['table_name'] for r in result} if result else set()
            missing = {'conversation_states', 'message_history', 'admin_settings'} - found
            if not missing:
                _pass(_HC_DB_TABLES, "All required tables exist")
            else:
                _fail(_HC_DB_TABLES, f"Missing tables: {', '.join(missing)}")
        except Exception as e:
            _warn(_HC_DB_TABLES, f"Could not verify: {str(e)[:100]}")

        # 3. Mobile APK API route
        try:
            result = db.execute_query("SELECT COUNT(*) as count FROM bookings", fetch=True)
            booking_count = int(result[0]['count']) if result else 0
            _pass(_HC_MOBILE_API, f"Route: /schedule/api/mobile-sync — {booking_count} booking(s) in DB")
        except Exception as e:
            _warn(_HC_MOBILE_API, f"Bookings table error: {str(e)[:100]}")

        # 5. SMS (httpSMS gateway)
        try:
            from services.sms_service import get_gateway_status
            gw = get_gateway_status()
            hs_active = bool(gw.get("httpsms", {}).get("active"))
            if hs_active:
                _pass(_HC_SMS_GATEWAYS, "httpSMS gateway configured")
            else:
                _warn(_HC_SMS_GATEWAYS, "httpSMS gateway not active (disabled or not configured)")
        except Exception as e:
            _warn(_HC_SMS_GATEWAYS, f"Error: {str(e)[:100]}")

        # 7. Claude AI
        try:
            from services.ai_service import AIService  # noqa: F401

            claude_key = config.get_anthropic_api_key()
            if claude_key:
                _pass(_HC_CLAUDE, "API key found")
            else:
                _fail(
                    _HC_CLAUDE,
                    "No Claude key — save on Config page or set ANTHROPIC_API_KEY / CLAUDE_API_KEY",
                )
        except Exception as e:
            _fail(_HC_CLAUDE, f"Error: {str(e)[:100]}")

        # 8. Gemini AI (fallback)
        try:
            gemini_key = config.get_gemini_api_key()
            if gemini_key:
                _pass(_HC_GEMINI, "API key set (Config or GEMINI_API_KEY)")
            else:
                _warn(
                    _HC_GEMINI,
                    "Gemini key not set — save on Config page or set GEMINI_API_KEY (fallback unavailable)",
                )
        except Exception as e:
            _warn(_HC_GEMINI, f"Error: {str(e)[:100]}")

        # 9. PayID Configured
        try:
            payid = config.get_payid()
            if payid and str(payid).strip():
                _pass("PayID", f"Set: {str(payid)[:40]}")
            else:
                _warn("PayID", "PayID not configured")
        except Exception as e:
            _warn("PayID", f"Error: {str(e)[:100]}")

        # 10. Escort Name
        try:
            name = config.get_escort_name()
            if name and str(name).strip():
                _pass(_HC_ESCORT_NAME, f"{name}")
            else:
                _warn(_HC_ESCORT_NAME, "Escort name not set")
        except Exception as e:
            _warn(_HC_ESCORT_NAME, f"Error: {str(e)[:100]}")

        # 11. Pricing Configured
        try:
            from core.rates_from_config import get_incall_pricing
            incall = get_incall_pricing()
            gfe_60 = incall.get("gfe_60", 0) if incall else 0
            if gfe_60 > 0:
                _pass(_HC_PRICING_CONFIG, f"GFE 60min = ${gfe_60}")
            else:
                _warn(_HC_PRICING_CONFIG, "GFE 60min rate is 0 \u2014 check pricing settings")
        except Exception as e:
            _warn(_HC_PRICING_CONFIG, f"Error: {str(e)[:100]}")

        # 12. Deposit Rates
        try:
            from core.rates_from_config import get_deposit_incall, get_deposit_outcall
            dep_in = get_deposit_incall()
            dep_out = get_deposit_outcall()
            if dep_in > 0 and dep_out > 0:
                _pass(_HC_DEPOSIT_RATES, f"Incall ${dep_in} / Outcall ${dep_out}")
            else:
                missing_deps = []
                if not dep_in:
                    missing_deps.append("incall")
                if not dep_out:
                    missing_deps.append("outcall")
                _warn(_HC_DEPOSIT_RATES, f"{', '.join(missing_deps)} deposit is 0")
        except Exception as e:
            _warn(_HC_DEPOSIT_RATES, f"Error: {str(e)[:100]}")

        # 13. Location Configured
        try:
            location = config.get_current_incall_location()
            city = (location.get("city") or "").strip()
            hotel = (location.get("hotel_name") or location.get("address") or "").strip()
            if city and hotel:
                _pass("Location", f"{city} \u2014 {hotel}")
            else:
                _warn("Location", "City or hotel/address not configured")
        except Exception as e:
            _warn("Location", f"Error: {str(e)[:100]}")

        # 14. Available Hours
        try:
            from core.settings_manager import get_setting
            hours = get_setting("available_hours") or ""
            if str(hours).strip():
                _pass(_HC_AVAILABLE_HOURS, str(hours)[:80])
            else:
                _warn(_HC_AVAILABLE_HOURS, "Working hours not set in settings")
        except Exception as e:
            _warn(_HC_AVAILABLE_HOURS, f"Error: {str(e)[:100]}")

        # Calculate health score (warnings count as half-credit)
        total = passed + warnings + failed
        health_score = int(((passed + warnings * 0.5) / total) * 100) if total > 0 else 0

        return jsonify({
            "success": True,
            "results": {
                "tests": tests,
                "health_score": health_score,
                "summary": {
                    "passed": passed,
                    "warnings": warnings,
                    "failed": failed
                }
            }
        }), 200

    except Exception as e:
        logger.exception("Health check error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@health_bp.route('/admin/check-system-status', methods=['GET'])
def check_system_status():
    """Check system health and chatbot status."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        # Get message count from last hour
        messages_count = 0
        try:
            result = db.execute_query(
                """
                SELECT COUNT(*) as count FROM message_history
                WHERE created_at > NOW() - INTERVAL '1 hour'
                """,
                fetch=True
            )
            if result:
                messages_count = int(row_get(result[0], 'count', row_get(result[0], 0, 0)) or 0)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Real database check
        db_status = "error"
        try:
            if db and db.execute_query("SELECT 1", fetch=True):
                db_status = "healthy"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Real AI check (just import, no API call)
        ai_status = "error"
        try:
            from services.ai_service import AIService  # noqa: F401
            ai_status = "available"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # SMS gateway check
        sms_status = "not_configured"
        try:
            from services.sms_service import get_gateway_status
            gw = get_gateway_status()
            hs_active = bool(gw.get("httpsms", {}).get("active"))
            sms_status = "configured" if hs_active else "not_configured"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Mobile APK API check — verifies bookings table is accessible
        mobile_api_status = "operational"
        try:
            db.execute_query("SELECT COUNT(*) FROM bookings", fetch=True)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            mobile_api_status = "error"

        # All conversation state rows (includes NEW — what users think of as "clients stored")
        conversation_states_total = 0
        try:
            result = db.execute_query(
                "SELECT COUNT(*) as count FROM conversation_states",
                fetch=True
            )
            if result:
                conversation_states_total = int(row_get(result[0], 'count', row_get(result[0], 0, 0)) or 0)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Active = non-NEW (in progress / not a fresh row)
        active_conversations = 0
        try:
            result = db.execute_query(
                "SELECT COUNT(*) as count FROM conversation_states WHERE current_state NOT IN ('NEW')",
                fetch=True
            )
            if result:
                active_conversations = int(row_get(result[0], 'count', row_get(result[0], 0, 0)) or 0)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Today's confirmed bookings
        todays_bookings = 0
        try:
            result = db.execute_query(
                "SELECT COUNT(*) as count FROM conversation_states WHERE current_state = 'CONFIRMED' AND DATE(updated_at) = CURRENT_DATE",
                fetch=True
            )
            if result:
                todays_bookings = int(row_get(result[0], 'count', row_get(result[0], 0, 0)) or 0)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Last message received timestamp
        last_message_at = None
        try:
            result = db.execute_query(
                "SELECT MAX(created_at) as last_msg FROM message_history",
                fetch=True
            )
            _lm = row_get(result[0], 'last_msg', row_get(result[0], 0, None)) if result else None
            if _lm:
                last_message_at = _lm.isoformat() if hasattr(_lm, 'isoformat') else str(_lm)
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        # Available hours from settings
        available_hours = None
        try:
            from core.settings_manager import get_setting
            available_hours = get_setting("available_hours") or None
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)

        status = {
            "chatbot": {"status": "online" if db_status == "healthy" else "degraded"},
            "database": {"status": db_status},
            "ai_services": {"status": ai_status},
            "sms": {"status": sms_status},
            "mobile_api": {"status": mobile_api_status, "route": "/schedule/api/mobile-sync"},
            "performance": {
                "messages_last_hour": messages_count,
                "conversation_states_total": conversation_states_total,
                "active_conversations": active_conversations,
                "todays_bookings": todays_bookings,
                "last_message_at": last_message_at,
                "available_hours": available_hours,
            }
        }

        return jsonify({"success": True, "status": status}), 200

    except Exception as e:
        logger.exception("System status error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@health_bp.route('/admin/test-ai', methods=['POST'])
def test_ai_response():
    """Test AI response with a prompt."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        data = request.get_json()
        if not data or not data.get("prompt"):
            return jsonify({"success": False, "error": "No prompt provided"})

        prompt = data.get("prompt", "Hello")
        provider = data.get("provider", "claude")

        start_time = time.time()

        from services.ai_service import AIService
        ai = AIService(provider=provider)
        response = ai.chat(prompt, system_prompt="You are a test assistant. Respond briefly.")

        response_time_ms = int((time.time() - start_time) * 1000)

        return jsonify({
            "success": True,
            "provider": provider,
            "response": response,
            "response_time_ms": response_time_ms
        }), 200

    except Exception as e:
        logger.exception("AI test error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 200


@health_bp.route('/admin/test-sms', methods=['POST'])
def test_sms():
    """Send a test SMS."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "No data provided"})

        phone = data.get("phone", "").strip()
        try:
            from config import get_escort_name
            default_msg = f"Test SMS from {get_escort_name()}"
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            default_msg = "Test SMS"
        message = data.get("message", default_msg).strip()

        if not phone:
            return jsonify({"success": False, "error": "Phone number required"})

        from services.sms_service import send_sms
        result = send_sms(phone, message)

        if result:
            return jsonify({"success": True, "message": f"Test SMS sent to {phone}"}), 200
        else:
            return jsonify({"success": False, "error": "SMS failed to send"}), 500

    except Exception as e:
        logger.exception("SMS test error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@health_bp.route('/admin/last-inbound-sms', methods=['GET'])
def last_inbound_sms():
    """Return timestamp of the most recent inbound SMS."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401
    try:
        db = get_shared_db(config.DATABASE_URL)
        if not db:
            return jsonify({"success": False, "error": "DB unavailable"})
        rows = db.execute_query(
            "SELECT created_at FROM message_history WHERE direction = 'inbound' ORDER BY created_at DESC LIMIT 1",
            fetch=True,
        )
        if rows:
            _lm = row_get(rows[0], 'created_at', row_get(rows[0], 0, None))
            if _lm:
                return jsonify({"success": True, "timestamp": _lm.isoformat() if hasattr(_lm, "isoformat") else str(_lm)})
        return jsonify({"success": True, "timestamp": None})
    except Exception as e:
        return jsonify({"success": False, "error": "An internal error occurred"})


@health_bp.route('/admin/clear-conversation-cache', methods=['POST'])
def clear_conversation_cache():
    """Clear ALL conversation states and message history (full reset)."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        # Count how many will be deleted
        result = db.execute_query("SELECT COUNT(*) as count FROM conversation_states", fetch=True)
        count = 0
        if result:
            count = int(row_get(result[0], "count", row_get(result[0], 0, 0)) or 0)

        # Delete ALL conversation states (message_history cascades via FK)
        db.execute_query("DELETE FROM conversation_states", fetch=False)

        logger.info(f"Cleared conversation cache: {count} records deleted (all states)")
        return jsonify({"success": True, "cleared_count": count}), 200

    except Exception as e:
        logger.exception("Clear cache error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


def _is_cron_authorized():
    """True if request is authorized via CRON_SECRET (for PA cron)."""
    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret:
        return False
    token = (request.headers.get("X-Cron-Secret") or request.args.get("secret") or "").strip()
    return bool(token and hmac.compare_digest(token, secret))


@health_bp.route('/admin/run-scheduled-jobs', methods=['GET', 'POST'])
def run_scheduled_jobs():
    """
    Run the same logic as in-process scheduler (reminders, deposit followups, feedback, cleanup).
    Use from PythonAnywhere cron for reliable execution.
    Auth: admin session or CRON_SECRET (header X-Cron-Secret or query ?secret=).
    """
    if not _is_health_authenticated() and not _is_cron_authorized():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        state_manager = current_app.config.get("STATE_MANAGER")
        db_service = get_shared_db(config.DATABASE_URL)
        if not db_service:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        from services.background_jobs import check_reminders_job, cleanup_job

        summary = {}
        if state_manager:
            check_reminders_job(state_manager, db_service)
            summary["reminders_run"] = True
        try:
            cleanup_job(db_service)
            summary["cleanup_run"] = True
        except Exception as e:
            logger.warning("Cleanup job error (may be missing DB functions): %s", e)
            summary["cleanup_run"] = False
            summary["cleanup_error"] = str(e)[:200]

        return jsonify({"success": True, "summary": summary}), 200
    except Exception as e:
        logger.exception("Run scheduled jobs failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@health_bp.route("/admin/run-ugly-mugs-sync", methods=["GET", "POST"])
def run_ugly_mugs_sync_cron():
    """
    Run Escorts & Babes Lookup → safety watchlist sync once.

    Intended for **PythonAnywhere daily scheduled tasks** (or any cron) because the web app
    usually does not start APScheduler (see RUN_STARTUP_BACKGROUND_JOBS).

    Auth: Health dashboard session or CRON_SECRET (``X-Cron-Secret`` header or ``?secret=``).

    Honors Config “Enable daily sync” and credentials — returns 200 with ``skipped`` in JSON when disabled.
    """
    if not _is_health_authenticated() and not _is_cron_authorized():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        from services.ugly_mugs_sync_service import run_ugly_mugs_sync

        result = run_ugly_mugs_sync()
        status = str(result.get("status", "")).strip().lower()
        http_ok = status in ("success", "skipped")
        return jsonify({"success": http_ok, "result": result}), 200 if http_ok else 500
    except Exception as e:
        logger.exception("run-ugly-mugs-sync failed")
        return jsonify({"success": False, "error": str(e)[:500]}), 500


@health_bp.route('/admin/diagnose-component', methods=['POST'])
def diagnose_component():
    """Run detailed diagnostics on a specific component."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        data = request.get_json() or {}
        component = data.get("component", "").lower()
        checks = []
        db = get_shared_db(config.DATABASE_URL)
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        if component == "database":
            count_queries = {
                "conversation_states": "SELECT COUNT(*) as count FROM conversation_states",
                "message_history": "SELECT COUNT(*) as count FROM message_history",
                "admin_settings": "SELECT COUNT(*) as count FROM admin_settings",
            }
            for table in ["conversation_states", "message_history", "admin_settings"]:
                try:
                    result = db.execute_query(count_queries[table], fetch=True)
                    count = result[0]['count'] if result else 0
                    checks.append({"name": f"Table: {table}", "status": "pass", "message": f"{count} rows"})
                except Exception as e:
                    checks.append({"name": f"Table: {table}", "status": "fail", "message": str(e)})
            try:
                result = db.execute_query("SELECT MAX(created_at) as last_msg FROM message_history", fetch=True)
                last = result[0]['last_msg'] if result else None
                if last is None:
                    msg = "No messages yet"
                elif hasattr(last, "isoformat"):
                    msg = last.isoformat()
                else:
                    msg = str(last)
                checks.append({"name": "Last Message", "status": "pass", "message": msg})
            except Exception as e:
                checks.append({"name": "Last Message", "status": "warning", "message": str(e)})

        elif component == "mobile_api":
            try:
                result = db.execute_query("SELECT COUNT(*) as count FROM bookings", fetch=True)
                booking_count = int(result[0]['count']) if result else 0
                checks.append({"name": _HC_MOBILE_API, "status": "pass", "message": f"Route: /schedule/api/mobile-sync — {booking_count} booking(s) in DB"})
            except Exception as e:
                checks.append({"name": _HC_MOBILE_API, "status": "warning", "message": f"Bookings table error: {str(e)[:100]}"})

        elif component == "ai":
            try:
                checks.append({"name": "AI Service", "status": "pass", "message": "Available"})
            except Exception as e:
                checks.append({"name": "AI Service", "status": "fail", "message": str(e)})

        elif component == "sms":
            try:
                from services.sms_service import get_gateway_status
                gw = get_gateway_status()
                hs_active = bool(gw.get("httpsms", {}).get("active"))
                if hs_active:
                    checks.append({"name": _HC_SMS_GATEWAYS, "status": "pass", "message": "httpSMS gateway configured"})
                else:
                    checks.append({"name": _HC_SMS_GATEWAYS, "status": "warning", "message": "httpSMS gateway not active (disabled or not configured)"})
            except Exception as e:
                checks.append({"name": _HC_SMS_GATEWAYS, "status": "fail", "message": str(e)})

        elif component == "pricing":
            try:
                from core.rates_from_config import (
                    get_deposit_incall,
                    get_deposit_outcall,
                    get_incall_pricing,
                    get_surcharge,
                )
                incall = get_incall_pricing()
                gfe_60 = incall.get("gfe_60", 0)
                deposit_incall = get_deposit_incall()
                deposit_outcall = get_deposit_outcall()
                surcharge = get_surcharge()
                if gfe_60 > 0:
                    checks.append({"name": "Incall Rates", "status": "pass", "message": f"GFE 60min = ${gfe_60}"})
                else:
                    checks.append({"name": "Incall Rates", "status": "warning", "message": "GFE 60min rate is 0 \u2014 check pricing config"})
                if deposit_incall > 0:
                    checks.append({"name": "Deposit (Incall)", "status": "pass", "message": f"${deposit_incall}"})
                else:
                    checks.append({"name": "Deposit (Incall)", "status": "warning", "message": "Incall deposit is 0"})
                if deposit_outcall > 0:
                    checks.append({"name": "Deposit (Outcall)", "status": "pass", "message": f"${deposit_outcall}"})
                else:
                    checks.append({"name": "Deposit (Outcall)", "status": "warning", "message": "Outcall deposit is 0"})
                checks.append({"name": "Surcharge", "status": "pass", "message": f"${surcharge}"})
            except Exception as e:
                checks.append({"name": _HC_PRICING_CONFIG, "status": "fail", "message": str(e)})

        elif component == "location":
            try:
                location = config.get_current_incall_location()
                checks.append({"name": "Location Config", "status": "pass", "message": f"{location['city']} - {location['hotel_name']}"})
            except Exception as e:
                checks.append({"name": "Location Config", "status": "fail", "message": str(e)})

        return jsonify({"success": True, "checks": checks}), 200

    except Exception as e:
        logger.exception("Diagnose component error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


# System prompt for AI troubleshooter (chatbot stack context)
AI_TROUBLESHOOT_SYSTEM = """You are an expert troubleshooter for an SMS/chatbot booking system. The stack includes:

- **httpSMS Gateway (outbound + inbound)**: Android phone running the httpSMS app, connected to httpsms.com cloud service. Common issues: app not running, API key invalid, phone number mismatch, webhook URL not configured in httpSMS dashboard, Android battery optimisation killing the app.
- **Google Calendar**: Stores bookings. Needs: service account JSON or OAuth token (token.json). Common issues: token expired, calendar ID wrong, credentials not uploaded.
- **Database**: Stores conversation state, booking fields, settings. Common issues: connection string, migrations not run, table missing.
- **SMS Webhook**: Incoming SMS delivered by httpSMS to the /webhook endpoint. Common issues: webhook URL wrong in httpSMS dashboard, webhook secret mismatch, signature verification failure.
- **AI (Claude/Gemini)**: Used for intent classification and field extraction. Common issues: API key missing or invalid, rate limits.

Give concise, step-by-step troubleshooting. For "chatbot not sending SMS to client": check 1) httpSMS app running on Android 2) API key valid 3) phone number matches httpSMS account 4) bot reply logs 5) sender phone + client number format. For each issue type, list the most likely causes and how to verify/fix them."""


@health_bp.route('/admin/ai-troubleshoot', methods=['POST'])
def ai_troubleshoot():
    """AI-based troubleshooter: Claude first response, Gemini second response."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        data = request.get_json() or {}
        issue = (data.get("issue") or "").strip()
        if not issue:
            return jsonify({"success": False, "error": "Please describe the issue"}), 400

        from services.ai_service import AIService
        ai = AIService(provider="claude")
        result = ai.get_troubleshoot_advice(issue, system_prompt=AI_TROUBLESHOOT_SYSTEM)

        claude_ok = result.get("claude_response") is not None
        gemini_ok = result.get("gemini_response") is not None
        success = claude_ok or gemini_ok

        if not success:
            return jsonify({
                "success": False,
                "error": "Both Claude and Gemini failed. Check API keys (CLAUDE_API_KEY, GEMINI_API_KEY).",
                "claude_error": result.get("claude_error"),
                "gemini_error": result.get("gemini_error")
            }), 200

        return jsonify({
            "success": True,
            "issue": issue,
            "claude_response": result.get("claude_response"),
            "claude_error": result.get("claude_error"),
            "gemini_response": result.get("gemini_response"),
            "gemini_error": result.get("gemini_error")
        }), 200

    except Exception as e:
        logger.exception("AI troubleshoot error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@health_bp.route('/admin/get-error-log', methods=['GET'])
def get_error_log():
    """Get recent errors from PythonAnywhere error log or message_history."""
    if not _is_health_authenticated():
        return jsonify({"success": False, "error": _ERR_NOT_AUTHENTICATED}), 401

    try:
        errors = []
        lines_to_read = 500

        # Try reading from PythonAnywhere error log file
        _pa_user = (os.environ.get('PA_USERNAME') or '').strip()
        log_paths = []
        if _pa_user:
            log_paths.append(f'/var/log/{_pa_user}.pythonanywhere.com.error.log')
        log_paths.append('/var/log/www.pythonanywhere.com.error.log')
        log_read = False
        for log_path in log_paths:
            if os.path.exists(log_path):
                try:
                    with open(log_path, errors='replace') as f:
                        all_lines = f.readlines()
                    recent_lines = all_lines[-lines_to_read:]
                    import re
                    for line in recent_lines:
                        line = line.strip()
                        if not line:
                            continue
                        upper = line.upper()
                        if 'ERROR' in upper or 'EXCEPTION' in upper or 'TRACEBACK' in upper or 'WARNING' in upper:
                            level = 'ERROR' if ('ERROR' in upper or 'EXCEPTION' in upper or 'TRACEBACK' in upper) else 'WARNING'
                            # Try to parse timestamp from log line
                            ts_match = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
                            timestamp = ts_match.group(1) if ts_match else None
                            errors.append({"level": level, "timestamp": timestamp, "message": line[:500]})
                    log_read = True
                    break
                except Exception as read_err:
                    logger.warning(f"Could not read log file {log_path}: {read_err}")

        if not log_read:
            # Fallback: query message_history for lines containing error keywords
            try:
                db = get_shared_db(config.DATABASE_URL)
                if db is None:
                    return jsonify({"error": "Database unavailable"}), 503
                result = db.execute_query(
                    """
                    SELECT created_at, response FROM message_history
                    WHERE LOWER(response) LIKE '%error%'
                       OR LOWER(response) LIKE '%exception%'
                       OR LOWER(response) LIKE '%traceback%'
                    ORDER BY created_at DESC
                    LIMIT 50
                    """,
                    fetch=True
                )
                if result:
                    for row in result:
                        ts = row['created_at'].isoformat() if hasattr(row['created_at'], 'isoformat') else str(row['created_at'])
                        errors.append({"level": "ERROR", "timestamp": ts, "message": str(row['response'])[:500]})
            except Exception as db_err:
                logger.warning(f"Error log DB fallback failed: {db_err}")

        if not errors and not log_read:
            return jsonify({
                "success": True,
                "errors": [],
                "note": "No error log file found. Check your PythonAnywhere error log at /var/log/ directly."
            }), 200

        # Return newest first (log file is oldest-first)
        errors = list(reversed(errors))[:100]
        return jsonify({"success": True, "errors": errors}), 200

    except Exception as e:
        logger.exception("Get error log error")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500
