"""
Intent classifier adapter.
Implements core/ports/classifier_port.ClassifierPort.

Wraps the existing classifier implementation as a clean adapter.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ClassifierAdapter:
    """Wraps the existing intent classifier behind the ClassifierPort interface."""

    def __init__(self, ai_service=None) -> None:
        self._ai_service = ai_service

    def classify(self, phone: str, message: str, state: str) -> str:
        """
        Classify the intent of an inbound message.
        Returns an intent string. Never raises — returns 'other' on error.
        """
        try:
            from core.classifier import Classifier  # type: ignore

            classifier = Classifier(ai_service=self._ai_service)
            return classifier.classify(
                message=message,
                context={"state": {"current_state": state or "NEW"}},
            )
        except ImportError:
            pass
        except Exception:
            logger.exception("classifier_adapter.error", extra={"phone": phone[:4] + "****"})
        return "other"
