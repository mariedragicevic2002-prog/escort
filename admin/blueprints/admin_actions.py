"""Admin action endpoints - POST handlers for admin dashboard."""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import date, datetime, timezone

from flask import Blueprint, jsonify, make_response, request
from psycopg2 import sql as psy_sql

from admin.auth import hash_password, require_auth, verify_password
from config import get_effective_escort_timezone
from core.settings_manager import get_setting, set_setting
from services.database_service import get_shared_db
from utils.log_sanitize import LOG_SUPPRESSED_FMT, sanitize_log_value

logger = logging.getLogger("escort_chatbot.admin.actions")

admin_actions_bp = Blueprint('admin_actions', __name__)

_NEXT_BOOKING_CACHE_TTL_SECONDS = 30
_NEXT_BOOKING_CACHE: dict[str, tuple[float, dict]] = {}
_NEXT_BOOKING_CACHE_LOCK = threading.Lock()


def _jsonify_nb_with_etag(payload):
    """Return JSON response with ETag; 304 if client already has it."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    etag = '"' + hashlib.md5(raw.encode()).hexdigest() + '"'
    if request.headers.get("If-None-Match") == etag:
        return make_response("", 304)
    resp = make_response(raw, 200)
    resp.headers["Content-Type"] = "application/json"
    resp.headers["ETag"] = etag
    return resp

_SAFE_IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_UTC_OFFSET = "+00:00"
_ERR_DB_UNAVAILABLE = "Database unavailable"
_ERR_INCORRECT_PASSWORD = "Incorrect password"
_ERR_NO_DATA = "No data provided"


def _get_database_url() -> str:
    return os.getenv('DATABASE_URL', '')


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _training_backup_dir() -> str:
    path = os.path.join(_project_root(), "backups", "training_data")
    os.makedirs(path, exist_ok=True)
    return path


def _table_exists(db, table_name: str) -> bool:
    try:
        rows = db.execute_query(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = %s
            ) AS exists
            """,
            (table_name,),
            fetch=True,
        ) or []
        from utils.row_utils import row_get
        return bool(rows and row_get(rows[0], 'exists', row_get(rows[0], 0, False)))
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return False


def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value


def _list_backups_by_prefix(prefix: str):
    bdir = _training_backup_dir()
    items = []
    for name in sorted(os.listdir(bdir), reverse=True):
        if not (name.startswith(prefix) and name.endswith(".json")):
            continue
        path = os.path.join(bdir, name)
        try:
            stat = os.stat(path)
            items.append({
                "filename": name,
                "size_kb": round(stat.st_size / 1024, 2),
                "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .isoformat()
                .replace(_UTC_OFFSET, "Z"),
            })
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            continue
    return items


