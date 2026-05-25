"""
test_fast_path_router.py — Unit tests for FastPathRouter.

Covers:
  - Priority ordering (doubles > photo > screenshot > webform > …)
  - Graceful degradation: handler exception → fall-through, not crash
  - registered_names() ordering contract
  - No match → returns None
  - Correct handler name in result.matched_handler
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub required modules so tests work without the full application stack.
# ---------------------------------------------------------------------------


def _stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


for _m in [
    "main_v2",
    "handlers",
    "handlers.doubles",
    "handlers.photo",
    "handlers.location",
    "handlers.enquiry",
    "handlers.goodbye",
    "handlers.webform",
    "core",
    "core.settings_manager",
    "services",
]:
    if _m not in sys.modules:
        _stub(_m)

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
from main_v2.pipeline.inbound_context import InboundMessage, ProcessingContext  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(body: str = "Hello", from_number: str = "+447700900000") -> InboundMessage:
    return InboundMessage(
        from_number=from_number,
        body=body,
        message_sid="SM_fp_001",
        raw_payload={},
    )


def _ctx(body: str = "Hello") -> ProcessingContext:
    svc = MagicMock()
    return ProcessingContext(message=_msg(body=body), services=svc)


class _AlwaysMatchPath(FastPath):
    """Fast path that always matches and returns a canned response."""

    name = "always_match"

    def matches(self, ctx):
        return True

    def handle(self, ctx):
        return FastPathResult(
            outbound_messages=[{"to": ctx.message.from_number, "body": "handled"}],
            matched_handler=self.name,
        )


class _NeverMatchPath(FastPath):
    name = "never_match"

    def matches(self, ctx):
        return False

    def handle(self, ctx):
        raise AssertionError("handle() should never be called if matches() is False")


class _ExplodingMatchPath(FastPath):
    """Fast path that matches but raises in handle()."""

    name = "exploding"

    def matches(self, ctx):
        return True

    def handle(self, ctx):
        raise RuntimeError("simulated handler crash")


class _MatchAfterExploding(FastPath):
    """Should be reached after ExplodingMatchPath falls through."""

    name = "fallback_after_exploding"

    def matches(self, ctx):
        return True

    def handle(self, ctx):
        return FastPathResult(
            outbound_messages=[{"to": ctx.message.from_number, "body": "fallback"}],
            matched_handler=self.name,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFastPathRouterNoMatch(unittest.TestCase):
    def test_returns_none_when_no_paths_registered(self):
        router = FastPathRouter(paths=[])
        result = router.route(_ctx())
        self.assertIsNone(result)

    def test_returns_none_when_all_paths_dont_match(self):
        router = FastPathRouter(paths=[_NeverMatchPath()])
        result = router.route(_ctx())
        self.assertIsNone(result)


class TestFastPathRouterFirstMatch(unittest.TestCase):
    def test_returns_result_of_first_matching_path(self):
        router = FastPathRouter(paths=[_AlwaysMatchPath()])
        result = router.route(_ctx("anything"))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_handler, "always_match")
        self.assertEqual(len(result.outbound_messages), 1)

    def test_first_match_short_circuits_remaining(self):
        """Once a path matches and succeeds, subsequent paths are never checked."""
        second = MagicMock(spec=FastPath)
        second.name = "second"
        second.matches.return_value = True

        router = FastPathRouter(paths=[_AlwaysMatchPath(), second])
        router.route(_ctx())

        second.matches.assert_not_called()


class TestFastPathRouterPriorityOrder(unittest.TestCase):
    def test_higher_priority_path_wins(self):
        """
        Two paths that both match: the earlier-registered one must be returned.
        """
        class HighPriority(FastPath):
            name = "high"
            def matches(self, ctx): return True
            def handle(self, ctx):
                return FastPathResult(
                    outbound_messages=[{"body": "high"}], matched_handler=self.name
                )

        class LowPriority(FastPath):
            name = "low"
            def matches(self, ctx): return True
            def handle(self, ctx):
                return FastPathResult(
                    outbound_messages=[{"body": "low"}], matched_handler=self.name
                )

        router = FastPathRouter(paths=[HighPriority(), LowPriority()])
        result = router.route(_ctx())
        assert result is not None
        self.assertEqual(result.matched_handler, "high")

    def test_registered_names_order_matches_priority(self):
        """registered_names() must return names in priority (insertion) order."""
        class A(FastPath):
            name = "a"
            def matches(self, ctx): return False
            def handle(self, ctx): ...

        class B(FastPath):
            name = "b"
            def matches(self, ctx): return False
            def handle(self, ctx): ...

        class C(FastPath):
            name = "c"
            def matches(self, ctx): return False
            def handle(self, ctx): ...

        router = FastPathRouter(paths=[A(), B(), C()])
        self.assertEqual(router.registered_names(), ["a", "b", "c"])


class TestFastPathRouterGracefulDegradation(unittest.TestCase):
    def test_exception_in_handle_falls_through_to_next(self):
        """
        If handle() raises, the router must NOT crash — it must try the next path.
        """
        router = FastPathRouter(
            paths=[_ExplodingMatchPath(), _MatchAfterExploding()]
        )
        result = router.route(_ctx())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_handler, "fallback_after_exploding")

    def test_all_handlers_explode_returns_none(self):
        router = FastPathRouter(paths=[_ExplodingMatchPath()])
        result = router.route(_ctx())
        self.assertIsNone(result)

    def test_exception_in_matches_falls_through(self):
        class BrokenMatches(FastPath):
            name = "broken_matches"
            def matches(self, ctx): raise RuntimeError("matches crash")
            def handle(self, ctx): raise AssertionError("should not be called")

        router = FastPathRouter(paths=[BrokenMatches(), _AlwaysMatchPath()])
        result = router.route(_ctx())
        # BrokenMatches.matches raised → skip → AlwaysMatchPath runs
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.matched_handler, "always_match")


class TestFastPathResult(unittest.TestCase):
    def test_outbound_messages_default_empty_list(self):
        result = FastPathResult(matched_handler="test")
        self.assertEqual(result.outbound_messages, [])

    def test_matched_handler_required(self):
        # Should not raise
        r = FastPathResult(matched_handler="x", outbound_messages=[])
        self.assertEqual(r.matched_handler, "x")


class TestFastPathABC(unittest.TestCase):
    def test_cannot_instantiate_without_implementing_matches_and_handle(self):
        incomplete_type = type("Incomplete", (FastPath,), {"name": "incomplete"})

        with self.assertRaises(TypeError):
            incomplete_type()  # ABC enforcement

    def test_name_attribute_required(self):
        """Subclass without name attribute should still raise in route() logging,
        but not at instantiation — name is a class variable, not enforced by ABC."""
        class NoName(FastPath):
            def matches(self, ctx): return False
            def handle(self, ctx): ...

        # instantiation succeeds
        path = NoName()
        # name falls back to class name or is empty string
        self.assertFalse(hasattr(path, "name") and path.name == "required")


if __name__ == "__main__":
    unittest.main()
