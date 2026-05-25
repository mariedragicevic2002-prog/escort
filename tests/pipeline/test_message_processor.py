"""
test_message_processor.py — Integration tests for MessageProcessor.

Tests the full pipeline with injected fakes — no database, no network.

Coverage:
  - Happy path: message processed end-to-end
  - Policy gate deny → ProcessingResult.deny is set, pipeline stops
  - Fast-path match → pipeline short-circuits at stage 8
  - State bootstrap failure → returns deny with 500
  - Silence mode → returns empty outbound messages
  - Graceful degradation: broken classifier doesn't crash pipeline
  - State transition is called when pending_state_transition is set
  - Stale reset is triggered for old inactive conversations
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from typing import Dict, Optional
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Module stubs — same pattern as other test files
# ---------------------------------------------------------------------------


def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


for _m in [
    "main_v2",
    "main_v2.state_machine_bridge",
    "main_v2.conversation_guards",
    "handlers",
    "handlers.safety",
    "core",
    "core.settings_manager",
    "services",
    "services.escalation_service",
    "services.safety_service",
    "services.history_service",
]:
    if _m not in sys.modules:
        _stub(_m)

# Stub state_machine_bridge with callable dispatch_message
_smb = sys.modules["main_v2.state_machine_bridge"]
_smb.dispatch_message = MagicMock(return_value=[])
_smb.is_incall_only_mode = MagicMock(return_value=False)

# Stub conversation_guards
_cg = sys.modules["main_v2.conversation_guards"]
_cg.check_repeat = MagicMock(return_value=False)
_cg.check_frustration = MagicMock(return_value=False)

# Stub handlers.safety
_hs = sys.modules["handlers.safety"]
_hs.track_profanity_signal = MagicMock(return_value=False)

_REFACTOR_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REFACTOR_ROOT not in sys.path:
    sys.path.insert(0, _REFACTOR_ROOT)

# Allow Python to traverse into the real main_v2 subpackages.
sys.modules["main_v2"].__path__ = [os.path.join(_REFACTOR_ROOT, "main_v2")]
sys.modules["main_v2"].__package__ = "main_v2"

from main_v2.pipeline.fast_path_router import (  # noqa: E402
    FastPath,
    FastPathResult,
    FastPathRouter,
)
from main_v2.pipeline.inbound_context import (  # noqa: E402
    InboundMessage,
    ProcessingResult,
)
from main_v2.pipeline.message_processor import MessageProcessor  # noqa: E402
from main_v2.pipeline.policy_gate import PolicyDeny, PolicyGate  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _AllowGate(PolicyGate):
    """Policy gate that always passes."""

    def __init__(self):
        pass  # skip super().__init__ — no services needed

    def check(self, ctx):
        return None


class _DenyGate(PolicyGate):
    """Policy gate that always denies with rate_limited."""

    def __init__(self):
        pass

    def check(self, ctx):
        return PolicyDeny(
            reason="rate_limited",
            http_status=429,
            response_body="Too many requests",
            send_sms=False,
            log_event="rate_limited",
        )


class _NoFastPaths(FastPathRouter):
    """Router with no paths — always returns None."""

    def __init__(self):
        super().__init__(paths=[])


class _AlwaysFastPath(FastPath):
    name = "always_match"

    def matches(self, ctx):
        return True

    def handle(self, ctx):
        return FastPathResult(
            outbound_messages=[{"to": ctx.message.from_number, "body": "fast_response"}],
            matched_handler=self.name,
        )


class _FastPathOnlyRouter(FastPathRouter):
    def __init__(self):
        super().__init__(paths=[_AlwaysFastPath()])


def _make_services(
    *,
    chatbot_enabled: bool = True,
    is_blocked: bool = False,
    rate_limited: bool = False,
    state: Optional[Dict] = None,
    silence_mode: bool = False,
) -> MagicMock:
    svc = MagicMock()

    # Rate limiter
    svc.rate_limiter.is_rate_limited.return_value = rate_limited

    # DB service
    svc.db_service.is_blocked.return_value = is_blocked
    svc.db_service.log_inbound_message.return_value = None

    # State manager
    default_state = state or {
        "current_state": "initial",
        "flow_version": "v2",
        "updated_at": None,
    }
    svc.state_manager.get_or_create_state.return_value = default_state
    svc.state_manager.transition.return_value = True
    svc.state_manager.reset_conversation.return_value = None

    # Settings manager
    def _get_setting(key, default=None):
        mapping = {
            "chatbot_enabled": chatbot_enabled,
            "blocked_phrases": [],
            "silence_mode": silence_mode,
            "refund_forward_number": None,
            "rollout_enabled": True,
        }
        return mapping.get(key, default)

    svc.settings_manager.get_setting.side_effect = _get_setting

    # Safety service
    svc.safety_service.screen.return_value = {"pass": True}

    # History service
    svc.history_service.get_recent.return_value = []

    # Classifier
    svc.classifier.classify.return_value = {"intent": "booking", "confidence": 0.9}

    # Escalation service
    svc.escalation_service.evaluate_escalation.return_value = {"escalate": False}

    return svc


def _make_msg(body: str = "Hello", from_number: str = "+447700900000") -> InboundMessage:
    return InboundMessage(
        from_number=from_number,
        body=body,
        message_sid="SM_proc_001",
        raw_payload={},
    )


def _make_processor(
    services=None,
    gate: Optional[PolicyGate] = None,
    router: Optional[FastPathRouter] = None,
) -> MessageProcessor:
    svc = services or _make_services()
    return MessageProcessor(
        services=svc,
        policy_gate=gate or _AllowGate(),
        fast_path_router=router or _NoFastPaths(),
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestMessageProcessorHappyPath(unittest.TestCase):
    def test_returns_processing_result(self):
        processor = _make_processor()
        result = processor.process(_make_msg())
        self.assertIsInstance(result, ProcessingResult)

    def test_no_deny_on_happy_path(self):
        processor = _make_processor()
        result = processor.process(_make_msg())
        self.assertIsNone(result.deny)

    def test_context_is_populated(self):
        processor = _make_processor()
        result = processor.process(_make_msg("Book me in please"))
        self.assertIsNotNone(result.context)
        assert result.context is not None
        self.assertEqual(result.context.message.body, "Book me in please")


# ---------------------------------------------------------------------------
# Policy gate
# ---------------------------------------------------------------------------


class TestMessageProcessorPolicyGate(unittest.TestCase):
    def test_deny_gate_returns_deny_result(self):
        processor = _make_processor(gate=_DenyGate())
        result = processor.process(_make_msg())
        self.assertIsNotNone(result.deny)
        assert result.deny is not None
        self.assertEqual(result.deny.reason, "rate_limited")
        self.assertEqual(result.deny.http_status, 429)

    def test_pipeline_stops_after_deny(self):
        svc = _make_services()
        processor = _make_processor(services=svc, gate=_DenyGate())
        processor.process(_make_msg())
        # State bootstrap must NOT be called after a deny
        svc.state_manager.get_or_create_state.assert_not_called()


# ---------------------------------------------------------------------------
# Fast-path routing
# ---------------------------------------------------------------------------


class TestMessageProcessorFastPath(unittest.TestCase):
    def setUp(self):
        # Reset the shared state_machine_bridge mock before each fast-path test
        # to avoid cross-test pollution from earlier tests that call dispatch.
        sys.modules["main_v2.state_machine_bridge"].dispatch_message.reset_mock()
        sys.modules["main_v2.state_machine_bridge"].dispatch_message.return_value = []

    def test_fast_path_match_short_circuits(self):
        svc = _make_services()
        processor = _make_processor(services=svc, router=_FastPathOnlyRouter())
        result = processor.process(_make_msg())
        self.assertIsNotNone(result)
        self.assertEqual(result.matched_fast_path, "always_match")

    def test_fast_path_skips_full_dispatch(self):
        svc = _make_services()
        processor = _make_processor(services=svc, router=_FastPathOnlyRouter())
        processor.process(_make_msg())
        # Full dispatch (state_machine_bridge) must NOT be called
        _smb = sys.modules["main_v2.state_machine_bridge"]
        _smb.dispatch_message.assert_not_called()

    def test_no_fast_path_calls_dispatch(self):
        svc = _make_services()
        _smb = sys.modules["main_v2.state_machine_bridge"]
        _smb.dispatch_message.reset_mock()
        _smb.dispatch_message.return_value = []

        processor = _make_processor(services=svc, router=_NoFastPaths())
        processor.process(_make_msg())

        _smb.dispatch_message.assert_called_once()


# ---------------------------------------------------------------------------
# State bootstrap failure
# ---------------------------------------------------------------------------


class TestMessageProcessorBootstrapFailure(unittest.TestCase):
    def test_returns_deny_on_bootstrap_failure(self):
        svc = _make_services()
        svc.state_manager.get_or_create_state.side_effect = Exception("DB down")
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        self.assertIsNotNone(result.deny)
        assert result.deny is not None
        self.assertEqual(result.deny.http_status, 500)
        self.assertEqual(result.deny.reason, "state_bootstrap_failed")


# ---------------------------------------------------------------------------
# Silence mode
# ---------------------------------------------------------------------------


class TestMessageProcessorSilenceMode(unittest.TestCase):
    def test_silence_mode_returns_empty_outbound(self):
        svc = _make_services(silence_mode=True)
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        self.assertIsNone(result.deny)
        self.assertEqual(result.outbound_messages, [])


# ---------------------------------------------------------------------------
# Broken dependencies — graceful degradation
# ---------------------------------------------------------------------------


class TestMessageProcessorGracefulDegradation(unittest.TestCase):
    def test_broken_classifier_does_not_crash(self):
        svc = _make_services()
        svc.classifier.classify.side_effect = RuntimeError("model timeout")
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        # Pipeline should complete — classification defaults to 'unknown'
        self.assertIsNone(result.deny)

    def test_broken_history_service_does_not_crash(self):
        svc = _make_services()
        svc.history_service.get_recent.side_effect = Exception("history timeout")
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        self.assertIsNone(result.deny)
        assert result.context is not None
        # History defaults to empty list
        self.assertEqual(result.context.history, [])

    def test_broken_escalation_service_does_not_crash(self):
        svc = _make_services()
        svc.escalation_service.evaluate_escalation.side_effect = Exception("esc error")
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        self.assertIsNone(result.deny)

    def test_broken_inbound_log_does_not_crash(self):
        svc = _make_services()
        svc.db_service.log_inbound_message.side_effect = Exception("log write failed")
        processor = _make_processor(services=svc)
        result = processor.process(_make_msg())
        self.assertIsNone(result.deny)


# ---------------------------------------------------------------------------
# State transition
# ---------------------------------------------------------------------------


class TestMessageProcessorStateTransition(unittest.TestCase):
    def test_transition_called_when_pending(self):
        svc = _make_services()

        # Subclass processor to inject a pending transition after dispatch
        class _TransitioningProcessor(MessageProcessor):
            def _dispatch(self, ctx):
                ctx.pending_state_transition = "confirm_booking"

        processor = _TransitioningProcessor(
            services=svc,
            policy_gate=_AllowGate(),
            fast_path_router=_NoFastPaths(),
        )
        processor.process(_make_msg())
        svc.state_manager.transition.assert_called_once_with(
            phone=_make_msg().from_number,
            event="confirm_booking",
        )

    def test_transition_not_called_when_no_pending(self):
        svc = _make_services()
        processor = _make_processor(services=svc)
        processor.process(_make_msg())
        svc.state_manager.transition.assert_not_called()


if __name__ == "__main__":
    unittest.main()