@admin_actions_bp.route('/admin/backup-training-data', methods=['POST'])
@require_auth
def backup_training_data():
    """Create JSON backups for training examples and successful conversation states."""
    try:
        db = get_shared_db(_get_database_url())
        if not db:
            return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        bdir = _training_backup_dir()
        training_file = os.path.join(bdir, f"training_examples_{ts}.json")
        conv_file = os.path.join(bdir, f"conversation_history_{ts}.json")

        training_rows = []
        if _table_exists(db, "training_examples"):
            training_rows = db.execute_query(
                "SELECT * FROM training_examples ORDER BY created_at DESC",
                fetch=True
            ) or []

        conversation_rows = []
        if _table_exists(db, "conversation_states"):
            conversation_rows = db.execute_query(
                """
                SELECT phone_number, current_state, version, date, time, duration,
                       experience_type, incall_outcall, outcall_address, client_name,
                       confirmed_at, last_message_at
                FROM conversation_states
                WHERE current_state IN ('CONFIRMED', 'POST_BOOKING')
                ORDER BY last_message_at DESC
                """,
                fetch=True
            ) or []

        with open(training_file, "w", encoding="utf-8") as f:
            json.dump(
                {"exported_at": datetime.now(timezone.utc).isoformat().replace(_UTC_OFFSET, "Z"), "rows": _json_safe(training_rows)},
                f,
                ensure_ascii=False,
                indent=2,
            )
        with open(conv_file, "w", encoding="utf-8") as f:
            json.dump(
                {"exported_at": datetime.now(timezone.utc).isoformat().replace(_UTC_OFFSET, "Z"), "rows": _json_safe(conversation_rows)},
                f,
                ensure_ascii=False,
                indent=2,
            )

        return jsonify({
            "success": True,
            "backup_files": {
                "training_examples": os.path.basename(training_file),
                "conversation_history": os.path.basename(conv_file),
            },
            "row_counts": {
                "training_examples": len(training_rows),
                "conversation_history": len(conversation_rows),
            },
        })
    except Exception as e:
        logger.exception("backup_training_data failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@admin_actions_bp.route('/admin/list-backups', methods=['GET'])
@require_auth
def list_backups():
    """List available training-data backup files."""
    try:
        return jsonify({
            "success": True,
            "backups": {
                "training_examples": _list_backups_by_prefix("training_examples_"),
                "conversation_history": _list_backups_by_prefix("conversation_history_"),
            },
        })
    except Exception as e:
        logger.exception("list_backups failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


from flask import render_template
from services.database_service import get_shared_db

@admin_actions_bp.route('/admin/audit-log', methods=['GET'])
@require_auth
def audit_log():
    db = get_shared_db(_get_database_url())
    rows = []
    if db:
        try:
            rows = db.execute_query(
                "SELECT id, action, details, created_at FROM admin_audit_log ORDER BY created_at DESC LIMIT 20",
                fetch=True
            ) or []
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
    return render_template('admin_audit_log.html', audit_log=rows)

def _restore_training_examples(db, bdir, training_files):
    """Insert training examples from latest backup; returns count restored."""
    restored = 0
    if not (training_files and _table_exists(db, "training_examples")):
        return restored
    latest = os.path.join(bdir, training_files[0]["filename"])
    with open(latest, encoding="utf-8") as f:
        rows = (json.load(f) or {}).get("rows") or []
    for row in rows:
        if not isinstance(row, dict) or not row:
            continue
        cols = [k for k in row.keys() if k != "id" and _SAFE_IDENTIFIER_RE.fullmatch(str(k or ""))]
        if not cols:
            continue
        col_sql = psy_sql.SQL(", ").join(psy_sql.Identifier(c) for c in cols)
        placeholder_sql = psy_sql.SQL(", ").join(psy_sql.Placeholder() for _ in cols)
        sql = psy_sql.SQL(
            "INSERT INTO training_examples ({}) VALUES ({}) ON CONFLICT DO NOTHING"
        ).format(col_sql, placeholder_sql)
        try:
            db.execute_query(sql, tuple(row.get(c) for c in cols))
            restored += 1
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
    return restored


_CONV_UPSERT_COLS = [
    "phone_number", "current_state", "version", "date", "time", "duration",
    "experience_type", "incall_outcall", "outcall_address", "client_name",
    "confirmed_at", "last_message_at",
]
_CONV_UPSERT_SQL = """
    INSERT INTO conversation_states
    (phone_number, current_state, version, date, time, duration,
     experience_type, incall_outcall, outcall_address, client_name,
     confirmed_at, last_message_at)
    VALUES (%s, %s, COALESCE(%s, 1), %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (phone_number) DO UPDATE SET
        current_state = EXCLUDED.current_state,
        version = EXCLUDED.version,
        date = EXCLUDED.date,
        time = EXCLUDED.time,
        duration = EXCLUDED.duration,
        experience_type = EXCLUDED.experience_type,
        incall_outcall = EXCLUDED.incall_outcall,
        outcall_address = EXCLUDED.outcall_address,
        client_name = EXCLUDED.client_name,
        confirmed_at = EXCLUDED.confirmed_at,
        last_message_at = EXCLUDED.last_message_at
"""


def _restore_conversation_states(db, bdir, conv_files):
    """Upsert conversation states from latest backup; returns count restored."""
    restored = 0
    if not (conv_files and _table_exists(db, "conversation_states")):
        return restored
    latest = os.path.join(bdir, conv_files[0]["filename"])
    with open(latest, encoding="utf-8") as f:
        rows = (json.load(f) or {}).get("rows") or []
    for row in rows:
        if not isinstance(row, dict):
            continue
        phone = (row.get("phone_number") or "").strip()
        if not phone:
            continue
        try:
            db.execute_query(_CONV_UPSERT_SQL, tuple(row.get(c) for c in _CONV_UPSERT_COLS))
            restored += 1
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
    return restored


@admin_actions_bp.route('/admin/restore-training-data', methods=['POST'])
@require_auth
def restore_training_data():
    """Restore latest training-data backups into DB where applicable."""
    try:
        db = get_shared_db(_get_database_url())
        if not db:
            return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

        bdir = _training_backup_dir()
        training_files = _list_backups_by_prefix("training_examples_")
        conv_files = _list_backups_by_prefix("conversation_history_")
        if not training_files and not conv_files:
            return jsonify({"success": False, "error": "No backup files found"}), 404

        return jsonify({
            "success": True,
            "restored_counts": {
                "training_examples": _restore_training_examples(db, bdir, training_files),
                "conversation_history": _restore_conversation_states(db, bdir, conv_files),
            },
        })
    except Exception as e:
        logger.exception("restore_training_data failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


def _fetch_old_conversations(db, min_age_days: int, limit: int) -> list:
    return db.execute_query(
        """
        SELECT phone_number, current_state, confirmed_at, last_message_at
        FROM conversation_states
        WHERE current_state IN ('CONFIRMED', 'POST_BOOKING')
          AND COALESCE(last_message_at, confirmed_at, NOW()) <= NOW() - make_interval(days => %s)
        ORDER BY COALESCE(last_message_at, confirmed_at, NOW()) ASC
        LIMIT %s
        """,
        (min_age_days, limit),
        fetch=True,
    ) or []


def _fetch_message_history(db, phones: list) -> dict:
    """Return message history grouped by phone number."""
    if not (phones and _table_exists(db, "message_history")):
        return {}
    rows = db.execute_query(
        """
        SELECT phone_number, direction, message_body, created_at
        FROM message_history
        WHERE phone_number = ANY(%s)
        ORDER BY phone_number, created_at ASC
        """,
        (phones,),
        fetch=True,
    ) or []
    grouped: dict = {}
    for r in rows:
        p = r.get("phone_number")
        if p:
            grouped.setdefault(p, []).append({
                "direction": r.get("direction"),
                "message_body": r.get("message_body"),
                "created_at": _json_safe(r.get("created_at")),
            })
    return grouped


def _write_archive_py(archived_examples: list) -> str:
    """Write archived conversations to a dated .py file; return file path."""
    archive_dir = os.path.join(_project_root(), "training_archives")
    os.makedirs(archive_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    py_path = os.path.join(archive_dir, f"archived_conversations_{stamp}.py")
    with open(py_path, "w", encoding="utf-8") as f:
        f.write('"""Auto-generated archived conversations for AI training preservation."""\n\n')
        f.write("ARCHIVED_CONVERSATIONS = ")
        f.write(json.dumps(_json_safe(archived_examples), ensure_ascii=False, indent=2))
        f.write("\n")
    return py_path


@admin_actions_bp.route('/admin/archive-training-to-code', methods=['POST'])
@require_auth
def archive_training_to_code():
    """Archive successful older conversations into a Python file."""
    try:
        db = get_shared_db(_get_database_url())
        if not db:
            return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

        payload = request.get_json(silent=True) or {}
        min_age_days = max(0, int(payload.get("min_age_days", 7) or 7))
        limit = max(1, int(payload.get("limit", 50) or 50))
        remove_from_db = bool(payload.get("remove_from_db", False))

        if not _table_exists(db, "conversation_states"):
            return jsonify({"success": True, "archived_count": 0, "removed_count": 0,
                            "message": "No conversation_states table found."})

        rows = _fetch_old_conversations(db, min_age_days, limit)
        if not rows:
            return jsonify({"success": True, "archived_count": 0, "removed_count": 0,
                            "message": f"No successful conversations older than {min_age_days} days."})

        phones = [r.get("phone_number") for r in rows if r.get("phone_number")]
        grouped_history = _fetch_message_history(db, phones)
        archived_examples = [
            {
                "phone_number": r.get("phone_number"),
                "current_state": r.get("current_state"),
                "confirmed_at": _json_safe(r.get("confirmed_at")),
                "last_message_at": _json_safe(r.get("last_message_at")),
                "messages": grouped_history.get(r.get("phone_number"), []),
            }
            for r in rows
        ]

        py_path = _write_archive_py(archived_examples)

        removed_count = 0
        if remove_from_db and phones:
            try:
                db.execute_query("DELETE FROM conversation_states WHERE phone_number = ANY(%s)", (phones,))
                removed_count = len(phones)
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)

        return jsonify({
            "success": True,
            "archived_count": len(archived_examples),
            "removed_count": removed_count,
            "archive_file": os.path.basename(py_path),
        })
    except Exception as e:
        logger.exception("archive_training_to_code failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


# =============================================================================
# CHATBOT CONTROL
# =============================================================================

@admin_actions_bp.route('/admin/toggle-chatbot', methods=['POST'])
@require_auth
def toggle_chatbot():
    """Toggle chatbot on/off."""
    password = request.form.get('password', '')
    enable = request.form.get('enable', 'true') == 'true'

    if not verify_password(password):
        return jsonify({"success": False, "error": _ERR_INCORRECT_PASSWORD}), 403

    set_setting("chatbot_enabled", "1" if enable else "0")

    return jsonify({
        "success": True,
        "message": f"Chatbot {'enabled' if enable else 'disabled'}",
        "enabled": enable
    })


# =============================================================================
# AVAILABLE HOURS
# =============================================================================

@admin_actions_bp.route('/admin/update-hours', methods=['POST'])
@require_auth
def update_hours():
    """Update available hours."""
    password = request.form.get('password', '')
    available_hours = request.form.get('available_hours', '').strip()

    if not verify_password(password):
        return jsonify({"success": False, "error": "Incorrect password"}), 403

    if not available_hours:
        return jsonify({"success": False, "error": "Available hours cannot be empty"}), 400

    set_setting("available_hours", available_hours)
    logger.info(f"Available hours updated to: {available_hours}")

    return jsonify({
        "success": True,
        "message": "Available hours updated successfully",
        "available_hours": available_hours
    })


# =============================================================================
# AI SETTINGS
# =============================================================================

@admin_actions_bp.route('/admin/save-ai-settings', methods=['POST'])
@require_auth
def save_ai_settings():
    """Save AI response settings."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": _ERR_NO_DATA})

        if "ai_provider" in data:
            provider = data.get("ai_provider", "claude")
            if provider in ("claude", "gemini", "random"):
                set_setting("ai_provider", provider)

        if "personality_tone" in data:
            set_setting("ai_personality_tone", str(data.get("personality_tone", 3)))

        if "response_length" in data:
            set_setting("ai_response_length", str(data.get("response_length", 3)))

        if "use_emojis" in data:
            set_setting("ai_use_emojis", "true" if data.get("use_emojis") else "false")

        if "max_chars" in data:
            set_setting("ai_max_chars", str(data.get("max_chars", 0)))

        if "personality_name" in data:
            set_setting("ai_personality_name", data.get("personality_name", "Flirty"))

        if "custom_personality" in data:
            set_setting("ai_custom_personality", data.get("custom_personality", ""))

        if "templates_first" in data:
            set_setting("ai_templates_first", "true" if data.get("templates_first") else "false")

        logger.info("AI settings updated successfully")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error saving AI settings")
        return jsonify({"success": False, "error": "An internal error occurred"})


@admin_actions_bp.route('/admin/save-greeting', methods=['POST'])
@require_auth
def save_greeting():
    """Save custom greeting message."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": _ERR_NO_DATA})

        greeting = str(data.get("greeting", ""))
        set_setting("custom_greeting", greeting)

        logger.info("Custom greeting updated")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error saving greeting")
        return jsonify({"success": False, "error": "An internal error occurred"})


@admin_actions_bp.route('/admin/save-blocked-phrases', methods=['POST'])
@require_auth
def save_blocked_phrases():
    """Save blocked phrases list."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": _ERR_NO_DATA})

        phrases = str(data.get("phrases", ""))
        set_setting("blocked_phrases", phrases)

        logger.info("Blocked phrases updated")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error saving blocked phrases")
        return jsonify({"success": False, "error": "An internal error occurred"})


@admin_actions_bp.route('/admin/save-profanity-words', methods=['POST'])
@require_auth
def save_profanity_words():
    """Save profanity word list."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": _ERR_NO_DATA})

        words = str(data.get("words", ""))
        set_setting("profanity_words", words)

        logger.info("Profanity word list updated")
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error saving profanity words")
        return jsonify({"success": False, "error": "An internal error occurred"})


