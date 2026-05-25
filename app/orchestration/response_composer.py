"""
ResponseComposer — assembles outbound message list from a ProcessingResult.

Pure function layer: no I/O, no state mutations.
Input: ProcessingResult from ConversationEngine.
Output: List[str] of message texts ready for OutboundDispatcher.
"""
from __future__ import annotations

import logging
from typing import Any, List

logger = logging.getLogger(__name__)


class ResponseComposer:
    """Converts a pipeline ProcessingResult into a flat list of SMS texts."""

    def compose(self, result: Any) -> List[str]:
        """
        Extract outbound message texts from a ProcessingResult.

        Handles both v1 (list of dicts with 'message' key) and v2
        (list of strings) outbound message formats.

        Returns an empty list if the result carries a deny/block signal.
        """
        if result is None:
            return []

        # Hard deny — no outbound
        if getattr(result, "deny", None) is not None:
            return []

        outbound = getattr(result, "outbound_messages", None) or []
        texts: List[str] = []

        for item in outbound:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("message") or item.get("text") or "").strip()
            else:
                text = str(item).strip()

            if text:
                texts.append(text)

        return texts
