"""Integration: Stage 6 slot selection (_stage_slot_selection) with CollectingCtx + fake state."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
import pytz

from handlers.booking_coll._provide_field_context import CollectingCtx
from handlers.booking_coll._provide_field_stages_extract import _available_now_inline_calendar_check
from handlers.booking_coll._provide_field_stages_slot_load import _stage_slot_selection
from handlers.booking_coll._shared_dinner_doubles import _check_doubles_supply_response
from tests.fakes import FakeStateManager
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999334"


def test_stage_slot_selection_colonless_430_matches_four_thirty_pm_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Doubles-style reply "430" after 3:30 / 4:30 / 5:30 pm offers must bind to 4:30 pm,
    not mis-parse as "43" or fall through to generic availability.
    """
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 11, 25, 0))
    monkeypatch.setattr("handlers.booking_coll._shared.get_current_datetime", lambda: frozen)

    sm = scenario_state_manager(
        PHONE,
        current_state="COLLECTING_BOOKING_FIELDS",
        offered_slot_hours=[15, 16, 17],
        offered_slot_minutes=[30, 30, 30],
        offered_slot_date="2026-05-05",
        experience_type="gfe",
        incall_outcall="incall",
    )
    ctx_dict = build_context(phone_number=PHONE, message="430", state_manager=sm)
    ctx = CollectingCtx.from_context(ctx_dict)
    ctx.current_fields = {}

    out = _stage_slot_selection(ctx)

    assert out is not None
    assert out.get("new_state") is None
    assert isinstance(out.get("messages"), list) and len(out["messages"]) >= 1

    assert ctx.current_fields["time"] == (16, 30)
    assert str(ctx.current_fields["date"])[:10] == "2026-05-05"

    persisted = sm.get_state(PHONE) or {}
    assert persisted.get("time") == (16, 30)
    assert str(persisted.get("date") or "")[:10] == "2026-05-05"


def test_doubles_escort_supply_persists_slots_then_colonless_445_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Regression: escort-organised doubles SMS must persist offered_slot_* so a lone '445'
    hits Stage 6 (not extraction + outside-hours with empty slot context).
    """
    tz = pytz.timezone("Australia/Adelaide")
    frozen_reply = tz.localize(datetime(2026, 5, 5, 11, 42, 0))
    frozen_pick = tz.localize(datetime(2026, 5, 5, 11, 44, 28))

    fake_slots = [
        (tz.localize(datetime(2026, 5, 5, 15, 45, 0)), "Tue 5th May 3:45pm"),
        (tz.localize(datetime(2026, 5, 5, 16, 45, 0)), "Tue 5th May 4:45pm"),
        (tz.localize(datetime(2026, 5, 5, 17, 45, 0)), "Tue 5th May 5:45pm"),
    ]

    monkeypatch.setattr(
        "utils.availability_slots.get_next_available_time_slots",
        lambda *args, **kwargs: list(fake_slots),
    )
    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen_reply)

    sm = FakeStateManager(
        initial={
            PHONE: {
                "booking_type": "doubles_mff",
                "booking_status": "doubles_supply_gate",
                "client_name": "Harry",
            }
        }
    )
    st0 = sm.get_state(PHONE) or {}
    _check_doubles_supply_response(
        "Can you sort out the other chick?",
        PHONE,
        st0,
        sm,
        doubles_supply_gate_follow_up=True,
    )

    st1 = sm.get_state(PHONE) or {}
    assert st1.get("offered_slot_hours") == [15, 16, 17]
    assert st1.get("offered_slot_minutes") == [45, 45, 45]
    assert str(st1.get("offered_slot_date") or "")[:10] == "2026-05-05"

    monkeypatch.setattr("handlers.booking_coll._shared.get_current_datetime", lambda: frozen_pick)

    ctx_dict = build_context(phone_number=PHONE, message="445", state_manager=sm)
    ctx = CollectingCtx.from_context(ctx_dict)
    ctx.current_fields = {}

    out = _stage_slot_selection(ctx)
    assert out is not None
    assert ctx.current_fields["time"] == (16, 45)
    assert str(ctx.current_fields["date"])[:10] == "2026-05-05"

    st2 = sm.get_state(PHONE) or {}
    assert st2.get("time") == (16, 45)


def test_doubles_escort_supply_survives_get_next_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If slot computation throws, client still gets escort-supply copy (avoid silent handler failures)."""

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated calendar failure")

    monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", _boom)

    sm = FakeStateManager(
        initial={
            PHONE: {
                "booking_type": "doubles_mff",
                "booking_status": "doubles_supply_gate",
                "client_name": "Harry",
            }
        }
    )
    st0 = sm.get_state(PHONE) or {}
    out = _check_doubles_supply_response(
        "Can you sort out the other chick?",
        PHONE,
        st0,
        sm,
        doubles_supply_gate_follow_up=True,
    )
    assert out is not None
    msgs = out.get("messages") or []
    assert len(msgs) == 1
    body = (msgs[0] or "").lower()
    assert "organise the other escort" in body or "organize the other escort" in body


