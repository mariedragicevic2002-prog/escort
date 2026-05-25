"""Create and manage GRAPE/LAVENDER travel time blocks on the calendar."""

import logging
from datetime import timedelta

import config
from config import (
    ADELLA_CALENDAR_SOFT_HOLD_MARKER,
    COLOR_GRAPE,
    COLOR_LAVENDER,
)

from services.database_service import get_shared_db
from services.calendar.travel_routing import (
    _build_outcall_route_addresses,
    _sanitize_routing_address,
    get_escort_base_address_for_travel,
    get_outcall_one_way_travel_minutes,
    get_outcall_return_travel_minutes,
    get_travel_minutes_between,
)
from utils.dinner_date import extract_client_address_from_message

logger = logging.getLogger(__name__)


def _truncate_for_summary(text: str, max_len: int = 55) -> str:
    t = (text or "").strip().replace("\n", " ")
    if max_len <= 0 or len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _one_line_address(addr: str) -> str:
    return " ".join((addr or "").split()) or "(address not set)"


def _with_soft_hold_marker_if_lavender(description: str, color_id) -> str:
    """Append webform marker for pending (lavender) travel only; grape confirmed travel stays blocking."""
    if str(color_id) == str(COLOR_LAVENDER):
        return description.rstrip() + "\n\n" + ADELLA_CALENDAR_SOFT_HOLD_MARKER
    return description


def _apply_lavender_soft_hold_calendar_fields(event: dict, color_id) -> None:
    """
    LAVENDER = pending travel: must stay bookable over in the bot (see
    ``is_webform_non_blocking_calendar_event``) and should show as *free* in Google Calendar
    free/busy, not as busy like GRAPE.
    """
    if str(color_id) == str(COLOR_LAVENDER):
        event["transparency"] = "transparent"


def _standard_outcall_leg_block_description(
    *,
    leg: str,
    experience_type: str,
    current_addr: str,
    dest_addr: str,
    minutes: int,
    escort_name: str,
    client_name: str,
) -> str:
    """
    LAVENDER/GRAPE body for a single standard outcall leg (not dinner date).

    * Outbound: start = escort, destination = client; travel = minutes to client.
    * Return: start = client, destination = escort; travel = minutes back.
    """
    exp = (experience_type or "").strip() or "Outcall"
    cname = (client_name or "Client").strip() or "Client"
    ename = (escort_name or "Escort").strip() or "Escort"
    cur = (current_addr or "").strip() or "(address not set)"
    dest = (dest_addr or "").strip() or "(address not set)"
    cur_block = _one_line_address(cur)
    dest_block = _one_line_address(dest)
    if leg == "outbound":
        title = f"Travel there ({ename} – {cname})"
        cur_caption = f"Current address (start of travel — {ename}'s location)"
        dest_caption = "Destination address (client outcall)"
    else:
        title = f"Client travel back ({cname} – {ename})"
        cur_caption = f"Current address (start of travel — client / outcall)"
        dest_caption = f"Destination address ({ename}'s location)"
    return (
        "TRAVEL TIME — calendar block, drive-time reserved\n"
        f"{title}\n\n"
        f"Experience type: {exp}\n"
        f"Travel time: {minutes} min (Google Directions)\n\n"
        f"{cur_caption}:\n{cur_block}\n\n"
        f"{dest_caption}:\n{dest_block}\n"
    )


