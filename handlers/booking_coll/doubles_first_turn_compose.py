"""
First SMS after a doubles MMF/MFF enquiry: availability + deposit + conditional outcall copy.

Assumes booking_coll supply-state flags are already updated (escort vs ambiguous gate).

Golden rule: until we know whether the client or the escort supplies the second person, any
requested start earlier than ``DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES``
from now is never treated as available (calendar-free or not). Offer alternatives on/after that floor.
Once the client has confirmed they bring the other person, normal booking logic applies without this gate.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from core.booking_substates import DOUBLES_SUPPLY_CONFIRMED, DOUBLES_SUPPLY_ESCORT
from utils.log_sanitize import LOG_SUPPRESSED_FMT

logger = logging.getLogger("adella_chatbot.handlers.collecting")

# Doubles: until we know if the *client* or the *escort* supplies the second person,
# never treat a requested start within this many hours from now as available (worst case: we must arrange them).
# Same floor applies once escort-sourcing is confirmed (arrangement notice).
DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES = 4


def _escort_supply_notice_floor_start(now):
    """Return the earliest slot floor for escort-sourced doubles.

    If the message arrives while the escort is working, the 4-hour notice is
    measured from the text time. If it arrives off-shift, the notice starts
    from the escort's next work start.
    """
    from core.settings_manager import get_setting
    from handlers.booking_coll._shared import (
        _check_day_within_available_days,
        check_within_available_hours_and_days,
        parse_available_hours_window_hhmm,
        resolve_available_days_for_checks,
    )

    available_hours = (get_setting("available_hours", "") or "").strip()
    available_days = (get_setting("available_days", "7 days a week") or "7 days a week").strip()
    within, _ = check_within_available_hours_and_days(
        now.date(),
        (now.hour, now.minute),
        available_hours,
        available_days,
    )
    if within:
        return now + timedelta(hours=DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES)

    window = parse_available_hours_window_hhmm(available_hours)
    if not window:
        return now + timedelta(hours=DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES)

    start_raw, _ = window
    try:
        start_hour, start_minute = (int(part) for part in start_raw.split(":", 1))
    except Exception:
        return now + timedelta(hours=DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES)

    days_eff = resolve_available_days_for_checks(available_hours, available_days)
    candidate = now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_hour * 60 + start_minute
    if current_minutes >= start_minutes:
        candidate += timedelta(days=1)
    while not _check_day_within_available_days(candidate.date(), days_eff):
        candidate += timedelta(days=1)
    return candidate + timedelta(hours=DOUBLES_MIN_LEAD_HOURS_WHEN_SECOND_PARTY_UNCONFIRMED_OR_ESCORT_SUPPLIES)


def _cbd_label(city: str = "") -> str:
    c = (city or "").strip()
    if c:
        return f"{c} CBD"
    try:
        from config import get_cbd_label_for_messages

        return get_cbd_label_for_messages()
    except Exception:
        return "the CBD"


def _fmt_yes_available_header(client_name: str, dt, doubles_label: str) -> str:
    from utils.time_formatting import format_time_12h, get_day_ordinal_suffix

    name_part = f" {client_name}" if (client_name or "").strip() else ""
    d = dt.date() if hasattr(dt, "date") else dt
    h, m = dt.hour, dt.minute
    tstr = format_time_12h(h, m)
    weekday_short = d.strftime("%a")
    month = d.strftime("%B")
    day_num = d.day
    suf = get_day_ordinal_suffix(day_num)
    return (
        f"Hi{name_part} ✅ Yes I'm available at {tstr} {weekday_short} {day_num}{suf} {month} "
        f"I love {doubles_label} bookings."
    )


def _fmt_unavailable_header(client_name: str, dt, doubles_label: str) -> str:
    from utils.time_formatting import format_time_12h, get_day_ordinal_suffix

    name_part = f" {client_name}" if (client_name or "").strip() else ""
    d = dt.date() if hasattr(dt, "date") else dt
    h, m = dt.hour, dt.minute
    tstr = format_time_12h(h, m)
    weekday_short = d.strftime("%a")
    month = d.strftime("%B")
    day_num = d.day
    suf = get_day_ordinal_suffix(day_num)
    return (
        f"Hi{name_part} ❌ Unfortunately I'm not available at {tstr} on {weekday_short} {day_num}{suf} {month} "
        f"but I love {doubles_label} bookings."
    )


def _mandatory_doubles_deposit_line(deposit: int) -> str:
    return f"A ${deposit} deposit is required for all doubles bookings."


def _append_doubles_unavailable_near_requested_time_tail(
    chunks: list[str],
    *,
    alt_lines: list[str],
    profile_url: str,
    loc_line: str,
    city: str,
    is_outcall: bool,
    deposit: int,
    wf: str,
    escort_sources_second: bool,
    extra_before_deposit: str | None = None,
) -> None:
    """Shared SMS tail after the ❌ opener (bullets → profile → location → ask → deposit → webform)."""
    if alt_lines:
        chunks.append("\n".join(f"• {line}" for line in alt_lines))
    if profile_url:
        chunks.append(profile_url.strip())
    if loc_line:
        chunks.append(loc_line)
    if is_outcall:
        chunks.append(
            f"I only do outcalls to hotels or apartments within 15km of {_cbd_label(city)}."
        )
        if escort_sources_second:
            chunks.append(_outcall_escort_sources_fee_lines())
        chunks.append(
            "What time suits you, how long did you want to book for, and what's your address?"
        )
    else:
        chunks.append("What time suits you and how long did you want to book for?")
    if extra_before_deposit:
        chunks.append(extra_before_deposit.strip())
    chunks.append(_mandatory_doubles_deposit_line(deposit))
    chunks.append(_webform_lines(wf))


def _four_hour_notice_block() -> str:
    return (
        "Just so you know, when I need to arrange someone there is a minimum 4 hours notice required."
    )


def _webform_lines(webform_url: str) -> str:
    if not (webform_url or "").strip():
        return "I STRONGLY recommend booking through my webform for all doubles bookings."
    return (
        "I STRONGLY recommend booking through my webform for all doubles bookings:\n"
        f"{webform_url.strip()}"
    )


def _outcall_escort_sources_fee_lines() -> str:
    """Short pair-travel + deposit wording when escort sources second provider & client wants outcall."""
    try:
        from core.rates_from_config import (
            get_deposit_mff_pair,
            get_surcharge,
            get_surcharge_doubles_escort_supplied_outcall,
        )

        pair = int(get_surcharge_doubles_escort_supplied_outcall())
        solo = int(get_surcharge())
        deposit = int(get_deposit_mff_pair())
        extra = max(0, pair - solo)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        pair, solo, deposit, extra = 200, 100, 200, 100
    return (
        f"If you're wanting me to organise the other person there will be a ${pair} surcharge "
        f"+ ${deposit} deposit required for all outcall doubles bookings.\n\n"
        f"(The extra ${extra} is because both of us travel to you.)"
    )


def _doubles_label(doubles_type: str) -> str:
    dtype = (doubles_type or "").strip().lower()
    return "doubles MMF" if dtype == "mmf" else "doubles MFF" if dtype == "mff" else "doubles"


def _build_doubles_first_turn_context(
    *,
    phone_number: str,
    webform_url: str,
    get_current_incall_location,
    get_effective_booking_city,
    get_profile_url,
    get_webform_url,
    get_deposit_mff_pair,
    special_location_display,
) -> dict[str, Any]:
    location = get_current_incall_location() or {}
    city = (get_effective_booking_city() or location.get("city") or "").strip()
    profile_url = (get_profile_url() or "").strip()
    wf = (webform_url or "").strip() or (get_webform_url(phone_number) or "").strip()
    try:
        deposit = int(get_deposit_mff_pair())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        deposit = 200

    hotel_name = (location.get("hotel_name") or location.get("display_name") or "").strip()
    address = (location.get("address") or "").strip()
    loc_line = special_location_display(city=city, hotel_name=hotel_name, address=address)
    return {
        "city": city,
        "profile_url": profile_url,
        "wf": wf,
        "deposit": deposit,
        "loc_line": loc_line,
    }


def _safe_infer_requested_datetime(message: str, now, infer_requested_datetime_for_booking):
    try:
        return infer_requested_datetime_for_booking(message, now=now)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return None


def _build_doubles_booking_payload(
    *,
    dt,
    is_outcall: bool,
    booking_type: str,
    experience_type: str,
    escort_supply_source: str | None = None,
    booking_status: str | None = None,
) -> dict[str, Any]:
    payload = {
        "date": dt.strftime("%Y-%m-%d"),
        "time": (dt.hour, dt.minute),
        "duration": 60,
        "incall_outcall": "outcall" if is_outcall else "incall",
        "booking_type": booking_type,
        "experience_type": experience_type,
    }
    if escort_supply_source is not None:
        payload["escort_supply_source"] = escort_supply_source
    if booking_status is not None:
        payload["booking_status"] = booking_status
    return payload


def _check_requested_doubles_availability(
    *,
    extracted_dt,
    floor,
    is_outcall: bool,
    booking_type: str,
    experience_type: str,
    check_conflict,
    escort_supply_source: str | None = None,
    booking_status: str | None = None,
) -> tuple[bool, bool]:
    too_soon = extracted_dt < floor
    if too_soon:
        return True, False
    try:
        conflict_type, _ = check_conflict(
            _build_doubles_booking_payload(
                dt=extracted_dt,
                is_outcall=is_outcall,
                booking_type=booking_type,
                experience_type=experience_type,
                escort_supply_source=escort_supply_source,
                booking_status=booking_status,
            )
        )
        return False, conflict_type == "none"
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        return False, False


def _format_alternative_slot_lines(raw_alts, floor, format_slot_display_short, *, limit: int = 3) -> list[str]:
    alt_lines: list[str] = []
    for adt in raw_alts:
        if len(alt_lines) >= limit:
            break
        if adt < floor:
            continue
        try:
            alt_lines.append(format_slot_display_short(adt))
        except Exception as e:
            logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return alt_lines


def _extend_with_gap_slot_lines(
    alt_lines: list[str],
    *,
    now,
    floor,
    phone_number: str,
    state_manager: Any,
    get_next_available_time_slots,
    limit: int = 3,
) -> list[str]:
    if len(alt_lines) >= limit:
        return alt_lines
    try:
        gap_slots = get_next_available_time_slots(
            now,
            num_slots=limit,
            check_calendar=True,
            start_from=floor,
            persist_slots_for_phone=phone_number,
            persist_slots_state_manager=state_manager,
        )
        for _, slot_line in gap_slots:
            if len(alt_lines) >= limit:
                break
            if slot_line not in alt_lines:
                alt_lines.append(slot_line)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return alt_lines


def _collect_alternative_slot_lines(
    *,
    extracted_dt,
    floor,
    is_outcall: bool,
    booking_type: str,
    experience_type: str,
    escort_supply_source: str | None,
    booking_status: str | None,
    find_alternative_slots,
    format_slot_display_short,
    now,
    phone_number: str,
    state_manager: Any,
    get_next_available_time_slots,
) -> list[str]:
    alt_lines: list[str] = []
    try:
        raw_alts = list(
            find_alternative_slots(
                _build_doubles_booking_payload(
                    dt=extracted_dt,
                    is_outcall=is_outcall,
                    booking_type=booking_type,
                    experience_type=experience_type,
                    escort_supply_source=escort_supply_source,
                    booking_status=booking_status,
                ),
                max_results=12,
            )
        )
        alt_lines = _format_alternative_slot_lines(raw_alts, floor, format_slot_display_short)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
    return _extend_with_gap_slot_lines(
        alt_lines,
        now=now,
        floor=floor,
        phone_number=phone_number,
        state_manager=state_manager,
        get_next_available_time_slots=get_next_available_time_slots,
    )


def _compose_unavailable_doubles_response(
    *,
    client_name: str,
    extracted_dt,
    doubles_label: str,
    lead_line: str | None = None,
    alt_lines: list[str],
    profile_url: str,
    loc_line: str,
    city: str,
    is_outcall: bool,
    deposit: int,
    wf: str,
    escort_sources_second: bool,
    extra_before_deposit: str | None = None,
) -> str:
    chunks = [_fmt_unavailable_header(client_name, extracted_dt, doubles_label)]
    if lead_line:
        chunks.append(lead_line)
    _append_doubles_unavailable_near_requested_time_tail(
        chunks,
        alt_lines=alt_lines,
        profile_url=profile_url,
        loc_line=loc_line,
        city=city,
        is_outcall=is_outcall,
        deposit=deposit,
        wf=wf,
        escort_sources_second=escort_sources_second,
        extra_before_deposit=extra_before_deposit,
    )
    return "\n\n".join(chunks)


def _compose_available_doubles_response(
    *,
    client_name: str,
    extracted_dt,
    doubles_label: str,
    profile_url: str,
    city: str,
    is_outcall: bool,
    loc_line: str,
    deposit: int,
    wf: str,
    outcall_fee_line: str | None,
) -> str:
    parts = [_fmt_yes_available_header(client_name, extracted_dt, doubles_label)]
    if profile_url:
        parts.append(profile_url)
    if is_outcall:
        parts.append(
            f"I only do outcalls to hotels or apartments within 15km of {_cbd_label(city)}.\n\n"
            "What's your address and how long of a booking are you after?"
        )
        if outcall_fee_line:
            parts.append(outcall_fee_line)
    else:
        if loc_line:
            parts.append(loc_line)
        parts.append("How long a booking were you after? (eg. 30 mins 1 hr etc)")
        parts.append(_mandatory_doubles_deposit_line(deposit))
    parts.append(_webform_lines(wf))
    return "\n\n".join(parts)


def _client_outcall_fee_line(
    *,
    extracted_dt,
    booking_type: str,
    experience_type: str,
    deposit: int,
    get_outcall_travel_surcharge_for_booking,
) -> str:
    try:
        surcharge = int(
            get_outcall_travel_surcharge_for_booking(
                _build_doubles_booking_payload(
                    dt=extracted_dt,
                    is_outcall=True,
                    booking_type=booking_type,
                    experience_type=experience_type,
                    escort_supply_source="client",
                    booking_status=DOUBLES_SUPPLY_CONFIRMED,
                )
            )
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        surcharge = 100
    return (
        f"There is a ${surcharge} travel surcharge + ${deposit} deposit required for all outcall doubles bookings."
    )


def _persist_requested_slot(state_manager: Any, phone_number: str, extracted_dt) -> None:
    try:
        state_manager.update_fields(
            phone_number,
            {
                "date": extracted_dt.strftime("%Y-%m-%d"),
                "time": (extracted_dt.hour, extracted_dt.minute),
            },
        )
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)


def _append_common_context_parts(
    parts: list[str],
    *,
    profile_url: str,
    loc_line: str,
    city: str,
    include_outcall_restriction: bool,
) -> None:
    if profile_url:
        parts.append(profile_url)
    if loc_line:
        parts.append(loc_line)
    if include_outcall_restriction:
        parts.append(f"I only do outcalls to hotels or apartments within 15km of {_cbd_label(city)}.")


def _compose_escort_no_specific_time_response(
    *,
    client_name: str,
    doubles_label: str,
    now,
    min_start,
    phone_number: str,
    state_manager: Any,
    get_next_available_time_slots,
    profile_url: str,
    loc_line: str,
    deposit: int,
    wf: str,
) -> str:
    parts = [
        f"Hi{' ' + client_name.strip() if (client_name or '').strip() else ''}\n\n"
        f"I love {doubles_label} bookings.\n\n"
        f"{_four_hour_notice_block()}"
    ]
    slot_lines = _extend_with_gap_slot_lines(
        [],
        now=now,
        floor=min_start,
        phone_number=phone_number,
        state_manager=state_manager,
        get_next_available_time_slots=get_next_available_time_slots,
    )
    if slot_lines:
        parts.append("\n".join(f"• {line}" for line in slot_lines))
    _append_common_context_parts(
        parts,
        profile_url=profile_url,
        loc_line=loc_line,
        city="",
        include_outcall_restriction=False,
    )
    parts.append("What time suits you?")
    parts.append(_mandatory_doubles_deposit_line(deposit))
    parts.append(_webform_lines(wf))
    return "\n\n".join(parts)


def _compose_ambiguous_available_response(
    *,
    client_name: str,
    extracted_dt,
    doubles_label: str,
    gate_primary: str,
    profile_url: str,
    loc_line: str,
    city: str,
    is_outcall: bool,
    gate_follow: str,
    deposit: int,
    wf: str,
) -> str:
    parts = [
        _fmt_yes_available_header(client_name, extracted_dt, doubles_label),
        gate_primary,
        _four_hour_notice_block(),
    ]
    _append_common_context_parts(
        parts,
        profile_url=profile_url,
        loc_line=loc_line,
        city=city,
        include_outcall_restriction=is_outcall,
    )
    parts.append(gate_follow)
    parts.append(_mandatory_doubles_deposit_line(deposit))
    parts.append(_webform_lines(wf))
    return "\n\n".join(parts)


def _compose_ambiguous_mmf_outcall_no_time_response(
    *,
    client_name: str,
    profile_url: str,
    city: str,
    wf: str,
) -> str:
    cn = (client_name or "").strip()
    np = f" {cn}" if cn else ""
    parts = [
        f"Hi{np} I love doubles MMF bookings will you be bringing the other person yourself, or do you need me to organise them for you?",
        "Just so you know, when I need to arrange someone there is a minimum 4 hours notice required.",
    ]
    if profile_url:
        parts.append(profile_url)
    try:
        from core.rates_from_config import get_deposit_mff_pair, get_surcharge_doubles_escort_supplied_outcall

        pair = float(get_surcharge_doubles_escort_supplied_outcall())
        dep_amt = int(get_deposit_mff_pair())
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e, exc_info=False)
        pair = 200.0
        dep_amt = 200
    parts.append(
        f"I only do outcalls to hotels or apartments within 15km of {_cbd_label(city)}. "
        f"If your wanting me to organise the other person there will be a ${pair:.2f} + ${dep_amt} deposit required for all doubles bookings."
    )
    parts.append("(The additional surcharge is because two of us would need to travel to you)")
    parts.append(_webform_lines(wf))
    return "\n\n".join(parts)


def _compose_ambiguous_mmf_no_time_response(*, client_name: str, wf: str) -> str:
    cn = (client_name or "").strip()
    np = f" {cn}" if cn else ""
    parts = [
        f"Hi{np}\n\nI love doubles MMF bookings. Will you be bringing the other person yourself, "
        "or did you need me to organise them for you?",
        "Just so you know, when I need to arrange someone there is a minimum 4 hours notice required.",
        _webform_lines(wf),
    ]
    return "\n\n".join(parts)


def _compose_ambiguous_no_specific_time_response(
    *,
    client_name: str,
    doubles_label: str,
    dtype: str,
    is_outcall: bool,
    profile_url: str,
    loc_line: str,
    city: str,
    gate_follow: str,
    deposit: int,
    wf: str,
) -> str:
    if dtype == "mmf":
        if is_outcall:
            return _compose_ambiguous_mmf_outcall_no_time_response(
                client_name=client_name,
                profile_url=profile_url,
                city=city,
                wf=wf,
            )
        return _compose_ambiguous_mmf_no_time_response(client_name=client_name, wf=wf)

    cn = (client_name or "").strip()
    np = f" {cn}" if cn else ""
    parts = [
        f"Hi{np}\n\nI love {doubles_label} bookings. Will you be bringing the other person yourself, "
        "or did you need me to organise them for you?",
        _four_hour_notice_block(),
    ]
    _append_common_context_parts(
        parts,
        profile_url=profile_url,
        loc_line=loc_line,
        city=city,
        include_outcall_restriction=is_outcall,
    )
    parts.append(gate_follow)
    parts.append(_mandatory_doubles_deposit_line(deposit))
    parts.append(_webform_lines(wf))
    return "\n\n".join(parts)


def compose_escort_sourced_doubles_first_turn(
    *,
    message: str,
    phone_number: str,
    state_manager: Any,
    client_name: str,
    doubles_type: str,
    booking_type: str,
    experience_type: str,
    webform_url: str,
    is_outcall: bool,
) -> str:
    """Escort will source the second provider — availability-aware first reply."""
    from config import get_current_incall_location, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_mff_pair
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.special_bookings import _special_location_display
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    doubles_label = _doubles_label(doubles_type)
    now = get_current_datetime()
    min_start = _escort_supply_notice_floor_start(now)
    context = _build_doubles_first_turn_context(
        phone_number=phone_number,
        webform_url=webform_url,
        get_current_incall_location=get_current_incall_location,
        get_effective_booking_city=get_effective_booking_city,
        get_profile_url=get_profile_url,
        get_webform_url=get_webform_url,
        get_deposit_mff_pair=get_deposit_mff_pair,
        special_location_display=_special_location_display,
    )
    extracted_dt = _safe_infer_requested_datetime(message, now, infer_requested_datetime_for_booking)

    if extracted_dt is not None:
        too_soon, is_available = _check_requested_doubles_availability(
            extracted_dt=extracted_dt,
            floor=min_start,
            is_outcall=is_outcall,
            booking_type=booking_type,
            experience_type=experience_type,
            check_conflict=check_conflict,
            escort_supply_source="escort",
            booking_status=DOUBLES_SUPPLY_ESCORT,
        )
        if too_soon or not is_available:
            alt_lines = _collect_alternative_slot_lines(
                extracted_dt=extracted_dt,
                floor=min_start,
                is_outcall=is_outcall,
                booking_type=booking_type,
                experience_type=experience_type,
                escort_supply_source="escort",
                booking_status=DOUBLES_SUPPLY_ESCORT,
                find_alternative_slots=find_alternative_slots,
                format_slot_display_short=format_slot_display_short,
                now=now,
                phone_number=phone_number,
                state_manager=state_manager,
                get_next_available_time_slots=get_next_available_time_slots,
            )
            return _compose_unavailable_doubles_response(
                client_name=client_name,
                extracted_dt=extracted_dt,
                doubles_label=doubles_label,
                alt_lines=alt_lines,
                profile_url=context["profile_url"],
                loc_line=context["loc_line"],
                city=context["city"],
                is_outcall=is_outcall,
                deposit=context["deposit"],
                wf=context["wf"],
                escort_sources_second=True,
            )

        response = _compose_available_doubles_response(
            client_name=client_name,
            extracted_dt=extracted_dt,
            doubles_label=doubles_label,
            profile_url=context["profile_url"],
            city=context["city"],
            is_outcall=is_outcall,
            loc_line=context["loc_line"],
            deposit=context["deposit"],
            wf=context["wf"],
            outcall_fee_line=_outcall_escort_sources_fee_lines() if is_outcall else None,
        )
        _persist_requested_slot(state_manager, phone_number, extracted_dt)
        return response

    return _compose_escort_no_specific_time_response(
        client_name=client_name,
        doubles_label=doubles_label,
        now=now,
        min_start=min_start,
        phone_number=phone_number,
        state_manager=state_manager,
        get_next_available_time_slots=get_next_available_time_slots,
        profile_url=context["profile_url"],
        loc_line=context["loc_line"],
        deposit=context["deposit"],
        wf=context["wf"],
    )


def compose_client_supplied_doubles_first_turn(
    *,
    message: str,
    phone_number: str,
    state_manager: Any,
    client_name: str,
    doubles_type: str,
    booking_type: str,
    experience_type: str,
    webform_url: str,
    is_outcall: bool,
) -> str | None:
    """Client brings the second person — if they named a concrete time, reply ✅/❌ + alternatives.

    Returns ``None`` when no clock was inferred (caller shows generic closest-slot SMS).
    """
    from config import get_current_incall_location, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_mff_pair, get_outcall_travel_surcharge_for_booking
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.special_bookings import _special_location_display
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    doubles_label = _doubles_label(doubles_type)
    now = get_current_datetime()
    floor = now
    context = _build_doubles_first_turn_context(
        phone_number=phone_number,
        webform_url=webform_url,
        get_current_incall_location=get_current_incall_location,
        get_effective_booking_city=get_effective_booking_city,
        get_profile_url=get_profile_url,
        get_webform_url=get_webform_url,
        get_deposit_mff_pair=get_deposit_mff_pair,
        special_location_display=_special_location_display,
    )
    extracted_dt = _safe_infer_requested_datetime(message, now, infer_requested_datetime_for_booking)
    if extracted_dt is None:
        return None

    too_soon, is_available = _check_requested_doubles_availability(
        extracted_dt=extracted_dt,
        floor=floor,
        is_outcall=is_outcall,
        booking_type=booking_type,
        experience_type=experience_type,
        check_conflict=check_conflict,
        escort_supply_source="client",
        booking_status=DOUBLES_SUPPLY_CONFIRMED,
    )
    if too_soon or not is_available:
        alt_lines = _collect_alternative_slot_lines(
            extracted_dt=extracted_dt,
            floor=floor,
            is_outcall=is_outcall,
            booking_type=booking_type,
            experience_type=experience_type,
            escort_supply_source="client",
            booking_status=DOUBLES_SUPPLY_CONFIRMED,
            find_alternative_slots=find_alternative_slots,
            format_slot_display_short=format_slot_display_short,
            now=now,
            phone_number=phone_number,
            state_manager=state_manager,
            get_next_available_time_slots=get_next_available_time_slots,
        )
        return _compose_unavailable_doubles_response(
            client_name=client_name,
            extracted_dt=extracted_dt,
            doubles_label=doubles_label,
            alt_lines=alt_lines,
            profile_url=context["profile_url"],
            loc_line=context["loc_line"],
            city=context["city"],
            is_outcall=is_outcall,
            deposit=context["deposit"],
            wf=context["wf"],
            escort_sources_second=False,
        )

    response = _compose_available_doubles_response(
        client_name=client_name,
        extracted_dt=extracted_dt,
        doubles_label=doubles_label,
        profile_url=context["profile_url"],
        city=context["city"],
        is_outcall=is_outcall,
        loc_line=context["loc_line"],
        deposit=context["deposit"],
        wf=context["wf"],
        outcall_fee_line=(
            _client_outcall_fee_line(
                extracted_dt=extracted_dt,
                booking_type=booking_type,
                experience_type=experience_type,
                deposit=context["deposit"],
                get_outcall_travel_surcharge_for_booking=get_outcall_travel_surcharge_for_booking,
            )
            if is_outcall
            else None
        ),
    )
    _persist_requested_slot(state_manager, phone_number, extracted_dt)
    return response


def compose_ambiguous_doubles_supply_first_turn(
    *,
    message: str,
    phone_number: str,
    state_manager: Any,
    client_name: str,
    doubles_type: str,
    booking_type: str,
    experience_type: str,
    webform_url: str,
    is_outcall: bool,
) -> str:
    """Doubles enquiry but bring-vs-organise not yet known — still check availability if time given."""
    from config import get_current_incall_location, get_effective_booking_city, get_profile_url
    from core.rates_from_config import get_deposit_mff_pair
    from core.webform_security import get_webform_url
    from services.calendar_service import check_conflict, find_alternative_slots
    from templates.special_bookings import _special_location_display
    from utils.availability_slots import format_slot_display_short, get_next_available_time_slots
    from utils.time_parser import infer_requested_datetime_for_booking
    from utils.timezone import get_current_datetime

    dtype = (doubles_type or "").strip().lower()
    doubles_label = _doubles_label(doubles_type)
    now = get_current_datetime()
    min_start = _escort_supply_notice_floor_start(now)
    context = _build_doubles_first_turn_context(
        phone_number=phone_number,
        webform_url=webform_url,
        get_current_incall_location=get_current_incall_location,
        get_effective_booking_city=get_effective_booking_city,
        get_profile_url=get_profile_url,
        get_webform_url=get_webform_url,
        get_deposit_mff_pair=get_deposit_mff_pair,
        special_location_display=_special_location_display,
    )
    extracted_dt = _safe_infer_requested_datetime(message, now, infer_requested_datetime_for_booking)
    gate_primary = (
        "Will you be bringing the other person yourself, or did you need me to organise them for you?"
    )
    gate_follow = (
        "Please advise if you will be supplying the other person or if you need me to do so?"
    )

    if extracted_dt is not None:
        too_soon, is_available = _check_requested_doubles_availability(
            extracted_dt=extracted_dt,
            floor=min_start,
            is_outcall=is_outcall,
            booking_type=booking_type,
            experience_type=experience_type,
            check_conflict=check_conflict,
        )
        if is_available:
            response = _compose_ambiguous_available_response(
                client_name=client_name,
                extracted_dt=extracted_dt,
                doubles_label=doubles_label,
                gate_primary=gate_primary,
                profile_url=context["profile_url"],
                loc_line=context["loc_line"],
                city=context["city"],
                is_outcall=is_outcall,
                gate_follow=gate_follow,
                deposit=context["deposit"],
                wf=context["wf"],
            )
            _persist_requested_slot(state_manager, phone_number, extracted_dt)
            return response

        alt_lines = _collect_alternative_slot_lines(
            extracted_dt=extracted_dt,
            floor=min_start,
            is_outcall=is_outcall,
            booking_type=booking_type,
            experience_type=experience_type,
            escort_supply_source=None,
            booking_status=None,
            find_alternative_slots=find_alternative_slots,
            format_slot_display_short=format_slot_display_short,
            now=now,
            phone_number=phone_number,
            state_manager=state_manager,
            get_next_available_time_slots=get_next_available_time_slots,
        )
        return _compose_unavailable_doubles_response(
            client_name=client_name,
            extracted_dt=extracted_dt,
            doubles_label=doubles_label,
            lead_line=gate_primary,
            alt_lines=alt_lines,
            profile_url=context["profile_url"],
            loc_line=context["loc_line"],
            city=context["city"],
            is_outcall=is_outcall,
            deposit=context["deposit"],
            wf=context["wf"],
            escort_sources_second=False,
            extra_before_deposit=gate_follow,
        )

    return _compose_ambiguous_no_specific_time_response(
        client_name=client_name,
        doubles_label=doubles_label,
        dtype=dtype,
        is_outcall=is_outcall,
        profile_url=context["profile_url"],
        loc_line=context["loc_line"],
        city=context["city"],
        gate_follow=gate_follow,
        deposit=context["deposit"],
        wf=context["wf"],
    )
