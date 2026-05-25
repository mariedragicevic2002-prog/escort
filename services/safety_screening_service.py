"""Safety screening services for flagged client numbers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import config
from core.settings_manager import get_setting, set_setting
from services.database_service import get_shared_db
from utils.phone_normalization import extract_normalized_au_mobile
from utils.log_sanitize import sanitize_log_value

logger = logging.getLogger("adella_chatbot.safety_screening")


def _parse_optional_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None

    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        if "." in text:
            as_float = float(text)
            return int(as_float) if as_float.is_integer() else None
        return int(text)
    except (TypeError, ValueError):
        return None


def _setting_bool(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in ("1", "true", "yes", "on")


def is_screening_enabled() -> bool:
    return bool(config.safety_screening_is_enabled())


def get_screening_mode() -> str:
    mode = (config.get_safety_screening_mode() or "warn_only").strip().lower()
    return "auto_block" if mode == "auto_block" else "warn_only"


def get_watchlist_stats() -> dict[str, object]:
    db = get_shared_db(config.DATABASE_URL)
    count = 0
    if db:
        try:
            rows = db.execute_query(
                "SELECT COUNT(*) AS c FROM safety_screening_watchlist WHERE is_active = TRUE",
                fetch=True,
            ) or []
            if rows:
                from utils.row_utils import row_get
                count = int(row_get(rows[0], 'c', 0) or 0)
        except Exception as e:
            logger.warning("Could not fetch safety watchlist count: %s", e)

    return {
        "count": count,
        "last_uploaded_at": (get_setting("safety_screening_last_uploaded_at") or "").strip(),
        "last_uploaded_filename": (get_setting("safety_screening_last_uploaded_filename") or "").strip(),
    }


def replace_watchlist(numbers: list[tuple], filename: str = "") -> dict[str, int]:
    """
    Replace active watchlist rows with supplied normalized numbers.

    Args:
        numbers: List of:
            - (normalized_phone, raw_value)
            - (normalized_phone, raw_value, warning_recency_rank, report_count)
        filename: Optional source filename for UI display
    """
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        raise RuntimeError("Database connection unavailable")

    unique_rows: dict[str, dict[str, object]] = {}
    for row in numbers:
        if not row:
            continue
        from utils.row_utils import row_get
        normalized = row_get(row, 0, "")
        raw = row_get(row, 1, normalized)
        warning_recency_rank = _parse_optional_int(row_get(row, 2, None))
        if warning_recency_rank is not None and warning_recency_rank <= 0:
            warning_recency_rank = None
        report_count = _parse_optional_int(row_get(row, 3, None))
        if report_count is None or report_count < 0:
            report_count = 0

        normalized_val = extract_normalized_au_mobile(normalized)
        if not normalized_val:
            continue
        if normalized_val not in unique_rows:
            unique_rows[normalized_val] = {
                "raw_phone": str(raw or "").strip(),
                "warning_recency_rank": warning_recency_rank,
                "report_count": report_count,
            }

    conn = None
    cursor = None
    from_pool = False
    inserted = 0
    try:
        conn, from_pool = db.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM safety_screening_watchlist")
        if unique_rows:
            payload = [
                (
                    phone,
                    str(data.get("raw_phone") or "").strip(),
                    int(str(data.get("warning_recency_rank") or 0)),
                    int(str(data.get("report_count") or 0)),
                )
                for phone, data in unique_rows.items()
            ]
            cursor.executemany(
                """
                INSERT INTO safety_screening_watchlist (
                    normalized_phone,
                    raw_phone,
                    source_label,
                    is_active,
                    warning_recency_rank,
                    report_count
                )
                VALUES (%s, %s, %s, TRUE, %s, %s)
                ON CONFLICT (normalized_phone)
                DO UPDATE SET
                    raw_phone = EXCLUDED.raw_phone,
                    source_label = EXCLUDED.source_label,
                    is_active = TRUE,
                    warning_recency_rank = EXCLUDED.warning_recency_rank,
                    report_count = EXCLUDED.report_count,
                    updated_at = CURRENT_TIMESTAMP
                """,
                [(phone, raw, "config_excel_upload", warning_recency_rank, report_count) for phone, raw, warning_recency_rank, report_count in payload],
            )
            inserted = len(payload)
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            db.return_connection(conn, from_pool)

    now_iso = datetime.now(timezone.utc).isoformat()
    set_setting("safety_screening_last_uploaded_at", now_iso)
    set_setting("safety_screening_last_uploaded_filename", (filename or "").strip())
    set_setting("safety_screening_watchlist_count", str(inserted))

    return {"inserted": inserted}


def lookup_flagged_number(phone_number: str) -> dict[str, object]:
    normalized = extract_normalized_au_mobile(phone_number)
    if not normalized:
        return {"matched": False, "normalized_phone": ""}

    db = get_shared_db(config.DATABASE_URL)
    if not db:
        return {"matched": False, "normalized_phone": normalized}

    try:
        rows = db.execute_query(
            """
            SELECT normalized_phone, raw_phone, warning_recency_rank, report_count
            FROM safety_screening_watchlist
            WHERE normalized_phone = %s AND is_active = TRUE
            LIMIT 1
            """,
            (normalized,),
            fetch=True,
        ) or []
        if rows:
            from utils.row_utils import row_get
            warning_recency_rank = _parse_optional_int(row_get(rows[0], 'warning_recency_rank'))
            report_count = _parse_optional_int(row_get(rows[0], 'report_count'))
            return {
                "matched": True,
                "normalized_phone": normalized,
                "raw_phone": row_get(rows[0], 'raw_phone', "") or "",
                "warning_recency_rank": warning_recency_rank,
                "report_count": max(0, int(report_count or 0)),
            }
    except Exception as e:
        logger.warning("Safety screening lookup failed for %s: %s", sanitize_log_value(phone_number), e)
    return {"matched": False, "normalized_phone": normalized}


def should_notify_escort(normalized_phone: str, cooldown_hours: int = 12) -> bool:
    if not normalized_phone:
        return True
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        return True
    try:
        rows = db.execute_query(
            """
            SELECT 1
            FROM safety_screening_match_log
            WHERE normalized_phone = %s
              AND escort_notified = TRUE
              AND created_at >= (CURRENT_TIMESTAMP - (%s || ' hours')::interval)
            LIMIT 1
            """,
            (normalized_phone, str(max(1, int(cooldown_hours)))),
            fetch=True,
        ) or []
        return not bool(rows)
    except Exception as e:
        logger.warning(
            "Safety alert cooldown check failed for %s: %s",
            sanitize_log_value(normalized_phone),
            e,
        )
        return True


def log_match(
    *,
    phone_number: str,
    normalized_phone: str,
    matched: bool,
    action_taken: str,
    escort_notified: bool = False,
    note: str = "",
) -> None:
    db = get_shared_db(config.DATABASE_URL)
    if not db:
        return
    try:
        db.execute_query(
            """
            INSERT INTO safety_screening_match_log
                (phone_number, normalized_phone, matched, action_taken, escort_notified, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                (phone_number or "").strip(),
                (normalized_phone or "").strip(),
                bool(matched),
                (action_taken or "warn_only").strip(),
                bool(escort_notified),
                (note or "").strip()[:500],
            ),
            fetch=False,
        )
    except Exception as e:
        logger.warning(
            "Failed to log safety screening match for %s: %s",
            sanitize_log_value(phone_number),
            e,
        )

