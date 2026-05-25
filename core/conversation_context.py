"""
Conversation Context - Tracks client preferences and history.

Also exports ``BookingContext``, the strongly-typed per-request context used
by v2 event-driven handlers.  The legacy ``ConversationContext`` class is
preserved unchanged for full backward compatibility.
"""

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from utils.row_utils import row_get

logger = logging.getLogger("escort_chatbot.conversation_context")


class ConversationContext:
    """Manages conversation context and client memory."""
    
    def __init__(self, db_service_or_phone=None):
        """
        Initialize conversation context manager.
        
        Args:
            db_service_or_phone: Database service instance or phone number (for testing)
        """
        if isinstance(db_service_or_phone, str):
            # For testing: create a mock context
            self.db = None
            self.phone_number = db_service_or_phone
            self.fields = {}
        else:
            self.db = db_service_or_phone
            self.phone_number = None
            self.fields = {}
    
    def set_field(self, field: str, value: Any) -> None:
        """Set a field value (for testing)."""
        self.fields[field] = value
    
    def get_field(self, field: str) -> Any:
        """Get a field value (for testing)."""
        return self.fields.get(field)
    
    def get_client_context(self, phone_number: str) -> dict[str, Any]:
        """
        Get client conversation context and preferences.
        
        Args:
            phone_number: Client's phone number
            
        Returns:
            Dict with client context
        """
        if not self.db:
            return {}
        try:
            bookings: list[dict[str, Any]] = []
            preferred_duration = None
            preferred_experience = None
            preferred_location = None
            total_bookings = 0
            last_booking = None

            # Primary preference source: durable aggregate row.
            pref_rows = self.db.execute_query(
                """
                SELECT preferred_duration, preferred_experience, preferred_location,
                       total_bookings, last_booking_date
                FROM client_preferences
                WHERE phone_number = %s
                LIMIT 1
                """,
                (phone_number,),
                fetch=True,
            ) or []
            if pref_rows:
                pref = pref_rows[0]
                preferred_duration = row_get(pref, "preferred_duration")
                preferred_experience = row_get(pref, "preferred_experience")
                preferred_location = row_get(pref, "preferred_location")
                total_bookings = int(row_get(pref, "total_bookings", 0) or 0)
                last_booking = row_get(pref, "last_booking_date")

            # Secondary source: durable booking_history log (append-only per-confirmation).
            history_rows = self.db.execute_query(
                """
                SELECT booking_date AS date, booking_time, duration, experience_type, incall_outcall,
                       confirmed_at, total_booking_cost, deposit_paid
                FROM booking_history
                WHERE phone_number = %s
                ORDER BY confirmed_at DESC
                LIMIT 10
                """,
                (phone_number,),
                fetch=True,
            ) or []
            for row in history_rows:
                bookings.append(
                    {
                        "date": row_get(row, "date"),
                        "duration": row_get(row, "duration"),
                        "experience_type": row_get(row, "experience_type"),
                        "incall_outcall": row_get(row, "incall_outcall"),
                        "confirmed_at": row_get(row, "confirmed_at"),
                    }
                )

            # Tertiary source: booking_analytics snapshots when booking_history is empty.
            if not bookings:
                analytics_rows = self.db.execute_query(
                    """
                    SELECT booking_fields, created_at
                    FROM booking_analytics
                    WHERE phone_number = %s
                      AND booking_fields IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 10
                    """,
                    (phone_number,),
                    fetch=True,
                ) or []
                for row in analytics_rows:
                    raw_fields = row_get(row, "booking_fields") or {}
                    if isinstance(raw_fields, str):
                        try:
                            raw_fields = json.loads(raw_fields)
                        except Exception:
                            raw_fields = {}
                    if not isinstance(raw_fields, dict):
                        continue
                    bookings.append(
                        {
                            "date": raw_fields.get("date"),
                            "duration": raw_fields.get("duration"),
                            "experience_type": raw_fields.get("experience_type"),
                            "incall_outcall": raw_fields.get("incall_outcall"),
                        }
                    )

            # Final fallback: current-state row (only if no better history exists).
            if not bookings:
                state_rows = self.db.execute_query(
                    """
                    SELECT date, duration, experience_type, incall_outcall
                    FROM conversation_states
                    WHERE phone_number = %s
                      AND confirmed_at IS NOT NULL
                    ORDER BY confirmed_at DESC
                    LIMIT 5
                    """,
                    (phone_number,),
                    fetch=True,
                ) or []
                for row in state_rows:
                    bookings.append(
                        {
                            "date": row_get(row, "date"),
                            "duration": row_get(row, "duration"),
                            "experience_type": row_get(row, "experience_type"),
                            "incall_outcall": row_get(row, "incall_outcall"),
                        }
                    )

            if preferred_duration is None:
                preferred_duration = self._calculate_preference(bookings, "duration")
            if preferred_experience is None:
                preferred_experience = self._calculate_preference(bookings, "experience_type")
            if preferred_location is None:
                preferred_location = self._calculate_preference(bookings, "incall_outcall")
            if last_booking is None and bookings:
                last_booking = bookings[0].get("date")
            if total_bookings < len(bookings):
                total_bookings = len(bookings)

            return {
                "total_bookings": total_bookings,
                "last_booking_date": last_booking,
                "preferred_duration": preferred_duration,
                "preferred_experience": preferred_experience,
                "preferred_location": preferred_location,
                "booking_history": bookings[:5],
            }
        except Exception as e:
            logger.error(f"Failed to get client context for {phone_number}: {e}")
            return {}
    
    def _calculate_preference(self, bookings: list, field: str) -> str | None:
        """Calculate most common preference from booking history."""
        if not bookings:
            return None
        
        values = [b.get(field) for b in bookings if b.get(field)]
        if not values:
            return None
        
        # Return most common value
        counter = Counter(values)
        return counter.most_common(1)[0][0] if counter else None
    
    def get_smart_defaults(self, phone_number: str) -> dict[str, Any]:
        """
        Get smart defaults based on client history.
        
        Args:
            phone_number: Client's phone number
            
        Returns:
            Dict with suggested defaults
        """
        context = self.get_client_context(phone_number)
        
        defaults = {}
        
        if context.get('preferred_duration'):
            defaults['duration'] = context['preferred_duration']
        
        if context.get('preferred_experience'):
            defaults['experience_type'] = context['preferred_experience']
        
        if context.get('preferred_location'):
            defaults['incall_outcall'] = context['preferred_location']
        
        return defaults
    
    def update_client_notes(self, phone_number: str, notes: str) -> bool:
        """
        Update client notes.
        
        Args:
            phone_number: Client's phone number
            notes: Notes text
            
        Returns:
            True if successful
        """
        try:
            if self.db is None:
                return False
            # Store in conversation_states or separate table
            # For now, using a JSONB field in conversation_states
            self.db.execute_query(
                """UPDATE conversation_states 
                   SET client_notes = %s, updated_at = CURRENT_TIMESTAMP
                   WHERE phone_number = %s""",
                (notes, phone_number)
            )
            return True
        except Exception as e:
            logger.error(f"Failed to update client notes: {e}")
            return False
    
    def get_client_notes(self, phone_number: str) -> str | None:
        """Get client notes."""
        try:
            if self.db is None:
                return None
            result = self.db.execute_query(
                """SELECT client_notes FROM conversation_states WHERE phone_number = %s""",
                (phone_number,),
                fetch=True
            )
            if result:
                return row_get(result[0], 'client_notes', None)
            return None
        except Exception as e:
            logger.error(f"Failed to get client notes: {e}")
            return None


    def get(self, key: str, default = None):
        """Dict-like interface (for test compatibility)."""
        return self.fields.get(key, default)


