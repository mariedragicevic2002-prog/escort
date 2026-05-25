"""
Atomic deduplication for httpSMS inbound message_id (retries / slow responses).

Uses INSERT ... ON CONFLICT DO NOTHING — safe under concurrency (unlike get_setting + set_setting).

Also handles IntegrityError when ON CONFLICT is unavailable or a race hits the unique index
before the conflict clause applies.

**Id-less payloads:** When the gateway omits ``message_id`` / ``id`` but includes a
**provider timestamp** (``received_at``, ``created_at``, ``timestamp``, etc.), we derive a
stable synthetic key from ``phone + timestamp + body``. We do **not** hash body alone — that
would block two legitimate identical texts back-to-back.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from psycopg2 import OperationalError
from psycopg2 import errors as pg_errors

if TYPE_CHECKING:
    from services.database_service import DatabaseService

logger = logging.getLogger("adella_chatbot.httpsms_dedup")

# Fields that may carry a per-message time from the provider (first non-empty wins).
_TIMESTAMP_FIELD_NAMES = (
    "received_at",
    "created_at",
    "timestamp",
    "sent_at",
    "delivered_at",
    "date",
)


def _first_timestamp(*dicts: dict | None) -> str:
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for key in _TIMESTAMP_FIELD_NAMES:
            v = d.get(key)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                return s[:512]
    return ""


def build_inbound_dedup_key(
    msg_data: dict | None,
    payload: dict | None,
    *,
    phone_number: str = "",
    message_body: str = "",
) -> str:
    """
    Stable provider id for the same logical inbound SMS (retries must reuse the same value).

    1. Prefer explicit ids: ``message_id``, ``id``, ``sms_id`` (several payload shapes).
    2. Else if a **provider timestamp** and **phone** are present: synthetic ``tsfb:`` + SHA-256
       of ``phone|timestamp|body`` (retries must resend the same timestamp).
    3. Else return "" (no dedupe key — observability should count ``webhook_dedup_key_missing``).
    """
    msg_data = msg_data if isinstance(msg_data, dict) else {}
    payload = payload if isinstance(payload, dict) else {}
    nested = payload.get("data")
    nested_d: dict = nested if isinstance(nested, dict) else {}

    candidates = [
        msg_data.get("message_id"),
        msg_data.get("id"),
        msg_data.get("sms_id"),
        nested_d.get("message_id"),
        nested_d.get("id"),
        payload.get("message_id"),
        payload.get("id"),
    ]
    for c in candidates:
        if c is None:
            continue
        s = str(c).strip()
        if s:
            return s[:2048]

    ts = _first_timestamp(msg_data, nested_d, payload)
    phone = (phone_number or "").strip()
    if ts and phone:
        norm = f"{ts}|{phone}|{(message_body or '').strip()}"
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()
        return f"tsfb:{digest}"

    return ""


def try_claim_httpsms_message_id(db: "DatabaseService | None", message_id: str) -> bool:
    """
    Record this message_id if new.

    Returns:
        True if this is the first time we see this id (caller should process the message).
        False if duplicate (caller should skip).

    Raises:
        OperationalError: Transient DB failure — caller should return 503 so the gateway retries.
    """
    if not db or not (message_id or "").strip():
        return True
    mid = (message_id or "").strip()
    if len(mid) > 2048:
        mid = mid[-2048:]
    try:
        rows = db.execute_query(
            """
            INSERT INTO httpsms_message_dedup (message_id)
            VALUES (%s)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING message_id
            """,
            (mid,),
            fetch=True,
        )
        return bool(rows)
    except pg_errors.UniqueViolation:
        # Rare without ON CONFLICT (e.g. migration lag); treat as duplicate, not as "allow".
        return False
    except OperationalError:
        # Pool/connection/timeouts — do not process without a successful claim (avoids dup under load).
        raise
    except Exception as e:
        logger.exception("httpsms dedup insert failed (fail-closed): %s", e)
        raise
