"""Doubles flow: client asks for pics/profile of the escort-sourced second provider (MFF + MMF)."""

from handlers.booking_coll._shared_dinner_doubles import doubles_supply_patterns_touch
from main_v2.helpers import (
    _active_escort_sourced_doubles_flow,
    _asks_about_other_doubles_partner_media_or_identity,
    _build_doubles_other_escort_media_reply,
    build_doubles_other_escort_media_reply_bundle,
    _inactive_other_partner_deferral,
    _is_doubles_escort_sourcing_other_person,
)


def test_sourcing_state_detects_booking_status_escort():
    assert _is_doubles_escort_sourcing_other_person({"booking_status": "doubles_supply_escort"})
    assert _is_doubles_escort_sourcing_other_person({"booking_status": "doubles_supply_gate"})
    assert not _is_doubles_escort_sourcing_other_person({"booking_status": "doubles_supply_confirmed"})


def test_active_flow_requires_doubles_and_state():
    base = {
        "booking_type": "doubles_mff",
        "doubles_type": "mff",
        "booking_status": "doubles_supply_gate",
        "current_state": "COLLECTING",
    }
    assert _active_escort_sourced_doubles_flow(base, "COLLECTING")
    assert _active_escort_sourced_doubles_flow(
        {**base, "booking_status": "doubles_supply_escort", "escort_supply_source": "escort"},
        "CHECKING_AVAILABILITY",
    )
    assert not _active_escort_sourced_doubles_flow(base, "NEW")


def test_inactive_when_client_supplies_partner():
    merged = {
        "booking_type": "doubles_mff",
        "booking_status": "doubles_supply_confirmed",
        "escort_supply_source": "client",
        "current_state": "COLLECTING",
    }
    assert _inactive_other_partner_deferral(merged)
    assert not _active_escort_sourced_doubles_flow(merged, "COLLECTING")


def test_sourcing_state_detects_escort_supply_mmf_mff():
    assert _is_doubles_escort_sourcing_other_person(
        {
            "escort_supply_source": "escort",
            "booking_type": "Doubles MMF",
            "doubles_type": "mmf",
        }
    )
    assert _is_doubles_escort_sourcing_other_person(
        {
            "escort_supply_source": "escort",
            "experience_type": "doubles_mff",
        }
    )


def test_not_sourcing_when_client_brings_partner():
    assert not _is_doubles_escort_sourcing_other_person(
        {
            "escort_supply_source": "client",
            "booking_type": "doubles_mff",
            "booking_status": "doubles_supply_confirmed",
        }
    )


def test_asks_other_partner_mff_examples():
    assert _asks_about_other_doubles_partner_media_or_identity(
        "Who are you working with atm can you send me a pic or a profile link to the other girl"
    )
    assert _asks_about_other_doubles_partner_media_or_identity(
        "pic of the other escort please"
    )


def test_asks_other_partner_mmf_examples():
    assert _asks_about_other_doubles_partner_media_or_identity(
        "profile link for the other male escort?"
    )
    assert _asks_about_other_doubles_partner_media_or_identity(
        "who will you be bringing for the mmf"
    )


def test_harry_combined_message_supplies_escort_and_media_ask():
    """Combined SMS hits escort-supply patterns → webhook must not use photo fast-path (nor deferral-only bundle)."""
    msg = (
        "Can you suss out the other chick who are you working with atm can you send some pics?"
    )
    assert doubles_supply_patterns_touch(msg)
    assert _asks_about_other_doubles_partner_media_or_identity(msg)


def test_bundle_appends_gate_pickup_prompt():
    class _FakeSM:
        def __init__(self, state, fields):
            self._s = state
            self._f = fields

        def get_state(self, _phone):
            return dict(self._s)

        def get_booking_fields(self, _phone):
            return dict(self._f)

    sm = _FakeSM(
        {
            "current_state": "COLLECTING",
            "booking_status": "doubles_supply_gate",
            "booking_type": "doubles_mff",
            "doubles_type": "mff",
        },
        {},
    )
    body = build_doubles_other_escort_media_reply_bundle(sm, "+61400000000", "pics of the other girl")
    assert "working with yet" in body.lower()
    assert "bringing the other person yourself" in body.lower()


def test_generic_pic_request_not_other_partner_query():
    assert not _asks_about_other_doubles_partner_media_or_identity("any more pics?")


def test_reply_contains_other_escort_commitment():
    body = _build_doubles_other_escort_media_reply()
    assert "working with yet" in body.lower()
    assert "link to their profile" in body.lower()
    assert body.strip().endswith("x")
