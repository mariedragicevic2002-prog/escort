from typing import Protocol


class ClassifierPort(Protocol):
    """Port: classify an inbound message into an intent string."""

    def classify(self, phone: str, message: str, state: str) -> str: ...
