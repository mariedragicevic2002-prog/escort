"""List calendar events in a window and read event color."""

from utils.log_sanitize import LOG_SUPPRESSED_FMT


import logging

import pytz

from config import (
    ADELLA_CALENDAR_SOFT_HOLD_MARKER,
    COLOR_BANANA,
    COLOR_GRAPHITE,
    COLOR_LAVENDER,
    COLOR_TOMATO,
    get_google_calendar_id,
)

logger = logging.getLogger(__name__)

# Public webform + cached conflict checks: only these two colours are "empty" (fully bookable over).
# All other event colours block the slot, including:
#   - BANANA (5): manual maintenance (hair, nails, etc.) from admin schedule
#   - TOMATO (10): manual social / personal from admin schedule
#   - Basil, Peacock, Grape, and any other colorId
WEBFORM_SOFT_HOLD_COLOR_IDS = frozenset((COLOR_GRAPHITE, COLOR_LAVENDER))
# Intentional hard blocks (subset of "not in SOFT_HOLD"); kept for docs/tests so Banana/Tomato are not conflated with soft holds.
WEBFORM_MANUAL_SCHEDULE_BLOCKING_COLOR_IDS: frozenset[str] = frozenset(
    (COLOR_BANANA, COLOR_TOMATO)
)


def normalize_calendar_color_id(event: dict | None) -> str | None:
    """Return canonical Google Calendar event colorId (1-11 as a decimal string), or None if unset.

    The Calendar API may return ``colorId`` as an int, a string, or a zero-padded string (e.g.
    ``\"08\"``). Config constants use unpadded strings (``\"8\"`` for Graphite). Normalizing
    avoids false mismatches in conflict checks and webform booked-time filtering.
    """
    if not event:
        return None
    raw = event.get("colorId")
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        try:
            return str(int(raw))
        except (TypeError, ValueError, OverflowError):
            return None
    s = str(raw).strip()
    if not s:
        return None
    if s.isdigit():
        return str(int(s, 10))
    try:
        return str(int(float(s)))
    except (TypeError, ValueError):
        return s


def is_webform_non_blocking_calendar_event(event: dict | None) -> bool:
    """True only for **Graphite** and **Lavender** style soft holds: times behave as if nothing were booked.

    **Policy:** The public booking webform and cached calendar overlap logic treat the schedule
    as **unavailable** for Basil (confirmed), Peacock (reserved), Grape (confirmed travel and
    travel buffers), and **every** other Google Calendar ``colorId``, including
    :data:`WEBFORM_MANUAL_SCHEDULE_BLOCKING_COLOR_IDS` (Banana = maintenance, Tomato = social).
    Only :data:`WEBFORM_SOFT_HOLD_COLOR_IDS` (Graphite = pending deposit, Lavender =
    pending travel) is treated as fully available. Missing ``colorId`` is never "free" on its
    own—we only return True when text/markers show the event is one of those two holds (see
    below).

    ``travel_blocks`` sets ``transparency: transparent`` on Lavender so free/busy matches this.

    **Fallback** when the API omits ``colorId``: match bot-written markers
    :data:`config.ADELLA_CALENDAR_SOFT_HOLD_MARKER` and graphite/pending heuristics from
    ``event_crud`` / schedule UI. Confirmed travel (GRAPE) does not use the soft-hold marker;
    title-only "Travel time" without the marker does **not** count as free.
    """
    if not event:
        return False
    cid = normalize_calendar_color_id(event)
    if cid in WEBFORM_SOFT_HOLD_COLOR_IDS:
        return True
    if cid:
        return False

    summary_raw = (event.get("summary") or "").strip()
    summary_l = summary_raw.lower()
    desc_l = (event.get("description") or "").lower()

    # Bot-written marker (see config.ADELLA_CALENDAR_SOFT_HOLD_MARKER) — survives missing colorId
    if ADELLA_CALENDAR_SOFT_HOLD_MARKER in desc_l:
        return True

    # travel_blocks: without colorId, do not block on summary alone — GRAPE (confirmed) also uses
    # "Travel there" / "Client travel back" titles; only LAVENDER pending has the soft-hold marker.
    dash_norm = summary_l.replace("—", "-").replace("–", "-")
    if (dash_norm.startswith("travel time") or dash_norm.startswith("travel there")) and (
        ADELLA_CALENDAR_SOFT_HOLD_MARKER in desc_l
    ):
        return True
    # Legacy: dinner template used "calendar block (lavender)" in the first line for both colours;
    # GRAPE does not get the soft-hold marker — require marker so confirmed travel still blocks.
    if "calendar block (lavender)" in desc_l and ADELLA_CALENDAR_SOFT_HOLD_MARKER in desc_l:
        return True
    # event_crud graphite / schedule pending (avoid matching "deposit paid" on confirmed lines)
    if "pending deposit" in summary_l and "paid" not in summary_l and "confirm" not in summary_l:
        return True
    # admin schedule create_event pending_deposit (custom title, graphite color often present)
    if "deposit: pending" in desc_l and "created manually via schedule page" in desc_l:
        return True
    return False


def _list_events_for_window(service, start_dt, end_dt):
    """List calendar events within a time window."""
    from utils.api_resilience import call_with_retry_calendar_execute

    try:
        req = service.events().list(
            calendarId=get_google_calendar_id(),
            timeMin=start_dt.astimezone(pytz.UTC).isoformat(),
            timeMax=end_dt.astimezone(pytz.UTC).isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        events_result = call_with_retry_calendar_execute(req.execute)
        return events_result.get("items", [])
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
        return []


def _event_color(event):
    """Get color ID from calendar event (canonical string, or empty if unset)."""
    cid = normalize_calendar_color_id(event)
    return cid if cid is not None else ""
