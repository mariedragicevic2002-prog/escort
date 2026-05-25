"""
test_domain_handler_registry.py — Unit tests for DomainHandlerRegistry.

Coverage:
  - register_booking/escalation/moderation_handler with single and multiple intents
  - resolve_handler_type returns correct DomainHandlerType
  - resolve_handler_type returns None for unknown intents
  - dispatch routes to correct port per domain type
  - dispatch raises DomainHandlerNotRegisteredError for unknown intent
  - dispatch populates BookingHandlerPayload / EscalationHandlerPayload / ModerationHandlerPayload correctly
  - build_intent_handlers returns callable handlers for every registered intent
  - Returned handlers call dispatch and update context.metadata
  - Intent normalisation: uppercase + whitespace handled transparently
  - Overwrite: re-registering an intent replaces the handler
"""
from __future__ import annotations

import os
import sys
import unittest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_REFACTOR_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REFACTOR_ROOT not in sys.path:
    sys.path.insert(0, _REFACTOR_ROOT)

from domain.handler_ports import (  # noqa: E402
    BookingHandlerPayload,
    BookingHandlerPort,
    DomainHandlerResponse,
    EscalationHandlerPayload,
    EscalationHandlerPort,
    ModerationHandlerPayload,
    ModerationHandlerPort,
)
from app.runtime.context import (  # noqa: E402
    InboundSMSMessage,
    OrchestrationContext,
    RuntimeServices,
)
from app.runtime.domain_handler_registry import (  # noqa: E402
    DomainHandlerNotRegisteredError,
    DomainHandlerRegistry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _response(messages: list[str] | None = None) -> DomainHandlerResponse:
    return DomainHandlerResponse(messages=list(messages or []))


class _StubBookingHandler:
    def __init__(self, reply: str = "booking_reply"):
        self.reply = reply
        self.received: list[BookingHandlerPayload] = []

    def handle_booking(self, payload: BookingHandlerPayload) -> DomainHandlerResponse:
        self.received.append(payload)
        return _response([self.reply])


class _StubEscalationHandler:
    def __init__(self, reply: str = "escalation_reply"):
        self.reply = reply
        self.received: list[EscalationHandlerPayload] = []

    def handle_escalation(self, payload: EscalationHandlerPayload) -> DomainHandlerResponse:
        self.received.append(payload)
        return _response([self.reply])


class _StubModerationHandler:
    def __init__(self, reply: str = "moderation_reply"):
        self.reply = reply
        self.received: list[ModerationHandlerPayload] = []

    def handle_moderation(self, payload: ModerationHandlerPayload) -> DomainHandlerResponse:
        self.received.append(payload)
        return _response([self.reply])


def _ctx(intent: str = "booking") -> OrchestrationContext:
    inbound = InboundSMSMessage(
        phone_number="+61400000001",
        body="test message",
        message_data={},
        request_payload={},
        request_id="req-001",
    )
    runtime = RuntimeServices(
        state_manager=None,
        db_service=None,
        legacy_processor=lambda p, b: [],
    )
    return OrchestrationContext(inbound=inbound, runtime=runtime, metadata={"intent": intent})


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestDomainHandlerRegistryRegistration(unittest.TestCase):

    def test_register_single_booking_intent(self):
        reg = DomainHandlerRegistry()
        handler = _StubBookingHandler()
        reg.register_booking_handler("booking", handler)
        self.assertEqual(reg.resolve_handler_type("booking"), "booking")

    def test_register_multiple_booking_intents(self):
        reg = DomainHandlerRegistry()
        handler = _StubBookingHandler()
        reg.register_booking_handler(["booking", "booking_started", "fields_complete"], handler)
        for intent in ("booking", "booking_started", "fields_complete"):
            self.assertEqual(reg.resolve_handler_type(intent), "booking")

    def test_register_escalation_handler(self):
        reg = DomainHandlerRegistry()
        reg.register_escalation_handler("escalation", _StubEscalationHandler())
        self.assertEqual(reg.resolve_handler_type("escalation"), "escalation")

    def test_register_moderation_handler(self):
        reg = DomainHandlerRegistry()
        reg.register_moderation_handler("moderation", _StubModerationHandler())
        self.assertEqual(reg.resolve_handler_type("moderation"), "moderation")

    def test_overwrite_replaces_previous_handler(self):
        reg = DomainHandlerRegistry()
        old = _StubBookingHandler("old")
        new = _StubBookingHandler("new")
        reg.register_booking_handler("booking", old)
        reg.register_booking_handler("booking", new)
        ctx = _ctx("booking")
        resp = reg.dispatch(ctx, intent="booking")
        self.assertEqual(resp.messages, ["new"])

    def test_intent_normalised_on_register(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler("  BOOKING  ", _StubBookingHandler())
        self.assertEqual(reg.resolve_handler_type("booking"), "booking")

    def test_empty_string_intent_ignored(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler(["", "booking"], _StubBookingHandler())
        # empty string should be filtered out, 'booking' should be registered
        self.assertEqual(reg.resolve_handler_type("booking"), "booking")
        self.assertIsNone(reg.resolve_handler_type(""))


# ---------------------------------------------------------------------------
# resolve_handler_type
# ---------------------------------------------------------------------------

class TestResolveHandlerType(unittest.TestCase):

    def setUp(self):
        self.reg = DomainHandlerRegistry()
        self.reg.register_booking_handler("booking", _StubBookingHandler())
        self.reg.register_escalation_handler("escalation", _StubEscalationHandler())
        self.reg.register_moderation_handler("moderation", _StubModerationHandler())

    def test_returns_booking_for_booking_intent(self):
        self.assertEqual(self.reg.resolve_handler_type("booking"), "booking")

    def test_returns_escalation_for_escalation_intent(self):
        self.assertEqual(self.reg.resolve_handler_type("escalation"), "escalation")

    def test_returns_moderation_for_moderation_intent(self):
        self.assertEqual(self.reg.resolve_handler_type("moderation"), "moderation")

    def test_returns_none_for_unknown_intent(self):
        self.assertIsNone(self.reg.resolve_handler_type("completely_unknown"))

    def test_returns_none_for_empty_intent(self):
        self.assertIsNone(self.reg.resolve_handler_type(""))

    def test_returns_none_for_none_intent(self):
        self.assertIsNone(self.reg.resolve_handler_type(None))

    def test_case_insensitive_lookup(self):
        self.assertEqual(self.reg.resolve_handler_type("BOOKING"), "booking")


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

class TestDispatch(unittest.TestCase):

    def setUp(self):
        self.reg = DomainHandlerRegistry()
        self.booking_handler = _StubBookingHandler("b_reply")
        self.escalation_handler = _StubEscalationHandler("e_reply")
        self.moderation_handler = _StubModerationHandler("m_reply")
        self.reg.register_booking_handler("booking", self.booking_handler)
        self.reg.register_escalation_handler("escalation", self.escalation_handler)
        self.reg.register_moderation_handler("moderation", self.moderation_handler)

    def test_dispatch_booking_returns_messages(self):
        resp = self.reg.dispatch(_ctx("booking"), intent="booking")
        self.assertEqual(resp.messages, ["b_reply"])

    def test_dispatch_escalation_returns_messages(self):
        resp = self.reg.dispatch(_ctx("escalation"), intent="escalation")
        self.assertEqual(resp.messages, ["e_reply"])

    def test_dispatch_moderation_returns_messages(self):
        resp = self.reg.dispatch(_ctx("moderation"), intent="moderation")
        self.assertEqual(resp.messages, ["m_reply"])

    def test_dispatch_passes_correct_phone_to_booking(self):
        ctx = _ctx("booking")
        ctx.inbound = InboundSMSMessage(
            phone_number="+61499111222",
            body="I want to book",
            message_data={},
            request_payload={},
            request_id="r1",
        )
        self.reg.dispatch(ctx, intent="booking")
        self.assertEqual(self.booking_handler.received[0].phone_number, "+61499111222")

    def test_dispatch_passes_correct_body_to_escalation(self):
        ctx = _ctx("escalation")
        ctx.inbound = InboundSMSMessage(
            phone_number="+61400000001",
            body="this is urgent",
            message_data={},
            request_payload={},
            request_id="r2",
        )
        self.reg.dispatch(ctx, intent="escalation")
        self.assertEqual(self.escalation_handler.received[0].message_body, "this is urgent")

    def test_dispatch_raises_for_unknown_intent(self):
        with self.assertRaises(DomainHandlerNotRegisteredError):
            self.reg.dispatch(_ctx("unknown_intent"), intent="unknown_intent")

    def test_dispatch_normalises_intent_before_lookup(self):
        resp = self.reg.dispatch(_ctx("BOOKING"), intent="BOOKING")
        self.assertEqual(resp.messages, ["b_reply"])

    def test_dispatch_passes_intent_to_payload(self):
        self.reg.dispatch(_ctx("booking"), intent="booking")
        self.assertEqual(self.booking_handler.received[0].intent, "booking")


# ---------------------------------------------------------------------------
# build_intent_handlers
# ---------------------------------------------------------------------------

class TestBuildIntentHandlers(unittest.TestCase):

    def test_returns_handler_for_each_registered_booking_intent(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler(["booking", "booking_started"], _StubBookingHandler())
        handlers = reg.build_intent_handlers()
        self.assertIn("booking", handlers)
        self.assertIn("booking_started", handlers)

    def test_returned_handler_is_callable(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler("booking", _StubBookingHandler("hello"))
        handlers = reg.build_intent_handlers()
        result = handlers["booking"](_ctx("booking"))
        self.assertEqual(result, ["hello"])

    def test_handler_updates_context_metadata(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler("booking", _StubBookingHandler())
        handlers = reg.build_intent_handlers()
        ctx = _ctx("booking")
        handlers["booking"](ctx)
        self.assertEqual(ctx.metadata.get("domain_handler_type"), "booking")
        self.assertEqual(ctx.metadata.get("domain_handler_intent"), "booking")

    def test_escalation_handler_type_stamped(self):
        reg = DomainHandlerRegistry()
        reg.register_escalation_handler("escalation", _StubEscalationHandler())
        handlers = reg.build_intent_handlers()
        ctx = _ctx("escalation")
        handlers["escalation"](ctx)
        self.assertEqual(ctx.metadata.get("domain_handler_type"), "escalation")

    def test_moderation_handler_type_stamped(self):
        reg = DomainHandlerRegistry()
        reg.register_moderation_handler("moderation", _StubModerationHandler())
        handlers = reg.build_intent_handlers()
        ctx = _ctx("moderation")
        handlers["moderation"](ctx)
        self.assertEqual(ctx.metadata.get("domain_handler_type"), "moderation")

    def test_empty_registry_returns_empty_dict(self):
        reg = DomainHandlerRegistry()
        self.assertEqual(reg.build_intent_handlers(), {})

    def test_all_domain_types_included(self):
        reg = DomainHandlerRegistry()
        reg.register_booking_handler("booking", _StubBookingHandler())
        reg.register_escalation_handler("escalation", _StubEscalationHandler())
        reg.register_moderation_handler("moderation", _StubModerationHandler())
        handlers = reg.build_intent_handlers()
        self.assertIn("booking", handlers)
        self.assertIn("escalation", handlers)
        self.assertIn("moderation", handlers)

    def test_handler_returns_empty_list_for_empty_response(self):
        class _EmptyHandler:
            def handle_booking(self, payload):
                return DomainHandlerResponse(messages=[])

        reg = DomainHandlerRegistry()
        reg.register_booking_handler("booking", _EmptyHandler())
        handlers = reg.build_intent_handlers()
        result = handlers["booking"](_ctx("booking"))
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
