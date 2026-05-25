from typing import Any, Dict, Optional, Protocol


class BookingRepo(Protocol):
    """Port: read and write booking records."""

    def get_booking(self, booking_id: str) -> Optional[Dict[str, Any]]: ...

    def save_booking(self, _booking: Dict[str, Any]) -> None: ...