# ---------------------------------------------------------------------------
# BookingContext — strongly-typed per-request context (v2 event-driven path)
# ---------------------------------------------------------------------------

@dataclass
class BookingContext:
    """
    Immutable-by-convention context object threaded through v2 event-driven
    handlers.

    Rules
    -----
    * Handlers read from this object but MUST NOT call ``state_manager``
      directly to change state.  They return ``(event, response)`` instead.
    * The router resolves the next state via ``state_machine.transition()``,
      then persists it with ``state_manager.transition()``.
    * ``booking_data`` mirrors the ``conversation_states`` row for the active
      phone number.  It is populated by the router before dispatch and is
      read-only inside handlers.
    * ``metadata`` carries per-request ephemeral data (intent confidence,
      timing, flags) that does not need to be persisted.

    Backward compatibility
    ----------------------
    The v1 ``context: dict[str, Any]`` contract used by all existing handlers
    is unchanged.  ``BookingContext`` is *only* used when the router detects
    ``flow_version == "v2"`` on the DB row.
    """

    # ----- identity --------------------------------------------------------
    user_id: str                    # phone_number (canonical identifier)
    flow_version: str = "v1"        # "v1" = legacy dict path, "v2" = event-driven

    # ----- FSM state -------------------------------------------------------
    state: str = "NEW"              # current FSM state from DB row

    # ----- booking payload (mirrors conversation_states row) ---------------
    booking_data: dict[str, Any] = field(default_factory=dict)

    # ----- per-request ephemeral data (NOT persisted) ----------------------
    metadata: dict[str, Any] = field(default_factory=dict)

    # ----- convenience accessors -------------------------------------------

    @classmethod
    def from_db_row(cls, row: dict[str, Any], flow_version: str = "v1") -> "BookingContext":
        """
        Build a ``BookingContext`` from a raw ``conversation_states`` DB row.

        Args:
            row:           Dict returned by ``StateManager.get_state()``.
            flow_version:  ``"v1"`` or ``"v2"`` — controls router dispatch.

        Returns:
            Populated ``BookingContext`` ready for handler dispatch.
        """
        from core.state_machine import is_valid_state

        raw_state = row.get("current_state", "NEW")
        if not is_valid_state(raw_state):
            logger.warning(
                "BookingContext.from_db_row: unrecognised state %r for %r — defaulting to NEW",
                raw_state, row.get("phone_number"),
            )
            raw_state = "NEW"

        return cls(
            user_id=str(row.get("phone_number", "")),
            state=raw_state,
            flow_version=flow_version,
            booking_data={k: v for k, v in row.items() if k != "phone_number"},
        )

    def assert_state_valid(self) -> None:
        """Raise ``AssertionError`` if the current state is not in VALID_STATES."""
        from core.state_machine import assert_valid_state
        assert_valid_state(self.state)

    @property
    def phone_number(self) -> str:
        """Alias for ``user_id`` — keeps handler code readable."""
        return self.user_id
