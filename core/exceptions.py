"""
Custom Exceptions for Escort Chatbot
Provides a hierarchy of specific exception types for better error handling.
"""


class ChatBotError(Exception):
    """Base exception for all chatbot errors."""
    
    def __init__(self, message: str, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
    
    def __str__(self):
        if self.details:
            return f"{self.message} | Details: {self.details}"
        return self.message


class DatabaseError(ChatBotError):
    """Database operation failed."""
    
    def __init__(self, message: str, query: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.query = query


class DatabaseConnectionError(DatabaseError):
    """Failed to establish database connection."""
    pass


class DatabaseQueryError(DatabaseError):
    """Query execution failed."""
    pass


class CalendarError(ChatBotError):
    """Calendar API operation failed."""
    
    def __init__(self, message: str, event_id: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.event_id = event_id


class CalendarConnectionError(CalendarError):
    """Failed to connect to Calendar API."""
    pass


class CalendarEventError(CalendarError):
    """Failed to create, update, or delete calendar event."""
    pass


class CalendarConflictError(CalendarError):
    """Time slot conflict detected."""
    
    def __init__(self, message: str, conflicting_event: dict | None = None, details: dict | None = None):
        super().__init__(message, details=details)
        self.conflicting_event = conflicting_event


class AIServiceError(ChatBotError):
    """AI service (Claude/Gemini) operation failed."""
    
    def __init__(self, message: str, provider: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.provider = provider  # "claude" or "gemini"


class AIConnectionError(AIServiceError):
    """Failed to connect to AI service."""
    pass


class AIResponseError(AIServiceError):
    """AI returned invalid or unusable response."""
    pass


class AIRateLimitError(AIServiceError):
    """AI service rate limit exceeded."""
    pass


class AITimeoutError(AIServiceError):
    """AI service request timed out."""
    pass


class ValidationError(ChatBotError):
    """Input validation failed."""
    
    def __init__(self, message: str, field: str | None = None, value=None, details: dict | None = None):
        super().__init__(message, details)
        self.field = field
        self.value = value


class BookingValidationError(ValidationError):
    """Booking details validation failed."""
    pass


class PhoneValidationError(ValidationError):
    """Phone number validation failed."""
    pass


class DateTimeValidationError(ValidationError):
    """Date or time validation failed."""
    pass


class ConfigurationError(ChatBotError):
    """Missing or invalid configuration."""
    
    def __init__(self, message: str, setting_name: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.setting_name = setting_name


class BookingError(ChatBotError):
    """Booking operation failed."""
    
    def __init__(self, message: str, phone_number: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.phone_number = phone_number


class BookingConflictError(BookingError):
    """Booking conflicts with existing event."""
    pass


class BookingNotFoundError(BookingError):
    """Requested booking not found."""
    pass


class DepositError(ChatBotError):
    """Deposit-related operation failed."""
    pass


class RateLimitError(ChatBotError):
    """Rate limit exceeded."""
    
    def __init__(self, message: str, limit: int | None = None, window_seconds: int | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.limit = limit
        self.window_seconds = window_seconds


class SecurityError(ChatBotError):
    """Security violation or authentication error."""
    pass


class ExternalServiceError(ChatBotError):
    """External service call failed (SMS gateway, Google Maps, etc.)."""

    def __init__(self, message: str, service_name: str | None = None, details: dict | None = None):
        super().__init__(message, details)
        self.service_name = service_name


class SMSError(ExternalServiceError):
    """SMS gateway error."""
    pass


class GoogleMapsError(ExternalServiceError):
    """Google Maps API error."""
    pass


# Exception groups for common catch patterns
RETRIABLE_ERRORS = (
    DatabaseConnectionError,
    CalendarConnectionError,
    AIConnectionError,
    AITimeoutError,
    AIRateLimitError,
    ExternalServiceError,
)

VALIDATION_ERRORS = (
    ValidationError,
    BookingValidationError,
    PhoneValidationError,
    DateTimeValidationError,
)

AI_ERRORS = (
    AIServiceError,
    AIConnectionError,
    AIResponseError,
    AIRateLimitError,
    AITimeoutError,
)
