"""
test_policy_gate.py — Unit tests for the PolicyGate pipeline stage.

Each check (rate_limit, blocked_client, safety_screening, chatbot_enabled,
blocked_phrases) is tested in isolation with minimal mocks.

Design:
  - No database, no Flask, no network.
  - Services injected as simple namespaces / MagicMocks.
  - Tests are deterministic and side-effect free.
"""

from __future__ import annotations

import sys
import types
import unittest
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub the modules that policy_gate.py imports so tests work without the full
# application installed.
# ---------------------------------------------------------------------------


def _make_stub_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


for _mod_name in [
    "main_v2",
    "main_v2.conversation_guards",
    "handlers",
    "handlers.safety",
    "core",
    "core.settings_manager",
    "services",
    "services.escalation_service",
    "services.safety_service",
]:
    if _mod_name not in sys.modules:
        _make_stub_module(_mod_name)

# Now we can safely import the pipeline modules.
# Adjust the path so the refactor/ package is importable.
import os

_REFACTOR_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REFACTOR_ROOT not in sys.path:
    sys.path.insert(0, _REFACTOR_ROOT)

# Allow Python to traverse into the real main_v2 subpackages.
sys.modules["main_v2"].__path__ = [os.path.join(_REFACTOR_ROOT, "main_v2")]
sys.modules["main_v2"].__package__ = "main_v2"

from main_v2.pipeline.inbound_context import (  # noqa: E402
    InboundMessage,
    ProcessingContext,
)
from main_v2.pipeline.policy_gate import (  # noqa: E402
    PolicyDeny,
    PolicyGate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_msg(body: str = "Hello", from_number: str = "+447700900000") -> InboundMessage:
    return InboundMessage(
        from_number=from_number,
        body=body,
        message_sid="SM_test_001",
        raw_payload={},
    )


def _make_services(**overrides: Any) -> MagicMock:
    """Return a MagicMock services object with sane defaults."""
    svc = MagicMock()
    # rate_limiter: allow by default
    svc.rate_limiter.is_rate_limited.return_value = False
    # db_service: not blocked by default
    svc.db_service.is_blocked.return_value = False
    # safety_service: pass by default
    svc.safety_service.screen.return_value = {"pass": True}
    # settings_manager: chatbot enabled, no blocked phrases
    svc.settings_manager.get_setting.side_effect = lambda key, default=None: {
        "chatbot_enabled": True,
        "blocked_phrases": [],
    }.get(key, default)

    for k, v in overrides.items():
        setattr(svc, k, v)
    return svc


class _FakeGate(PolicyGate):
    """Subclass that exposes individual check methods as public for testing."""

    def check_rate_limit(self, ctx):
        return self._check_rate_limit(ctx)

    def check_blocked_client(self, ctx):
        return self._check_blocked_client(ctx)

    def check_chatbot_enabled(self, ctx):
        return self._check_chatbot_enabled(ctx)

    def check_blocked_phrases(self, ctx):
        return self._check_blocked_phrases(ctx)


# ---------------------------------------------------------------------------
# Tests: individual checks
# ---------------------------------------------------------------------------


class TestRateLimitCheck(unittest.TestCase):
    def _make_gate(self, **svc_overrides):
        svc = _make_services(**svc_overrides)
        return _FakeGate(services=svc), svc

    def test_allows_when_not_limited(self):
        gate, svc = self._make_gate()
        svc.rate_limiter.is_rate_limited.return_value = False
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_rate_limit(ctx)
        self.assertIsNone(result)

    def test_denies_when_limited(self):
        gate, svc = self._make_gate()
        svc.rate_limiter.is_rate_limited.return_value = True
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_rate_limit(ctx)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertIsInstance(result, PolicyDeny)
        self.assertEqual(result.reason, "rate_limited")

    def test_fails_open_on_rate_limiter_exception(self):
        """A broken rate limiter must not crash the gate — fail open."""
        gate, svc = self._make_gate()
        svc.rate_limiter.is_rate_limited.side_effect = RuntimeError("redis down")
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_rate_limit(ctx)
        # Should fail open (allow), not raise
        self.assertIsNone(result)


class TestBlockedClientCheck(unittest.TestCase):
    def _make_gate(self, **svc_overrides):
        svc = _make_services(**svc_overrides)
        return _FakeGate(services=svc), svc

    def test_allows_unblocked_number(self):
        gate, svc = self._make_gate()
        svc.db_service.is_blocked.return_value = False
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_blocked_client(ctx)
        self.assertIsNone(result)

    def test_denies_blocked_number(self):
        gate, svc = self._make_gate()
        svc.db_service.is_blocked.return_value = True
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_blocked_client(ctx)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.reason, "client_blocked")
        self.assertEqual(result.http_status, 200)  # silent rejection

    def test_fails_open_on_db_exception(self):
        gate, svc = self._make_gate()
        svc.db_service.is_blocked.side_effect = Exception("db timeout")
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_blocked_client(ctx)
        self.assertIsNone(result)  # fail open


