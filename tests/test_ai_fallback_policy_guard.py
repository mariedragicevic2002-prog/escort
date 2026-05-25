from __future__ import annotations

import handlers.ai_fallback as af


class _FakeAI:
    def __init__(self, reply: str):
        self.reply = reply
        self.called = False
        self.last_system_prompt = ""

    def chat(self, *_args, **_kwargs):
        self.called = True
        self.last_system_prompt = _kwargs.get("system_prompt", "") or ""
        return self.reply


def test_fallback_rewrites_hardcoded_pricing(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "configured rates apply")

    ctx = {
        "message": "do you do outcalls to glenelg?",
        "ai_service": _FakeAI("Yes, outcalls are $1000/hr. What date works for you?"),
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "COLLECTING_BOOKING_FIELDS"},
    }
    reply = af.get_ai_fallback_response(ctx) or ""

    assert "$" not in reply
    assert "Rates are set by booking policy" in reply


def test_fallback_enforces_required_deposit_message(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")

    ctx = {
        "message": "do you do overnight bookings?",
        "ai_service": _FakeAI("Yes, overnights are available."),
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "COLLECTING_BOOKING_FIELDS"},
    }
    reply = af.get_ai_fallback_response(ctx) or ""

    assert "deposit is required" in reply.lower()


def test_fallback_rewrites_service_denial(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")

    ctx = {
        "message": "what services can you do for couples?",
        "ai_service": _FakeAI("Couples sessions are not something I offer."),
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "COLLECTING_BOOKING_FIELDS"},
    }
    reply = af.get_ai_fallback_response(ctx) or ""

    assert "I can help with that booking request" in reply
    assert "not something i offer" not in reply.lower()


