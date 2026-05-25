"""
ConversationEngine — thin facade over the existing MessageProcessor pipeline.

Adapter layer:
- Accepts a normalised InboundMessage
- Delegates to main_v2.pipeline.MessageProcessor (10-stage pipeline)
- Returns a ProcessingResult

This class exists to decouple the webhook controller from the pipeline
implementation, making both independently testable and allowing the
underlying pipeline to evolve without touching the HTTP layer.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class ConversationEngine:
    """
    Facade over the MessageProcessor pipeline.

    Constructed once at app startup via the DI container and reused
    across requests (thread-safe: MessageProcessor has no shared mutable state).
    """

    def __init__(self, processor=None) -> None:
        """
        Args:
            processor: An instance of main_v2.pipeline.MessageProcessor.
                       Resolved lazily on first call if not injected.
        """
        self._processor = processor

    def _resolve_processor(self):
        if self._processor is None:
            try:
                from main_v2.pipeline.message_processor import MessageProcessor  # type: ignore
                from main_v2.pipeline.policy_gate import PolicyGate  # type: ignore
                from main_v2.pipeline.fast_path_router import FastPathRouter  # type: ignore
                import main_v2.runtime as _runtime  # type: ignore

                # runtime module itself is the services object — its attributes
                # (db_service, state_manager, router, classifier, ai_service)
                # are populated during app startup and accessed by MessageProcessor.
                services = _runtime
                self._processor = MessageProcessor(
                    services=services,
                    policy_gate=PolicyGate(services=services),
                    fast_path_router=FastPathRouter(paths=[]),
                )
            except Exception:
                logger.exception("conversation_engine.processor_init_failed")
                raise
        return self._processor

    def process(self, inbound_message: Any) -> Any:
        """
        Process an inbound message through the full pipeline.

        Args:
            inbound_message: main_v2.pipeline.InboundMessage or compatible dict.

        Returns:
            ProcessingResult with .deny (None = allowed) and .outbound_messages.
        """
        processor = self._resolve_processor()
        try:
            return processor.process(inbound_message)
        except Exception:
            logger.exception(
                "conversation_engine.process_error",
                extra={"phone": getattr(inbound_message, "phone", "unknown")[:4] + "****"},
            )
            raise
