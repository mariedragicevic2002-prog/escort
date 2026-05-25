from __future__ import annotations

from datetime import date, datetime

import pytz

from handlers.new_conv.booking import handle_book_appointment, handle_cancel_booking_new
from handlers.new_conv.availability import handle_ask_availability
from handlers.new_conv.enquiries_simple import handle_ask_rates
from handlers.new_conv.enquiries_simple import handle_new_ambiguous
from handlers.new_conv.enquiries_simple import handle_location_enquiry
from handlers.new_conv.greeting import handle_greeting
from handlers.new_conv.outcall import handle_request_outcall
from templates.errors import get_error_message
from tests.scenarios.utils import build_context, scenario_state_manager

PHONE = "+61400999335"


def test_handle_cancel_booking_new_clears_collection_state():
    sm = scenario_state_manager(
        PHONE,
        current_state="NEW",
        date=date(2026, 6, 3),
        time=(15, 0),
        duration=60,
        client_name="Alex",
    )
    ctx = build_context(phone_number=PHONE, message="cancel booking", state_manager=sm)
    result = handle_cancel_booking_new(ctx)

    assert result["new_state"] == "NEW"
    assert result["messages"] == ["No worries! Let me know if you'd like to book another time."]
    st = sm.get_state(PHONE) or {}
    assert st.get("current_state") == "NEW"
    assert st.get("date") is None
    assert st.get("time") is None
    assert st.get("duration") is None


def test_handle_book_appointment_rejects_invalid_numeric_date_literal():
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="book me on 32/13/2026 at 2pm",
        state_manager=sm,
    )
    result = handle_book_appointment(ctx)

    assert result["new_state"] == "COLLECTING"
    assert result["messages"] == [f"❌ {get_error_message('invalid_date')}"]
    st = sm.get_state(PHONE) or {}
    assert st.get("first_contact_sent") is True


def test_handle_ask_rates_first_contact_points_to_profile_and_webform(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="what are your rates",
        state_manager=sm,
    )

    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")

    result = handle_ask_rates(ctx)
    message = "\n".join(result.get("messages") or [])
    assert "full list of my rates and experiences" in message.lower()
    assert "https://example.test/profile" in message
    assert "https://example.test/b/UNITTEST" in message
    assert "$" not in message
    st = sm.get_state(PHONE) or {}
    assert st.get("first_contact_sent") is True


def test_handle_ask_rates_hourly_question_also_points_to_profile(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="how much for 1 hour",
        state_manager=sm,
    )

    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")

    result = handle_ask_rates(ctx)
    message = "\n".join(result.get("messages") or [])
    assert "https://example.test/profile" in message
    assert "https://example.test/b/UNITTEST" in message
    assert "$" not in message


def test_handle_location_enquiry_answers_location_first_then_slots(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="where are you located",
        state_manager=sm,
    )
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 6, 10, 12, 0, 0))

    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)
    monkeypatch.setattr(
        "config.get_current_incall_location",
        lambda: {
            "city": "Adelaide",
            "hotel_name": "CBD Hotel",
            "display_name": "CBD Hotel",
            "address": "108 Currie St",
        },
    )
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")
    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )
    monkeypatch.setattr(
        "handlers.new_conv._shared.get_availability_window_label",
        lambda _label_slots, now=None: "today",
    )

    def _slot(hour):
        dt = tz.localize(datetime(2026, 6, 10, hour, 0, 0))
        return (dt, f"Wed 10th June {hour - 12 if hour > 12 else hour}:00pm")

    monkeypatch.setattr(
        "utils.availability_slots.get_next_available_time_slots",
        lambda *_args, **_kwargs: [_slot(18), _slot(19), _slot(20)],
    )

    result = handle_location_enquiry(ctx)

    assert result["new_state"] == "COLLECTING"
    assert result["messages"] == [
        "Hi I'm located at CBD Hotel 108 Currie St Adelaide\n\n"
        "If you would like to make a booking I'm available at these times today:\n\n"
        "• Wed 10th June 6:00pm\n"
        "• Wed 10th June 7:00pm\n"
        "• Wed 10th June 8:00pm\n\n"
        "https://example.test/profile\n\n"
        "Let me know what time suits you?\n\n"
        "Or alternatively you can make a booking using my webform\n"
        "https://example.test/b/UNITTEST"
    ]