def test_available_now_inline_calendar_blocks_until_doubles_supply_confirmed() -> None:
    sm = scenario_state_manager(
        PHONE,
        current_state="COLLECTING_BOOKING_FIELDS",
        booking_type="Doubles MMF",
        booking_status="doubles_supply_gate",
        escort_supply_confirmed=False,
        incall_outcall="incall",
    )
    ctx_dict = build_context(phone_number=PHONE, message="10am", state_manager=sm)
    ctx = CollectingCtx.from_context(ctx_dict)
    ctx.current_fields = {"incall_outcall": "incall"}

    out = _available_now_inline_calendar_check(
        ctx,
        {"time": (10, 0)},
        None,
        datetime(2026, 5, 5, 8, 0),
    )

    assert out is not None
    assert out.get("new_state") is None
    assert "Before I can check availability" in (out.get("messages") or [""])[0]


def test_available_now_inline_calendar_enforces_4h_for_escort_sourced_doubles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_slots(now, num_slots, check_calendar, **kwargs):
        _ = (now, num_slots, check_calendar)
        captured["start_from"] = kwargs.get("start_from")
        return [
            (datetime(2026, 5, 5, 12, 30), "Tue 5th May 12:30pm"),
            (datetime(2026, 5, 5, 1, 30), "Tue 5th May 1:30pm"),
            (datetime(2026, 5, 5, 2, 30), "Tue 5th May 2:30pm"),
        ]

    monkeypatch.setattr("utils.time_parser.infer_time_from_hour", lambda _h, now: (now.date(), None))
    monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", _fake_slots)
    monkeypatch.setattr("services.calendar_service.check_conflict", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr(
        "core.settings_manager.get_setting",
        lambda key, default=None: {
            "available_hours": "1pm-4am, 7 days a week",
            "available_days": "7 days a week",
        }.get(key, default),
    )

    sm = scenario_state_manager(
        PHONE,
        current_state="COLLECTING_BOOKING_FIELDS",
        booking_type="doubles_mff",
        booking_status="doubles_supply_escort",
        escort_supply_source="escort",
        escort_supply_confirmed=True,
        incall_outcall="incall",
    )
    ctx_dict = build_context(phone_number=PHONE, message="10am", state_manager=sm)
    ctx = CollectingCtx.from_context(ctx_dict)
    ctx.current_fields = {"incall_outcall": "incall"}
    now = datetime(2026, 5, 5, 8, 0)

    out = _available_now_inline_calendar_check(ctx, {"time": (10, 0)}, None, now)

    assert out is not None
    assert "minimum 4 hours notice" in (out.get("messages") or [""])[0].lower()
    assert captured.get("start_from") == datetime(2026, 5, 5, 17, 0)


def test_available_now_inline_calendar_uses_text_time_floor_when_in_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_slots(now, num_slots, check_calendar, **kwargs):
        _ = (now, num_slots, check_calendar)
        captured["start_from"] = kwargs.get("start_from")
        return []

    monkeypatch.setattr("utils.time_parser.infer_time_from_hour", lambda _h, now: (now.date(), None))
    monkeypatch.setattr("utils.availability_slots.get_next_available_time_slots", _fake_slots)
    monkeypatch.setattr("services.calendar_service.check_conflict", lambda *_a, **_k: ("none", None))
    monkeypatch.setattr(
        "core.settings_manager.get_setting",
        lambda key, default=None: {
            "available_hours": "1pm-4am, 7 days a week",
            "available_days": "7 days a week",
        }.get(key, default),
    )

    sm = scenario_state_manager(
        PHONE,
        current_state="COLLECTING_BOOKING_FIELDS",
        booking_type="doubles_mff",
        booking_status="doubles_supply_escort",
        escort_supply_source="escort",
        escort_supply_confirmed=True,
        incall_outcall="incall",
    )
    ctx_dict = build_context(phone_number=PHONE, message="10pm", state_manager=sm)
    ctx = CollectingCtx.from_context(ctx_dict)
    ctx.current_fields = {"incall_outcall": "incall"}
    now = datetime(2026, 5, 5, 20, 0)

    out = _available_now_inline_calendar_check(ctx, {"time": (22, 0)}, None, now)

    assert out is not None
    assert captured.get("start_from") == now + timedelta(hours=4)
