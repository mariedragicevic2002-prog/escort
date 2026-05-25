"""Regression: booking time must not be taken from pasted log/metadata clocks."""

from __future__ import annotations

import config as cfg
import pytest

from booking.field_collector import FieldCollector


@pytest.fixture
def collector() -> FieldCollector:
    return FieldCollector(cfg)


def test_parse_time_7pm_wins_over_trailing_au_log_timestamp(collector: FieldCollector) -> None:
    raw = "7pm 1hr\n05/05/2026, 23:00:00"
    assert collector._parse_time(raw, duration_minutes=60) == (19, 0)


def test_parse_time_7pm_wins_over_trailing_au_log_timestamp_with_early_evening(collector: FieldCollector) -> None:
    raw = "7pm 1hr\n05/05/2026, 06:35:18"
    assert collector._parse_time(raw, duration_minutes=60) == (19, 0)


def test_parse_time_7pm_wins_over_iso_timestamp_line(collector: FieldCollector) -> None:
    raw = "7pm 1hr\n2026-05-05T06:35:18Z"
    assert collector._parse_time(raw, duration_minutes=60) == (19, 0)


def test_parse_time_colon_with_suffix_am_pm_on_token(collector: FieldCollector) -> None:
    assert collector._parse_time("see you at 3:30pm", duration_minutes=None) == (15, 30)


def test_parse_time_colonless_hhmm_nearest_future(monkeypatch, collector: FieldCollector) -> None:
    """Colonless '430' without am/pm → nearest future reading (4:30 pm at 11:25 am)."""
    from datetime import datetime

    import pytz

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 11, 25, 0))
    monkeypatch.setattr("booking.field_collector.get_current_datetime", lambda: frozen)

    assert collector._parse_time("430", duration_minutes=None) == (16, 30)


def test_parse_time_colonless_hhmm_445_at_noon_nearest_future(monkeypatch, collector: FieldCollector) -> None:
    """Bare '445' at ~noon → 4:45pm (never 4:45am). Golden rule regression."""
    from datetime import datetime

    import pytz

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 12, 15, 0))
    monkeypatch.setattr("booking.field_collector.get_current_datetime", lambda: frozen)

    assert collector._parse_time("445", duration_minutes=None) == (16, 45)


def test_parse_time_colonless_hhmm_445_at_2am_nearest_future(monkeypatch, collector: FieldCollector) -> None:
    """Bare '445' at 2am → next 4:45am same morning."""
    from datetime import datetime

    import pytz

    tz = pytz.timezone("Australia/Adelaide")
    frozen = tz.localize(datetime(2026, 5, 5, 2, 0, 0))
    monkeypatch.setattr("booking.field_collector.get_current_datetime", lambda: frozen)

    assert collector._parse_time("445", duration_minutes=None) == (4, 45)


def test_strip_log_timestamp_fragments_module() -> None:
    from booking.field_collector import _strip_log_timestamp_fragments

    s = _strip_log_timestamp_fragments("7pm please\n05/05/2026, 23:00:00")
    assert "23:00" not in s
    assert "7pm" in s
