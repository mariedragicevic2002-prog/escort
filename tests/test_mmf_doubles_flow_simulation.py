from __future__ import annotations

from datetime import date, timedelta

import pytest

from booking.mmf_exploration import decode_mmf_exploration_tags
from handlers.booking_collection import handle_provide_field
from tests.scenarios.utils import build_context, scenario_state_manager

BASE_DATE = date.today() + timedelta(days=2)

_DURATION_PHRASES = [
    "30 mins",
    "45 mins",
    "1 hr",
    "1 hour",
    "90 mins",
    "2 hrs",
    "2 hours",
    "60 mins",
] * 5  # 40 targeted simulation runs


@pytest.mark.parametrize("idx,duration_message", list(enumerate(_DURATION_PHRASES, start=1)))
def test_mmf_escort_sourced_duration_simulation_requires_mandatory_prompt_before_summary(
    idx: int,
    duration_message: str,
) -> None:
    phone = f"+6140990{idx:04d}"
    sm = scenario_state_manager(
        phone,
        current_state="COLLECTING",
        first_contact_sent=True,
        booking_type="Doubles MMF",
        experience_type="Doubles MMF",
        doubles_type="mmf",
        booking_status="doubles_supply_escort",
        escort_supply_source="escort",
        escort_supply_confirmed=True,
        incall_outcall="incall",
        date=BASE_DATE,
        time=(17, 15),
        duration=None,
        client_name="Joe",
    )
    ctx = build_context(phone_number=phone, message=duration_message, state_manager=sm)

    result = handle_provide_field(ctx)

    body = "\n".join(result.get("messages") or [])
    assert "booking summary" not in body.lower()
    assert "mmf doubles booking" in body.lower()
    assert result.get("new_state") in (None, "COLLECTING")

    st = sm.get_state(phone) or {}
    assert st.get("mmf_exploration_prompt_sent") is True
    assert st.get("duration") is not None


def test_mmf_escort_sourced_flow_resumes_after_mandatory_exploration_answer() -> None:
    phone = "+61409909999"
    sm = scenario_state_manager(
        phone,
        current_state="COLLECTING",
        first_contact_sent=True,
        booking_type="doubles_mmf",
        experience_type="doubles_mmf",
        doubles_type="mmf",
        booking_status="doubles_supply_escort",
        escort_supply_source="escort",
        escort_supply_confirmed=True,
        incall_outcall="incall",
        date=BASE_DATE,
        time=(17, 15),
        duration=None,
        client_name="Joe",
    )

    first = handle_provide_field(build_context(phone_number=phone, message="1 hr", state_manager=sm))
    first_body = "\n".join(first.get("messages") or [])
    assert "mmf doubles booking" in first_body.lower()
    assert "booking summary" not in first_body.lower()

    second = handle_provide_field(
        build_context(
            phone_number=phone,
            message="Bisexual and humiliation",
            state_manager=sm,
        )
    )
    second_body = "\n".join(second.get("messages") or [])
    assert "just to confirm you would like to book for" in second_body.lower()

    st = sm.get_state(phone) or {}
    tags = decode_mmf_exploration_tags(st.get("mmf_exploration_tags"))
    assert "bisexual" in tags
    assert "humiliation" in tags
