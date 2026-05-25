"""
admin/blueprints/mobile_api.py

RESTful booking API consumed by the mobile APK.
Auth: Bearer token validated against schedule_api_key in admin_settings
      (same key the APK already has configured for the schedule endpoints).

Routes:
  GET  /api/healthz
  GET  /api/bookings[?date=YYYY-MM-DD]
  POST /api/bookings
  GET  /api/bookings/<id>
  PATCH /api/bookings/<id>
  DELETE /api/bookings/<id>                  — silent delete, no SMS
  POST /api/bookings/<id>/reschedule         — conflict check + SMS + webform URL
  POST /api/bookings/<id>/cancel             — SMS notification + cleanup
  GET  /api/stats[?weeks=8]                  — server-side booking stats
  GET  /api/messages/<booking_id>
"""

import hmac
import logging
import time
import uuid
from collections import defaultdict
from threading import Lock

import config
from flask import Blueprint, jsonify, request
from services.database_service import get_shared_db
from services.push_notification_service import send_new_booking_push_for_booking_id
from utils.row_utils import row_get

logger = logging.getLogger("escort_chatbot.mobile_api")

# ---------------------------------------------------------------------------
# Per-IP / per-key rate limiter (in-process, thread-safe).
# 60 requests per 60-second sliding window per API key (or IP if unauthenticated).
# On multi-worker deploys swap for Redis-backed Flask-Limiter.
# ---------------------------------------------------------------------------
_RL_LIMIT = 60
_RL_WINDOW = 60  # seconds
_rl_lock = Lock()
_rl_windows: dict[str, list[float]] = defaultdict(list)


def _rl_key() -> str:
    """Rate-limit bucket: prefer API key over IP to track authenticated callers."""
    auth = (request.headers.get("Authorization") or request.headers.get("X-API-Key") or "").strip()
    return auth[:64] if auth else (request.remote_addr or "unknown")


def _check_rate_limit() -> bool:
    """Return True if the request is within the rate limit, False otherwise."""
    key = _rl_key()
    now = time.monotonic()
    cutoff = now - _RL_WINDOW
    with _rl_lock:
        ts = _rl_windows[key]
        # Evict timestamps outside the window
        while ts and ts[0] < cutoff:
            ts.pop(0)
        if len(ts) >= _RL_LIMIT:
            return False
        ts.append(now)
    return True


mobile_api_bp = Blueprint("mobile_api", __name__)


@mobile_api_bp.before_request
def _mobile_api_rate_limit():
    """Apply sliding-window rate limit to all mobile API routes."""
    if not _check_rate_limit():
        logger.warning("Rate limit exceeded for key %s", _rl_key()[:16])
        return jsonify({"error": "Rate limit exceeded. Try again later."}), 429


# ---------------------------------------------------------------------------
# Startup bootstrap — create table + seed test data if needed
# ---------------------------------------------------------------------------

