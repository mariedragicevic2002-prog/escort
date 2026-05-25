"""Outcall location acknowledgement copy and venue labelling."""

from templates.booking_collection_messages import (
    format_outcall_location_check_ack,
    pick_outcall_venue_display_name,
    venue_is_au_multistate_hotel_chain,
)


def test_pick_outcall_drops_geocoder_city_region_for_hotel_name() -> None:
    vinfo = {
        "verified_hotel_name": "Perth WA",
        "city": "Perth",
        "original_address": "",
    }
    assert (
        pick_outcall_venue_display_name(
            vinfo,
            "pan pacific hotel",
            booking_city="Perth",
        )
        == "pan pacific hotel"
    )


def test_format_ack_chain_adds_address_line() -> None:
    body = format_outcall_location_check_ack(
        city="Perth",
        venue_name="Pan Pacific Perth",
        verified_address="207 Adelaide Terrace, Perth WA 6000",
    )
    assert "Pan Pacific" in body
    assert "207 Adelaide Terrace" in body
    assert "chains use the same name" in body


def test_format_ack_non_chain_no_extra_paragraph() -> None:
    body = format_outcall_location_check_ack(
        city="Perth",
        venue_name="Leedy Boutique Stay",
        verified_address="123 Street, Perth WA 6000",
    )
    assert "Just checking you're in Perth at Leedy Boutique Stay?" in body
    assert "chains use the same name" not in body


def test_venue_chain_detector_pan_pacific() -> None:
    assert venue_is_au_multistate_hotel_chain("Pan Pacific Perth") is True
    assert venue_is_au_multistate_hotel_chain("Joe's Pub") is False
