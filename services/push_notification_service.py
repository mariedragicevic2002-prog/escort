"""
Push notification service for mobile booking alerts.

Supports:
- Expo push tokens (Expo push gateway)
- Native FCM tokens (Firebase HTTP v1, when service-account creds are available)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

import config
from core.settings_manager import get_setting
from utils.row_utils import row_get

logger = logging.getLogger("adella_chatbot.push_notifications")

_EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
_FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_REMINDER_MINUTES = (60, 30, 10)
_REMINDER_WINDOW_MINUTES = 5
_PUSH_NOTIFIABLE_STATUSES = {"confirmed", "reschedule-confirmed", "reserved"}

_fcm_cached_access_token: str | None = None
_fcm_cached_expiry: datetime | None = None
_fcm_cached_project_id: str | None = None


def _ensure_push_schema(db) -> None:
    db.execute_query(
        """
        CREATE TABLE IF NOT EXISTS push_device_tokens (
            id BIGSERIAL PRIMARY KEY,
            token TEXT NOT NULL UNIQUE,
            token_type VARCHAR(10) NOT NULL CHECK (token_type IN ('expo', 'fcm')),
            platform VARCHAR(20) NOT NULL DEFAULT 'android',
            provider VARCHAR(20) NOT NULL DEFAULT 'fcm',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_error TEXT
        )
        """,
        fetch=False,
    )
    db.execute_query(
        "CREATE INDEX IF NOT EXISTS idx_push_device_tokens_active ON push_device_tokens(active, token_type)",
        fetch=False,
    )
    db.execute_query(
        """
        CREATE TABLE IF NOT EXISTS push_delivery_log (
            id BIGSERIAL PRIMARY KEY,
            booking_id TEXT NOT NULL,
            notification_type VARCHAR(32) NOT NULL,
            reminder_minutes INTEGER,
            token_hash VARCHAR(64) NOT NULL,
            delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """,
        fetch=False,
    )
    db.execute_query(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_push_delivery_dedupe
        ON push_delivery_log (booking_id, notification_type, COALESCE(reminder_minutes, -1), token_hash)
        """,
        fetch=False,
    )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _safe_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        try:
            k = str(key).strip()[:80]
            if not k:
                continue
            if value is None:
                continue
            if isinstance(value, (dict, list, tuple)):
                cleaned[k] = value
            else:
                cleaned[k] = str(value)[:500]
        except Exception:
            continue
    return cleaned


def _upsert_token(
    db,
    *,
    token: str,
    token_type: str,
    platform: str,
    provider: str,
    metadata: dict[str, Any] | None,
) -> None:
    db.execute_query(
        """
        INSERT INTO push_device_tokens (token, token_type, platform, provider, metadata, active, last_seen_at, updated_at)
        VALUES (%s, %s, %s, %s, %s::jsonb, TRUE, NOW(), NOW())
        ON CONFLICT (token)
        DO UPDATE SET
            token_type = EXCLUDED.token_type,
            platform = EXCLUDED.platform,
            provider = EXCLUDED.provider,
            metadata = EXCLUDED.metadata,
            active = TRUE,
            last_error = NULL,
            last_seen_at = NOW(),
            updated_at = NOW()
        """,
        (
            token,
            token_type,
            platform,
            provider,
            json.dumps(_safe_metadata(metadata)),
        ),
        fetch=False,
    )


