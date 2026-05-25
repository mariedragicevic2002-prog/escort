"""
Dependency Injection Container — infrastructure layer.

The single place where concrete implementations are bound to
port interfaces. All application use cases receive their
dependencies from here.

Wire order: DB pool → repos → external clients → use cases.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class AppContainer:
    """
    Lazy-initialising DI container.

    Usage:
        container = AppContainer()
        use_case = container.process_inbound_message()
    """

    def __init__(self) -> None:
        self._instances: dict[str, Any] = {}

    # ------------------------------------------------------------------ #
    # Infrastructure                                                     #
    # ------------------------------------------------------------------ #

    def db_pool(self):
        if "db_pool" not in self._instances:
            from infrastructure.db.connection import get_pool

            self._instances["db_pool"] = get_pool()
        return self._instances["db_pool"]

    def event_bus(self):
        if "event_bus" not in self._instances:
            from application.event_bus import EventBus

            self._instances["event_bus"] = EventBus()
        return self._instances["event_bus"]

    # ------------------------------------------------------------------ #
    # Adapters (concrete port implementations)                          #
    # ------------------------------------------------------------------ #

    def conversation_repo(self):
        if "conversation_repo" not in self._instances:
            try:
                from adapters.persistence.conversation_repo import PsycopgConversationRepo

                self._instances["conversation_repo"] = PsycopgConversationRepo(self.db_pool())
            except ImportError:
                logger.warning(
                    "container.conversation_repo: adapter not yet implemented, using stub"
                )
                self._instances["conversation_repo"] = _StubRepo()
        return self._instances["conversation_repo"]

    def sms_gateway(self):
        if "sms_gateway" not in self._instances:
            try:
                from adapters.outbound.sms_adapter import HttpSmsAdapter

                self._instances["sms_gateway"] = HttpSmsAdapter()
            except ImportError:
                try:
                    from services.sms_service import send_sms

                    self._instances["sms_gateway"] = _CallableSmsGateway(send_sms)
                except ImportError:
                    logger.warning("container.sms_gateway: adapter not yet implemented, using stub")
                    self._instances["sms_gateway"] = _StubSms()
        return self._instances["sms_gateway"]

    def classifier(self):
        if "classifier" not in self._instances:
            try:
                from adapters.ai.classifier_adapter import ClassifierAdapter

                self._instances["classifier"] = ClassifierAdapter()
            except ImportError:
                try:
                    from core.classifier import Classifier

                    self._instances["classifier"] = _CoreClassifierAdapter(Classifier())
                except ImportError:
                    logger.warning("container.classifier: adapter not yet implemented, using stub")
                    self._instances["classifier"] = _StubClassifier()
        return self._instances["classifier"]

    # ------------------------------------------------------------------ #
    # Use cases                                                         #
    # ------------------------------------------------------------------ #

    def process_inbound_message(self):
        if "process_inbound_message" not in self._instances:
            from application.use_cases.process_inbound_message import ProcessInboundMessage

            self._instances["process_inbound_message"] = ProcessInboundMessage(
                conversation_repo=self.conversation_repo(),
                classifier=self.classifier(),
                sms_gateway=self.sms_gateway(),
                event_publisher=self.event_bus(),
            )
        return self._instances["process_inbound_message"]


class _StubRepo:
    def get_state(self, phone: str):
        return None

    def save_state(self, phone: str, state: dict[str, Any]) -> None:
        return None


class _StubSms:
    def send_message(self, phone: str, text: str) -> bool:
        logger.warning("stub_sms.send", extra={"phone": phone[:4] + "****"})
        return False


class _StubClassifier:
    def classify(self, phone: str, message: str, state: str) -> str:
        return "other"


class _CallableSmsGateway:
    def __init__(self, sender) -> None:
        self._sender = sender

    def send_message(self, phone: str, text: str) -> bool:
        return bool(self._sender(phone, text))


class _CoreClassifierAdapter:
    def __init__(self, classifier) -> None:
        self._classifier = classifier

    def classify(self, phone: str, message: str, state: str) -> str:
        context = {"state": {"current_state": state, "phone": phone}}
        return str(self._classifier.classify(message, context=context))


_container: AppContainer | None = None


def get_container() -> AppContainer:
    global _container
    if _container is None:
        _container = AppContainer()
    return _container


__all__ = ["AppContainer", "get_container"]
