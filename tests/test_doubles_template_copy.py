from __future__ import annotations

from templates.special_bookings import get_threesome_clarification_template
from utils.golden_booking_rules import GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT


def test_threesome_clarification_template_includes_requested_mmf_mff_definitions() -> None:
    msg = get_threesome_clarification_template(client_name="Joe", webform_url="https://example.test/form")

    assert "Doubles MMF booking? (2 men + myself)" in msg
    assert "MFF booking? (1 man + 2 girls)" in msg
    assert "Doubles MMF or Doubles MFF" in msg
    assert "I STRONGLY recommend making your booking through my webform for doubles bookings:" in msg


def test_golden_mmf_prompt_contains_required_mandatory_wording() -> None:
    msg = GOLDEN_MMF_ESCORT_SOURCED_EXPLORATION_PROMPT

    assert "Can you please confirm what your wanting to explore in your MMF doubles booking:" in msg
    assert "* Humiliation (have me or both of us humiliate you)" in msg
    assert "* Voyeurism (Watch me get fucked by male bull)" in msg
    assert "* Bisexual (get fucked/sucked by both of us)" in msg
    assert "* Heterosexual (Just touch and fuck me only)" in msg
    assert "Please note I don't offer double penetration in MMF bookings." in msg
    assert "Let me know want your wanting so I know what male escort I need to source" in msg