def register_push_device_token(
    db,
    *,
    platform: str,
    provider: str,
    expo_push_token: str = "",
    fcm_token: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    """Register/refresh push tokens for the authenticated schedule API client."""
    _ensure_push_schema(db)

    count = 0
    expo_push_token = (expo_push_token or "").strip()
    fcm_token = (fcm_token or "").strip()

    if expo_push_token:
        _upsert_token(
            db,
            token=expo_push_token,
            token_type="expo",
            platform=platform,
            provider=provider,
            metadata=metadata,
        )
        count += 1

    if fcm_token:
        _upsert_token(
            db,
            token=fcm_token,
            token_type="fcm",
            platform=platform,
            provider=provider,
            metadata=metadata,
        )
        count += 1

    return count


def _list_active_tokens(db) -> list[dict[str, Any]]:
    rows = db.execute_query(
        """
        SELECT token, token_type, platform, provider, metadata
        FROM push_device_tokens
        WHERE active = TRUE
        ORDER BY updated_at DESC
        """,
        fetch=True,
    ) or []
    # Prefer native FCM tokens to avoid double-delivery (Expo + FCM both firing)
    fcm_rows = [r for r in rows if str(row_get(r, "token_type") or "").lower() == "fcm"]
    if fcm_rows:
        return fcm_rows
    return rows


def _delivery_exists(
    db,
    *,
    booking_id: str,
    notification_type: str,
    reminder_minutes: int | None,
    token_hash: str,
) -> bool:
    rows = db.execute_query(
        """
        SELECT 1
        FROM push_delivery_log
        WHERE booking_id = %s
          AND notification_type = %s
          AND COALESCE(reminder_minutes, -1) = COALESCE(%s, -1)
          AND token_hash = %s
        LIMIT 1
        """,
        (booking_id, notification_type, reminder_minutes, token_hash),
        fetch=True,
    ) or []
    return bool(rows)


def _mark_delivery(
    db,
    *,
    booking_id: str,
    notification_type: str,
    reminder_minutes: int | None,
    token_hash: str,
) -> None:
    db.execute_query(
        """
        INSERT INTO push_delivery_log (booking_id, notification_type, reminder_minutes, token_hash)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (booking_id, notification_type, reminder_minutes, token_hash),
        fetch=False,
    )


def _mark_token_error(db, token: str, error_text: str, *, deactivate: bool = False) -> None:
    db.execute_query(
        """
        UPDATE push_device_tokens
        SET last_error = %s,
            active = CASE WHEN %s THEN FALSE ELSE active END,
            updated_at = NOW()
        WHERE token = %s
        """,
        ((error_text or "")[:1000], deactivate, token),
        fetch=False,
    )


def _send_expo_push(token: str, title: str, body: str, data: dict[str, str], *, channel_id: str = "") -> tuple[bool, str, bool]:
    payload: dict[str, Any] = {
        "to": token,
        "title": title,
        "body": body,
        "data": data,
        "priority": "high",
    }
    if channel_id:
        payload["channelId"] = channel_id
    else:
        payload["sound"] = "default"
    payload["categoryId"] = "BOOKING_REMINDER"
    try:
        response = requests.post(_EXPO_PUSH_URL, json=payload, timeout=8)
        if response.status_code >= 400:
            return False, f"expo_http_{response.status_code}: {response.text[:300]}", False
        result = response.json() if response.content else {}
        entry = result.get("data") if isinstance(result, dict) else None
        if isinstance(entry, dict):
            if entry.get("status") == "ok":
                return True, "", False
            details = entry.get("details") or {}
            error_name = str(entry.get("message") or entry.get("error") or details.get("error") or "expo_push_error")
            deactivate = "DeviceNotRegistered" in error_name
            return False, error_name[:300], deactivate
        return False, "expo_push_invalid_response", False
    except Exception as e:
        return False, f"expo_exception: {e}", False


def _load_sa_from_firebase_env() -> "dict | None":
    firebase_env = os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip()
    if not firebase_env:
        return None
    try:
        parsed = json.loads(firebase_env) if firebase_env.startswith("{") else None
        if parsed is None and os.path.isfile(firebase_env):
            with open(firebase_env, encoding="utf-8") as f:
                parsed = json.load(f)
        if isinstance(parsed, dict):
            return parsed
    except Exception as e:
        logger.warning("Failed to parse FIREBASE_CREDENTIALS_JSON: %s", e)
    return None


def _load_sa_from_firebase_file() -> "dict | None":
    firebase_file = os.path.join(config.BASE_DIR, "firebase_credentials.json")
    if not os.path.isfile(firebase_file):
        return None
    try:
        with open(firebase_file, encoding="utf-8") as f:
            parsed = json.load(f)
        if isinstance(parsed, dict):
            return parsed
    except Exception as e:
        logger.warning("Failed to read firebase_credentials.json: %s", e)
    return None


def _load_sa_from_legacy_env() -> "dict | None":
    raw_env = (config.CREDENTIALS_JSON_ENV or "").strip()
    if not raw_env:
        return None
    try:
        if raw_env.startswith("{"):
            parsed = json.loads(raw_env)
            if isinstance(parsed, dict):
                return parsed
        if os.path.isfile(raw_env):
            with open(raw_env, encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                return parsed
    except Exception as e:
        logger.warning("Failed to parse CREDENTIALS_JSON for FCM: %s", e)
    return None


def _load_sa_from_sa_file() -> "dict | None":
    if not (config.SERVICE_ACCOUNT_FILE and os.path.isfile(config.SERVICE_ACCOUNT_FILE)):
        return None
    try:
        with open(config.SERVICE_ACCOUNT_FILE, encoding="utf-8") as f:
            parsed = json.load(f)
        if isinstance(parsed, dict):
            return parsed
    except Exception as e:
        logger.warning("Failed to read service-account file for FCM: %s", e)
    return None


def _load_service_account_info() -> dict[str, Any] | None:
    """Load Firebase service account credentials.

    Priority:
    1. FIREBASE_CREDENTIALS_JSON env var (JSON string or file path) — Firebase-specific
    2. firebase_credentials.json file in BASE_DIR — Firebase-specific
    3. CREDENTIALS_JSON env var — legacy fallback (may be a non-Firebase SA)
    4. credentials.json (SERVICE_ACCOUNT_FILE) — legacy fallback
    """
    return (
        _load_sa_from_firebase_env()
        or _load_sa_from_firebase_file()
        or _load_sa_from_legacy_env()
        or _load_sa_from_sa_file()
    )


def _get_fcm_access_token_and_project_id() -> tuple[str, str] | None:
    global _fcm_cached_access_token, _fcm_cached_expiry, _fcm_cached_project_id

    now = datetime.now(timezone.utc)
    if _fcm_cached_access_token and _fcm_cached_expiry and _fcm_cached_project_id:
        if _fcm_cached_expiry - timedelta(minutes=2) > now:
            return _fcm_cached_access_token, _fcm_cached_project_id

    info = _load_service_account_info()
    if not info:
        return None

    project_id = (
        (get_setting("firebase_project_id") or "").strip()
        or str(info.get("project_id") or "").strip()
    )
    if not project_id:
        logger.warning("FCM send skipped: firebase_project_id/project_id not configured")
        return None

    try:
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[_FCM_SCOPE],
        )
        creds.refresh(GoogleRequest())
        token = (creds.token or "").strip()
        if not token:
            return None

        expiry = creds.expiry
        if expiry is None:
            expiry = now + timedelta(minutes=50)
        elif expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)

        _fcm_cached_access_token = token
        _fcm_cached_expiry = expiry
        _fcm_cached_project_id = project_id
        return token, project_id
    except Exception as e:
        logger.warning("FCM auth unavailable: %s", e)
        return None


def _send_fcm_push(token: str, title: str, body: str, data: dict[str, str], *, channel_id: str = "") -> tuple[bool, str, bool]:
    auth = _get_fcm_access_token_and_project_id()
    if not auth:
        return False, "fcm_auth_unavailable", False
    access_token, project_id = auth
    endpoint = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    android_config: dict[str, Any] = {"priority": "HIGH"}
    if channel_id:
        android_config["notification"] = {"channel_id": channel_id}

    payload = {
        "message": {
            "token": token,
            "notification": {
                "title": title,
                "body": body,
            },
            "data": {str(k): str(v) for k, v in {**data, "categoryIdentifier": "BOOKING_REMINDER"}.items()},
            "android": android_config,
        }
    }

    try:
        response = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
            timeout=8,
        )
        if response.status_code in (200, 201):
            return True, "", False
        text = (response.text or "")[:400]
        deactivate = "UNREGISTERED" in text or "registration-token-not-registered" in text
        return False, f"fcm_http_{response.status_code}: {text}", deactivate
    except Exception as e:
        return False, f"fcm_exception: {e}", False


def _send_token(
    *,
    token: str,
    token_type: str,
    title: str,
    body: str,
    data: dict[str, str],
    channel_id: str = "",
) -> tuple[bool, str, bool]:
    if token_type == "expo":
        return _send_expo_push(token, title, body, data, channel_id=channel_id)
    if token_type == "fcm":
        return _send_fcm_push(token, title, body, data, channel_id=channel_id)
    return False, f"unsupported_token_type:{token_type}", False


def _extract_sound_id_from_channel(channel_id: str) -> str:
    """Extract soundId from channel ID format: bookings-{type1}-{type2}-{soundId}-v{n}"""
    parts = (channel_id or "").split("-")
    # Format: bookings | incall/outcall | new/reminder | soundId | v5
    if len(parts) == 5 and parts[0] == "bookings" and parts[-1].startswith("v"):
        return parts[3]
    return ""


def _format_booking_time(start_time) -> str:
    try:
        import pytz

        tz = pytz.timezone(config.get_effective_escort_timezone())
    except Exception:
        tz = timezone.utc

    try:
        if hasattr(start_time, "astimezone"):
            local_start = start_time.astimezone(tz)
        else:
            local_start = start_time
        return local_start.strftime("%I:%M%p")
    except Exception:
        return str(start_time)


def _resolve_token_channel_info(metadata: dict, btype: str, is_reminder: bool) -> tuple[str, str]:
    """Return (channel_id, normalized_type) for a push token based on booking type."""
    if "incall" in btype:
        if is_reminder:
            channel_id = str(metadata.get("incallReminderChannelId") or metadata.get("incallChannelId") or metadata.get("channelId") or "")
        else:
            channel_id = str(metadata.get("incallChannelId") or metadata.get("channelId") or "")
        return channel_id, "incall"
    elif "outcall" in btype:
        if is_reminder:
            channel_id = str(metadata.get("outcallReminderChannelId") or metadata.get("outcallChannelId") or metadata.get("channelId") or "")
        else:
            channel_id = str(metadata.get("outcallChannelId") or metadata.get("channelId") or "")
        return channel_id, "outcall"
    else:
        return str(metadata.get("channelId") or ""), btype


def _handle_single_push(
    db, *, token: str, token_type: str, booking_id: str, notification_type: str,
    reminder_minutes, title: str, body: str, token_data: dict, channel_id: str,
) -> int:
    """Send to one token, log result, mark delivery. Returns 1 on success, 0 on failure."""
    ok, error_text, deactivate = _send_token(
        token=token, token_type=token_type, title=title, body=body,
        data=token_data, channel_id=channel_id,
    )
    token_preview = token[:12] + "…" + token[-6:] if len(token) > 20 else token
    if ok:
        logger.info("Push sent OK [%s/%s] booking=%s type=%s", token_type, token_preview, booking_id, notification_type)
        _mark_delivery(db, booking_id=booking_id, notification_type=notification_type,
                       reminder_minutes=reminder_minutes, token_hash=_token_hash(token))
        return 1
    logger.warning("Push FAILED [%s/%s] booking=%s type=%s error=%s deactivate=%s",
                   token_type, token_preview, booking_id, notification_type, error_text, deactivate)
    _mark_token_error(db, token, error_text, deactivate=deactivate)
    return 0


def _send_booking_push(
    db,
    *,
    booking_id: str,
    notification_type: str,
    reminder_minutes: int | None,
    title: str,
    body: str,
    data: dict[str, str],
    booking_type: str = "",
) -> int:
    btype = booking_type.lower().strip()
    is_reminder = notification_type == "reminder"
    sent = 0
    for row in _list_active_tokens(db):
        token = str(row_get(row, "token") or "").strip()
        token_type = str(row_get(row, "token_type") or "").strip().lower()
        if not token or token_type not in {"expo", "fcm"}:
            continue

        metadata = row_get(row, "metadata") or {}
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        channel_id, normalized_type = _resolve_token_channel_info(metadata, btype, is_reminder)
        sound_id = _extract_sound_id_from_channel(channel_id)
        token_data = dict(data)
        token_data["bookingType"] = normalized_type
        if sound_id:
            token_data["soundId"] = sound_id

        if _delivery_exists(db, booking_id=booking_id, notification_type=notification_type,
                            reminder_minutes=reminder_minutes, token_hash=_token_hash(token)):
            continue

        sent += _handle_single_push(
            db, token=token, token_type=token_type, booking_id=booking_id,
            notification_type=notification_type, reminder_minutes=reminder_minutes,
            title=title, body=body, token_data=token_data, channel_id=channel_id,
        )

    return sent


def send_deposit_paid_push_for_booking_id(db, booking_id: str) -> int:
    """Send a 'deposit paid' push when a reserved/peacock booking transitions to confirmed/basil."""
    _ensure_push_schema(db)
    rows = db.execute_query(
        """
        SELECT id, start_time, client_name, type, experience
        FROM bookings
        WHERE id = %s
        LIMIT 1
        """,
        (booking_id,),
        fetch=True,
    ) or []
    if not rows:
        return 0

    row = rows[0]
    b_id = str(row_get(row, "id") or booking_id)
    client = str(row_get(row, "client_name") or "Client")
    booking_type = str(row_get(row, "type") or "booking")
    experience = str(row_get(row, "experience") or "").strip()
    start_time = row_get(row, "start_time")
    time_str = _format_booking_time(start_time)
    subtitle = experience or booking_type or "Booking"

    title = "💰 Deposit Paid — Booking Confirmed"
    body = f"{client} — {subtitle} at {time_str}"
    data = {
        "bookingId": b_id,
        "kind": "deposit-paid",
    }
    btype = booking_type.lower().strip()
    return _send_booking_push(
        db,
        booking_id=b_id,
        notification_type="deposit-paid",
        reminder_minutes=None,
        title=title,
        body=body,
        data=data,
        booking_type=btype,
    )


def send_new_booking_push_for_booking_id(db, booking_id: str) -> int:
    """Send immediate push notifications for one booking ID."""
    _ensure_push_schema(db)
    rows = db.execute_query(
        """
        SELECT id, start_time, client_name, type, experience, status
        FROM bookings
        WHERE id = %s
        LIMIT 1
        """,
        (booking_id,),
        fetch=True,
    ) or []
    if not rows:
        return 0

    row = rows[0]
    status = str(row_get(row, "status") or "").strip().lower()
    if status not in _PUSH_NOTIFIABLE_STATUSES:
        return 0

    b_id = str(row_get(row, "id") or booking_id)
    client = str(row_get(row, "client_name") or "Client")
    booking_type = str(row_get(row, "type") or "booking")
    experience = str(row_get(row, "experience") or "").strip()
    start_time = row_get(row, "start_time")
    time_str = _format_booking_time(start_time)
    subtitle = experience or booking_type or "Booking"

    title = "📅 New Booking"
    body = f"{client} — {subtitle} at {time_str}"
    data = {
        "bookingId": b_id,
        "kind": "new-booking",
    }
    btype = booking_type.lower().strip()
    return _send_booking_push(
        db,
        booking_id=b_id,
        notification_type="new-booking",
        reminder_minutes=None,
        title=title,
        body=body,
        data=data,
        booking_type=btype,
    )


def _send_new_booking_pushes(db_service) -> int:
    """Send 'new booking' push for any booking created in the last 15 minutes."""
    try:
        recent_rows = db_service.execute_query(
            """
            SELECT id
            FROM bookings
            WHERE created_at >= (NOW() - INTERVAL '15 minutes')
              AND status IN ('confirmed', 'reschedule-confirmed', 'reserved')
            ORDER BY created_at DESC
            LIMIT 200
            """,
            fetch=True,
        ) or []
        sent = 0
        for row in recent_rows:
            booking_id = str(row_get(row, "id") or "").strip()
            if booking_id:
                sent += send_new_booking_push_for_booking_id(db_service, booking_id)
        return sent
    except Exception as e:
        logger.warning("new-booking push pass failed: %s", e)
        return 0


def _send_reminder_push_for_row(db_service, row, now_utc) -> int:
    """Send reminder pushes for a single upcoming booking row."""
    booking_id = str(row_get(row, "id") or "").strip()
    start_time = row_get(row, "start_time")
    if not booking_id or not start_time:
        return 0
    try:
        if getattr(start_time, "tzinfo", None) is None:
            start_utc = start_time.replace(tzinfo=timezone.utc)
        else:
            start_utc = start_time.astimezone(timezone.utc)
        minutes_to_start = int((start_utc - now_utc).total_seconds() // 60)
    except Exception:
        return 0

    client = str(row_get(row, "client_name") or "Client")
    booking_type = str(row_get(row, "type") or "booking")
    btype = booking_type.lower().strip()
    experience = str(row_get(row, "experience") or "").strip()
    subtitle = experience or booking_type or "Booking"
    time_str = _format_booking_time(start_time)
    sent = 0
    for target in _REMINDER_MINUTES:
        if 0 <= target - minutes_to_start < _REMINDER_WINDOW_MINUTES:
            label = "1 hour" if target == 60 else f"{target} minutes"
            sent += _send_booking_push(
                db_service,
                booking_id=booking_id,
                notification_type="reminder",
                reminder_minutes=target,
                title=f"Booking in {label}",
                body=f"{client} — {subtitle} at {time_str}",
                data={"bookingId": booking_id, "kind": "reminder", "reminderMinutes": str(target)},
                booking_type=btype,
            )
    return sent


def check_and_send_booking_push_notifications(db_service) -> int:
    """
    Periodic server-side push sender for closed-app notifications.

    - Sends "new booking" push shortly after booking creation
    - Sends 60/30/10 minute reminders for upcoming active bookings
    """
    try:
        _ensure_push_schema(db_service)
    except Exception as e:
        logger.warning("push schema ensure failed: %s", e)
        return 0

    sent_total = _send_new_booking_pushes(db_service)

    try:
        rows = db_service.execute_query(
            """
            SELECT id, start_time, client_name, type, experience, status
            FROM bookings
            WHERE start_time > NOW()
              AND start_time <= (NOW() + INTERVAL '65 minutes')
              AND status IN ('confirmed', 'reschedule-confirmed', 'reserved')
            ORDER BY start_time ASC
            LIMIT 500
            """,
            fetch=True,
        ) or []
    except Exception as e:
        logger.warning("reminder push query failed: %s", e)
        return sent_total

    now_utc = datetime.now(timezone.utc)
    for row in rows:
        sent_total += _send_reminder_push_for_row(db_service, row, now_utc)

    return sent_total
