"""Tests for MMF exploration helpers."""

from __future__ import annotations

import json

from booking import mmf_exploration as me


def test_decode_encode_roundtrip():
    s = me.encode_mmf_exploration_tags(["voyeurism", "bisexual"])
    assert json.loads(s) == ["voyeurism", "bisexual"]
    assert me.decode_mmf_exploration_tags(s) == ["voyeurism", "bisexual"]


def test_parse_reply_keywords():
    assert "humiliation" in me.parse_mmf_exploration_reply("into humiliation thanks")
    assert "voyeurism" in me.parse_mmf_exploration_reply("voyeurism and bull stuff")
    assert "bisexual" in me.parse_mmf_exploration_reply("bisexual please")
    assert "heterosexual" in me.parse_mmf_exploration_reply("straight only")


def test_escort_organises_male_for_mmf():
    base = {
        "booking_type": "Doubles MMF",
        "experience_type": "Doubles MMF",
        "escort_supply_source": "escort",
        "booking_status": "doubles_supply_escort",
    }
    assert me.escort_organises_male_for_mmf(base)
    spaced = {**base, "booking_type": "doubles mmf", "experience_type": "Doubles MMF"}
    assert me.escort_organises_male_for_mmf(spaced)
    assert not me.escort_organises_male_for_mmf(
        {
            **base,
            "escort_supply_source": "client",
            "booking_status": "doubles_supply_confirmed",
        }
    )


def test_should_append_calendar_only_when_tags_and_escort_sources():
    det = {
        "booking_type": "Doubles MMF",
        "escort_supply_source": "escort",
        "mmf_exploration_tags": '["heterosexual"]',
    }
    assert me.should_append_mmf_exploration_to_calendar(det)
    assert not me.should_append_mmf_exploration_to_calendar({**det, "mmf_exploration_tags": "[]"})


def test_format_calendar_line_accepts_json_string():
    line = me.format_mmf_exploration_calendar_line('["humiliation", "bisexual"]')
    assert line.startswith("MMF Exploration:")
    assert "Humiliation" in line and "Bisexual" in line


def test_parse_reply_does_not_treat_clock_times_as_option_numbers():
    assert me.parse_mmf_exploration_reply("same time 3pm works") == []
    assert me.parse_mmf_exploration_reply("jan 15") == []


def test_parse_reply_accepts_compact_digit_selection():
    assert me.parse_mmf_exploration_reply("13") == ["humiliation", "bisexual"]
    assert me.parse_mmf_exploration_reply("2 4") == ["voyeurism", "heterosexual"]


def test_sms_prompt_uses_golden_rule_body_for_incall():
    from utils import golden_booking_rules as gbr

    assert me.mmf_exploration_sms_prompt({}) == gbr.GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT
    assert me.mmf_exploration_sms_prompt({"incall_outcall": "incall"}) == gbr.GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT


def test_schedule_should_show_mmf_preferences_never_dinner_date():
    assert not me.schedule_should_show_mmf_preferences(
        {
            "experience": "Dinner Date",
            "organise_other_escort": "yes",
            "booking_type": "dinner_date",
        }
    )


def test_schedule_should_show_mmf_preferences_escort_sourced_Doubles_MMF_only():
    assert me.schedule_should_show_mmf_preferences(
        {"experience": "Doubles MMF", "organise_other_escort": "yes"}
    )
    assert not me.schedule_should_show_mmf_preferences(
        {"experience": "Doubles MMF", "organise_other_escort": "no"}
    )
    assert not me.schedule_should_show_mmf_preferences(
        {"experience": "Doubles MFF", "organise_other_escort": "yes"}
    )


def test_scrub_schedule_mmf_preferences_clears_when_not_applicable():
    d = {
        "experience": "Dinner Date",
        "organise_other_escort": "",
        "preferences": "Humiliation, Bisexual",
    }
    me.scrub_schedule_mmf_preferences(d)
    assert d["preferences"] == ""

    ok = {
        "experience": "Doubles MMF",
        "organise_other_escort": "yes",
        "preferences": "Humiliation",
    }
    me.scrub_schedule_mmf_preferences(ok)
    assert ok["preferences"] == "Humiliation"