def _travel_time_block_description(
    *,
    leg_heading: str,
    from_label: str,
    from_addr: str,
    to_label: str,
    to_addr: str,
    client_name: str,
    minutes: int,
    experience_type: str = "",
    escort_display_name: str = "Escort",
) -> str:
    """Structured travel block body (dinner date and legacy layout)."""
    fa = (from_addr or "").strip() or "(address not set)"
    ta = (to_addr or "").strip() or "(address not set)"
    exp = (experience_type or "").strip() or "Outcall"
    fa_line = _one_line_address(fa)
    ta_line = _one_line_address(ta)
    return (
        "TRAVEL TIME — calendar block (lavender), drive-time reserved\n"
        f"Experience type: {exp}\n"
        f"Travel time: {minutes} min (Google Directions)\n"
        f"Current address ({escort_display_name}'s location): {fa_line}\n"
        f"Destination address: {ta_line}\n\n"
        f"{leg_heading}\n\n"
        f"From ({from_label}):\n{fa}\n\n"
        f"To ({to_label}):\n{ta}\n\n"
        f"Client (booking name): {client_name}\n"
        f"Estimated drive: {minutes} min (Google Directions)"
    )


def split_travel_return_event_ids(return_event_id: str | None) -> list[str]:
    """Split stored travel_return_event_id (may be 'id1|id2' for dinner date legs)."""
    if not return_event_id:
        return []
    return [p.strip() for p in str(return_event_id).split("|") if p.strip()]


def _get_db():
    return get_shared_db(config.DATABASE_URL)


def _extract_inserted_id(result) -> str | None:
    if not result:
        return None
    row = result[0]
    if isinstance(row, dict):
        value = row.get('id')
    elif hasattr(row, 'id'):
        value = row.id
    else:
        try:
            value = row[0]
        except Exception:
            value = None
    return str(value) if value is not None else None


def _travel_status_from_color(color_id) -> str:
    return 'pending-travel' if str(color_id) == str(COLOR_LAVENDER) else 'travel'


def _insert_travel_block(start_dt, end_dt, *, client_name, status, notes, duration_minutes, outcall_address, experience_type):
    db = _get_db()
    if not db:
        logger.error('create_travel_time_blocks: database unavailable')
        return None
    result = db.execute_query(
        """INSERT INTO bookings (
               start_time, end_time, client_name, phone, phone_number, duration, type,
               experience, status, notes, outcall_address, updated_at
           ) VALUES (%s, %s, %s, '', '', %s, 'travel', %s, %s, %s, %s, NOW())
           RETURNING id""",
        (
            start_dt,
            end_dt,
            client_name or 'Client',
            f"{max(1, int(duration_minutes))} minutes",
            (experience_type or '').strip(),
            status,
            notes,
            outcall_address,
        ),
        fetch=True,
    )
    return _extract_inserted_id(result)


