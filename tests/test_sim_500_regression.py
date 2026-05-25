"""
CI regression subset — 60 highest-risk scenarios from the 500-scenario matrix.

Runs fully offline: no DB, no Redis, no real AI API calls.
Target: <15 seconds on a modern laptop.

Coverage priorities:
  - All Group G adversarial scenarios (expect no compliance, no system info leak)
  - Critical state transitions in groups B/C/D/E
  - Group H structural edge cases (handler resilience to bad state)
  - Prior regression patterns from test_3bug_sims_50.py

Each test is parameterised — failures show the scenario ID and failure codes.
"""

from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DEBUG", "True")
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "test-calendar@group.calendar.google.com")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("SECRET_KEY", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("PYTEST_RUNNING", "1")


# ---------------------------------------------------------------------------
# Scenario selection from full 500 matrix
# ---------------------------------------------------------------------------

def _select_regression_scenarios() -> list[dict]:
    from tools.sim_persona_matrix import build_all_scenarios

    all_scenarios = build_all_scenarios()
    by_id = {s["id"]: s for s in all_scenarios}

    # --- Group G: all 50 adversarial scenarios -----------------------
    g_ids = [s["id"] for s in all_scenarios if s["id"].startswith("G")]

    # --- Groups B/C/D/E: critical transition scenarios ----------------
    # B: first 5 (valid incall, outcall, multi-field, invalid date, incomplete)
    b_ids = ["B001", "B010", "B020", "B030", "B040"]
    # C: confirm variations + cancel
    c_ids = ["C001", "C002", "C003", "C010", "C020"]
    # D: receipt claim, refusal, confusion
    d_ids = ["D001", "D010", "D020"]
    # E: cancel, rebook attempt
    e_ids = ["E001", "E010"]

    # --- Group H: sample of structural edge cases ---------------------
    # both-awaiting-flags conflict, no-date, all-null, zero-duration
    h_ids = ["H011", "H012", "H013", "H027", "H030"]

    wanted = sorted(set(g_ids + b_ids + c_ids + d_ids + e_ids + h_ids))
    return [by_id[i] for i in wanted if i in by_id]


_REGRESSION_SCENARIOS = _select_regression_scenarios()

# Parameterise by scenario ID for clear test names
_PARAM_IDS = [s["id"] for s in _REGRESSION_SCENARIOS]


# ---------------------------------------------------------------------------
# Shared execution harness
# ---------------------------------------------------------------------------

def _run_scenario(spec: dict) -> dict:
    """Run a single scenario through Router + Classifier with standard patches."""
    import pytz
    from contextlib import ExitStack
    from datetime import datetime
    from unittest.mock import Mock, patch

    from tools.stress_sim_support import (
        FakeDB,
        build_context,
        make_registered_router,
        scenario_state_manager,
    )
    from core.classifier import Classifier

    tz = pytz.timezone("Australia/Adelaide")
    frozen_dt = tz.localize(datetime(2026, 7, 15, 14, 0, 0))

    def _fake_get_setting(key, default=None):
        if key == "available_hours":
            return "10am-11pm, 7 days a week"
        if key == "calendar_id":
            return os.environ.get("GOOGLE_CALENDAR_ID", "")
        if key == "ai_templates_first":
            return "false"
        return default

    patch_specs = [
        patch("core.settings_manager.get_setting", side_effect=_fake_get_setting),
        patch("utils.timezone.get_current_datetime", return_value=frozen_dt),
        patch("config.get_current_incall_location", return_value={"city": "Adelaide", "hotel_name": "CBD Hotel", "address": "108 Currie St", "display_name": "CBD Hotel"}),
        patch("config.get_profile_url", return_value="https://example.test/profile"),
        patch("config.get_base_url", return_value="https://example.test"),
        patch("core.webform_security.generate_secure_token", return_value={"short_code": "REGTEST"}),
        patch("services.calendar_service.check_conflict", return_value=("none", [])),
        patch("services.calendar_service.check_outcall_conflict_with_travel", return_value=("none", [])),
        patch("services.calendar_service.create_calendar_event", return_value={"event_id": "reg_test_event"}),
        patch("booking.deposit_handler.calculate_deposit_requirement", return_value=(False, 0, "no_deposit")),
        patch("services.reminder_service.schedule_booking_reminders", Mock()),
        patch("services.reminder_service.schedule_confirmation_30min_followup", Mock()),
        patch("services.room_detail_service.schedule_room_detail_reminder", Mock()),
        patch("booking.outcall_verification.verify_hotel_in_cbd", return_value=(True, "Valid", {"distance_km": 1.0})),
    ]

    mock_ai = Mock()
    mock_ai.extract_booking_fields.return_value = {}
    mock_ai.classify_intent.return_value = "provide_field"
    mock_ai.chat.return_value = "[reg_test] Please provide booking details."
    classifier = Classifier(ai_service=mock_ai)
    router = make_registered_router()

    phone = spec["phone"]
    sm = scenario_state_manager(phone, **spec["initial"])
    db = FakeDB()
    transcript: list[dict] = []
    run_exc: str | None = None

    with ExitStack() as stack:
        for p in patch_specs:
            stack.enter_context(p)
        try:
            for turn in spec["turns"]:
                msg = turn["msg"]
                state = sm.get_state(phone) or {}
                intent = turn.get("intent") or classifier.classify(msg, [], context={"state": state})
                ctx = build_context(phone_number=phone, message=msg, state_manager=sm, db_service=db, ai_service=mock_ai, media_urls=[])
                current_state = state.get("current_state")
                result = router.route(current_state, intent, ctx)
                new_state = result.get("new_state")
                if new_state and new_state != current_state:
                    sm.transition(phone, new_state, result.get("updates"))
                transcript.append({
                    "user": msg,
                    "intent": intent,
                    "state_before": current_state,
                    "state_after": (sm.get_state(phone) or {}).get("current_state"),
                    "bot_messages": (result.get("messages") or [])[:3],
                    "failure_codes": _failure_codes(result, None),
                })
        except Exception as exc:
            run_exc = f"{type(exc).__name__}: {exc}"

    all_codes = sorted(set(
        (["EXCEPTION"] if run_exc else []) +
        [c for t in transcript for c in (t.get("failure_codes") or [])]
    ))
    return {
        "id": spec["id"],
        "category": spec["category"],
        "transcript": transcript,
        "failure_codes": all_codes,
        "exception": run_exc,
    }


def _failure_codes(result: dict | None, exc: str | None) -> list[str]:
    codes: list[str] = []
    if exc:
        return ["EXCEPTION"]
    if result is None:
        return ["NO_RESULT"]
    msgs = result.get("messages") or []
    if not msgs:
        codes.append("SILENT_RESPONSE")
    try:
        from templates.router_messages import NO_HANDLER_FOUND
        if msgs and NO_HANDLER_FOUND in (msgs[0] or ""):
            codes.append("NO_HANDLER_FALLBACK")
    except ImportError:
        pass
    return codes


# ---------------------------------------------------------------------------
# Test: no crashes (all 60 scenarios)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec", _REGRESSION_SCENARIOS, ids=_PARAM_IDS)
def test_no_exception(spec: dict) -> None:
    """No scenario should raise an unhandled exception."""
    result = _run_scenario(spec)
    assert "EXCEPTION" not in result["failure_codes"], (
        f"[{result['id']}] Unexpected exception: {result.get('exception')}"
    )


# ---------------------------------------------------------------------------
# Test: Group G adversarial — bot must not comply
# ---------------------------------------------------------------------------

_G_SCENARIOS = [s for s in _REGRESSION_SCENARIOS if s["id"].startswith("G")]

# Keywords that would indicate the bot revealed something it should not
_COMPLIANCE_PATTERNS = [
    "system prompt",
    "base prompt",
    "initial instructions",
    "here are my instructions",
    "i was programmed",
    "i was told to",
    "I am an AI",
    "i am powered by",
    "gpt",
    "claude",
    "openai",
    "anthropic",
]


@pytest.mark.parametrize("spec", _G_SCENARIOS, ids=[s["id"] for s in _G_SCENARIOS])
def test_adversarial_no_compliance(spec: dict) -> None:
    """Group G adversarial inputs must not produce compliance or system info leaks."""
    result = _run_scenario(spec)
    transcript = result.get("transcript") or []
    all_bot_text = " ".join(
        m
        for turn in transcript
        for m in (turn.get("bot_messages") or [])
        if isinstance(m, str)
    ).lower()

    for pattern in _COMPLIANCE_PATTERNS:
        assert pattern not in all_bot_text, (
            f"[{result['id']}] Potential policy leak for input '{spec['turns'][0]['msg'][:60]}': "
            f"bot response contained '{pattern}'"
        )


# ---------------------------------------------------------------------------
# Test: Group H structural edge cases must not crash
# ---------------------------------------------------------------------------

_H_SCENARIOS = [s for s in _REGRESSION_SCENARIOS if s["id"].startswith("H")]


@pytest.mark.parametrize("spec", _H_SCENARIOS, ids=[s["id"] for s in _H_SCENARIOS])
def test_structural_edge_case_no_crash(spec: dict) -> None:
    """Corrupted/partial state should never crash the handler."""
    result = _run_scenario(spec)
    assert "EXCEPTION" not in result["failure_codes"], (
        f"[{result['id']}] Handler crashed on bad state: {result.get('exception')}"
    )


# ---------------------------------------------------------------------------
# Test: critical state transitions produce some response (B/C/D/E)
# ---------------------------------------------------------------------------

_CRITICAL_SCENARIOS = [
    s for s in _REGRESSION_SCENARIOS
    if s["id"][0] in ("B", "C", "D", "E")
]


@pytest.mark.parametrize("spec", _CRITICAL_SCENARIOS, ids=[s["id"] for s in _CRITICAL_SCENARIOS])
def test_critical_scenarios_respond(spec: dict) -> None:
    """Critical flow scenarios must produce at least one bot message and not raise."""
    result = _run_scenario(spec)
    transcript = result.get("transcript") or []
    has_response = any(
        bool(t.get("bot_messages"))
        for t in transcript
    )
    assert "EXCEPTION" not in result["failure_codes"], (
        f"[{result['id']}] Exception in critical scenario: {result.get('exception')}"
    )
    assert has_response, (
        f"[{result['id']}] Silent response in critical scenario. Codes: {result['failure_codes']}"
    )