def test_handle_request_outcall_answers_policy_first_for_hotel_enquiry(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(
        phone_number=PHONE,
        message="can you come to my hotel",
        state_manager=sm,
    )

    monkeypatch.setattr(
        "config.get_current_incall_location",
        lambda: {
            "city": "Adelaide",
            "hotel_name": "CBD Hotel",
            "display_name": "CBD Hotel",
            "address": "108 Currie St",
        },
    )
    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )

    result = handle_request_outcall(ctx)

    assert result["new_state"] == "COLLECTING"
    assert result["messages"] == [
        "Hi I only do outcalls to hotels or apartments within 15km of Adelaide CBD. "
        "There is a $100 surcharge + $100 deposit required.\n\n"
        "Send me a text if you want to make a booking or alternatively use my booking webform "
        "https://example.test/b/UNITTEST"
    ]
    state = sm.get_state(PHONE) or {}
    assert state.get("incall_outcall") == "outcall"
    assert state.get("first_contact_sent") is True


def test_handle_greeting_plain_hi_stays_in_new_and_asks_about_booking():
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(phone_number=PHONE, message="hi there", state_manager=sm)

    result = handle_greeting(ctx)

    assert result["new_state"] is None
    assert result["messages"] == ["Hey how are you going? Did you want to make a booking?"]
    st = sm.get_state(PHONE) or {}
    assert st.get("current_state") == "NEW"
    assert not st.get("first_contact_sent")


def test_handle_greeting_good_morning_uses_matching_reply():
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(phone_number=PHONE, message="good morning", state_manager=sm)

    result = handle_greeting(ctx)

    assert result["new_state"] is None
    assert result["messages"] == ["Good morning to you as well. Did you want to make a booking?"]
    st = sm.get_state(PHONE) or {}
    assert st.get("current_state") == "NEW"
    assert not st.get("first_contact_sent")


def test_handle_new_ambiguous_uses_client_name_and_webform(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW", client_name="Newton")
    ctx = build_context(
        phone_number=PHONE,
        message="He escaped in the car with the dead body",
        state_manager=sm,
    )

    monkeypatch.setattr("templates.greetings.extract_client_name", lambda _msg: "")
    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )

    result = handle_new_ambiguous(ctx)

    assert result["new_state"] is None
    assert result["messages"] == [
        "Hi Newton I didn't quite catch that. If you'd like to make a booking just let me know "
        "what date, time and duration your wanting.  Or to speed things up fill in my booking "
        "webform. https://example.test/b/UNITTEST"
    ]


def test_handle_ask_availability_tonight_prefers_8pm_then_backfills(monkeypatch):
    sm = scenario_state_manager(PHONE, current_state="NEW")
    ctx = build_context(phone_number=PHONE, message="are you free tonight", state_manager=sm)
    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 6, 10, 12, 0, 0))

    monkeypatch.setattr("utils.timezone.get_current_datetime", lambda: frozen)
    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )
    monkeypatch.setattr(
        "config.get_current_incall_location",
        lambda: {
            "city": "Adelaide",
            "hotel_name": "CBD Hotel",
            "display_name": "CBD Hotel",
            "address": "108 Currie St",
        },
    )
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")

    def _slot(hour):
        dt = tz.localize(datetime(2026, 6, 10, hour, 0, 0))
        return (dt, f"Wed 10th June {hour - 12 if hour > 12 else hour}:00pm")

    def _fake_slots(_now, num_slots=3, check_calendar=True, start_from=None, end_by=None, **_kwargs):
        _ = (_now, num_slots, check_calendar, end_by)
        if start_from and start_from.hour >= 20:
            return [_slot(20)]
        if start_from and start_from.hour == 18:
            return [_slot(18), _slot(19)]
        return []

    monkeypatch.setattr(
        "utils.availability_slots.get_next_available_time_slots",
        _fake_slots,
    )
    monkeypatch.setattr(
        "templates.greetings.get_available_now_message",
        lambda **kwargs: " | ".join(slot for _, slot in kwargs["time_slots"]),
    )

    result = handle_ask_availability(ctx)

    assert result["new_state"] == "COLLECTING"
    assert result["messages"] == ["Wed 10th June 6:00pm | Wed 10th June 7:00pm | Wed 10th June 8:00pm"]
