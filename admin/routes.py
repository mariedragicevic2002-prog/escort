"""Admin web interface routes - Main dashboard only."""

import json
import logging
import os
import secrets

from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for
from flask.typing import ResponseReturnValue

from admin.auth import begin_2fa_challenge, login_user, logout_user, verify_password
from admin import totp as admin_totp
from admin.rate_limiter import get_lockout_remaining, is_ip_locked_out, rate_limit_login
from config import BASE_DIR, get_available_hours, get_escort_phone_number
from core.settings_manager import get_setting

try:
    from services.database_service import get_shared_db_with_retry
except ImportError:
    from services.database_service import get_shared_db as get_shared_db_with_retry

logger = logging.getLogger("escort_chatbot.admin.routes")

admin_bp = Blueprint('admin', __name__, template_folder='templates')


def _is_admin_or_config_authenticated() -> bool:
    """
    True when the user is logged into the main admin dashboard OR the /config area.
    Config uses a separate session flag (config_authenticated) so 2FA setup must accept both.
    """
    return bool(session.get("admin_authenticated") or session.get("config_authenticated"))


def _ensure_session_csrf() -> None:
    """
    Ensure a CSRF token exists and the session cookie is saved (critical for standalone
    2FA setup pages that POST back to /admin/* — avoids missing/invalid CSRF after long
    pauses or clients that drop hidden fields).
    """
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_hex(32)
    session.permanent = True
    session.modified = True