def create_travel_time_blocks(
    booking_start,
    booking_end,
    client_address,
    client_name="Client",
    color_id=None,
    *,
    dinner_date_mode: bool = False,
    skip_return_travel: bool = False,
    return_destination_address: str | None = None,
    experience_type: str | None = None,
    dinner_restaurant_to_play_minutes: int | None = None,
):
    """Create travel blocks in the bookings table."""
    if color_id is None:
        color_id = COLOR_GRAPE
    from config import get_escort_name

    _exp_label = (experience_type or '').strip() or ('Dinner Date' if dinner_date_mode else 'Outcall')
    status = _travel_status_from_color(color_id)

    escort_name = 'Escort'
    try:
        escort_name = get_escort_name()
    except Exception as e:
        logger.warning('get_escort_name failed, using default label: %s', e)

    origin, destination = _build_outcall_route_addresses(client_address)
    travel_minutes = max(1, int(get_outcall_one_way_travel_minutes(client_address) or 0))
    dinner_end = booking_start + timedelta(minutes=60) if dinner_date_mode else None

    try:
        outbound_start = booking_start - timedelta(minutes=travel_minutes)
        _restaurant_line = destination or _sanitize_routing_address(client_address or '')
        if dinner_date_mode:
            outbound_notes = _with_soft_hold_marker_if_lavender(
                _travel_time_block_description(
                    leg_heading=f"Leg 1 of dinner date: drive from {escort_name}'s address to restaurant address.",
                    from_label=f"{escort_name} / my location",
                    from_addr=origin,
                    to_label='restaurant (dinner meet point)',
                    to_addr=_restaurant_line,
                    client_name=client_name,
                    minutes=travel_minutes,
                    experience_type=_exp_label,
                    escort_display_name=escort_name,
                ),
                color_id,
            )
            outbound_dest = _restaurant_line
        else:
            outbound_notes = _with_soft_hold_marker_if_lavender(
                _standard_outcall_leg_block_description(
                    leg='outbound',
                    experience_type=_exp_label,
                    current_addr=origin,
                    dest_addr=client_address or destination,
                    minutes=travel_minutes,
                    escort_name=escort_name,
                    client_name=client_name,
                ),
                color_id,
            )
            outbound_dest = client_address or destination

        outbound_id = _insert_travel_block(
            outbound_start,
            booking_start,
            client_name=client_name,
            status=status,
            notes=outbound_notes,
            duration_minutes=travel_minutes,
            outcall_address=outbound_dest,
            experience_type=_exp_label,
        )
        if not outbound_id:
            return None, None

        if dinner_date_mode and skip_return_travel:
            hotel_addr = get_escort_base_address_for_travel()
            if not hotel_addr:
                logger.warning('Dinner date (after hotel): no escort hotel address; only outbound travel created: %s', outbound_id)
                return outbound_id, None
            hotel_addr_display = _sanitize_routing_address(hotel_addr)
            rest_from = _restaurant_line
            if dinner_restaurant_to_play_minutes is not None:
                try:
                    ret_min = max(1, int(dinner_restaurant_to_play_minutes))
                except (TypeError, ValueError):
                    ret_min = max(1, int(get_travel_minutes_between(rest_from, hotel_addr_display) or 0))
            else:
                ret_min = max(1, int(get_travel_minutes_between(rest_from, hotel_addr_display) or 0))
            mid_start = dinner_end or (booking_start + timedelta(minutes=60))
            mid_end = mid_start + timedelta(minutes=ret_min)
            return_id = _insert_travel_block(
                mid_start,
                mid_end,
                client_name=client_name,
                status=status,
                notes=_with_soft_hold_marker_if_lavender(
                    _travel_time_block_description(
                        leg_heading=f"After dinner: drive from restaurant address to {escort_name}'s hotel address.",
                        from_label='restaurant (dinner)',
                        from_addr=rest_from,
                        to_label=f"{escort_name}'s hotel / my location",
                        to_addr=hotel_addr_display,
                        client_name=client_name,
                        minutes=ret_min,
                        experience_type=_exp_label,
                        escort_display_name=escort_name,
                    ),
                    color_id,
                ),
                duration_minutes=ret_min,
                outcall_address=hotel_addr_display,
                experience_type=_exp_label,
            )
            return outbound_id, return_id

        if dinner_date_mode and return_destination_address:
            client_home_display = _sanitize_routing_address(extract_client_address_from_message(return_destination_address))
            if dinner_restaurant_to_play_minutes is not None:
                try:
                    ret_to_client = max(1, int(dinner_restaurant_to_play_minutes))
                except (TypeError, ValueError):
                    ret_to_client = max(1, int(get_travel_minutes_between(_restaurant_line, client_home_display) or 0))
            else:
                ret_to_client = max(1, int(get_travel_minutes_between(_restaurant_line, client_home_display) or 0))
            leg1_start = dinner_end or (booking_start + timedelta(minutes=60))
            leg1_end = leg1_start + timedelta(minutes=ret_to_client)
            leg1_id = _insert_travel_block(
                leg1_start,
                leg1_end,
                client_name=client_name,
                status=status,
                notes=_with_soft_hold_marker_if_lavender(
                    _travel_time_block_description(
                        leg_heading='After dinner: drive from restaurant address to client home address.',
                        from_label='restaurant (dinner)',
                        from_addr=_restaurant_line,
                        to_label='client home (after dinner)',
                        to_addr=client_home_display,
                        client_name=client_name,
                        minutes=ret_to_client,
                        experience_type=_exp_label,
                        escort_display_name=escort_name,
                    ),
                    color_id,
                ),
                duration_minutes=ret_to_client,
                outcall_address=client_home_display,
                experience_type=_exp_label,
            )

            ret_home = max(1, int(get_travel_minutes_between(client_home_display, origin) or 0))
            leg2_start = booking_end
            leg2_end = leg2_start + timedelta(minutes=ret_home)
            leg2_id = _insert_travel_block(
                leg2_start,
                leg2_end,
                client_name=client_name,
                status=status,
                notes=_with_soft_hold_marker_if_lavender(
                    _travel_time_block_description(
                        leg_heading=f"End of booking: drive from client home back to {escort_name}'s address.",
                        from_label='client home',
                        from_addr=client_home_display,
                        to_label=f"{escort_name} / my location",
                        to_addr=origin,
                        client_name=client_name,
                        minutes=ret_home,
                        experience_type=_exp_label,
                        escort_display_name=escort_name,
                    ),
                    color_id,
                ),
                duration_minutes=ret_home,
                outcall_address=origin,
                experience_type=_exp_label,
            )
            combined = '|'.join(x for x in (leg1_id, leg2_id) if x)
            return outbound_id, combined or None

        return_travel_minutes = max(1, int(get_outcall_return_travel_minutes(client_address) or 0))
        return_end = booking_end + timedelta(minutes=return_travel_minutes)
        return_id = _insert_travel_block(
            booking_end,
            return_end,
            client_name=client_name,
            status=status,
            notes=_with_soft_hold_marker_if_lavender(
                _standard_outcall_leg_block_description(
                    leg='return',
                    experience_type=_exp_label,
                    current_addr=client_address or destination,
                    dest_addr=origin,
                    minutes=return_travel_minutes,
                    escort_name=escort_name,
                    client_name=client_name,
                ),
                color_id,
            ),
            duration_minutes=return_travel_minutes,
            outcall_address=origin,
            experience_type=_exp_label,
        )
        return outbound_id, return_id
    except Exception as e:
        logger.error(f'Travel blocks error: {e}')
        return None, None