# =============================================================================
# PAYMENT SETTINGS
# =============================================================================

@admin_actions_bp.route('/admin/update-payid', methods=['POST'])
@require_auth
def update_payid():
    """Update PayID (email or mobile number) and account name."""
    password = request.form.get('password', '')
    new_payid = request.form.get('new_payid', '').strip()
    account_name = request.form.get('account_name', '').strip()

    if not verify_password(password):
        return jsonify({"success": False, "error": _ERR_INCORRECT_PASSWORD}), 403

    if new_payid:
        set_setting("payid", new_payid)
        set_setting("payid_email", new_payid)

    if account_name:
        set_setting("account_name", account_name)

    logger.info(f"PayID updated to {new_payid}, Account name: {account_name}")

    return jsonify({
        "success": True,
        "message": "PayID settings updated",
        "payid": new_payid,
        "account_name": account_name
    })


@admin_actions_bp.route('/admin/update-deposit-settings', methods=['POST'])
@require_auth
def update_deposit_settings():
    """Update deposit settings (toggle and amounts)."""
    password = request.form.get('password', '')
    require_deposits = request.form.get('require_deposits', 'false')
    deposit_amount_incall = request.form.get('deposit_amount_incall', '50')
    deposit_amount_outcall = request.form.get('deposit_amount_outcall', '100')

    if not verify_password(password):
        return jsonify({"success": False, "error": _ERR_INCORRECT_PASSWORD}), 403

    try:
        incall_amount = int(deposit_amount_incall)
        outcall_amount = int(deposit_amount_outcall)
        if incall_amount < 0 or outcall_amount < 0:
            raise ValueError("Amounts must be positive")
    except ValueError:
        return jsonify({"success": False, "error": "Invalid deposit amounts"}), 400

    set_setting("require_deposits", require_deposits)
    set_setting("deposit_amount_incall", str(incall_amount))
    set_setting("deposit_amount_outcall", str(outcall_amount))
    # Also set deposit_incall/deposit_outcall so config page and bot logic see the same values
    set_setting("deposit_incall", str(incall_amount))
    set_setting("deposit_outcall", str(outcall_amount))

    logger.info(
        "Deposit settings updated: enabled=%s, incall=$%s, outcall=$%s",
        sanitize_log_value(require_deposits),
        sanitize_log_value(incall_amount),
        sanitize_log_value(outcall_amount),
    )

    return jsonify({
        "success": True,
        "message": "Deposit settings updated",
        "require_deposits": require_deposits == "true",
        "deposit_amount_incall": incall_amount,
        "deposit_amount_outcall": outcall_amount
    })


