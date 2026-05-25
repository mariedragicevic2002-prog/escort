"""GFE/DGFE/PSE prompt gating: standard vs special bookings."""

from templates.booking_collection_messages import (
    experience_already_set_for_gfe_prompt,
    service_style_experience_is_set,
    special_booking_skip_gfe_style_prompt,
)
from templates.field_prompts import _experience_suffix


def test_service_style_detects_gfe_dgfe_pse() -> None:
    assert service_style_experience_is_set("GFE") is True
    assert service_style_experience_is_set(" dgfe ") is True
    assert service_style_experience_is_set("1 hr PSE") is True
    assert service_style_experience_is_set("Doubles MMF") is False
    assert service_style_experience_is_set("") is False


def test_special_booking_skips_gfe_prompt_doubles_dinner() -> None:
    assert special_booking_skip_gfe_style_prompt({"experience_type": "Doubles MMF", "booking_type": "doubles_mff"}) is True
    assert special_booking_skip_gfe_style_prompt({"booking_type": "couples_booking"}) is True
    assert special_booking_skip_gfe_style_prompt({"booking_type": "dinner_date"}) is True
    assert special_booking_skip_gfe_style_prompt({"experience_type": "dinner date"}) is True
    assert special_booking_skip_gfe_style_prompt({"doubles_type": "mmf"}) is True
    assert special_booking_skip_gfe_style_prompt({"booking_status": "doubles_supply_gate"}) is True
    assert special_booking_skip_gfe_style_prompt({"experience_type": "", "booking_type": ""}) is False


def test_experience_already_set_for_gfe_prompt_standard_vs_special() -> None:
    assert experience_already_set_for_gfe_prompt({"booking_type": None, "experience_type": None}) is False
    assert experience_already_set_for_gfe_prompt({"experience_type": "PSE"}) is True
    assert experience_already_set_for_gfe_prompt({"experience_type": "Doubles MMF", "booking_type": "doubles_mff"}) is True


def test_experience_suffix_restored_when_not_set() -> None:
    low = _experience_suffix(experience_already_set=False).lower()
    assert "gfe" in low
    assert "/experience" in low or "experience" in low

def test_experience_suffix_empty_when_set() -> None:
    assert _experience_suffix(experience_already_set=True) == ""
