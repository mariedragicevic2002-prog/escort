"""
message_processor.py — Unified conversation processing pipeline.

Replaces the procedural logic scattered across webhook_main_flow and
sms_gateway with a single, testable, stage-based pipeline.

Pipeline stages (in order):
  1. Policy gate       — deny immediately; no state access on reject
  2. Log inbound       — graceful; failure does not abort pipeline
  3. State bootstrap   — critical; failure returns 500 deny
  4. Load history      — graceful; defaults to []
  5. Fast-path routing — match short-circuits stages 6-10
  6. Silence mode      — returns empty outbound; no dispatch
  7. Classify intent   — graceful; defaults to 'unknown'
  8. Evaluate escalation — graceful
  9. Dispatch (_dispatch) — overrideable hook; default calls state_machine_bridge
  10. Execute pending state transition — if set by dispatch

Design:
  - All dependencies injected at construction; no in-function imports.
  - Override _dispatch() in subclasses to replace dispatch behaviour.
  - Thread-safe: no shared mutable state.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, List, Optional

from .fast_path_router import FastPathRouter
from .inbound_context import InboundMessage, ProcessingContext, ProcessingResult
from .policy_gate import PolicyDeny, PolicyGate

logger = logging.getLogger(__name__)


class MessageProcessor:
    """
    Orchestrates the full inbound message processing pipeline.

    Usage::

        processor = MessageProcessor(
            services=runtime_services,
            policy_gate=PolicyGate(services=runtime_services),
            fast_path_router=FastPathRouter(paths=[...]),
        )
        result = processor.process(inbound_message)
        if result.deny:
            return result.deny.response_body, result.deny.http_status
        # send result.outbound_messages, return HTTP 200
    """

    def __init__(
        self,
        services: Any,
        policy_gate: PolicyGate,
        fast_path_router: FastPathRouter,
    ) -> None:
        self._services = services
        self._gate = policy_gate
        self._router = fast_path_router

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, message: InboundMessage) -> ProcessingResult:
        """Run all pipeline stages and return a typed ProcessingResult."""
        ctx = ProcessingContext(message=message, services=self._services)

        # Stage 1: Policy gate — fastest possible reject path
        deny = self._gate.check(ctx)
        if deny is not None:
            return ProcessingResult(deny=deny)

        # Stage 2: Log inbound message (graceful)
        try:
            self._services.db_service.log_inbound_message(
                phone=message.from_number,
                body=message.body,
                sid=message.message_sid,
            )
        except Exception:
            logger.exception("log_inbound_message failed — continuing")

        # Stage 3: State bootstrap (critical — 500 deny on failure)
        try:
            ctx.state = self._services.state_manager.get_or_create_state(
                phone=message.from_number
            )
        except Exception:
            logger.exception("state bootstrap failed for %s", message.from_number)
            return ProcessingResult(
                deny=PolicyDeny(
                    reason="state_bootstrap_failed",
                    http_status=500,
                    response_body="Internal error",
                    send_sms=False,
                    log_event="state_bootstrap_failed",
                )
            )

        # Stage 4: Load history (graceful — default [])
        try:
            ctx.history = self._services.history_service.get_recent(message.from_number)
        except Exception:
            logger.exception("history_service.get_recent failed — defaulting to []")
            ctx.history = []

        # Stage 5: Fast-path routing (short-circuit on match)
        fp_result = self._router.route(ctx)
        if fp_result is not None:
            return ProcessingResult(
                context=ctx,
                outbound_messages=fp_result.outbound_messages,
                matched_fast_path=fp_result.matched_handler,
            )

        # Stage 6: Silence mode — return immediately with empty outbound
        try:
            if self._services.settings_manager.get_setting("silence_mode", False):
                return ProcessingResult(context=ctx, outbound_messages=[])
        except Exception:
            logger.exception("silence_mode check failed — continuing")

        # Stage 7: Classify intent (graceful)
        try:
            ctx.classification = self._services.classifier.classify(
                body=message.body,
                history=ctx.history,
            )
        except Exception:
            logger.exception("classifier.classify failed — defaulting to unknown")
            ctx.classification = {"intent": "unknown", "confidence": 0.0}

        # Stage 8: Evaluate escalation (graceful)
        try:
            self._services.escalation_service.evaluate_escalation(ctx)
        except Exception:
            logger.exception("escalation_service.evaluate_escalation failed — continuing")

        # Stage 9: Dispatch (overrideable hook)
        outbound: List = []
        try:
            dispatched = self._dispatch(ctx)
            if dispatched is not None:
                outbound = list(dispatched)
        except Exception:
            logger.exception("_dispatch raised — returning empty outbound")

        # Stage 10: Execute pending state transition
        if ctx.pending_state_transition:
            try:
                self._services.state_manager.transition(
                    phone=message.from_number,
                    event=ctx.pending_state_transition,
                )
            except Exception:
                logger.exception(
                    "state_manager.transition failed for event %s",
                    ctx.pending_state_transition,
                )

        return ProcessingResult(context=ctx, outbound_messages=outbound)

    # ------------------------------------------------------------------
    # Overrideable dispatch hook
    # ------------------------------------------------------------------

    def _dispatch(self, ctx: ProcessingContext) -> Optional[List]:
        """
        Dispatch to the state-machine bridge.

        Override in subclasses to replace or augment dispatch behaviour.
        The return value (list of outbound messages or None) is captured by
        process() and used as the result outbound_messages.
        """
        bridge = sys.modules.get("main_v2.state_machine_bridge")
        if bridge is None:
            logger.error(
                "main_v2.state_machine_bridge not in sys.modules — skipping dispatch"
            )
            return []
        return bridge.dispatch_message(ctx)