# =============================================================================
# PROFILE SETTINGS
# =============================================================================

@admin_actions_bp.route('/admin/update-profile-link', methods=['POST'])
@require_auth
def update_profile_link():
    """Update the escort's profile URL (no password required)."""
    profile_url = request.form.get('profile_url', '').strip()

    if not profile_url:
        return jsonify({"success": False, "error": "Profile URL cannot be empty"}), 400

    # Remove https:// or http:// if present
    profile_url = profile_url.replace("https://", "").replace("http://", "")
    if not set_setting("profile_url", profile_url):
        logger.error("update_profile_link: set_setting failed for profile_url")
        return jsonify({"success": False, "error": "Database save failed — check server logs and DATABASE_URL."}), 500

    logger.info("Profile URL updated to %s", profile_url)

    return jsonify({
        "success": True,
        "message": f"Profile link updated to {profile_url}",
        "profile_url": profile_url
    })


# =============================================================================
# ADMIN PHONE MANAGEMENT
# =============================================================================

@admin_actions_bp.route('/admin/add-admin-phone', methods=['POST'])
@require_auth
def add_admin_phone():
    """Add a new admin phone number."""
    password = request.form.get('password', '')
    new_phone = request.form.get('phone_number', '').strip()

    if not verify_password(password):
        return jsonify({"success": False, "error": _ERR_INCORRECT_PASSWORD}), 403

    # Normalize phone number
    new_phone = "".join(ch for ch in new_phone if ch.isdigit() or ch == "+")

    if new_phone.startswith("04") and len(new_phone) == 10:
        new_phone = "+61" + new_phone[1:]
    elif new_phone.startswith("4") and len(new_phone) == 9:
        new_phone = "+61" + new_phone

    if not (new_phone.startswith("+61") and len(new_phone) == 12):
        return jsonify({"success": False, "error": "Invalid Australian phone number"}), 400

    db = get_shared_db(_get_database_url())
    if db is None:
        return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

    # Check if already exists
    existing = db.execute_query(
        "SELECT phone_number FROM admin_phones WHERE phone_number = %s",
        (new_phone,), fetch=True
    )

    if existing:
        return jsonify({"success": False, "error": "Phone number already exists"}), 400

    # Add to database
    db.execute_query(
        "INSERT INTO admin_phones (phone_number, created_at) VALUES (%s, NOW())",
        (new_phone,)
    )

    logger.info(f"Admin phone added: {new_phone}")
    return jsonify({"success": True, "message": "Admin phone number added"})


