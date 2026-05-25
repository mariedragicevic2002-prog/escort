"""
main_v2/pipeline
================

Middleware pipeline for inbound message processing.

Staged architecture:
  InboundMessage
    → PolicyGate            (rate-limit / block / safety / phrases)
    → MessageProcessor      (state bootstrap, history, fast-path, dispatch)
    → ProcessingResult      (deny | outbound_messages | matched_fast_path)

Public surface
--------------
    from main_v2.pipeline import (
        InboundMessage,
        ProcessingContext,
        ProcessingResult,
        PolicyDeny,
        PolicyGate,
        FastPathResult,
        FastPath,
        FastPathRouter,
        MessageProcessor,
    )
"""

from main_v2.pipeline.inbound_context import (
    InboundMessage,
    ProcessingContext,
    ProcessingResult,
)
from main_v2.pipeline.policy_gate import PolicyDeny, PolicyGate
from main_v2.pipeline.fast_path_router import FastPath, FastPathResult, FastPathRouter
from main_v2.pipeline.message_processor import MessageProcessor

__all__ = [
    "InboundMessage",
    "ProcessingContext",
    "ProcessingResult",
    "PolicyDeny",
    "PolicyGate",
    "FastPath",
    "FastPathResult",
    "FastPathRouter",
    "MessageProcessor",
]