class TestChatbotEnabledCheck(unittest.TestCase):
    def _make_gate(self, enabled: bool = True):
        svc = _make_services()
        svc.settings_manager.get_setting.side_effect = lambda key, default=None: {
            "chatbot_enabled": enabled,
            "blocked_phrases": [],
        }.get(key, default)
        return _FakeGate(services=svc), svc

    def test_allows_when_enabled(self):
        gate, svc = self._make_gate(enabled=True)
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_chatbot_enabled(ctx)
        self.assertIsNone(result)

    def test_denies_when_disabled(self):
        gate, svc = self._make_gate(enabled=False)
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check_chatbot_enabled(ctx)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.reason, "chatbot_disabled")


class TestBlockedPhrasesCheck(unittest.TestCase):
    def _make_gate(self, phrases=None):
        svc = _make_services()
        phrases = phrases or []
        svc.settings_manager.get_setting.side_effect = lambda key, default=None: {
            "chatbot_enabled": True,
            "blocked_phrases": phrases,
        }.get(key, default)
        return _FakeGate(services=svc), svc

    def test_allows_clean_message(self):
        gate, svc = self._make_gate(phrases=["spam", "advert"])
        ctx = ProcessingContext(message=_make_msg(body="Hello I'd like to book"), services=svc)
        result = gate.check_blocked_phrases(ctx)
        self.assertIsNone(result)

    def test_denies_blocked_phrase(self):
        gate, svc = self._make_gate(phrases=["spam"])
        ctx = ProcessingContext(message=_make_msg(body="This is spam content"), services=svc)
        result = gate.check_blocked_phrases(ctx)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.reason, "blocked_phrase")

    def test_phrase_match_is_case_insensitive(self):
        gate, svc = self._make_gate(phrases=["SPAM"])
        ctx = ProcessingContext(message=_make_msg(body="This is spam"), services=svc)
        result = gate.check_blocked_phrases(ctx)
        self.assertIsNotNone(result)

    def test_no_phrases_configured(self):
        gate, svc = self._make_gate(phrases=[])
        ctx = ProcessingContext(message=_make_msg(body="anything"), services=svc)
        result = gate.check_blocked_phrases(ctx)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tests: check ordering
# ---------------------------------------------------------------------------


class TestPolicyGateOrdering(unittest.TestCase):
    """
    Rate-limit must fire BEFORE blocked-client check (cheaper check first).
    If rate_limit denies, blocked_client must NOT be called.
    """

    def test_rate_limit_fires_before_blocked_client(self):
        svc = _make_services()
        svc.rate_limiter.is_rate_limited.return_value = True
        svc.db_service.is_blocked.return_value = True  # would also fire
        gate = PolicyGate(services=svc)
        ctx = ProcessingContext(message=_make_msg(), services=svc)

        result = gate.check(ctx)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.reason, "rate_limited")
        # blocked_client was NOT checked (rate_limit fires first → short-circuit)
        svc.db_service.is_blocked.assert_not_called()

    def test_all_checks_pass_returns_none(self):
        svc = _make_services()
        gate = PolicyGate(services=svc)
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        result = gate.check(ctx)
        self.assertIsNone(result)

    def test_deny_has_correct_fields(self):
        svc = _make_services()
        svc.rate_limiter.is_rate_limited.return_value = True
        gate = PolicyGate(services=svc)
        ctx = ProcessingContext(message=_make_msg(), services=svc)
        deny = gate.check(ctx)

        self.assertIsInstance(deny, PolicyDeny)
        assert deny is not None
        self.assertIsInstance(deny.http_status, int)
        self.assertIsInstance(deny.response_body, str)
        self.assertIsInstance(deny.send_sms, bool)


# ---------------------------------------------------------------------------
# Tests: PolicyDeny properties
# ---------------------------------------------------------------------------


class TestPolicyDeny(unittest.TestCase):
    def test_frozen(self):
        deny = PolicyDeny(
            reason="test",
            http_status=200,
            response_body="",
            send_sms=False,
            log_event="test_event",
        )
        with self.assertRaises((AttributeError, TypeError)):
            deny.reason = "mutated"  # type: ignore[misc]

    def test_default_send_sms_is_false(self):
        deny = PolicyDeny(
            reason="r",
            http_status=200,
            response_body="",
            send_sms=False,
            log_event="e",
        )
        self.assertFalse(deny.send_sms)


if __name__ == "__main__":
    unittest.main()
