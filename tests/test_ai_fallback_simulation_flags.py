from __future__ import annotations

import pytest


@pytest.mark.skip(reason="run_ai_fallback_simulation script not yet committed to repo")
def test_flags_deposit_blanket_claim():
    from run_ai_fallback_simulation import flags_for_row  # noqa: F401

    flags = flags_for_row(
        "fallback",
        "i only have cash no deposit",
        "Deposit is required to secure all bookings, no exceptions.",
    )
    assert "deposit_blanket_claim" in flags