def _bootstrap_bookings_db():
    """Create bookings table if not exists and seed 5 May 2026 test data once."""
    try:
        import uuid as _uuid
        db = _get_db()
        if db is None:
            return

        db.execute_query("""
            CREATE TABLE IF NOT EXISTS bookings (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                start_time TIMESTAMPTZ NOT NULL,
                end_time TIMESTAMPTZ NOT NULL,
                client_name VARCHAR(200) NOT NULL DEFAULT '',
                phone VARCHAR(20) NOT NULL DEFAULT '',
                duration VARCHAR(50) NOT NULL DEFAULT '',
                type VARCHAR(20) NOT NULL DEFAULT 'incall',
                experience VARCHAR(100) NOT NULL DEFAULT '',
                preferences TEXT[] DEFAULT '{}',
                deposit_status VARCHAR(30) DEFAULT 'not_required',
                deposit_amount NUMERIC(10,2) DEFAULT 0,
                deposit_reference VARCHAR(200) DEFAULT '',
                status VARCHAR(30) NOT NULL DEFAULT 'reserved',
                special_requests TEXT,
                organise_other_escort BOOLEAN DEFAULT FALSE,
                notes TEXT,
                price_total NUMERIC(10,2),
                remaining_amount NUMERIC(10,2),
                outcall_address TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        # Only seed if no bookings exist for 5 May 2026
        # MED-03: use runtime timezone config instead of hardcoded Adelaide offset
        tz_name = config.get_effective_escort_timezone() or "Australia/Adelaide"
        existing = db.execute_query(
            f"SELECT COUNT(*) FROM bookings WHERE DATE(start_time AT TIME ZONE %s) = '2026-05-05'",
            (tz_name,),
            fetch=True,
        )
        from utils.row_utils import row_get as _rg
        count = _rg(existing[0], 0) or _rg(existing[0], "count") if existing else 0
        if count and int(count) > 0:
            return  # already seeded

        # Seed data intentionally uses Adelaide time (+09:30) for test consistency,
        # regardless of configured timezone. The existence check above uses the
        # configured timezone to determine whether seeding has occurred.
        TZ = "+09:30"

        def _ts(hhmm):
            return f"2026-05-05T{hhmm}:00{TZ}"

        # Ensure conversation_states rows exist (FK requirement)
        for phone in ("0411443221", "0425443990", "0418330989"):
            db.execute_query(
                "INSERT INTO conversation_states (phone_number, current_state) VALUES (%s, 'NEW') ON CONFLICT (phone_number) DO NOTHING",
                (phone,),
            )

        bookings_data = [
            # Mike — 30min GFE incall, reserved
            (str(_uuid.uuid4()), _ts("10:00"), _ts("10:30"), "Mike", "0425443990",
             "30 minutes", "incall", "GFE", [], "pending", 100.0, "", "reserved",
             None, False, None, 300.0, 200.0, None),
            # Dave — 2hr PSE incall, reserved
            (str(_uuid.uuid4()), _ts("12:00"), _ts("14:00"), "Dave", "0418330989",
             "2 hours", "incall", "PSE", [], "pending", 300.0, "", "reserved",
             None, False, None, 700.0, 400.0, None),
            # Travel outbound (grape/confirmed travel)
            (str(_uuid.uuid4()), _ts("13:45"), _ts("14:10"), "Travel to Mark", "0411443221",
             "25 minutes", "travel", "Travel", [], "not_required", 0.0, "", "travel",
             None, False, "Outbound: 165 Grote St, Adelaide → 158 Raglan Ave, Pennington",
             None, None, "158 Raglan Avenue, Pennington SA 5013"),
            # Mark — 2hr PSE outcall, confirmed, deposit paid
            (str(_uuid.uuid4()), _ts("14:10"), _ts("16:10"), "Mark", "0411443221",
             "2 hours", "outcall", "PSE", [], "paid", 300.0, "REF2405MARK", "confirmed",
             None, False, "Client has a private room, no other occupants.",
             700.0, 400.0, "158 Raglan Avenue, Pennington SA 5013"),
            # Travel return (grape/confirmed travel)
            (str(_uuid.uuid4()), _ts("16:10"), _ts("16:35"), "Travel from Mark", "0411443221",
             "25 minutes", "travel", "Travel", [], "not_required", 0.0, "", "travel",
             None, False, "Return: 158 Raglan Ave, Pennington → 165 Grote St, Adelaide",
             None, None, "165 Grote Street, Adelaide SA 5000"),
        ]

        for b in bookings_data:
            db.execute_query(
                """
                INSERT INTO bookings (
                    id, start_time, end_time, client_name, phone, duration, type,
                    experience, preferences, deposit_status, deposit_amount,
                    deposit_reference, status, special_requests, organise_other_escort,
                    notes, price_total, remaining_amount, outcall_address
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
                """,
                b,
            )

        _seed_sms_messages(db, TZ)
        logger.info("Test bookings for 5 May 2026 seeded successfully")
    except Exception as e:
        logger.warning("_bootstrap_bookings_db error: %s", e)


def _seed_sms_messages(db, TZ):
    """Insert realistic SMS conversations for Mark, Mike, Dave."""
    def sms(phone, direction, body, dt):
        try:
            db.execute_query(
                "INSERT INTO message_history (phone_number, direction, message_body, created_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (phone, direction, body, dt + TZ),
            )
        except Exception:
            pass

    # Mike
    sms("0425443990", "inbound",  "Hey, are you available Tuesday?",                       "2026-05-03T18:42:00")
    sms("0425443990", "outbound", "Hi! Yes I have some availability Tuesday. What time suits you and what were you thinking? 😊", "2026-05-03T18:44:00")
    sms("0425443990", "inbound",  "Maybe around 10am? Just a half hour GFE incall",        "2026-05-03T18:46:00")
    sms("0425443990", "outbound", "That works perfectly 🙌 10–10:30am Tuesday for a 30min GFE incall. Can I grab your name?", "2026-05-03T18:47:00")
    sms("0425443990", "inbound",  "It's Mike",                                             "2026-05-03T18:48:00")
    sms("0425443990", "outbound", "Great to meet you Mike! A deposit of $100 secures your booking. PayID adella@example.com — reference: Mike 😊", "2026-05-03T18:49:00")
    sms("0425443990", "inbound",  "Ok I'll sort that tonight",                             "2026-05-03T18:51:00")
    sms("0425443990", "outbound", "No worries! Once received I'll confirm your booking ✅", "2026-05-03T18:52:00")
    sms("0425443990", "inbound",  "Haven't sent the deposit yet is that ok",               "2026-05-04T20:03:00")
    sms("0425443990", "outbound", "No problem Mike, just keep in mind the spot isn't locked in until the deposit is received 😊", "2026-05-04T20:05:00")

    # Dave
    sms("0418330989", "inbound",  "Hi is Tuesday available? 2hr PSE incall",               "2026-05-02T21:10:00")
    sms("0418330989", "outbound", "Hi there! Yes Tuesday works 😊 What time were you thinking?", "2026-05-02T21:13:00")
    sms("0418330989", "inbound",  "12pm would be great",                                   "2026-05-02T21:14:00")
    sms("0418330989", "outbound", "Perfect — 12:00–2:00pm Tuesday, 2hr PSE incall. Can I get your name?", "2026-05-02T21:15:00")
    sms("0418330989", "inbound",  "Dave",                                                  "2026-05-02T21:16:00")
    sms("0418330989", "outbound", "Lovely to meet you Dave! A $300 deposit locks in your booking. PayID adella@example.com — reference: Dave ✅", "2026-05-02T21:17:00")
    sms("0418330989", "inbound",  "Done, just sent it",                                    "2026-05-02T21:45:00")
    sms("0418330989", "outbound", "Thank you Dave! I can see it's on the way — I'll confirm as soon as it clears 😊", "2026-05-02T21:47:00")
    sms("0418330989", "inbound",  "Any update on deposit?",                                "2026-05-03T10:22:00")
    sms("0418330989", "outbound", "Hi Dave! It hasn't appeared yet — sometimes transfers take until the next business day. I'll message you the moment it lands 🙏", "2026-05-03T10:25:00")

    # Mark
    sms("0411443221", "inbound",  "Hey Adella, do you do outcalls?",                       "2026-05-01T14:30:00")
    sms("0411443221", "outbound", "Hi! Yes I do outcalls 😊 Where are you located?",       "2026-05-01T14:32:00")
    sms("0411443221", "inbound",  "Pennington, 158 Raglan Ave",                            "2026-05-01T14:33:00")
    sms("0411443221", "outbound", "That works! What date/time and for how long?",          "2026-05-01T14:35:00")
    sms("0411443221", "inbound",  "Tuesday 2pm, 2 hours, PSE",                             "2026-05-01T14:36:00")
    sms("0411443221", "outbound", "Sounds amazing 💋 Tuesday 5th May, 2:00–4:00pm, 2hr PSE outcall at 158 Raglan Ave, Pennington. Can I grab your name?", "2026-05-01T14:38:00")
    sms("0411443221", "inbound",  "Mark",                                                  "2026-05-01T14:39:00")
    sms("0411443221", "outbound", "Great to meet you Mark! A $300 deposit is needed to secure the booking. PayID adella@example.com — reference: Mark 😊", "2026-05-01T14:40:00")
    sms("0411443221", "inbound",  "Done! Sent $300 reference Mark",                        "2026-05-01T15:10:00")
    sms("0411443221", "outbound", "Just checked — received! Your booking is confirmed ✅🎉 I'll head to you around 2:10pm. See you Tuesday!", "2026-05-01T15:15:00")
    sms("0411443221", "inbound",  "Perfect! Looking forward to it",                        "2026-05-01T15:16:00")
    sms("0411443221", "outbound", "Me too 😘 My reference for your records: REF2405MARK",  "2026-05-01T15:17:00")
    sms("0411443221", "inbound",  "Still coming today right?",                             "2026-05-05T09:45:00")
    sms("0411443221", "outbound", "Absolutely! Can't wait 😊 I'll be there around 2:10pm. Still 158 Raglan Ave Pennington?", "2026-05-05T09:47:00")
    sms("0411443221", "inbound",  "Yep same address, see you then!",                       "2026-05-05T09:48:00")
    sms("0411443221", "outbound", "Perfect, on my way! ETA ~10 mins 😊",                   "2026-05-05T14:00:00")
    sms("0411443221", "inbound",  "Great, door's open",                                    "2026-05-05T14:02:00")

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_BOOKING_COLS = (
    "id", "start_time", "end_time", "client_name", "phone", "duration",
    "type", "experience", "preferences", "deposit_status", "deposit_amount",
    "deposit_reference", "status", "special_requests", "organise_other_escort",
    "notes", "price_total", "remaining_amount", "outcall_address",
)
_BOOKING_SELECT = ", ".join(_BOOKING_COLS)


def _is_authenticated() -> bool:
    """Validate Bearer token against schedule_api_key (same key APK uses elsewhere)."""
    try:
        from core.settings_manager import get_setting
        stored_key = (get_setting("schedule_api_key") or "").strip()
        if not stored_key:
            return False
        auth_header = (request.headers.get("Authorization") or "").strip()
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        else:
            provided = (request.headers.get("X-API-Key") or "").strip()
        # CRIT-02: timing-safe comparison prevents character-by-character enumeration attacks
        return bool(provided and hmac.compare_digest(provided, stored_key))
    except Exception as e:
        logger.warning("Auth check failed: %s", e)
        return False


def _unauth():
    return jsonify({"error": "Unauthorized"}), 401


def _get_db():
    return get_shared_db(config.DATABASE_URL)


# ---------------------------------------------------------------------------
# Row → dict serialiser
# ---------------------------------------------------------------------------

def _row_to_booking(row) -> dict:
    """Convert a DB row (dict or tuple) into the API Booking schema."""
    def g(k):
        return row_get(row, k)

    start = g("start_time")
    end = g("end_time")
    prefs = g("preferences") or []

    return {
        "id": str(g("id") or ""),
        "start_time": start.isoformat() if hasattr(start, "isoformat") else str(start or ""),
        "end_time": end.isoformat() if hasattr(end, "isoformat") else str(end or ""),
        "client_name": g("client_name") or "",
        "phone": g("phone") or "",
        "duration": g("duration") or "",
        "type": g("type") or "",
        "experience": g("experience") or "",
        "preferences": list(prefs) if prefs else [],
        "deposit_status": g("deposit_status") or "not_required",
        "deposit_amount": float(g("deposit_amount") or 0),
        "deposit_reference": g("deposit_reference") or "",
        "status": g("status") or "reserved",
        "special_requests": g("special_requests") or "",
        "organise_other_escort": bool(g("organise_other_escort")),
        "notes": g("notes") or "",
        "price_total": float(g("price_total")) if g("price_total") is not None else None,
        "remaining_amount": float(g("remaining_amount")) if g("remaining_amount") is not None else None,
    }


def _invalidate_mobile_sync_cache():
    """Clear the mobile-sync response cache so next APK pull is fresh."""
    try:
        from admin.blueprints.schedule.api_routes import _mobile_sync_cache_invalidate
        _mobile_sync_cache_invalidate()
    except Exception:
        pass  # non-fatal if cache module not available


def _fetch_booking(db, booking_id):
    rows = db.execute_query(
        f"SELECT {_BOOKING_SELECT} FROM bookings WHERE id = %s",
        (booking_id,),
        fetch=True,
    ) or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@mobile_api_bp.route("/api/healthz", methods=["GET"])
def api_healthz():
    """Health check — no auth required."""
    try:
        db = _get_db()
        if db is None:
            return jsonify({"status": "degraded", "error": "Database unavailable"}), 503
        db.execute_query("SELECT 1", fetch=True)
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        logger.error("healthz DB error: %s", e)
        return jsonify({"status": "degraded", "error": "Service temporarily unavailable"}), 503



@mobile_api_bp.route("/api/bookings", methods=["GET"])
def list_bookings():
    if not _is_authenticated():
        return _unauth()
    date_str = request.args.get("date")
    tz_name = config.get_effective_escort_timezone() or "Australia/Adelaide"
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        if date_str:
            rows = db.execute_query(
                f"""
                SELECT {_BOOKING_SELECT} FROM bookings
                WHERE DATE(start_time AT TIME ZONE %s) = %s
                ORDER BY start_time ASC
                """,
                (tz_name, date_str),
                fetch=True,
            ) or []
        else:
            rows = db.execute_query(
                f"""
                SELECT {_BOOKING_SELECT} FROM bookings
                ORDER BY start_time ASC
                LIMIT 500
                """,
                fetch=True,
            ) or []
        return jsonify([_row_to_booking(r) for r in rows])
    except Exception as e:
        logger.error("list_bookings error: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@mobile_api_bp.route("/api/bookings", methods=["POST"])
def create_booking():
    if not _is_authenticated():
        return _unauth()
    data = request.get_json(silent=True) or {}
    required = ["start_time", "end_time", "client_name", "phone", "duration", "type", "experience"]
    missing = [f for f in required if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing required fields: {', '.join(missing)}"}), 400
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        booking_id = str(uuid.uuid4())
        db.execute_query(
            """
            INSERT INTO bookings (
                id, start_time, end_time, client_name, phone, phone_number, duration, type,
                experience, preferences, deposit_status, deposit_amount,
                deposit_reference, status, special_requests, organise_other_escort,
                notes, outcall_address
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                booking_id,
                data["start_time"], data["end_time"],
                data["client_name"], data["phone"], data["phone"],
                data["duration"], data["type"],
                data["experience"],
                data.get("preferences", []),
                data.get("deposit_status", "not_required"),
                data.get("deposit_amount", 0),
                data.get("deposit_reference", ""),
                data.get("status", "reserved"),
                data.get("special_requests"),
                data.get("organise_other_escort", False),
                data.get("notes"),
                data.get("outcall_address"),
            ),
        )
        row = _fetch_booking(db, booking_id)
        if not row:
            return jsonify({"error": "Insert failed"}), 500

        try:
            sent = send_new_booking_push_for_booking_id(db, booking_id)
            if sent > 0:
                logger.info("Sent %s push notification(s) for new booking %s", sent, booking_id)
        except Exception as push_err:
            logger.warning("New-booking push notification failed for %s: %s", booking_id, push_err)

        return jsonify(_row_to_booking(row)), 201
    except Exception as e:
        logger.error("create_booking error: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        _invalidate_mobile_sync_cache()


@mobile_api_bp.route("/api/bookings/<booking_id>", methods=["GET"])
def get_booking(booking_id):
    if not _is_authenticated():
        return _unauth()
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        row = _fetch_booking(db, booking_id)
        if not row:
            return jsonify({"error": "Booking not found"}), 404
        return jsonify(_row_to_booking(row))
    except Exception as e:
        logger.error("get_booking error: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@mobile_api_bp.route("/api/bookings/<booking_id>", methods=["PATCH"])
def update_booking(booking_id):
    if not _is_authenticated():
        return _unauth()
    data = request.get_json(silent=True) or {}
    # MED-02: frozenset is immutable — adding fields here is a security boundary.
    # All keys are interpolated as SQL column names; they must only ever be valid
    # column identifiers from the bookings table. Never add user-supplied strings.
    allowed_fields: frozenset[str] = frozenset({
        "start_time", "end_time", "client_name", "phone", "duration", "type",
        "experience", "preferences", "deposit_status", "deposit_amount",
        "deposit_reference", "status", "special_requests", "organise_other_escort",
        "notes", "price_total", "remaining_amount", "outcall_address",
    })
    updates = {k: v for k, v in data.items() if k in allowed_fields}
    if not updates:
        return jsonify({"error": "No valid fields to update"}), 400
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        values = list(updates.values()) + [booking_id]
        db.execute_query(
            f"UPDATE bookings SET {set_clause}, updated_at = NOW() WHERE id = %s",
            tuple(values),
        )
        row = _fetch_booking(db, booking_id)
        if not row:
            return jsonify({"error": "Booking not found"}), 404

        return jsonify(_row_to_booking(row))
    except Exception as e:
        logger.error("update_booking error: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        _invalidate_mobile_sync_cache()


@mobile_api_bp.route("/api/bookings/<booking_id>", methods=["DELETE"])
def delete_booking(booking_id):
    if not _is_authenticated():
        return _unauth()
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        db.execute_query("DELETE FROM bookings WHERE id = %s", (booking_id,))
        return jsonify({"success": True})
    except Exception as e:
        logger.error("delete_booking error: %s", e)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        _invalidate_mobile_sync_cache()


_BLOCKING_STATUSES = ["confirmed", "reschedule-confirmed", "reserved", "travel", "admin", "social"]

_DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

_EXP_NORMALIZE = {
    "gfe": "GFE",
    "dgfe": "DGFE",
    "pse": "PSE",
    "dinner date": "Dinner Date",
    "dinner_date": "Dinner Date",
    "doubles mff": "Doubles MFF",
    "doubles_mff": "Doubles MFF",
    "doubles mmf": "Doubles MMF",
    "doubles_mmf": "Doubles MMF",
    "couples mff": "Couples MFF",
    "couples_mff": "Couples MFF",
    "couples mmf": "Couples MMF",
    "couples_mmf": "Couples MMF",
    "couples": "Couples",
}


def _normalize_experience(exp):
    key = (exp or "").strip().lower()
    return _EXP_NORMALIZE.get(key, exp.strip() if exp and exp.strip() else "Other")


def _parse_iso_dt(s, tz):
    """Parse an ISO 8601 string; attach tz if naive."""
    from datetime import datetime as _dt
    dt = _dt.fromisoformat(str(s).replace("Z", "+00:00"))
    if not dt.tzinfo:
        dt = tz.localize(dt)
    return dt


@mobile_api_bp.route("/api/bookings/<booking_id>/reschedule", methods=["POST"])
def reschedule_booking(booking_id):
    """Reschedule a booking: conflict check, DB update, SMS with webform URL."""
    if not _is_authenticated():
        return _unauth()
    data = request.get_json(silent=True) or {}
    start_time = data.get("start_time")
    end_time = data.get("end_time")
    if not start_time or not end_time:
        return jsonify({"error": "start_time and end_time required"}), 400
    try:
        import pytz
        from config import get_effective_escort_timezone, get_escort_name
        from services.sms_service import send_sms
        from core.webform_security import get_webform_url

        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        rows = db.execute_query(
            "SELECT id, start_time, end_time, client_name, phone FROM bookings WHERE id = %s",
            (booking_id,), fetch=True,
        ) or []
        if not rows:
            return jsonify({"error": "Booking not found"}), 404

        booking = rows[0]
        orig_start = row_get(booking, "start_time")
        client_name = row_get(booking, "client_name") or "there"
        phone = row_get(booking, "phone") or ""

        tz = pytz.timezone(get_effective_escort_timezone())
        try:
            new_start = _parse_iso_dt(start_time, tz)
            new_end = _parse_iso_dt(end_time, tz)
        except Exception:
            return jsonify({"error": "Invalid datetime format for start_time / end_time"}), 400

        # Conflict check (exclude the booking being rescheduled)
        conflicts = db.execute_query(
            """SELECT client_name FROM bookings
               WHERE id != %s AND status = ANY(%s)
               AND start_time < %s AND end_time > %s LIMIT 3""",
            (booking_id, _BLOCKING_STATUSES, new_end.isoformat(), new_start.isoformat()),
            fetch=True,
        ) or []
        if conflicts:
            names = [row_get(r, "client_name") or "another booking" for r in conflicts]
            return jsonify({"error": f"Time conflicts with: {', '.join(names)}"}), 409

        db.execute_query(
            "UPDATE bookings SET start_time=%s, end_time=%s, status='pending', updated_at=NOW() WHERE id=%s",
            (new_start.isoformat(), new_end.isoformat(), booking_id),
        )

        sms_sent = False
        sms_preview = ""
        if phone:
            try:
                orig_local = (
                    orig_start.astimezone(tz)
                    if getattr(orig_start, "tzinfo", None)
                    else tz.localize(orig_start)
                )
                orig_fmt = orig_local.strftime("%A %d/%m/%Y %I:%M%p")
                new_local = new_start.astimezone(tz)
                new_fmt = new_local.strftime("%A %d/%m/%Y %I:%M %p")
                webform_url = get_webform_url(phone)
                escort_name = get_escort_name()
                sms_preview = (
                    f"Hi {client_name} I need to reschedule your booking from {orig_fmt} to {new_fmt}.\n\n"
                    "Please reply with the word YES to confirm if this is suitable for you.\n\n"
                    f"If this time is not suitable please submit your booking again by submitting my booking webform. {webform_url}\n\n"
                    "If you wish to cancel your booking then please reply with the word CANCEL.\n\n"
                    f"Kind regards {escort_name} ❤️"
                )
                send_sms(phone, sms_preview)
                sms_sent = True
            except Exception as sms_err:
                logger.warning("reschedule SMS failed: %s", sms_err)

        row = _fetch_booking(db, booking_id)
        result = _row_to_booking(row)
        result["sms_sent"] = sms_sent
        result["sms_preview"] = sms_preview
        return jsonify(result)
    except Exception as e:
        logger.error("reschedule_booking error: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        _invalidate_mobile_sync_cache()


@mobile_api_bp.route("/api/bookings/<booking_id>/cancel-preview", methods=["GET"])
def cancel_booking_preview(booking_id):
    """Return the SMS that would be sent for a cancellation, without cancelling."""
    if not _is_authenticated():
        return _unauth()
    try:
        import pytz
        from config import get_effective_escort_timezone, get_escort_name

        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        rows = db.execute_query(
            "SELECT phone, client_name, start_time, deposit_status, deposit_amount FROM bookings WHERE id = %s",
            (booking_id,), fetch=True,
        ) or []
        if not rows:
            return jsonify({"error": "Booking not found"}), 404

        booking = rows[0]
        phone = row_get(booking, "phone") or ""
        client_name = row_get(booking, "client_name") or "there"
        start_dt = row_get(booking, "start_time")
        dep_status = str(row_get(booking, "deposit_status") or "")
        dep_amount = row_get(booking, "deposit_amount") or 0
        deposit_paid = dep_status == "paid"
        try:
            deposit_amount = int(float(dep_amount))
        except (TypeError, ValueError):
            deposit_amount = 0

        tz = pytz.timezone(get_effective_escort_timezone())
        if start_dt:
            start_local = (
                start_dt.astimezone(tz) if getattr(start_dt, "tzinfo", None) else tz.localize(start_dt)
            )
            start_str = start_local.strftime("%A %d/%m/%Y %I:%M%p")
        else:
            start_str = "your scheduled time"

        webform_url = f"{config.get_base_url()}/booking"
        try:
            from core.webform_security import get_webform_url
            webform_url = get_webform_url(phone)
        except Exception:
            pass

        escort_name = get_escort_name()
        if deposit_paid and deposit_amount:
            msg = (
                f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_str}. "
                f"I apologise for any inconvenience. In order to issue you a refund for your deposit of ${deposit_amount} "
                f"please forward your banking details so I can process you a full refund. "
                f"If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                f"Hope to see you soon {escort_name}"
            )
        else:
            msg = (
                f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_str}. "
                f"I apologise for any inconvenience. If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                f"Hope to see you soon {escort_name}"
            )
        return jsonify({"message": msg, "phone": phone})
    except Exception as e:
        logger.error("cancel preview error: %s", e)
        return jsonify({"error": "Internal server error"}), 500


@mobile_api_bp.route("/api/bookings/<booking_id>/cancel", methods=["POST"])
def cancel_booking(booking_id):
    """Cancel a booking with optional SMS notification to client."""
    if not _is_authenticated():
        return _unauth()
    try:
        import pytz
        from config import get_effective_escort_timezone, get_escort_name
        from services.sms_service import send_sms

        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        rows = db.execute_query(
            "SELECT id, phone, client_name, start_time, deposit_status, deposit_amount FROM bookings WHERE id = %s",
            (booking_id,), fetch=True,
        ) or []
        if not rows:
            return jsonify({"error": "Booking not found"}), 404

        booking = rows[0]
        phone = row_get(booking, "phone") or ""
        client_name = row_get(booking, "client_name") or "there"
        start_dt = row_get(booking, "start_time")
        dep_status = str(row_get(booking, "deposit_status") or "")
        dep_amount = row_get(booking, "deposit_amount") or 0
        deposit_paid = dep_status == "paid"
        try:
            deposit_amount = int(float(dep_amount))
        except (TypeError, ValueError):
            deposit_amount = 0

        tz = pytz.timezone(get_effective_escort_timezone())
        if start_dt:
            start_local = (
                start_dt.astimezone(tz) if getattr(start_dt, "tzinfo", None) else tz.localize(start_dt)
            )
            start_str = start_local.strftime("%A %d/%m/%Y %I:%M%p")
        else:
            start_str = "your scheduled time"

        sms_sent = False
        if phone:
            escort_name = get_escort_name()
            custom_message = (request.get_json(silent=True) or {}).get("custom_message", "").strip()
            if custom_message:
                msg = custom_message
            else:
                webform_url = f"{config.get_base_url()}/booking"
                try:
                    from core.webform_security import get_webform_url
                    webform_url = get_webform_url(phone)
                except Exception:
                    pass
                if deposit_paid and deposit_amount:
                    msg = (
                        f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_str}. "
                        f"I apologise for any inconvenience. In order to issue you a refund for your deposit of ${deposit_amount} "
                        f"please forward your banking details so I can process you a full refund. "
                        f"If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                        f"Hope to see you soon {escort_name}"
                    )
                else:
                    msg = (
                        f"Hi {client_name} I'm very sorry but I need to cancel your booking scheduled for {start_str}. "
                        f"I apologise for any inconvenience. If you'd like to rebook for another time please text me back or instead fill in my booking webform {webform_url} "
                        f"Hope to see you soon {escort_name}"
                    )
            try:
                send_sms(phone, msg)
                sms_sent = True
            except Exception as sms_err:
                logger.warning("cancel SMS failed: %s", sms_err)

        # Delete any linked travel blocks via conversation_states
        state_rows = db.execute_query(
            "SELECT travel_outbound_event_id, travel_return_event_id FROM conversation_states WHERE phone_number = %s",
            (phone,), fetch=True,
        ) or []
        if state_rows:
            for key in ("travel_outbound_event_id", "travel_return_event_id"):
                travel_id = row_get(state_rows[0], key)
                if travel_id:
                    db.execute_query(
                        "DELETE FROM bookings WHERE id = %s AND type = 'travel'", (str(travel_id),)
                    )

        db.execute_query("DELETE FROM bookings WHERE id = %s", (booking_id,))

        # Reset conversation state so chatbot accepts new bookings from this client
        if phone:
            db.execute_query(
                """UPDATE conversation_states
                   SET current_state = 'NEW', date = NULL, time = NULL, duration = NULL,
                       experience_type = NULL, incall_outcall = NULL, outcall_address = NULL,
                       peacock_event_id = NULL, confirmed_event_id = NULL,
                       travel_outbound_event_id = NULL, travel_return_event_id = NULL,
                       confirmed_at = NULL, first_contact_sent = FALSE,
                       missing_fields = '["date","time","duration"]',
                       awaiting_refund_details = %s
                   WHERE phone_number = %s""",
                (deposit_paid and bool(deposit_amount), phone),
            )

        return jsonify({"success": True, "sms_sent": sms_sent})
    except Exception as e:
        logger.error("cancel_booking error: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500
    finally:
        _invalidate_mobile_sync_cache()


@mobile_api_bp.route("/api/stats", methods=["GET"])
def get_stats():
    """Server-side booking stats: summary cards + charts."""
    if not _is_authenticated():
        return _unauth()
    try:
        from config import get_effective_escort_timezone

        weeks = max(1, min(int(request.args.get("weeks", 8)), 52))
        tz_name = get_effective_escort_timezone()
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503

        _COUNTED = ["confirmed", "reschedule-confirmed", "reserved"]
        _CONFIRMED = ["confirmed", "reschedule-confirmed"]
        _UPCOMING = ["confirmed", "reschedule-confirmed", "reserved", "pending"]

        # ── Single-pass summary aggregation ────────────────────────────────
        summary_row = db.execute_query(
            f"""
            SELECT
              COUNT(*) FILTER (
                WHERE DATE(start_time AT TIME ZONE %s) = CURRENT_DATE AT TIME ZONE %s
                  AND status = ANY(%s)
              ) AS today_count,

              COUNT(*) FILTER (
                WHERE DATE_TRUNC('week', start_time AT TIME ZONE %s)
                      = DATE_TRUNC('week', NOW() AT TIME ZONE %s)
                  AND status = ANY(%s)
              ) AS week_count,

              COUNT(*) FILTER (
                WHERE DATE_TRUNC('month', start_time AT TIME ZONE %s)
                      = DATE_TRUNC('month', NOW() AT TIME ZONE %s)
                  AND status = ANY(%s)
              ) AS month_count,

              COUNT(*) FILTER (
                WHERE start_time > NOW()
                  AND status = 'pending'
              ) AS pending_count,

              COUNT(*) FILTER (
                WHERE start_time > NOW()
                  AND status = ANY(%s)
              ) AS confirmed_count,

              COUNT(*) FILTER (
                WHERE start_time > NOW()
                  AND status = ANY(%s)
              ) AS upcoming_count,

              COALESCE(SUM(deposit_amount) FILTER (
                WHERE deposit_status = 'paid'
              ), 0) AS total_deposits,

              COALESCE(SUM(price_total) FILTER (
                WHERE status = ANY(%s)
              ), 0) AS total_earned

            FROM bookings
            WHERE type != 'travel'
            """,
            (
                tz_name, tz_name, _COUNTED,           # today
                tz_name, tz_name, _COUNTED,            # week
                tz_name, tz_name, _COUNTED,            # month
                _CONFIRMED,                            # confirmed_count
                _UPCOMING,                             # upcoming_count
                _CONFIRMED,                            # total_earned
            ),
            fetch=True,
        ) or []

        s = summary_row[0] if summary_row else {}

        # Total enquiries = unique clients who have ever contacted the chatbot
        enquiry_row = db.execute_query(
            "SELECT COUNT(*) AS cnt FROM conversation_states", fetch=True,
        ) or []
        total_enquiries = int(row_get(enquiry_row[0], "cnt") or 0) if enquiry_row else 0

        # ── Bookings per calendar week (chart) ──────────────────────────────
        week_rows = db.execute_query(
            f"""
            SELECT
              DATE_TRUNC('week', start_time AT TIME ZONE %s)::DATE AS week_start,
              COUNT(*) AS cnt
            FROM bookings
            WHERE start_time >= NOW() - INTERVAL '{weeks} weeks'
              AND start_time <= NOW()
              AND status = ANY(%s)
              AND type != 'travel'
            GROUP BY week_start
            ORDER BY week_start ASC
            """,
            (tz_name, _COUNTED), fetch=True,
        ) or []

        bookings_by_week = []
        for r in week_rows:
            ws = row_get(r, "week_start")
            label = ws.strftime("%-d %b") if hasattr(ws, "strftime") else str(ws)
            bookings_by_week.append({"week": label, "count": int(row_get(r, "cnt") or 0)})

        # ── Experience breakdown (normalised) ───────────────────────────────
        exp_rows = db.execute_query(
            f"""
            SELECT experience, COUNT(*) AS cnt
            FROM bookings
            WHERE start_time >= NOW() - INTERVAL '{weeks} weeks'
              AND start_time <= NOW()
              AND status = ANY(%s)
              AND type != 'travel'
              AND experience IS NOT NULL AND experience != ''
            GROUP BY experience
            ORDER BY cnt DESC
            """,
            (_COUNTED,), fetch=True,
        ) or []

        exp_agg = {}
        for r in exp_rows:
            label = _normalize_experience(str(row_get(r, "experience") or ""))
            exp_agg[label] = exp_agg.get(label, 0) + int(row_get(r, "cnt") or 0)
        experience_breakdown = [
            {"experience": k, "count": v}
            for k, v in sorted(exp_agg.items(), key=lambda x: -x[1])
        ]

        # ── Bookings by day of week (chart) ─────────────────────────────────
        dow_rows = db.execute_query(
            f"""
            SELECT
              EXTRACT(DOW FROM start_time AT TIME ZONE %s)::INT AS dow,
              COUNT(*) AS cnt
            FROM bookings
            WHERE start_time >= NOW() - INTERVAL '{weeks} weeks'
              AND start_time <= NOW()
              AND status = ANY(%s)
              AND type != 'travel'
            GROUP BY dow
            ORDER BY dow ASC
            """,
            (tz_name, _COUNTED), fetch=True,
        ) or []

        dow_map = {int(row_get(r, "dow") or 0): int(row_get(r, "cnt") or 0) for r in dow_rows}
        bookings_by_day = [{"day": _DOW_NAMES[i], "count": dow_map.get(i, 0)} for i in range(7)]

        # ── Historical totals (within the rolling window) ───────────────────
        total_row = db.execute_query(
            f"""
            SELECT COUNT(*) AS total, COALESCE(SUM(price_total), 0) AS revenue
            FROM bookings
            WHERE start_time >= NOW() - INTERVAL '{weeks} weeks'
              AND start_time <= NOW()
              AND status = ANY(%s)
              AND type != 'travel'
            """,
            (_COUNTED,), fetch=True,
        ) or []
        total_bookings = int(row_get(total_row[0], "total") or 0) if total_row else 0
        total_revenue = float(row_get(total_row[0], "revenue") or 0.0) if total_row else 0.0

        return jsonify({
            # Summary cards
            "today": int(row_get(s, "today_count") or 0),
            "this_week": int(row_get(s, "week_count") or 0),
            "this_month": int(row_get(s, "month_count") or 0),
            "pending_bookings": int(row_get(s, "pending_count") or 0),
            "confirmed_bookings": int(row_get(s, "confirmed_count") or 0),
            "upcoming_bookings": int(row_get(s, "upcoming_count") or 0),
            "total_enquiries": total_enquiries,
            "total_deposits": float(row_get(s, "total_deposits") or 0.0),
            "total_earned": float(row_get(s, "total_earned") or 0.0),
            # Charts (rolling window of `weeks`)
            "weeks": weeks,
            "total_bookings": total_bookings,
            "total_revenue": total_revenue,
            "bookings_by_week": bookings_by_week,
            "experience_breakdown": experience_breakdown,
            "bookings_by_day": bookings_by_day,
        })
    except Exception as e:
        logger.error("get_stats error: %s", e, exc_info=True)
        return jsonify({"error": "Internal server error"}), 500


@mobile_api_bp.route("/api/messages/<booking_id>", methods=["GET"])
def get_messages(booking_id):
    if not _is_authenticated():
        return _unauth()
    try:
        db = _get_db()
        if db is None:
            return jsonify({"error": "Database unavailable"}), 503
        # Resolve phone number from booking
        phone_rows = db.execute_query(
            "SELECT phone FROM bookings WHERE id = %s",
            (booking_id,),
            fetch=True,
        ) or []
        if not phone_rows:
            return jsonify([])

        raw_phone = row_get(phone_rows[0], "phone") or ""
        phone_digits = "".join(ch for ch in raw_phone if ch.isdigit())
        if not phone_digits:
            return jsonify([])
        # Match last 9 digits to handle +61/0 prefix variations
        suffix = phone_digits[-9:] if len(phone_digits) >= 9 else phone_digits

        rows = db.execute_query(
            """
            SELECT id, direction, message_body, created_at, phone_number
            FROM message_history
            WHERE regexp_replace(COALESCE(phone_number, ''), '\\D', '', 'g') LIKE %s
            ORDER BY created_at ASC
            LIMIT 300
            """,
            (f"%{suffix}",),
            fetch=True,
        ) or []

        messages = []
        for row in rows:
            ts = row_get(row, "created_at")
            direction = row_get(row, "direction") or "outbound"
            phone_number = row_get(row, "phone_number") or ""
            messages.append({
                "id": str(row_get(row, "id") or ""),
                "booking_id": booking_id,
                "direction": direction,
                "body": row_get(row, "message_body") or "",
                "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts or ""),
                "from_number": phone_number if direction == "inbound" else "",
                "to_number": phone_number if direction == "outbound" else "",
            })
        return jsonify(messages)
    except Exception as e:
        logger.error("get_messages error: %s", e)
        return jsonify({"error": "Internal server error"}), 500
