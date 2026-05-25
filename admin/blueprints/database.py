"""
Database management routes.

Endpoints:
- /database : Client database dashboard
- /database/export : Download all clients as CSV (Excel-compatible UTF-8)
- /database/client/<phone> : Get client details
- /database/client/<phone>/notes : Save client notes
- /database/block/<phone> : Block a client
- /database/unblock/<phone> : Unblock a client
- /database/delete/<phone> : Delete client data
- /database/clear-database : Clear ALL data (clients, bookings, conversation history)
- /database/clear-progress : Clear all booking progress
"""

import csv
import logging
from datetime import datetime, timezone
from io import StringIO

from flask import Blueprint, flash, jsonify, redirect, render_template, request, Response, session, url_for

import config
from config import get_effective_escort_timezone
from admin.auth import verify_password
from utils.log_sanitize import LOG_SUPPRESSED_FMT, sanitize_log_value

_SQL_DELETE_BLOCKED_CLIENT = "DELETE FROM blocked_clients WHERE phone_number = %s"

try:
    from services.database_service import get_shared_db_with_retry
except ImportError:
    # Older deployments may not have uploaded database_service.py with get_shared_db_with_retry
    from services.database_service import get_shared_db as get_shared_db_with_retry


def _get_db():
    """Shared Postgres pool; may be None if DATABASE_URL is missing or pool init failed."""
    return get_shared_db_with_retry(config.DATABASE_URL)

logger = logging.getLogger("escort_chatbot.admin.database")

database_bp = Blueprint('database', __name__, template_folder='../templates')

_TEMPLATE_DATABASE_HTML = "database.html"
_JSON_ERR_NOT_AUTHENTICATED = "Not authenticated"
_JSON_ERR_DB_UNAVAILABLE = "Database connection unavailable"


def _is_database_authenticated():
    """Check if user is authenticated for database access."""
    return session.get("admin_authenticated", False) or session.get("database_authenticated", False)


def _search_params(search: str | None) -> tuple[bool, str]:
    """Search flag + LIKE term for server-side search (phone, client name, any message body)."""
    s = (search or "").strip()
    return (bool(s), f"%{s}%")


@database_bp.route("/database", methods=["GET", "POST"])
def database_dashboard():
    """Client database dashboard with authentication."""
    authenticated = _is_database_authenticated()
    error = None

    # Handle authentication
    if request.method == "POST" and not authenticated:
        password = request.form.get("password", "")
        if verify_password(password):
            session["database_authenticated"] = True
            authenticated = True
            logger.info("Successful database login")
        else:
            error = "Invalid password"
            logger.warning("Failed database login attempt")

    if not authenticated:
        return render_template(_TEMPLATE_DATABASE_HTML, authenticated=False, error=error)

    if not _get_db():
        flash(
            "Database connection is not available. Set DATABASE_URL in your PythonAnywhere Web environment, "
            "reload the web app, then check /healthcheck.",
            "error",
        )
        return render_template(
            _TEMPLATE_DATABASE_HTML,
            authenticated=True,
            clients=[],
            blocked_contacts=[],
            stats=_fetch_stats(0),
            page=1,
            total_pages=1,
            search=(request.args.get("search") or "").strip(),
        )

    # Get pagination + optional search (all clients, not just current page)
    page = request.args.get("page", 1, type=int)
    per_page = 50
    search = (request.args.get("search") or "").strip()

    clients, total_clients, total_pages = _fetch_clients(page, per_page, search=search)
    blocked_contacts = _fetch_blocked_contacts()
    stats = _fetch_stats(total_clients)

    return render_template(
        _TEMPLATE_DATABASE_HTML,
        authenticated=True,
        clients=clients,
        blocked_contacts=blocked_contacts,
        stats=stats,
        page=page,
        total_pages=total_pages,
        search=search,
    )