def test_confirmed_rewrites_deposit_waiver(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")

    ctx = {
        "message": "i only have cash no deposit",
        "ai_service": _FakeAI("No problem, cash is perfect, no deposit needed."),
        "message_history": [],
        "client_profile": {},
        "state": {
            "current_state": "CONFIRMED",
            "date": "2026-06-12",
            "time": "21:00",
            "duration": 60,
            "incall_outcall": "outcall",
        },
    }
    reply = af.get_ai_confirmed_booking_response(ctx) or ""

    assert "depend on your booking status" in reply.lower()
    assert "no deposit needed" not in reply.lower()


def test_fallback_rewrites_blanket_deposit_claim(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")

    ctx = {
        "message": "i only have cash no deposit",
        "ai_service": _FakeAI("Deposit is required to secure all bookings, no exceptions."),
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "COLLECTING_BOOKING_FIELDS"},
    }
    reply = af.get_ai_fallback_response(ctx) or ""

    assert "depend on booking type and status" in reply.lower()
    assert "all bookings" not in reply.lower()
    assert "no exceptions" not in reply.lower()


def test_fallback_uses_retrieval_first_for_rates(monkeypatch):
    monkeypatch.setattr(
        "handlers.ai_fallback.get_policy_snapshot",
        lambda: {
            "incall_gfe_60": 700,
            "incall_pse_60": 1000,
            "outcall_gfe_60": 800,
            "overnight": 5000,
            "outcall_surcharge": 100,
            "outcall_deposit": 100,
        },
    )
    monkeypatch.setattr("config.get_profile_url", lambda: "https://example.test/profile")
    monkeypatch.setattr(
        "core.webform_security.get_webform_url",
        lambda _phone: "https://example.test/b/UNITTEST",
    )
    ai = _FakeAI("ignore me")
    ctx = {
        "message": "what are your rates?",
        "phone_number": "+61400111222",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    reply = af.get_ai_fallback_response(ctx) or ""

    assert "full list of my rates and experiences" in reply.lower()
    assert "https://example.test/profile" in reply
    assert "https://example.test/b/UNITTEST" in reply
    assert "$" not in reply
    assert ai.called is False


def test_handle_fallback_with_ai_uses_retrieval_first(monkeypatch):
    monkeypatch.setattr(
        "handlers.ai_fallback.get_policy_snapshot",
        lambda: {
            "incall_gfe_60": 700,
            "incall_pse_60": 1000,
            "outcall_gfe_60": 800,
            "overnight": 5000,
            "outcall_surcharge": 100,
            "outcall_deposit": 100,
        },
    )
    ai = _FakeAI("ignore me")
    ctx = {
        "message": "is there a travel surcharge for outcall?",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    result = af.handle_fallback_with_ai(ctx)

    assert result["messages"]
    assert "Outcalls are within 15km" in result["messages"][0]
    assert "retrieval_policy_used" in result["actions"]
    assert ai.called is False


def test_fallback_prompt_includes_templates_first_state_layer(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")
    monkeypatch.setattr(
        "core.settings_manager.get_setting",
        lambda key, default=None: ("true" if key == "ai_templates_first" else default),
    )
    ai = _FakeAI("Sure — tell me a bit more.")
    ctx = {
        "message": "just saying hi",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    reply = af.get_ai_fallback_response(ctx) or ""

    assert reply
    assert ai.called is True
    assert "Template-first mode is enabled" in ai.last_system_prompt


def test_fallback_prompt_includes_operator_guardrails(monkeypatch):
    monkeypatch.setattr("config.get_escort_name", lambda: "Adella")
    monkeypatch.setattr("handlers.ai_fallback.get_rates_summary_snippet", lambda: "")

    ai = _FakeAI("Sure — tell me a bit more.")
    ctx = {
        "message": "just saying hi",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    reply = af.get_ai_fallback_response(ctx) or ""

    assert reply
    assert ai.called is True
    assert "Ask at most two questions in one SMS." in ai.last_system_prompt
    assert "30 mins" in ai.last_system_prompt
    assert "https://www.adella-allure.com.au/experience" in ai.last_system_prompt


def test_handle_fallback_with_ai_low_confidence_uses_template(monkeypatch):
    monkeypatch.setattr("handlers.ai_fallback._estimate_fallback_confidence", lambda _ctx: 0.10)
    monkeypatch.setattr("handlers.ai_fallback._get_fallback_confidence_threshold", lambda: 0.45)
    ai = _FakeAI("ignore me")
    ctx = {
        "message": "??",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    result = af.handle_fallback_with_ai(ctx)

    assert result["messages"]
    assert "fallback_template_low_confidence" in result["actions"]
    assert ai.called is False


def test_handle_fallback_with_ai_high_confidence_uses_ai(monkeypatch):
    monkeypatch.setattr("handlers.ai_fallback._estimate_fallback_confidence", lambda _ctx: 0.9)
    monkeypatch.setattr("handlers.ai_fallback._get_fallback_confidence_threshold", lambda: 0.45)
    ai = _FakeAI("Got it, tell me your preferred time.")
    ctx = {
        "message": "can you help me with booking",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "NEW"},
    }

    result = af.handle_fallback_with_ai(ctx)

    assert result["messages"][0].startswith("Got it")
    assert "ai_fallback_used" in result["actions"]
    assert ai.called is True


def test_handle_fallback_with_ai_uses_per_step_threshold_override(monkeypatch):
    monkeypatch.setattr("handlers.ai_fallback._estimate_fallback_confidence", lambda _ctx: 0.55)
    monkeypatch.setattr("handlers.ai_fallback._get_fallback_confidence_threshold", lambda: 0.45)
    monkeypatch.setattr(
        "core.settings_manager.get_setting",
        lambda key, default=None: (
            "0.60" if key == "ai_fallback_confidence_threshold_deposit" else default
        ),
    )
    ai = _FakeAI("ignore me")
    ctx = {
        "message": "sure",
        "ai_service": ai,
        "message_history": [],
        "client_profile": {},
        "state": {"current_state": "DEPOSIT_REQUIRED"},
    }

    result = af.handle_fallback_with_ai(ctx)

    assert result["messages"]
    assert "fallback_template_low_confidence" in result["actions"]
    assert ai.called is False