@admin_actions_bp.route('/admin/remove-admin-phone', methods=['POST'])
@require_auth
def remove_admin_phone():
    """Remove an admin phone number."""
    password = request.form.get('password', '')
    phone_to_remove = request.form.get('phone_number', '').strip()

    if not verify_password(password):
        return jsonify({"success": False, "error": _ERR_INCORRECT_PASSWORD}), 403

    db = get_shared_db(_get_database_url())
    if db is None:
        return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

    # Delete from database
    db.execute_query(
        "DELETE FROM admin_phones WHERE phone_number = %s",
        (phone_to_remove,)
    )

    logger.info(f"Admin phone removed: {phone_to_remove}")
    return jsonify({"success": True, "message": "Admin phone number removed"})


# =============================================================================
# SECURITY SETTINGS
# =============================================================================

@admin_actions_bp.route('/admin/change-password', methods=['POST'])
@require_auth
def change_password():
    """Change admin password."""
    try:
        # Accept both JSON and form data
        if request.is_json:
            data = request.get_json()
        else:
            data = request.form

        current_password = data.get("current_password", "").strip()
        new_password = data.get("new_password", "").strip()

        if not verify_password(current_password):
            return jsonify({"success": False, "error": "Current password is incorrect"})

        if not new_password or len(new_password) < 8:
            return jsonify({"success": False, "error": "Password must be at least 8 characters"})

        # Store argon2id hash in database (falls back to werkzeug pbkdf2 if argon2-cffi not installed)
        set_setting("admin_password_hash", hash_password(new_password))

        logger.info("Admin password changed successfully")
        return jsonify({"success": True, "message": "Password changed successfully"})

    except Exception as e:
        logger.exception("Error changing password")
        return jsonify({"success": False, "error": "An internal error occurred"})




# =============================================================================
# EXPORT SETTINGS (backup)
# =============================================================================

_SENSITIVE_SETTING_KEYS = frozenset({
    "claude_api_key", "gemini_api_key",
    "admin_password", "admin_password_hash",
})


