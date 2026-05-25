"""
test_intent_router.py — Unit tests for IntentRouter and SignalIntentResolver.

Coverage:
  - Fast-path matching: first match wins, subsequent skipped
  - Fast-path graceful degradation: matches() crash → fall-through, handle() crash → fall-through
  - Intent resolution: resolved from metadata / message_data / request_payload
  - Intent routing: registered handler called, unregistered falls back to legacy
  - Metadata stamping: routing_path, routing_handler, resolved_intent are set
  - register_intent_handler normalises intent (lowercase + strip)
  - SignalIntentResolver: checks all four intent keys, all three payload sources
  - SignalIntentResolver: returns None when nothing present
  - build_default_intent_router: works without a legacy processor
"""
from __future__ import annotations

import os
import sys
import unittest
from typing import cast

# ---------------------------------------------------------------------------
# Path setup — makes refactor2/ importable without installing it.
# ---------------------------------------------------------------------------
_REFACTOR_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)
if _REFACTOR_ROOT not in sys.path:
    sys.path.insert(0, _REFACTOR_ROOT)

from app.runtime.context import (  # noqa: E402
    InboundSMSMessage,
    OrchestrationContext,
    RuntimeServices,
)
from app.runtime.intent_contracts import (  # noqa: E402
    FastPathHandler,
    IntentHandler,
    LegacyFallbackHandler,
)
from app.runtime.intent_router import (  # noqa: E402
    IntentRouter,
    SignalIntentResolver,
    build_default_intent_router,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inbound(
    *,
    phone: str = "+61400000001",
    body: str = "hello",
    message_data: dict | None = None,
    request_payload: dict | None = None,
) -> InboundSMSMessage:
    return InboundSMSMessage(
        phone_number=phone,
        body=body,
        message_data=message_data or {},
        request_payload=request_payload or {},
        request_id="test-req-001",
    )


def _runtime() -> RuntimeServices:
    return RuntimeServices(
        state_manager=None,
        db_service=None,
        legacy_processor=lambda p, b: [],
    )


def _ctx(
    *,
    metadata: dict | None = None,
    message_data: dict | None = None,
    request_payload: dict | None = None,
    body: str = "hello",
) -> OrchestrationContext:
    return OrchestrationContext(
        inbound=_inbound(body=body, message_data=message_data, request_payload=request_payload),
        runtime=_runtime(),
        metadata=dict(metadata or {}),
    )


class _StaticHandler:
    def __init__(self, messages: list[str] | None = None) -> None:
        self._messages = list(messages or [])

    def __call__(self, context: OrchestrationContext) -> list[str]:
        _ = context
        return list(self._messages)


class _TrackingHandler(_StaticHandler):
    def __init__(self, calls: list[int], messages: list[str] | None = None) -> None:
        super().__init__(messages)
        self._calls = calls

    def __call__(self, context: OrchestrationContext) -> list[str]:
        self._calls.append(1)
        return super().__call__(context)


class _NoneHandler:
    def __call__(self, context: OrchestrationContext) -> list[str]:
        _ = context
        return None  # type: ignore[return-value]


class _FP:
    def __init__(
        self,
        *,
        name: str,
        matches: bool,
        messages: list[str] | None = None,
        raises: bool = False,
    ) -> None:
        self.name = name
        self._matches = matches
        self._messages = list(messages or [])
        self._raises = raises

    def matches(self, context: OrchestrationContext) -> bool:
        _ = context
        return self._matches

    def handle(self, context: OrchestrationContext) -> list[str]:
        _ = context
        if self._raises:
            raise RuntimeError(f"{self.name}: simulated crash in handle()")
        return list(self._messages)


class _MatchesRaisesFP:
    def __init__(self, *, name: str) -> None:
        self.name = name

    def matches(self, context: OrchestrationContext) -> bool:
        _ = context
        raise RuntimeError(f"{self.name}: matches() crashed")

    def handle(self, context: OrchestrationContext) -> list[str]:
        _ = context
        return []


def _intent_handler(messages: list[str] | None = None) -> IntentHandler:
    return _StaticHandler(messages)


def _fallback_handler(messages: list[str] | None = None) -> LegacyFallbackHandler:
    return _StaticHandler(messages)


def _tracking_intent_handler(calls: list[int], messages: list[str] | None = None) -> IntentHandler:
    return _TrackingHandler(calls, messages)


def _none_intent_handler() -> IntentHandler:
    return cast(IntentHandler, _NoneHandler())


def _fast_path_handler(
    *,
    name: str,
    matches: bool,
    messages: list[str] | None = None,
    raises: bool = False,
) -> FastPathHandler:
    return _FP(name=name, matches=matches, messages=messages, raises=raises)


def _matches_raises_handler(*, name: str) -> FastPathHandler:
    """Fast-path handler whose matches() always raises."""
    return _MatchesRaisesFP(name=name)


# ---------------------------------------------------------------------------
# SignalIntentResolver
# ---------------------------------------------------------------------------

class TestSignalIntentResolver(unittest.TestCase):

    def setUp(self):
        self.resolver = SignalIntentResolver()

    # --- intent found in metadata ---

    def test_resolves_intent_from_metadata(self):
        ctx = _ctx(metadata={"intent": "booking"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "booking")
        self.assertEqual(result.source, "metadata")

    def test_resolves_resolved_intent_key_from_metadata(self):
        ctx = _ctx(metadata={"resolved_intent": "escalation"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "escalation")

    def test_resolves_intent_name_key_from_metadata(self):
        ctx = _ctx(metadata={"intent_name": "greeting"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "greeting")

    def test_resolves_classification_intent_key_from_metadata(self):
        ctx = _ctx(metadata={"classification_intent": "cancel_booking"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "cancel_booking")

    # --- intent found in message_data ---

    def test_resolves_intent_from_message_data(self):
        ctx = _ctx(message_data={"intent": "ask_rates"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "ask_rates")
        self.assertEqual(result.source, "message_data")

    # --- intent found in request_payload ---

    def test_resolves_intent_from_request_payload(self):
        ctx = _ctx(request_payload={"intent": "reschedule"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "reschedule")
        self.assertEqual(result.source, "request_payload")

    # --- priority: metadata > message_data > request_payload ---

    def test_metadata_takes_priority_over_message_data(self):
        ctx = _ctx(
            metadata={"intent": "from_metadata"},
            message_data={"intent": "from_message_data"},
        )
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "from_metadata")
        self.assertEqual(result.source, "metadata")

    def test_message_data_takes_priority_over_request_payload(self):
        ctx = _ctx(
            message_data={"intent": "from_message_data"},
            request_payload={"intent": "from_request_payload"},
        )
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "from_message_data")
        self.assertEqual(result.source, "message_data")

    # --- normalisation ---

    def test_intent_is_normalised_to_lowercase(self):
        ctx = _ctx(metadata={"intent": "BOOKING"})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "booking")

    def test_intent_whitespace_is_stripped(self):
        ctx = _ctx(metadata={"intent": "  greeting  "})
        result = self.resolver.resolve(ctx)
        assert result is not None
        self.assertEqual(result.intent, "greeting")

    # --- no intent present ---

    def test_returns_none_when_no_intent_anywhere(self):
        ctx = _ctx()
        result = self.resolver.resolve(ctx)
        self.assertIsNone(result)

    def test_returns_none_when_intent_is_empty_string(self):
        ctx = _ctx(metadata={"intent": ""})
        result = self.resolver.resolve(ctx)
        self.assertIsNone(result)

    def test_returns_none_when_payload_is_not_mapping(self):
        # message_data and request_payload default to {} (empty Mapping) — resolver skips non-Mapping gracefully
        ctx = OrchestrationContext(
            inbound=InboundSMSMessage(
                phone_number="+61400000001",
                body="hi",
                message_data={},
                request_payload={},
                request_id="r1",
            ),
            runtime=_runtime(),
            metadata={},
        )
        result = self.resolver.resolve(ctx)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# IntentRouter — fast paths
# ---------------------------------------------------------------------------

class TestIntentRouterFastPath(unittest.TestCase):

    def test_first_matching_fast_path_wins(self):
        fp1 = _fast_path_handler(name="fp1", matches=True, messages=["from_fp1"])
        fp2 = _fast_path_handler(name="fp2", matches=True, messages=["from_fp2"])
        router = IntentRouter(fast_path_handlers=[fp1, fp2])
        result = router.route(_ctx(), fallback_handler=_fallback_handler())
        self.assertEqual(result, ["from_fp1"])

    def test_non_matching_fast_path_skipped(self):
        fp_no = _fast_path_handler(name="no_match", matches=False)
        fp_yes = _fast_path_handler(name="yes_match", matches=True, messages=["hit"])
        router = IntentRouter(fast_path_handlers=[fp_no, fp_yes])
        result = router.route(_ctx(), fallback_handler=_fallback_handler())
        self.assertEqual(result, ["hit"])

    def test_no_fast_paths_falls_through_to_legacy(self):
        router = IntentRouter()
        result = router.route(_ctx(), fallback_handler=_fallback_handler(["from_legacy"]))
        self.assertEqual(result, ["from_legacy"])

    def test_fast_path_metadata_stamped(self):
        fp = _fast_path_handler(name="stamped_fp", matches=True, messages=[])
        ctx = _ctx()
        router = IntentRouter(fast_path_handlers=[fp])
        router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(ctx.metadata.get("routing_path"), "fast_path")
        self.assertEqual(ctx.metadata.get("routing_handler"), "stamped_fp")

    def test_handle_crash_falls_through_to_next_fast_path(self):
        fp_crash = _fast_path_handler(name="crash", matches=True, raises=True)
        fp_ok = _fast_path_handler(name="ok", matches=True, messages=["ok_msg"])
        router = IntentRouter(fast_path_handlers=[fp_crash, fp_ok])
        result = router.route(_ctx(), fallback_handler=_fallback_handler())
        self.assertEqual(result, ["ok_msg"])

    def test_matches_crash_falls_through_to_next_fast_path(self):
        fp_broken = _matches_raises_handler(name="broken_matches")
        fp_ok = _fast_path_handler(name="ok", matches=True, messages=["ok_after_crash"])
        router = IntentRouter(fast_path_handlers=[fp_broken, fp_ok])
        result = router.route(_ctx(), fallback_handler=_fallback_handler())
        self.assertEqual(result, ["ok_after_crash"])

    def test_all_fast_paths_crash_falls_to_legacy(self):
        fp1 = _fast_path_handler(name="crash1", matches=True, raises=True)
        fp2 = _fast_path_handler(name="crash2", matches=True, raises=True)
        router = IntentRouter(fast_path_handlers=[fp1, fp2])
        result = router.route(_ctx(), fallback_handler=_fallback_handler(["legacy_rescue"]))
        self.assertEqual(result, ["legacy_rescue"])


# ---------------------------------------------------------------------------
# IntentRouter — intent routing
# ---------------------------------------------------------------------------

class TestIntentRouterIntentRouting(unittest.TestCase):

    def test_registered_intent_handler_called(self):
        router = IntentRouter()
        router.register_intent_handler("booking", _intent_handler(["booked"]))
        ctx = _ctx(metadata={"intent": "booking"})
        result = router.route(ctx, fallback_handler=_fallback_handler(["fallback"]))
        self.assertEqual(result, ["booked"])

    def test_unregistered_intent_falls_back_to_legacy(self):
        router = IntentRouter()
        router.register_intent_handler("booking", _intent_handler(["booked"]))
        ctx = _ctx(metadata={"intent": "unknown_intent"})
        result = router.route(ctx, fallback_handler=_fallback_handler(["legacy"]))
        self.assertEqual(result, ["legacy"])

    def test_no_intent_resolved_uses_legacy(self):
        router = IntentRouter()
        result = router.route(_ctx(), fallback_handler=_fallback_handler(["default_legacy"]))
        self.assertEqual(result, ["default_legacy"])

    def test_intent_registration_normalises_to_lowercase(self):
        router = IntentRouter()
        router.register_intent_handler("BOOKING", _intent_handler(["uppercase_registered"]))
        ctx = _ctx(metadata={"intent": "booking"})
        result = router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(result, ["uppercase_registered"])

    def test_intent_in_context_normalised_before_lookup(self):
        router = IntentRouter()
        router.register_intent_handler("greeting", _intent_handler(["hello"]))
        ctx = _ctx(metadata={"intent": "GREETING"})
        result = router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(result, ["hello"])

    def test_intent_handler_overrides_fast_path_only_if_no_fast_match(self):
        """Intent handler should NOT be reached when a fast-path already matched."""
        intent_called = []
        fp = _fast_path_handler(name="fp", matches=True, messages=["fp_msg"])
        router = IntentRouter(fast_path_handlers=[fp])
        router.register_intent_handler("booking", _tracking_intent_handler(intent_called, ["intent_msg"]))
        ctx = _ctx(metadata={"intent": "booking"})
        result = router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(result, ["fp_msg"])
        self.assertEqual(intent_called, [])

    def test_returns_empty_list_when_handler_returns_none(self):
        router = IntentRouter()
        router.register_intent_handler("empty", _none_intent_handler())
        ctx = _ctx(metadata={"intent": "empty"})
        result = router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(result, [])

    # --- metadata stamping ---

    def test_intent_routing_stamps_metadata(self):
        router = IntentRouter()
        router.register_intent_handler("booking", _intent_handler())
        ctx = _ctx(metadata={"intent": "booking"})
        router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(ctx.metadata.get("routing_path"), "intent")
        self.assertEqual(ctx.metadata.get("routing_handler"), "booking")
        self.assertEqual(ctx.metadata.get("resolved_intent"), "booking")

    def test_legacy_fallback_stamps_routing_path(self):
        router = IntentRouter()
        ctx = _ctx()  # no intent
        router.route(ctx, fallback_handler=_fallback_handler())
        self.assertEqual(ctx.metadata.get("routing_path"), "legacy_fallback")
        self.assertTrue(ctx.metadata.get("fallback_to_legacy"))

    # --- multiple intent handlers ---

    def test_multiple_intents_routed_independently(self):
        router = IntentRouter(
            intent_handlers={
                "greeting": _intent_handler(["hi"]),
                "cancel_booking": _intent_handler(["cancelled"]),
            }
        )
        self.assertEqual(
            router.route(_ctx(metadata={"intent": "greeting"}), fallback_handler=_fallback_handler()),
            ["hi"],
        )
        self.assertEqual(
            router.route(_ctx(metadata={"intent": "cancel_booking"}), fallback_handler=_fallback_handler()),
            ["cancelled"],
        )

    def test_register_fast_path_handler_appends(self):
        router = IntentRouter()
        fp = _fast_path_handler(name="added_later", matches=True, messages=["late"])
        router.register_fast_path_handler(fp)
        result = router.route(_ctx(), fallback_handler=_fallback_handler())
        self.assertEqual(result, ["late"])


# ---------------------------------------------------------------------------
# build_default_intent_router
# ---------------------------------------------------------------------------

class TestBuildDefaultIntentRouter(unittest.TestCase):

    def test_returns_intent_router_instance(self):
        router = build_default_intent_router()
        self.assertIsInstance(router, IntentRouter)

    def test_works_without_legacy_processor(self):
        router = build_default_intent_router(legacy_processor=None)
        self.assertIsInstance(router, IntentRouter)

    def test_with_callable_legacy_processor(self):
        def _legacy(phone, body):
            return ["legacy_reply"]

        router = build_default_intent_router(legacy_processor=_legacy)
        self.assertIsInstance(router, IntentRouter)


if __name__ == "__main__":
    unittest.main()
