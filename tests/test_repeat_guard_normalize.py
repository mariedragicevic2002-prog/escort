"""Repeat-message guard: normalized outbound bodies must match despite URL/token churn."""

from __future__ import annotations

from main_v2.conversation_guards import _normalize_for_repeat_check


def test_normalize_strips_https_urls_so_templates_match_across_tokens() -> None:
    base = "Unfortunately that time is outside my usual availability window"
    a = base + " https://example.com/booking/b/abc123xyz"
    b = base + " https://example.com.au/booking/b/different456"
    assert _normalize_for_repeat_check(a) == _normalize_for_repeat_check(b)


def test_normalize_collapses_whitespace_lowercase() -> None:
    assert _normalize_for_repeat_check("  Foo   BAR  ") == "foo bar"