def _setting_bool(setting_key: str, default: bool = False) -> bool:
    raw = get_setting(setting_key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("true", "1", "yes")


def _setting_float(setting_key: str, default: float) -> float:
    raw = get_setting(setting_key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _admin_dash_defaults() -> dict:
    return {
        "authenticated": False,
        "error": None,
        "ai_settings": {},
        "chatbot_enabled": False,
        "current_payid": "",
        "current_account_name": "",
        "booking_mode": "incall_outcall",
        "require_deposits": True,
        "require_incall_deposits": True,
        "deposit_amount_incall": 50,
        "deposit_amount_outcall": 100,
        "profile_url": "",
        "admin_phones": [],
        "available_hours": "",
        "escort_name": "",
        "escort_phone": "",
        "deposit_group": 200,
        "deposit_dinner_date_outcall": 100,
        "deposit_extended_experience_outcall": 200,
        "deposit_incall_scale_duration": False,
        "deposit_incall_base_hours": 1.0,
        "deposit_outcall_scale_duration": False,
        "deposit_outcall_base_hours": 1.0,
        "deposit_group_scale_duration": False,
        "deposit_group_base_hours": 2.0,
        "deposit_dinner_date_outcall_scale_duration": False,
        "deposit_dinner_date_outcall_base_hours": 2.0,
        "deposit_extended_experience_outcall_scale_duration": False,
        "deposit_extended_experience_outcall_base_hours": 2.0,
        "deposit_verification_vision": True,
        "profanity_deposit_enabled": True,
        "blocked_words_block_enabled": True,
        "ugly_mugs_sync_enabled": False,
        "profanity_words_list": [],
        "profanity_words_text": "",
        "available_days": "",
        "outcall_verification_strict": False,
        "client_feedback_enabled": True,
        "incall_1h_reminder_enabled": True,
        "incall_reminder_forward_replies": False,
        "escort_sms_enabled": True,
        "run_startup_db_migrations": False,
        "conversation_timeout_hours": "24",
        "base_url": "",
        "ESCORT_SMS_CATEGORIES": [],
        "escort_sms_categories": {},
    }


def _render_admin_dash(**overrides) -> str:
    context = _admin_dash_defaults()
    context.update(overrides)
    return render_template("admin_dash.html", **context)


def _get_client_ip() -> str:
    """Extract and normalize the real client IP from the request."""
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    if ip and ',' in ip:
        ip = ip.split(',')[0].strip()
    return ip or ''


def _handle_post_auth(authenticated: bool) -> tuple[bool, str | None, ResponseReturnValue | None]:
    """
    Handle POST-based login and 2FA challenge.

    Returns (authenticated, error_message, early_response).
    If early_response is not None, the caller should return it immediately.
    """
    if request.method != 'POST' or authenticated:
        return authenticated, None, None

    password = request.form.get('password')
    if not verify_password(password or ''):
        return False, 'Invalid password', None

    if not admin_totp.is_enabled():
        login_user()
        return True, None, None

    # Password OK but 2FA required.
    begin_2fa_challenge()
    if admin_totp.get_2fa_delivery() == "sms":
        ok, sms_err = admin_totp.issue_sms_login_code(record_resend_cooldown=False)
        if not ok:
            from admin.auth import clear_2fa_challenge
            clear_2fa_challenge()
            error = sms_err or "Could not send SMS code. Try again or use authenticator 2FA."
            logger.warning("SMS 2FA send failed after password OK: %s", sms_err)
            return False, error, _render_admin_dash(error=error)
    return False, None, redirect(url_for('admin.two_factor_verify'))


def _load_basic_dashboard_settings(db) -> dict:
    """Load chatbot, PayID, booking mode, deposit flags, and admin info."""
    chatbot_enabled = get_setting("chatbot_enabled")
    chatbot_enabled = chatbot_enabled != "0" if chatbot_enabled else True

    from core.rates_from_config import _load_pricing, get_default_pricing
    _pricing_defaults = get_default_pricing()
    _pricing_live = _load_pricing()

    deposit_amounts = _load_deposit_amounts(_pricing_live, _pricing_defaults)
    deposit_scale = _load_deposit_scale_settings()
    op_flags = _load_operational_flags()
    admin_phones = db.execute_query(
        "SELECT phone_number FROM admin_phones ORDER BY created_at DESC",
        fetch=True
    ) or []
    ESCORT_SMS_CATEGORIES, escort_sms_categories = _load_escort_sms_data()

    return {
        "chatbot_enabled": chatbot_enabled,
        "ai_settings": _load_ai_settings_data(),
        "current_payid": get_setting("payid") or get_setting("payid_email") or "",
        "current_account_name": get_setting("account_name") or "",
        "booking_mode": get_setting("booking_mode") or "incall_outcall",
        "require_deposits": _setting_bool("require_deposits", default=True),
        "require_incall_deposits": _setting_bool("require_incall_deposits", default=True),
        "deposit_amount_incall": deposit_amounts["deposit_amount_incall"],
        "deposit_amount_outcall": deposit_amounts["deposit_amount_outcall"],
        "deposit_group": deposit_amounts["deposit_group"],
        "deposit_dinner_date_outcall": deposit_amounts["deposit_dinner_date_outcall"],
        "deposit_extended_experience_outcall": deposit_amounts["deposit_extended_experience_outcall"],
        "profile_url": get_setting("profile_url") or "",
        "admin_phones": admin_phones,
        "available_hours": get_available_hours(),
        "escort_name": get_setting('escort_name', 'escort') or 'escort',
        "escort_phone": get_escort_phone_number(),
        "ESCORT_SMS_CATEGORIES": ESCORT_SMS_CATEGORIES,
        "escort_sms_categories": escort_sms_categories,
        **deposit_scale,
        **op_flags,
    }

_ESCORT_SMS_CATEGORIES = [
    ('deposit_validation_failed', 'Deposit validation failed'),
    ('outcall_notifications', 'Outcall notifications'),
    ('enquiry_forwarding', 'Enquiry forwarding (ENQUIRY <question>)'),
    ('refund_forwarding', 'Refund forwarding'),
    ('doubles_source_escort', 'Doubles booking — alert to source other escort (MFF/MMF)'),
    ('safety_screening', 'Safety screening match alerts'),
    ('special_bookings', 'Extended experience enquiry alerts (Overnight / Dirty Weekend / Fly Me To You)'),
    ('client_rating', 'Post-booking client rating (SMS link to feedback webform)'),
    ('feedback_replies', 'Post-booking feedback — SMS replies to escort (e.g. "Thank you" after 3 STAR / N Y N)'),
    ('incall_client_forwards', 'Incall — all texts to your phone (1h before start)'),
    ('deposit_followup', "Deposit follow-up reminder (sent to escort when client hasn't paid after 4h)"),
    ('prebooking_checkin', 'Pre-booking check-in (sent to escort ~2h before a confirmed booking starts)'),
]


def _load_ai_settings_data() -> dict:
    return {
        "ai_provider": get_setting("ai_provider") or "claude",
        "personality_tone": int(get_setting("ai_personality_tone") or 3),
        "response_length": int(get_setting("ai_response_length") or 3),
        "use_emojis": get_setting("ai_use_emojis") == "true",
        "max_chars": int(get_setting("ai_max_chars") or 0),
        "personality_name": get_setting("ai_personality_name") or "Flirty",
        "custom_personality": get_setting("ai_custom_personality") or "",
        "templates_first": get_setting("ai_templates_first") == "true",
        "custom_greeting": get_setting("custom_greeting") or "",
        "blocked_phrases": get_setting("blocked_phrases") or "",
    }


def _load_deposit_amounts(pricing_live: dict, pricing_defaults: dict) -> dict:
    """Load deposit amounts from settings, falling back to pricing config."""
    return {
        "deposit_amount_incall": int(
            get_setting("deposit_incall")
            or get_setting("deposit_amount_incall")
            or pricing_defaults.get("deposit_incall")
            or 50
        ),
        "deposit_amount_outcall": int(
            get_setting("deposit_outcall")
            or get_setting("deposit_amount_outcall")
            or pricing_defaults.get("deposit_outcall")
            or 100
        ),
        "deposit_group": int(get_setting('deposit_group') or pricing_defaults.get('deposit_mff_pair') or 200),
        "deposit_dinner_date_outcall": int(
            pricing_live.get('deposit_dinner_date_outcall', pricing_defaults.get('deposit_dinner_date_outcall', 100))
        ),
        "deposit_extended_experience_outcall": int(
            pricing_live.get(
                'deposit_extended_experience_outcall',
                pricing_defaults.get('deposit_extended_experience_outcall', 200),
            )
        ),
    }


def _load_deposit_scale_settings() -> dict:
    """Load all deposit duration-scaling settings."""
    return {
        "deposit_incall_scale_duration": _setting_bool('deposit_incall_scale_duration'),
        "deposit_incall_base_hours": _setting_float('deposit_incall_base_hours', 1.0),
        "deposit_outcall_scale_duration": _setting_bool('deposit_outcall_scale_duration'),
        "deposit_outcall_base_hours": _setting_float('deposit_outcall_base_hours', 1.0),
        "deposit_group_scale_duration": _setting_bool('deposit_group_scale_duration'),
        "deposit_group_base_hours": _setting_float('deposit_group_base_hours', 2.0),
        "deposit_dinner_date_outcall_scale_duration": _setting_bool('deposit_dinner_date_outcall_scale_duration'),
        "deposit_dinner_date_outcall_base_hours": _setting_float('deposit_dinner_date_outcall_base_hours', 2.0),
        "deposit_extended_experience_outcall_scale_duration": _setting_bool('deposit_extended_experience_outcall_scale_duration'),
        "deposit_extended_experience_outcall_base_hours": _setting_float('deposit_extended_experience_outcall_base_hours', 2.0),
    }


def _load_operational_flags() -> dict:
    """Load boolean operational toggles and miscellaneous settings."""
    from booking.deposit_handler import get_profanity_words
    profanity_words_list = get_profanity_words()
    return {
        "deposit_verification_vision": (get_setting('deposit_verification_vision') or 'true').lower() in ('true', '1', 'yes'),
        "profanity_deposit_enabled": (get_setting('profanity_deposit_enabled') or 'true').lower() in ('true', '1', 'yes'),
        "blocked_words_block_enabled": (get_setting('blocked_words_block_enabled') or 'true').lower() in ('true', '1', 'yes'),
        "profanity_words_list": profanity_words_list,
        "profanity_words_text": "\n".join(profanity_words_list),
        "available_days": get_setting('available_days', '7 days a week') or '7 days a week',
        "outcall_verification_strict": (get_setting('outcall_verification_strict', 'false') or 'false').lower() == 'true',
        "client_feedback_enabled": (get_setting('client_feedback_enabled') or 'true').lower() in ('true', '1', 'yes'),
        "incall_1h_reminder_enabled": (get_setting('incall_1h_reminder_enabled') or 'true').lower() in ('true', '1', 'yes'),
        "incall_reminder_forward_replies": (get_setting('incall_reminder_forward_replies') or 'false').lower() in ('true', '1', 'yes'),
        "escort_sms_enabled": (get_setting('escort_sms_enabled') or 'true').lower() in ('true', '1', 'yes'),
        "run_startup_db_migrations": (get_setting('run_startup_db_migrations') or 'false').lower() in ('true', '1', 'yes'),
        "conversation_timeout_hours": get_setting('conversation_timeout_hours') or '24',
        "base_url": (get_setting('base_url') or '').strip().rstrip('/'),
        "ugly_mugs_sync_enabled": (get_setting("ugly_mugs_sync_enabled") or "").strip().lower() in ("true", "1", "yes"),
    }


def _load_escort_sms_data() -> tuple:
    """Return (ESCORT_SMS_CATEGORIES list, escort_sms_categories enabled-dict)."""
    categories = {}
    for cat_key, _ in _ESCORT_SMS_CATEGORIES:
        raw = get_setting(f'escort_sms_{cat_key}')
        categories[cat_key] = (raw or 'true').strip().lower() in ('true', '1', 'yes')
    return _ESCORT_SMS_CATEGORIES, categories


@admin_bp.route('/admin', methods=['GET', 'POST'])
@rate_limit_login
def admin_dashboard() -> ResponseReturnValue:
    """Main admin dashboard with login and rate limiting."""
    authenticated = session.get('admin_authenticated', False)

    ip = _get_client_ip()
    if is_ip_locked_out(ip):
        remaining = get_lockout_remaining(ip)
        error = f'Too many failed attempts. Locked out for {remaining // 60} more minutes.'
        return _render_admin_dash(error=error)

    authenticated, error, early_resp = _handle_post_auth(authenticated)
    if early_resp is not None:
        return early_resp
    if not authenticated:
        return _render_admin_dash(error=error)

    db = get_shared_db_with_retry(os.getenv('DATABASE_URL', ''))
    if not db:
        flash(
            "Database not available: the app could not connect to Postgres. "
            "Set DATABASE_URL in your PythonAnywhere Web environment (or .env), use the host/port from the Databases tab, "
            "reload the web app, then open /healthcheck to verify the connection.",
            "error",
        )
        return _render_admin_dash(authenticated=True)

    return _render_admin_dash(authenticated=True, **_load_basic_dashboard_settings(db))


@admin_bp.route('/admin/logout')
def logout():
    """Logout admin."""
    logout_user()
    return redirect(url_for('admin.admin_dashboard'))


# ---------------------------------------------------------------------------
# 2FA routes
# ---------------------------------------------------------------------------

@admin_bp.route('/admin/2fa/verify', methods=['GET', 'POST'])
def two_factor_verify():
    """Second factor check. Reached when password is OK but pending_2fa is set."""
    if not session.get('pending_2fa') and not session.get('admin_authenticated'):
        return redirect(url_for('admin.admin_dashboard'))
    if session.get('admin_authenticated') and not session.get('pending_2fa'):
        return redirect(url_for('admin.admin_dashboard'))

    delivery = admin_totp.get_2fa_delivery()
    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        use_backup = request.form.get('use_backup') == '1'
        ok = False
        if use_backup:
            ok = admin_totp.verify_and_consume_backup_code(code)
        elif delivery == "sms":
            ok = admin_totp.verify_sms_login_code(code)
        else:
            ok = admin_totp.verify_totp(code)
        if ok:
            login_user()
            return redirect(url_for('admin.admin_dashboard'))
        logger.warning("Failed 2FA code attempt from %s", request.remote_addr)
        flash(
            "Invalid backup code. Try again or request a new SMS code."
            if use_backup
            else "Invalid code. Please try again.",
            "error",
        )
        if use_backup:
            return redirect(url_for("admin.two_factor_verify", backup="1"))
        return redirect(url_for("admin.two_factor_verify"))

    return render_template(
        'admin_2fa_verify.html',
        backup_remaining=admin_totp.backup_codes_remaining(),
        delivery=delivery,
        sms_hint=admin_totp.mask_phone_tail(admin_totp.get_sms_destination_phone()),
        can_resend_sms=admin_totp.can_resend_sms_login(),
    )


@admin_bp.route('/admin/2fa/setup', methods=['GET', 'POST'])
def two_factor_setup():
    """Enroll authenticator (TOTP) or SMS 2FA. Requires an existing authenticated session."""
    if not _is_admin_or_config_authenticated():
        return redirect(url_for('admin.admin_dashboard'))

    _ensure_session_csrf()

    flow = (request.args.get('flow') or request.form.get('flow') or '').strip().lower()
    if flow == "sms":
        return _two_factor_setup_sms()

    if not admin_totp.deps_available():
        flash("pyotp is not installed on the server — run `pip install -r requirements.txt`.", "error")
        return redirect(url_for('admin.admin_dashboard'))

    # Hold the candidate secret in the session so it survives the GET → POST round-trip.
    secret = session.get('pending_totp_secret')
    if not secret:
        secret = admin_totp.generate_new_secret()
        session['pending_totp_secret'] = secret

    if request.method == 'POST':
        code = (request.form.get('code') or '').strip()
        ok, codes = admin_totp.finalize_enrollment(secret, code)
        if ok:
            session.pop('pending_totp_secret', None)
            return render_template('admin_2fa_codes.html', backup_codes=codes, setup=True)
        flash("That code didn't verify. Try again — check your device time is in sync.", "error")

    uri = admin_totp.provisioning_uri(secret)
    qr_data_uri = admin_totp.qr_code_data_uri(uri) if admin_totp._QRCODE_AVAILABLE else ""
    return render_template(
        'admin_2fa_setup.html',
        secret=secret,
        qr_data_uri=qr_data_uri,
        manual_uri=uri,
        flow="totp",
        sms_ready=admin_totp.sms_2fa_ready(),
        default_sms_phone=admin_totp.get_sms_destination_phone(),
    )


def _two_factor_setup_sms():
    """SMS-based 2FA enrollment sub-flow."""
    if request.method == 'POST':
        action = (request.form.get('action') or "").strip().lower()
        if action == "send":
            phone = (request.form.get('sms_phone') or "").strip()
            ok, err = admin_totp.issue_sms_enrollment_code(phone)
            if ok:
                flash("Code sent. Enter it below to confirm this number.", "info")
            else:
                flash(err or "Could not send SMS.", "error")
        else:
            code = (request.form.get('code') or "").strip()
            ok, codes = admin_totp.verify_sms_enrollment_code(code)
            if ok and codes:
                return render_template('admin_2fa_codes.html', backup_codes=codes, setup=True)
            flash("That code didn't match. Request a new code and try again.", "error")

    return render_template(
        'admin_2fa_setup_sms.html',
        default_phone=admin_totp.get_sms_destination_phone(),
        sms_gateway_ok=admin_totp.sms_gateway_is_configured(),
    )


@admin_bp.route('/admin/2fa/resend-sms', methods=['POST'])
def two_factor_resend_sms():
    """Resend login SMS while pending_2fa (rate limited in admin.totp)."""
    if not session.get('pending_2fa') or session.get('admin_authenticated'):
        return redirect(url_for('admin.admin_dashboard'))
    if admin_totp.get_2fa_delivery() != "sms":
        return redirect(url_for('admin.two_factor_verify'))

    if not admin_totp.can_resend_sms_login():
        flash("Please wait a minute before requesting another code.", "error")
        return redirect(url_for('admin.two_factor_verify'))

    ok, err = admin_totp.issue_sms_login_code()
    if ok:
        flash("A new code was sent.", "info")
    else:
        flash(err or "Could not resend SMS.", "error")
    return redirect(url_for('admin.two_factor_verify'))


@admin_bp.route('/admin/2fa/disable', methods=['POST'])
def two_factor_disable():
    """Disable 2FA. Requires password re-verification for safety."""
    if not _is_admin_or_config_authenticated():
        return redirect(url_for('admin.admin_dashboard'))
    password = request.form.get('password') or ''
    if not verify_password(password):
        flash("Password incorrect — 2FA was NOT disabled.", "error")
        return redirect(url_for('admin.admin_dashboard'))
    admin_totp.disable_2fa()
    flash("2FA disabled. Re-enable any time from the Security panel.", "info")
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/2fa/regenerate-codes', methods=['POST'])
def two_factor_regenerate_codes():
    """Regenerate backup codes (invalidates previous set). Shown once."""
    if not _is_admin_or_config_authenticated():
        return redirect(url_for('admin.admin_dashboard'))
    if not admin_totp.is_enabled():
        flash("2FA is not enabled.", "error")
        return redirect(url_for('admin.admin_dashboard'))
    codes = admin_totp.regenerate_backup_codes()
    return render_template('admin_2fa_codes.html', backup_codes=codes, setup=False)
