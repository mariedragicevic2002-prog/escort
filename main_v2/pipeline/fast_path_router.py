"""
fast_path_router.py — Pluggable fast-path routing for the processing pipeline.

Fast paths are short-circuit handlers that bypass full state-machine dispatch
for well-known message patterns (e.g., photo uploads, opt-out responses).

Priority is determined by insertion order in FastPathRouter.  A handler error
in matches() or handle() causes fall-through to the next path rather than
crashing the pipeline.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result and abstract base
# ---------------------------------------------------------------------------


@dataclass
class FastPathResult:
    """Result of a successful fast-path handler execution."""

    matched_handler: str
    outbound_messages: List = field(default_factory=list)


class FastPath(ABC):
    """
    Abstract base class for fast-path message handlers.

    Subclasses must provide:
      - name (class variable str)  — used in logging and result attribution
      - matches(ctx) -> bool       — decides whether this path handles ctx
      - handle(ctx) -> FastPathResult — produces the response
    """

    @abstractmethod
    def matches(self, ctx) -> bool:
        """Return True if this handler should process the given context."""

    @abstractmethod
    def handle(self, ctx) -> FastPathResult:
        """Process the context and return a FastPathResult."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class FastPathRouter:
    """
    Routes incoming contexts to the first matching fast-path handler.

    Checks are evaluated in insertion order (highest priority first).
    Exceptions in matches() or handle() are logged and the router falls
    through to the next registered path.
    """

    def __init__(self, paths: List[FastPath]) -> None:
        self._paths = list(paths)

    def route(self, ctx) -> Optional[FastPathResult]:
        """
        Return the result of the first matching handler, or None if no match.

        Handles graceful degradation:
          - matches() raises  → skip this path, try next
          - handle()  raises  → skip this path, try next
        """
        for path in self._paths:
            name = getattr(path, "name", type(path).__name__)
            try:
                if not path.matches(ctx):
                    continue
            except Exception:
                logger.exception("FastPath %s: matches() raised — skipping", name)
                continue

            try:
                result = path.handle(ctx)
                logger.debug("FastPath %s matched and handled", name)
                return result
            except Exception:
                logger.exception("FastPath %s: handle() raised — falling through", name)
                continue

        return None

    def registered_names(self) -> List[str]:
        """Return handler names in priority (insertion) order."""
        return [getattr(p, "name", type(p).__name__) for p in self._paths]