@admin_actions_bp.route('/admin/export-settings', methods=['GET'])
@require_auth
def export_settings():
    """
    Export admin_settings as JSON for backup. Sensitive keys are redacted (value replaced with '[REDACTED]').
    """
    try:
        from core.settings_manager import get_all_settings
        settings = get_all_settings()
        out = {}
        for k, v in (settings or {}).items():
            out[k] = "[REDACTED]" if k in _SENSITIVE_SETTING_KEYS else (v or "")
        db = get_shared_db(_get_database_url())
        counts = {}
        if db:
            try:
                r = db.execute_query("SELECT COUNT(*) as c FROM conversation_states", fetch=True)
                counts["conversation_states"] = r[0]["c"] if r else 0
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
        return jsonify({
            "success": True,
            "exported_at": datetime.now(timezone.utc).isoformat().replace(_UTC_OFFSET, "Z"),
            "settings": out,
            "counts": counts,
        })
    except Exception as e:
        logger.exception("Export settings failed")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


# =============================================================================
# ACTIVITY LOGS
# =============================================================================

@admin_actions_bp.route('/admin/activity-logs', methods=['GET'])
@require_auth
def get_activity_logs():
    """Get admin activity logs."""
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))

        db = get_shared_db(_get_database_url())
        if db is None:
            return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

        logs = db.execute_query(
            """
            SELECT action, details, success, created_at as timestamp
            FROM admin_activity_log
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s
            """,
            (limit, offset),
            fetch=True
        ) or []

        formatted_logs = []
        for log in logs:
            timestamp = log.get("timestamp")
            formatted_logs.append({
                "timestamp": timestamp.strftime("%d/%m/%Y %H:%M") if timestamp else "",
                "action": log.get("action", "").replace("_", " ").title(),
                "details": log.get("details", "")[:100] if log.get("details") else "",
                "success": log.get("success", True)
            })

        return jsonify({
            "success": True,
            "logs": formatted_logs
        })
    except Exception as e:
        logger.exception("Error fetching activity logs")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


# =============================================================================
# DEPLOY (unzip + pip + reload) - called by upload script, no browser bash needed
# =============================================================================

def _unzip_deploy(project_root: str, zip_path: str, zip_name: str) -> list:
    """Unzip, remove archive; return steps list. Raises on failure."""
    import subprocess
    r = subprocess.run(['unzip', '-o', zip_name], cwd=project_root,
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "unzip failed")
    os.remove(zip_path)
    return ["unzip", "rm_zip"]


def _pip_install(project_root: str, pip_exe: str) -> list:
    """Run pip install -r requirements.txt; return steps. Raises on failure."""
    if not os.path.isfile(pip_exe):
        return []
    import subprocess
    r = subprocess.run([pip_exe, 'install', '-r', 'requirements.txt'], cwd=project_root,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "pip failed")
    return ["pip_install"]


def _pa_reload(pa_token: str, pa_user: str, domain: str) -> list:
    """Trigger PythonAnywhere webapp reload; return steps."""
    if not (pa_token and domain):
        return []
    try:
        import requests as req
        reload_url = f"https://www.pythonanywhere.com/api/v0/user/{pa_user}/webapps/{domain}/reload/"
        resp = req.post(reload_url, headers={'Authorization': f'Token {pa_token}'}, timeout=30)
        if resp.status_code != 200:
            return ["reload_warn"]
        return ["reload"]
    except Exception as e:
        logger.warning(f"Reload API call failed: {e}")
        return ["reload_error"]


@admin_actions_bp.route('/admin/api/mobile-api-key', methods=['GET'])
def api_mobile_api_key():
    """Return (or generate) the schedule_api_key the mobile APK uses for Bearer auth.

    Requires an active admin or schedule session cookie. Pass ?regenerate=1 to mint a fresh key.
    """
    from flask import session
    from core.settings_manager import get_setting

    if not (session.get('admin_authenticated') or session.get('schedule_authenticated')):
        return jsonify({"error": "unauthorized"}), 401

    regenerate = request.args.get('regenerate') in ('1', 'true', 'yes')
    existing = get_setting('schedule_api_key')
    if existing and not regenerate:
        return jsonify({"key": existing, "created": False})

    new_key = secrets.token_urlsafe(40)
    ok = set_setting('schedule_api_key', new_key)
    if not ok:
        return jsonify({"error": "Failed to persist key"}), 500
    return jsonify({"key": new_key, "created": True})


