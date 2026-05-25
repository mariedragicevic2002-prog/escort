"""
Use case: process a single inbound SMS message through the conversation engine.

Application layer rules:
- Imports ONLY from core/ (entities, ports, state_machine, events).
- Never imports from infrastructure/, adapters/, or framework code.
- All I/O goes through injected port interfaces.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from core.ports.classifier_port import ClassifierPort
from core.ports.conversation_repo import ConversationRepo
from core.ports.event_publisher import EventPublisher
from core.ports.sms_gateway import SmsGateway
from core.state_machine import TransitionError, validate_transition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    phone: str
    text: str
    request_id: str


class ProcessInboundMessage:
    """
    Orchestrates the response to a single inbound message.

    All collaborators are injected as port interfaces — no concrete
    infrastructure classes are referenced here.
    """

    def __init__(
        self,
        conversation_repo: ConversationRepo,
        classifier: ClassifierPort,
        sms_gateway: SmsGateway,
        event_publisher: EventPublisher,
    ) -> None:
        self._repo = conversation_repo
        self._classifier = classifier
        self._sms = sms_gateway
        self._events = event_publisher

    def execute(self, message: InboundMessage) -> None:
        """
        Process an inbound message end-to-end.
        Raises nothing — all errors are logged and handled gracefully.
        """
        phone = message.phone
        request_id = message.request_id

        try:
            state_record = self._repo.get_state(phone) or {}
            current_state = state_record.get("state", "NEW")

            intent = self._classifier.classify(phone, message.text, current_state)

            logger.info(
                "use_case.process_inbound",
                extra={"request_id": request_id, "state": current_state, "intent": intent},
            )

            from core.events import MessageReceived

            self._events.publish(
                MessageReceived(
                    phone=phone,
                    text=message.text,
                    intent=intent,
                    state=current_state,
                    request_id=request_id,
                )
            )

        except Exception:
            logger.exception(
                "use_case.process_inbound.error",
                extra={"request_id": request_id, "phone": phone[:4] + "****"},
            )
