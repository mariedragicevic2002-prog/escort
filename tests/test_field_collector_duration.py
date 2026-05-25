"""Duration parsing: combined address lines, typos, and street-number + bare duration."""

import config as cfg
import pytest

from booking.field_collector import FieldCollector
from templates.booking_collection_messages import message_looks_like_duration_attempt


@pytest.fixture
def collector() -> FieldCollector:
    return FieldCollector(cfg)


def test_hout_typo_with_leading_street_number(collector: FieldCollector) -> None:
    assert collector._parse_duration("240 Brisbane Street Perth 1 hout") == 60


def test_hr_after_street_number(collector: FieldCollector) -> None:
    assert collector._parse_duration("240 Brisbane St Perth 1 hr") == 60


def test_second_bare_duration_after_street_rejected(collector: FieldCollector) -> None:
    """First number is a street; later bare multiple still resolves as duration."""
    assert collector._parse_duration("240 Brisbane St Perth 60") == 60


def test_message_looks_like_duration_attempt() -> None:
    assert message_looks_like_duration_attempt("240 Brisbane Street Perth 1 hout") is True
    assert message_looks_like_duration_attempt("just the address") is False