@admin_actions_bp.route('/admin/api/next-booking', methods=['GET'])
def api_next_booking():
    """Return the next upcoming real booking for the header widget."""
    from datetime import timedelta
    from flask import session
    import pytz

    any_auth = any([
        session.get('admin_authenticated'),
        session.get('schedule_authenticated'),
        session.get('stats_authenticated'),
        session.get('database_authenticated'),
        session.get('health_authenticated'),
        session.get('config_authenticated'),
        session.get('location_authenticated'),
        session.get('rates_authenticated'),
    ])
    if not any_auth:
        return jsonify({"error": "unauthorized"}), 401

    tz_name = get_effective_escort_timezone()
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone('Australia/Adelaide')
        tz_name = 'Australia/Adelaide'

    now_monotonic = time.monotonic()
    with _NEXT_BOOKING_CACHE_LOCK:
        cached = _NEXT_BOOKING_CACHE.get(tz_name)
        if cached and (now_monotonic - cached[0]) < _NEXT_BOOKING_CACHE_TTL_SECONDS:
            return _jsonify_nb_with_etag(cached[1])

    now = datetime.now(tz)

    try:
        import config as _cfg
        from services.database_service import get_shared_db
        from utils.row_utils import row_get

        db = get_shared_db(_cfg.DATABASE_URL)
        if not db:
            payload = {"next": None, "timezone": tz_name}
            with _NEXT_BOOKING_CACHE_LOCK:
                _NEXT_BOOKING_CACHE[tz_name] = (time.monotonic(), payload)
            return _jsonify_nb_with_etag(payload)

        rows = db.execute_query(
            """
            SELECT id, start_time, end_time, client_name, phone, type,
                   experience, duration, status, outcall_address
            FROM bookings
            WHERE start_time > %s
              AND start_time <= %s
              AND status NOT IN ('cancelled', 'pending-travel')
              AND COALESCE(type, '') NOT IN ('travel', 'admin', 'social')
            ORDER BY start_time ASC
            LIMIT 5
            """,
            (now.isoformat(), (now + timedelta(days=14)).isoformat()),
            fetch=True,
        ) or []

        booking_row = None
        for row in rows:
            status_class = (row_get(row, "status") or "").strip().lower()
            if status_class in ("cancelled", "pending-travel"):
                continue
            booking_row = row
            break

        if not booking_row:
            payload = {"next": None, "timezone": tz_name}
            with _NEXT_BOOKING_CACHE_LOCK:
                _NEXT_BOOKING_CACHE[tz_name] = (time.monotonic(), payload)
            return _jsonify_nb_with_etag(payload)

        start_dt = row_get(booking_row, "start_time")
        end_dt = row_get(booking_row, "end_time")
        try:
            start_local = start_dt.astimezone(tz) if getattr(start_dt, "tzinfo", None) else tz.localize(start_dt)
            end_local = end_dt.astimezone(tz) if getattr(end_dt, "tzinfo", None) else tz.localize(end_dt)
        except Exception:
            start_local = start_dt
            end_local = end_dt

        loc_type = (row_get(booking_row, "type") or "").strip().lower() or "incall"
        if loc_type not in ("incall", "outcall"):
            loc_type = "incall"

        payload = {
            "next": {
                "start_iso": start_local.isoformat(),
                "end_iso": end_local.isoformat(),
                "client_name": (row_get(booking_row, "client_name") or "Client"),
                "phone": (row_get(booking_row, "phone") or ""),
                "location_type": loc_type,
                "address": (row_get(booking_row, "outcall_address") or ""),
                "experience": str(row_get(booking_row, "experience") or "").replace("_", " "),
                "duration": str(row_get(booking_row, "duration") or ""),
                "status": (row_get(booking_row, "status") or "reserved"),
            },
            "timezone": tz_name,
        }
        with _NEXT_BOOKING_CACHE_LOCK:
            _NEXT_BOOKING_CACHE[tz_name] = (time.monotonic(), payload)
        return _jsonify_nb_with_etag(payload)

    except Exception as e:
        logger.warning("api_next_booking error: %s", e)
        return _jsonify_nb_with_etag({"next": None, "timezone": tz_name})


