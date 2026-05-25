from typing import Any, Dict, Optional, Protocol


class ConversationRepo(Protocol):
    """Port: read and write conversation state."""

    def get_state(self, phone: str) -> Optional[Dict[str, Any]]: ...

    def save_state(self, phone: str, state: Dict[str, Any]) -> None: ...
