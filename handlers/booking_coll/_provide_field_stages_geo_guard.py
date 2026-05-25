"""COLLECTING-stage geo guard when client corrects they are in another city than escort Location."""

from __future__ import annotations

import re
from typing import Any

from handlers.booking_coll._provide_field_context import CollectingCtx
from templates.booking_collection_messages import build_wrong_city_outcall_abort_message

def _message_asserts_client_city_location(message: str) -> bool:
    """Require wording that sounds like stating/correcting where they are (vs asking about tours)."""
    m = (message or "").strip().lower()
    if not m:
        return False
    if re.match(r"^\s*no\b", m):
        return True
    if re.search(r"\bactually\b", m):
        return True
    if re.search(r"\bwrong\b", m):
        return True
    if re.search(r"\bi'?m\s+in\b", m):
        return True
    if re.search(r"\bim\s+in\b", m):
        return True
    if re.search(r"\bwe'?re\s+in\b", m):
        return True
    if re.search(r"\bnot\s+in\b", m):
        return True
    return False


def _cities_compatible_with_escort_base(escort_city: str, claimed_city: str) -> bool:
    """Allow suburbs that contain the metro name (e.g. East Perth vs Perth)."""
    e = escort_city.strip().lower()
    c = claimed_city.strip().lower()
    if not e or not c:
        return True
    if c == e:
        return True
    if c in e or e in c:
        return True
    return False


def _stage_outcall_wrong_city_correction(ctx: CollectingCtx) -> dict[str, Any] | None:
    """
    Client was progressing an outcall near escort Location but messages that they're
    actually in another Australian city — abort booking and offer touring SMS opt-in.
    """
    from config import get_effective_booking_city
    from handlers.touring_inquiry import extract_australian_city_from_message

    msg = (ctx.message or "").strip()
    if not _message_asserts_client_city_location(msg):
        return None

    claimed = extract_australian_city_from_message(msg)
    if not claimed:
        return None

    escort_city = (get_effective_booking_city() or "").strip()
    if not escort_city or _cities_compatible_with_escort_base(escort_city, claimed):
        return None

    fields = ctx.current_fields or {}
    if (fields.get("incall_outcall") or "").lower() != "outcall":
        return None
    if not (fields.get("outcall_address") or "").strip():
        return None

    client_name = (fields.get("client_name") or ctx.state.get("client_name") or "").strip()

    updates = {
        "date": None,
        "time": None,
        "duration": None,
        "experience_type": None,
        "incall_outcall": None,
        "outcall_address": None,
        "booking_type": None,
        "booking_status": None,
        "escort_supply_source": None,
        "offered_slot_hours": None,
        "offered_slot_minutes": None,
        "offered_slot_date": None,
        "offered_slot_dates": None,
        "available_now_requested": False,
        "arrival_time_minutes": None,
        "last_touring_inquiry_city": claimed,
    }
    if client_name:
        updates["client_name"] = client_name

    body = build_wrong_city_outcall_abort_message(
        client_name=client_name,
        escort_city=escort_city,
        claimed_city=claimed,
    )
    return {"messages": [body], "new_state": "NEW", "updates": updates, "actions": []}