@admin_actions_bp.route('/admin/api/redis-test', methods=['GET'])
def api_redis_test():
    """Quick Redis connectivity check — no auth required (returns no secrets)."""
    try:
        from config import get_redis_url, get_escort_phone_number, get_httpsms_phone_number
        import redis as _redis

        def _mask(p):
            digits = "".join(c for c in (p or "") if c.isdigit())
            if len(digits) >= 4:
                return "****" + digits[-4:]
            return "****"

        escort_phone = get_escort_phone_number()
        httpsms_phone = get_httpsms_phone_number()

        url = get_redis_url()
        if not url:
            return jsonify({"status": "not_configured", "message": "No Redis URL found",
                            "escort_phone_masked": _mask(escort_phone),
                            "httpsms_phone_masked": _mask(httpsms_phone)})
        masked = url[:15] + '...' + url[-15:] if len(url) > 30 else "***"
        test_url = url
        if "upstash.io" in url and url.startswith("redis://"):
            test_url = "rediss://" + url[len("redis://"):]
        r = _redis.from_url(test_url, socket_connect_timeout=4, socket_timeout=4)
        pong = r.ping()
        r.set('_copilot_test', 'ok', ex=30)
        val = r.get('_copilot_test')
        r.delete('_copilot_test')
        same_phone = escort_phone and httpsms_phone and (escort_phone.strip() == httpsms_phone.strip())
        return jsonify({
            "status": "ok",
            "ping": bool(pong),
            "set_get": val in (b'ok', 'ok'),
            "url_preview": masked,
            "protocol_used": "rediss://" if test_url.startswith("rediss://") else "redis://",
            "escort_phone_masked": _mask(escort_phone),
            "httpsms_phone_masked": _mask(httpsms_phone),
            "phones_are_same": same_phone,
            "warning": "escort_phone and httpsms_phone are the same number — notifications may loop to the sending phone" if same_phone else None
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@admin_actions_bp.route('/admin/sms-history', methods=['GET'])
@require_auth
def sms_history():
    """Return SMS message history for a given phone number."""
    phone = (request.args.get('phone') or '').strip()
    if not phone:
        return jsonify({"success": False, "error": "phone parameter required"}), 400

    db = get_shared_db(_get_database_url())
    if not db:
        return jsonify({"success": False, "error": _ERR_DB_UNAVAILABLE}), 503

    try:
        rows = db.execute_query(
            """
            SELECT direction, message_body, created_at
            FROM message_history
            WHERE phone_number = %s
            ORDER BY created_at ASC
            """,
            (phone,),
            fetch=True,
        ) or []
        messages = [
            {
                "direction": r.get("direction"),
                "body": r.get("message_body") or "",
                "ts": _json_safe(r.get("created_at")),
            }
            for r in rows
        ]
        return jsonify({"success": True, "phone": phone, "messages": messages, "timezone": get_effective_escort_timezone()})
    except Exception as e:
        logger.exception("sms_history failed for %s", sanitize_log_value(phone))
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@admin_actions_bp.route('/admin/deploy', methods=['POST'])
def deploy():
    """
    Unpack newbot-deploy.zip, run pip install -r requirements.txt, then reload webapp via PA API.
    Secured by DEPLOY_SECRET (header X-Deploy-Secret or body deploy_secret).

    **Upload from your PC (one step):** POST ``multipart/form-data`` with field ``file`` (or ``zip``)
    containing the zip — it is saved to ``newbot-deploy.zip`` then unpacked.

    **Legacy:** upload ``newbot-deploy.zip`` via PythonAnywhere Files tab, then POST with secret only.

    On PythonAnywhere set: DEPLOY_SECRET, PYTHONANYWHERE_API_TOKEN, PYTHONANYWHERE_USER, WEBAPP_DOMAIN, VIRTUALENV_PATH (optional).
    """
    secret = os.environ.get('DEPLOY_SECRET', '').strip()
    if not secret:
        return jsonify({"success": False, "error": "Deploy not configured (DEPLOY_SECRET)"}), 503
    provided = (request.headers.get('X-Deploy-Secret') or request.form.get('deploy_secret') or (request.get_json() or {}).get('deploy_secret') or '').strip()
    if not provided or not hmac.compare_digest(provided, secret):
        return jsonify({"success": False, "error": "Invalid deploy secret"}), 403

    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    zip_name = "newbot-deploy.zip"
    zip_path = os.path.join(project_root, zip_name)
    venv_path = os.environ.get('VIRTUALENV_PATH') or os.path.join(project_root, 'venv')
    pip_exe = os.path.join(venv_path, 'bin', 'pip')
    if not os.path.isfile(pip_exe):
        pip_exe = os.path.join(venv_path, 'Scripts', 'pip.exe')

    steps: list = []
    try:
        upload = request.files.get("file") or request.files.get("zip")
        if upload and getattr(upload, "filename", None):
            upload.save(zip_path)
            steps.append("saved_upload")
        if not os.path.isfile(zip_path):
            return jsonify({
                "success": False,
                "error": "No zip: POST multipart field 'file' with newbot-deploy.zip, or upload newbot-deploy.zip to the project folder first.",
                "steps": steps,
            }), 400
        steps += _unzip_deploy(project_root, zip_path, zip_name)
        steps += _pip_install(project_root, pip_exe)
        pa_token = (
            os.environ.get('PYTHONANYWHERE_API_TOKEN', '').strip()
            or (get_setting('pa_token') or '').strip()
        )
        pa_user = (
            os.environ.get('PYTHONANYWHERE_USER', '').strip()
            or (get_setting('pythonanywhere_user') or '').strip()
            or os.environ.get('PA_USERNAME', '').strip()
        )
        domain = (
            os.environ.get('WEBAPP_DOMAIN', '').strip()
            or (get_setting('webapp_domain') or '').strip()
            or (pa_user and f"{pa_user}.pythonanywhere.com")
        )
        reload_steps = _pa_reload(pa_token, pa_user, domain)
        steps += reload_steps
        if "reload_warn" in reload_steps:
            return jsonify({"success": True, "message": "Unzip and pip OK; reload failed", "steps": steps}), 200
        return jsonify({"success": True, "message": "Deploy complete", "steps": steps}), 200
    except RuntimeError as e:
        return jsonify({"success": False, "error": str(e), "steps": steps}), 500
    except Exception as e:
        logger.exception("Deploy failed")
        return jsonify({"success": False, "error": "An internal error occurred", "steps": steps}), 500