def _fetch_clients(page, per_page, search: str | None = None):
    """Fetch paginated client list (optional search across phone, name, message text)."""
    clients = []
    total_clients = 0
    total_pages = 1

    try:
        db = _get_db()
        if not db:
            return [], 0, 1
        has_search, term = _search_params(search)

        list_sql = """
            SELECT phone_number, name, last_contact, total_bookings, last_booking, status
            FROM (
                SELECT DISTINCT m.phone_number,
                    (SELECT cs.client_name FROM conversation_states cs WHERE cs.phone_number = m.phone_number LIMIT 1) as name,
                    (SELECT MAX(m2.created_at) FROM message_history m2 WHERE m2.phone_number = m.phone_number) as last_contact,
                    (SELECT COUNT(*) FROM conversation_states cs2 WHERE cs2.phone_number = m.phone_number AND cs2.confirmed_at IS NOT NULL) as total_bookings,
                    (SELECT cs3.confirmed_at FROM conversation_states cs3 WHERE cs3.phone_number = m.phone_number AND cs3.confirmed_at IS NOT NULL ORDER BY cs3.confirmed_at DESC LIMIT 1) as last_booking,
                    CASE WHEN EXISTS (SELECT 1 FROM blocked_clients bn WHERE bn.phone_number = m.phone_number) THEN 'blocked'
                         WHEN EXISTS (SELECT 1 FROM conversation_states cs4 WHERE cs4.phone_number = m.phone_number AND (cs4.confirmed_at IS NOT NULL OR cs4.current_state = 'CONFIRMED')) THEN 'confirmed'
                         WHEN EXISTS (SELECT 1 FROM conversation_states cs5 WHERE cs5.phone_number = m.phone_number AND cs5.current_state IN ('COLLECTING', 'CHECKING_AVAILABILITY', 'DEPOSIT_REQUIRED') AND cs5.confirmed_at IS NULL) THEN 'pending'
                         ELSE 'new'
                    END as status
                FROM message_history m
                LEFT JOIN conversation_states cs ON cs.phone_number = m.phone_number
                WHERE (
                    %s = FALSE OR
                    m.phone_number ILIKE %s OR
                    COALESCE(cs.client_name, '') ILIKE %s OR
                    EXISTS (
                        SELECT 1 FROM message_history mh
                        WHERE mh.phone_number = m.phone_number AND mh.message_body ILIKE %s
                    )
                )
            ) sub
            ORDER BY last_contact DESC NULLS LAST
            LIMIT %s OFFSET %s
        """
        params = (has_search, term, term, term, per_page, (page - 1) * per_page)
        phone_data = db.execute_query(list_sql, params, fetch=True)

        if phone_data:
            for row in phone_data:
                if isinstance(row, dict):
                    clients.append({
                        'phone': row.get('phone_number', ''),
                        'name': row.get('name', 'Unknown'),
                        'last_contact': str(row.get('last_contact', 'N/A'))[:16] if row.get('last_contact') else 'N/A',
                        'total_bookings': row.get('total_bookings', 0),
                        'last_booking': str(row.get('last_booking', 'N/A'))[:10] if row.get('last_booking') else 'N/A',
                        'status': row.get('status', 'new')
                    })
                else:
                    from utils.row_utils import row_get
                    clients.append({
                        'phone': row_get(row, 0, ''),
                        'name': row_get(row, 1, 'Unknown'),
                        'last_contact': (str(row_get(row, 2, ''))[:16]) if row_get(row,2,None) else 'N/A',
                        'total_bookings': row_get(row, 3, 0),
                        'last_booking': (str(row_get(row, 4, ''))[:10]) if row_get(row,4,None) else 'N/A',
                        'status': row_get(row, 5, 'new')
                    })

        count_sql = """
            SELECT COUNT(*) FROM (
                SELECT DISTINCT m.phone_number
                FROM message_history m
                LEFT JOIN conversation_states cs ON cs.phone_number = m.phone_number
                WHERE (
                    %s = FALSE OR
                    m.phone_number ILIKE %s OR
                    COALESCE(cs.client_name, '') ILIKE %s OR
                    EXISTS (
                        SELECT 1 FROM message_history mh
                        WHERE mh.phone_number = m.phone_number AND mh.message_body ILIKE %s
                    )
                )
            ) matched
        """
        total_result = db.execute_query(count_sql, (has_search, term, term, term), fetch=True)
        if total_result:
            from utils.row_utils import row_get
            if isinstance(total_result[0], dict):
                total_clients = list(total_result[0].values())[0] or 0
            else:
                total_clients = row_get(total_result[0], 0, 0) or 0
        total_pages = max(1, (total_clients + per_page - 1) // per_page)

    except Exception as e:
        logger.error(f"Database query error: {e}", exc_info=True)

    return clients, total_clients, total_pages


def _fetch_clients_for_export(search: str | None = None):
    """All matching clients for CSV (cap rows for safety)."""
    rows_out = []
    try:
        db = _get_db()
        if not db:
            return rows_out
        has_search, term = _search_params(search)
        export_sql = """
            SELECT phone_number, name, last_contact, message_count, total_bookings, last_booking, status
            FROM (
                SELECT DISTINCT m.phone_number,
                    (SELECT cs.client_name FROM conversation_states cs WHERE cs.phone_number = m.phone_number LIMIT 1) as name,
                    (SELECT MAX(m2.created_at) FROM message_history m2 WHERE m2.phone_number = m.phone_number) as last_contact,
                    (SELECT COUNT(*)::bigint FROM message_history mh WHERE mh.phone_number = m.phone_number) as message_count,
                    (SELECT COUNT(*) FROM conversation_states cs2 WHERE cs2.phone_number = m.phone_number AND cs2.confirmed_at IS NOT NULL) as total_bookings,
                    (SELECT cs3.confirmed_at FROM conversation_states cs3 WHERE cs3.phone_number = m.phone_number AND cs3.confirmed_at IS NOT NULL ORDER BY cs3.confirmed_at DESC LIMIT 1) as last_booking,
                    CASE WHEN EXISTS (SELECT 1 FROM blocked_clients bn WHERE bn.phone_number = m.phone_number) THEN 'blocked'
                         WHEN EXISTS (SELECT 1 FROM conversation_states cs4 WHERE cs4.phone_number = m.phone_number AND (cs4.confirmed_at IS NOT NULL OR cs4.current_state = 'CONFIRMED')) THEN 'confirmed'
                         WHEN EXISTS (SELECT 1 FROM conversation_states cs5 WHERE cs5.phone_number = m.phone_number AND cs5.current_state IN ('COLLECTING', 'CHECKING_AVAILABILITY', 'DEPOSIT_REQUIRED') AND cs5.confirmed_at IS NULL) THEN 'pending'
                         ELSE 'new'
                    END as status
                FROM message_history m
                LEFT JOIN conversation_states cs ON cs.phone_number = m.phone_number
                WHERE (
                    %s = FALSE OR
                    m.phone_number ILIKE %s OR
                    COALESCE(cs.client_name, '') ILIKE %s OR
                    EXISTS (
                        SELECT 1 FROM message_history mh
                        WHERE mh.phone_number = m.phone_number AND mh.message_body ILIKE %s
                    )
                )
            ) sub
            ORDER BY last_contact DESC NULLS LAST
            LIMIT 20000
        """
        phone_data = db.execute_query(export_sql, (has_search, term, term, term), fetch=True)
        if phone_data:
            for row in phone_data:
                if isinstance(row, dict):
                    rows_out.append(row)
                else:
                    from utils.row_utils import row_get
                    rows_out.append({
                        "phone_number": row_get(row, 0, ""),
                        "name": row_get(row, 1, ""),
                        "last_contact": row_get(row, 2, None),
                        "message_count": row_get(row, 3, 0),
                        "total_bookings": row_get(row, 4, 0),
                        "last_booking": row_get(row, 5, None),
                        "status": row_get(row, 6, ""),
                    })
    except Exception as e:
        logger.error(f"Export query error: {e}", exc_info=True)
    return rows_out


@database_bp.route("/database/export")
def export_clients_csv():
    """Download client list as UTF-8 CSV (opens in Excel). Respects ?search= like the dashboard."""
    if not _is_database_authenticated():
        return redirect(url_for("database.database_dashboard"))
    search = (request.args.get("search") or "").strip()
    data_rows = _fetch_clients_for_export(search=search)

    buf = StringIO()
    w = csv.writer(buf)
    w.writerow([
        "phone",
        "name",
        "status",
        "total_bookings",
        "message_count",
        "last_booking",
        "last_contact",
    ])
    for r in data_rows:
        if isinstance(r, dict):
            phone = r.get("phone_number") or ""
            name = r.get("name") or ""
            status = r.get("status") or ""
            tb = r.get("total_bookings", "")
            mc = r.get("message_count", "")
            lb = r.get("last_booking")
            lc = r.get("last_contact")
        else:
            continue
        w.writerow([
            phone,
            name,
            status,
            tb,
            mc,
            str(lb)[:19] if lb else "",
            str(lc)[:19] if lc else "",
        ])

    payload = "\ufeff" + buf.getvalue()
    fname = f"clients_export_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.csv"
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


def _fetch_stats(total_clients):
    """Fetch database statistics."""
    stats = {
        'total_clients': total_clients,
        'confirmed_bookings': 0,
        'pending_bookings': 0,
        'blocked_numbers': 0
    }

    db = _get_db()
    if not db:
        return stats

    try:
        confirmed = db.execute_query("SELECT COUNT(*) FROM conversation_states WHERE confirmed_at IS NOT NULL", fetch=True)
        if confirmed:
            from utils.row_utils import row_get
            stats['confirmed_bookings'] = list(confirmed[0].values())[0] if isinstance(confirmed[0], dict) else row_get(confirmed[0], 0, 0)
    except Exception as e:
        logger.warning(f"Failed to fetch confirmed bookings count: {e}")

    try:
        # Pending = Graphite only (in funnel, no confirmed booking yet). Peacock/Basil = confirmed_at set -> confirmed.
        pending = db.execute_query("""
            SELECT COUNT(*) FROM conversation_states
            WHERE current_state IN ('COLLECTING', 'CHECKING_AVAILABILITY', 'DEPOSIT_REQUIRED')
            AND confirmed_at IS NULL
        """, fetch=True)
        if pending:
            from utils.row_utils import row_get
            stats['pending_bookings'] = list(pending[0].values())[0] if isinstance(pending[0], dict) else row_get(pending[0], 0, 0)
    except Exception as e:
        logger.warning(f"Failed to fetch pending bookings count: {e}")

    try:
        blocked = db.execute_query("SELECT COUNT(*) FROM blocked_clients", fetch=True)
        if blocked:
            from utils.row_utils import row_get
            stats['blocked_numbers'] = list(blocked[0].values())[0] if isinstance(blocked[0], dict) else row_get(blocked[0], 0, 0)
    except Exception as e:
        logger.warning(f"Failed to fetch blocked numbers count: {e}")

    return stats


def _fetch_blocked_contacts(limit: int = 200):
    """Fetch blocked contacts for quick review and actions on the database page."""
    db = _get_db()
    if not db:
        return []

    out = []
    try:
        rows = db.execute_query(
            """
            SELECT
                bc.phone_number,
                COALESCE(MAX(cs.client_name), '') AS name,
                bc.reason,
                bc.blocked_at,
                COALESCE(bc.notes, '') AS notes
            FROM blocked_clients bc
            LEFT JOIN conversation_states cs ON cs.phone_number = bc.phone_number
            GROUP BY bc.phone_number, bc.reason, bc.blocked_at, bc.notes
            ORDER BY bc.blocked_at DESC
            LIMIT %s
            """,
            (limit,),
            fetch=True,
        )
        for row in rows or []:
            if isinstance(row, dict):
                blocked_at = row.get("blocked_at")
                out.append(
                    {
                        "phone": row.get("phone_number", ""),
                        "name": row.get("name", "") or "Unknown",
                        "reason": row.get("reason", "") or "-",
                        "blocked_at": str(blocked_at)[:19] if blocked_at else "N/A",
                        "notes": row.get("notes", "") or "",
                    }
                )
            else:
                from utils.row_utils import row_get
                blocked_at = row_get(row, 3, None)
                out.append(
                    {
                        "phone": row_get(row, 0, ""),
                        "name": (row_get(row, 1, "")) or "Unknown",
                        "reason": (row_get(row, 2, "")) or "-",
                        "blocked_at": str(blocked_at)[:19] if blocked_at else "N/A",
                        "notes": (row_get(row, 4, "")) or "",
                    }
                )
    except Exception as e:
        logger.warning(f"Failed to fetch blocked contacts: {e}")

    return out


@database_bp.route("/database/client/<phone>")
def get_client_details(phone):
    """Get detailed client information."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        client = _build_client_details(phone)
        if client is None:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503
        return jsonify({"success": True, "client": client, "timezone": get_effective_escort_timezone()})
    except Exception as e:
        logger.exception("Error getting client details")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


def _build_client_details(phone):
    """Build detailed client information dict."""
    client = {}
    db = _get_db()
    if not db:
        return None

    # Get basic info from message_history
    msg_data = db.execute_query("""
        SELECT phone_number, MIN(created_at) as first_contact, MAX(created_at) as last_contact, COUNT(*) as message_count
        FROM message_history WHERE phone_number = %s GROUP BY phone_number
    """, (phone,), fetch=True)

    if msg_data:
        row = msg_data[0]
        if isinstance(row, dict):
            client['phone'] = row.get('phone_number', phone)
            client['first_contact'] = str(row.get('first_contact', ''))[:16]
            client['last_contact'] = str(row.get('last_contact', ''))[:16]
            client['message_count'] = row.get('message_count', 0)
        else:
            from utils.row_utils import row_get
            client['phone'] = row_get(row, 0, phone)
            client['first_contact'] = (str(row_get(row, 1, ''))[:16]) if row_get(row,1,None) else ''
            client['last_contact'] = (str(row_get(row, 2, ''))[:16]) if row_get(row,2,None) else ''
            client['message_count'] = row_get(row, 3, 0)

    # Get pending (Graphite) booking details: only when no confirmed booking yet
    pending = db.execute_query("""
        SELECT 
            client_name,
            date,
            time,
            duration,
            experience_type,
            incall_outcall,
            current_state as booking_status
        FROM conversation_states 
        WHERE phone_number = %s 
        AND current_state IN ('COLLECTING', 'CHECKING_AVAILABILITY', 'DEPOSIT_REQUIRED')
        AND confirmed_at IS NULL
    """, (phone,), fetch=True)

    if pending:
        row = pending[0]
        if isinstance(row, dict):
            client['name'] = row.get('client_name', 'Unknown')
            client['pending_date'] = str(row.get('date', '') or '')
            client['pending_time'] = str(row.get('time', '') or '')
            client['pending_duration'] = row.get('duration', '')
            client['pending_experience'] = row.get('experience_type', '')
            client['pending_location'] = row.get('incall_outcall', '')
            client['booking_status'] = row.get('booking_status', '')
        else:
            from utils.row_utils import row_get
            client['name'] = row_get(row, 0, 'Unknown')
            client['pending_date'] = str(row_get(row, 1, '') or '')
            client['pending_time'] = str(row_get(row, 2, '') or '')
            client['pending_duration'] = row_get(row, 3, '')
            client['pending_experience'] = row_get(row, 4, '')
            client['pending_location'] = row_get(row, 5, '')
            client['booking_status'] = row_get(row, 6, '')

    # Get confirmed bookings count
    bookings = db.execute_query("SELECT COUNT(*) FROM conversation_states WHERE phone_number = %s AND confirmed_at IS NOT NULL", (phone,), fetch=True)
    if bookings:
        client['total_bookings'] = list(bookings[0].values())[0] if isinstance(bookings[0], dict) else bookings[0][0]  # type: ignore[index]

    # Get confirmed booking record (date, time, duration, experience, incall/outcall, deposit paid, total cost)
    confirmed = db.execute_query("""
        SELECT date, time, duration, experience_type, incall_outcall, deposit_paid, total_booking_cost
        FROM conversation_states
        WHERE phone_number = %s AND confirmed_at IS NOT NULL
    """, (phone,), fetch=True)
    if confirmed:
        row = confirmed[0]
        if isinstance(row, dict):
            client['booking_date'] = str(row.get('date') or '')[:10]
            client['booking_time'] = str(row.get('time') or '')
            d = row.get('duration')
            client['booking_duration'] = f"{d} min" if d is not None else ''
            client['booking_experience'] = row.get('experience_type') or ''
            client['booking_incall_outcall'] = row.get('incall_outcall') or ''
            client['deposit_paid'] = 'Yes' if row.get('deposit_paid') else 'No'
            tc = row.get('total_booking_cost')
            client['total_booking_cost'] = f"${tc}" if tc is not None else ''
        else:
            from utils.row_utils import row_get
            client['booking_date'] = str(row_get(row, 0, '') or '')[:10]
            client['booking_time'] = str(row_get(row, 1, '') or '')
            d = row_get(row, 2, None)
            client['booking_duration'] = f"{d} min" if d is not None else ''
            client['booking_experience'] = row_get(row, 3, '') or ''
            client['booking_incall_outcall'] = row_get(row, 4, '') or ''
            client['deposit_paid'] = 'Yes' if row_get(row, 5, False) else 'No'
            tc = row_get(row, 6, None)
            client['total_booking_cost'] = f"${tc}" if tc is not None else ''

    # Client feedback history (post-booking ratings from escort)
    try:
        feedback_rows = db.execute_query(
            """SELECT arrived_on_time, was_respectful, would_see_again, star_rating,
                      booking_date, feedback_received_at, comments
               FROM client_feedback WHERE client_phone_number = %s
               ORDER BY feedback_received_at DESC LIMIT 20""",
            (phone,),
            fetch=True
        )
        if feedback_rows:
            from utils.row_utils import row_get
            client['feedback_history'] = [
                {
                    'arrived_on_time': r.get('arrived_on_time') if isinstance(r, dict) else row_get(r, 0, None),
                    'was_respectful': r.get('was_respectful') if isinstance(r, dict) else row_get(r, 1, None),
                    'would_see_again': r.get('would_see_again') if isinstance(r, dict) else row_get(r, 2, None),
                    'star_rating': r.get('star_rating') if isinstance(r, dict) else row_get(r, 3, None),
                    'booking_date': str(r.get('booking_date') or '')[:10] if isinstance(r, dict) else str(row_get(r, 4, '') or '')[:10],
                    'feedback_received_at': str(r.get('feedback_received_at') or '')[:16] if isinstance(r, dict) else str(row_get(r, 5, '') or '')[:16],
                    'comments': (r.get('comments') or '').strip() if isinstance(r, dict) else (row_get(r, 6, '') or '').strip(),
                }
                for r in feedback_rows
            ]
        else:
            client['feedback_history'] = []
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        client['feedback_history'] = []

    # Check if blocked
    blocked = db.execute_query("SELECT reason FROM blocked_clients WHERE phone_number = %s", (phone,), fetch=True)
    client['is_blocked'] = bool(blocked)
    if blocked:
        client['block_reason'] = list(blocked[0].values())[0] if isinstance(blocked[0], dict) else blocked[0][0]  # type: ignore[index]

    # Get admin notes
    try:
        notes = db.execute_query("SELECT notes FROM client_notes WHERE phone_number = %s", (phone,), fetch=True)
        if notes:
            client['admin_notes'] = list(notes[0].values())[0] if isinstance(notes[0], dict) else notes[0][0]  # type: ignore[index]
        else:
            client['admin_notes'] = ''
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        client['admin_notes'] = ''

    # SMS transcript (chatbot ↔ client) for admin review
    client["conversation_history"] = []
    try:
        hist_rows = db.execute_query(
            """
            SELECT direction, message_body, created_at, intent_classified, state_at_time
            FROM message_history
            WHERE phone_number = %s
            ORDER BY created_at ASC
            LIMIT 10000
            """,
            (phone,),
            fetch=True,
        )
        if hist_rows:
            for r in hist_rows:
                if isinstance(r, dict):
                    body = r.get("message_body") or ""
                    if len(body) > 16000:
                        body = body[:16000] + "\n…"
                    client["conversation_history"].append(
                        {
                            "direction": (r.get("direction") or "").strip(),
                            "message_body": body,
                            "created_at": (r.get("created_at").isoformat() if hasattr(r.get("created_at"), "isoformat") else str(r.get("created_at") or "")),  # type: ignore[union-attr]
                            "intent_classified": (r.get("intent_classified") or "") or "",
                            "state_at_time": (r.get("state_at_time") or "") or "",
                        }
                    )
                else:
                    from utils.row_utils import row_get
                    body = row_get(r, 1, "") or ""
                    if len(body) > 16000:
                        body = body[:16000] + "\n…"
                    client["conversation_history"].append(
                        {
                            "direction": (row_get(r, 0, "") or "").strip(),
                            "message_body": body,
                            "created_at": (row_get(r, 2, None).isoformat() if hasattr(row_get(r, 2, None), "isoformat") else str(row_get(r, 2, "") or "")),
                            "intent_classified": row_get(r, 3, "") or "",
                            "state_at_time": row_get(r, 4, "") or "",
                        }
                    )
    except Exception as e:
        logger.warning("conversation_history load failed for %s: %s", sanitize_log_value(phone), e)

    return client


@database_bp.route("/database/client/<phone>/notes", methods=["POST"])
def save_client_notes(phone):
    """Save admin notes for a client."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        data = request.get_json()
        notes = data.get('notes', '') if data else ''

        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503

        # Ensure the client_notes table exists
        db.execute_query("""
            CREATE TABLE IF NOT EXISTS client_notes (
                phone_number VARCHAR(20) PRIMARY KEY,
                notes TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """, fetch=False)

        # Check if notes exist
        existing = db.execute_query(
            "SELECT 1 FROM client_notes WHERE phone_number = %s",
            (phone,),
            fetch=True
        )

        if existing:
            # Update existing notes
            db.execute_query(
                "UPDATE client_notes SET notes = %s, updated_at = CURRENT_TIMESTAMP WHERE phone_number = %s",
                (notes, phone),
                fetch=False
            )
        else:
            # Insert new notes
            db.execute_query(
                "INSERT INTO client_notes (phone_number, notes) VALUES (%s, %s)",
                (phone, notes),
                fetch=False
            )

        logger.info("Saved notes for client: %s", sanitize_log_value(phone))
        return jsonify({"success": True})

    except Exception as e:
        logger.exception("Error saving notes")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route("/database/block/<phone>", methods=["POST"])
def block_client(phone):
    """Block a client."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503
        db.execute_query("""
            INSERT INTO blocked_clients (phone_number, reason, blocked_at)
            VALUES (%s, 'Blocked from admin database', NOW())
            ON CONFLICT (phone_number) DO NOTHING
        """, (phone,))

        logger.info("Blocked client: %s", sanitize_log_value(phone))
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error blocking client")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route("/database/unblock/<phone>", methods=["POST"])
def unblock_client(phone):
    """Unblock a client."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503
        db.execute_query(_SQL_DELETE_BLOCKED_CLIENT, (phone,))

        logger.info("Unblocked client: %s", sanitize_log_value(phone))
        return jsonify({"success": True})
    except Exception as e:
        logger.exception("Error unblocking client")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route("/database/delete/<phone>", methods=["POST"])
def delete_client(phone):
    """Permanently delete a client and all their data."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503

        # Delete from all related tables atomically (message_history cascades from conversation_states).
        # client_notes is optional — if it doesn't exist, rollback-and-retry without it.
        try:
            with db.transaction() as _conn:
                db.execute_query(_SQL_DELETE_BLOCKED_CLIENT, (phone,), conn=_conn)
                db.execute_query("DELETE FROM conversation_states WHERE phone_number = %s", (phone,), conn=_conn)
                db.execute_query("DELETE FROM client_notes WHERE phone_number = %s", (phone,), conn=_conn)
        except Exception as _e:
            logger.warning("client_notes delete failed (%s) — retrying without it", _e)
            with db.transaction() as _conn:
                db.execute_query(_SQL_DELETE_BLOCKED_CLIENT, (phone,), conn=_conn)
                db.execute_query("DELETE FROM conversation_states WHERE phone_number = %s", (phone,), conn=_conn)

        logger.info("Permanently deleted client: %s", sanitize_log_value(phone))
        return jsonify({"success": True, "message": f"Client {sanitize_log_value(phone)} has been permanently deleted"})
    except Exception as e:
        logger.exception("Failed to delete client %s", sanitize_log_value(phone))
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route("/database/clear-database", methods=["POST"])
def clear_database():
    """Permanently delete ALL data: clients, bookings, conversation history."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    confirm = (request.form.get("confirm") or "").strip()
    if confirm != "DELETE ALL DATA":
        return jsonify({"success": False, "error": "Invalid confirmation. Type DELETE ALL DATA to confirm."}), 400

    try:
        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503

        # Clear in order: client_notes, upload_tokens, blocked_clients, conversation_states, message_history
        try:
            db.execute_query("DELETE FROM client_notes")
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            pass  # table may not exist
        try:
            db.execute_query("DELETE FROM upload_tokens")
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e)
            pass  # table may not exist
        db.execute_query("DELETE FROM blocked_clients")
        db.execute_query("DELETE FROM conversation_states")
        db.execute_query("DELETE FROM message_history")

        logger.info(
            "Cleared entire database (clients, bookings, conversation history, deposit upload tokens)"
        )
        return jsonify({
            "success": True,
            "message": (
                "Database cleared. All clients, bookings, conversation history, and deposit "
                "upload tokens have been permanently deleted."
            ),
        })
    except Exception as e:
        logger.exception("Error clearing database")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route("/database/clear-progress", methods=["POST"])
def clear_booking_progress():
    """Clear all booking progress data."""
    if not _is_database_authenticated():
        return jsonify({"success": False, "error": _JSON_ERR_NOT_AUTHENTICATED}), 401

    try:
        db = _get_db()
        if not db:
            return jsonify({"success": False, "error": _JSON_ERR_DB_UNAVAILABLE}), 503

        db.execute_query("DELETE FROM conversation_states")

        logger.info("Cleared all booking progress")
        return jsonify({"success": True, "message": "Booking progress cleared"})
    except Exception as e:
        logger.exception("Error clearing progress")
        return jsonify({"success": False, "error": "An internal error occurred"}), 500


@database_bp.route('/database/logout', methods=['GET', 'POST'])
def database_logout():
    """Logout from database page."""
    session.pop("database_authenticated", None)
    from flask import redirect, url_for
    return redirect(url_for('database.database_dashboard'))
