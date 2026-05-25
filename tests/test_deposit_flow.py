"""
Deposit-validation regression tests.

The rule (from deposit_flow.py): after 3 failed screenshot validations in a single booking,
the client is blocked and the escort is notified. Before the 3rd failure, we only increment
the counter and ask for a re-upload.

These tests mock out:
  - the image download (requests.get)
  - the Vision API validation (validate_deposit_screenshot_from_bytes)
  - the escort-notification call
so nothing leaves the process.
"""

from __future__ import annotations

import pytest

from handlers import deposit_flow
from tests.fakes import FakeStateManager


class _FakeResponse:
    def __init__(self, content: bytes = b"fake-image-bytes"):
        self.content = content

    def raise_for_status(self):
        return None


@pytest.fixture
def _patch_deposit_dependencies(monkeypatch):
    """
    Patch the three outbound calls handle_deposit_screenshot makes:
      - requests.get (image download)
      - validate_deposit_screenshot_from_bytes (Vision API)
      - notify_escort_deposit_validation_failed
    Returns a dict the test can mutate to control the Vision result + inspect notifications.
    """
    state = {
        "vision_result": {
            "valid": False,
            "details": {"payid_found": False, "amount_found": True, "date_found": True},
        },
        "escort_notified_for": [],
    }

    # 1. requests.get — pretend the download succeeded
    import requests
    monkeypatch.setattr(requests, "get", lambda *a, **kw: _FakeResponse())

    # 2. vision validation — imported inside handle_deposit_screenshot, so patch the source
    import services.vision_service as vision_service
    monkeypatch.setattr(
        vision_service,
        "validate_deposit_screenshot_from_bytes",
        lambda image_content, phone_number, required_amount, expected_reference: state["vision_result"],
    )

    # 3. escort notification — patch at source module
    import services.notification_service as notification_service

    def _fake_notify(phone, errors):
        state["escort_notified_for"].append((phone, list(errors)))

    monkeypatch.setattr(
        notification_service,
        "notify_escort_deposit_validation_failed",
        _fake_notify,
    )

    return state


def _make_context(state_manager: FakeStateManager, phone: str, attempts: int) -> dict:
    """Build the context dict handle_deposit_screenshot expects."""
    return {
        "phone_number": phone,
        "media_urls": ["https://example.test/screenshot.jpg"],
        "state": {
            "phone_number": phone,
            "current_state": "DEPOSIT_REQUIRED",
            "deposit_amount": 50,
            "deposit_payment_reference": None,
            "deposit_screenshot_attempts": attempts,
        },
        "state_manager": state_manager,
    }


# --- the contract -------------------------------------------------------------------------


def test_first_invalid_attempt_increments_counter_no_block(_patch_deposit_dependencies):
    phone = "+61400000010"
    sm = FakeStateManager(initial={phone: {"deposit_screenshot_attempts": 0}})
    ctx = _make_context(sm, phone, attempts=0)

    result = deposit_flow.handle_deposit_screenshot(ctx)

    # Counter bumps to 1; client not blocked; stays in DEPOSIT_REQUIRED
    assert any(u.get("deposit_screenshot_attempts") == 1 for _p, u in sm.updates)
    assert sm.blocks == []
    assert _patch_deposit_dependencies["escort_notified_for"] == []
    assert result["new_state"] is None  # stay in DEPOSIT_REQUIRED


def test_second_invalid_attempt_still_no_block(_patch_deposit_dependencies):
    phone = "+61400000011"
    sm = FakeStateManager(initial={phone: {"deposit_screenshot_attempts": 1}})
    ctx = _make_context(sm, phone, attempts=1)

    deposit_flow.handle_deposit_screenshot(ctx)

    assert any(u.get("deposit_screenshot_attempts") == 2 for _p, u in sm.updates)
    assert sm.blocks == []
    assert _patch_deposit_dependencies["escort_notified_for"] == []


def test_third_invalid_attempt_blocks_client_and_notifies_escort(_patch_deposit_dependencies):
    """THE regression: after 3 failed validations we MUST block the client and notify the escort."""
    phone = "+61400000012"
    sm = FakeStateManager(initial={phone: {"deposit_screenshot_attempts": 2}})
    ctx = _make_context(sm, phone, attempts=2)

    result = deposit_flow.handle_deposit_screenshot(ctx)

    # Counter bumps to 3, client blocked, escort notified, state resets to NEW
    assert any(u.get("deposit_screenshot_attempts") == 3 for _p, u in sm.updates)
    assert len(sm.blocks) == 1
    assert sm.blocks[0][0] == phone
    assert sm.blocks[0][1] == "deposit_validation_failed"
    assert len(_patch_deposit_dependencies["escort_notified_for"]) == 1
    assert _patch_deposit_dependencies["escort_notified_for"][0][0] == phone

    assert result["new_state"] == "NEW"
    assert "block_client" in result["actions"]
    assert "notify_escort" in result["actions"]


def test_no_media_just_prompts_for_screenshot(_patch_deposit_dependencies):
    """If the client didn't attach a screenshot, ask for one — don't count as a failure."""
    phone = "+61400000014"
    sm = FakeStateManager(initial={phone: {"deposit_screenshot_attempts": 0}})
    ctx = _make_context(sm, phone, attempts=0)
    ctx["media_urls"] = []

    result = deposit_flow.handle_deposit_screenshot(ctx)

    assert result["new_state"] is None
    assert "text message alone" in (result["messages"][0] or "").lower()
    assert sm.blocks == []
    # No attempt counter bump — no screenshot was even evaluated
    assert not any("deposit_screenshot_attempts" in u for _p, u in sm.updates)