def confirm_travel_time_blocks(outbound_event_id, return_event_id):
    """Update pending travel blocks to confirmed travel."""
    ids = [eid for eid in [outbound_event_id, *split_travel_return_event_ids(return_event_id)] if eid]
    if not ids:
        return False
    db = _get_db()
    if not db:
        logger.error('confirm_travel_time_blocks: database unavailable')
        return False
    try:
        db.execute_query(
            "UPDATE bookings SET status='travel', updated_at=NOW() WHERE id::text = ANY(%s) AND status='pending-travel'",
            (ids,),
            fetch=False,
        )
        return True
    except Exception as e:
        logger.error('Failed to confirm travel blocks %s: %s', ids, e)
        return False


def delete_travel_time_blocks(outbound_event_id, return_event_id):
    """Delete travel block rows for an outcall booking."""
    db = _get_db()
    if not db:
        logger.error('delete_travel_time_blocks: database unavailable')
        return False, False

    outbound_deleted = False
    return_deleted = False

    try:
        if outbound_event_id:
            deleted = db.execute_query(
                "DELETE FROM bookings WHERE id::text = ANY(%s) RETURNING id",
                ([outbound_event_id],),
                fetch=True,
            ) or []
            outbound_deleted = bool(deleted)

        return_ids = split_travel_return_event_ids(return_event_id)
        if return_ids:
            deleted = db.execute_query(
                "DELETE FROM bookings WHERE id::text = ANY(%s) RETURNING id",
                (return_ids,),
                fetch=True,
            ) or []
            return_deleted = bool(deleted)
    except Exception as e:
        logger.error('Failed to delete travel blocks outbound=%s return=%s: %s', outbound_event_id, return_event_id, e)

    return outbound_deleted, return_deleted
