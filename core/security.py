"""
Security Infrastructure - CSRF Protection, Security Headers, Input Sanitization, Audit Logging
"""

import logging
import os
import re
import secrets

from flask import request
from markupsafe import escape as _markupsafe_escape

import config
from services.database_service import DatabaseService

logger = logging.getLogger("adella_chatbot.security")

# Security audit logger
security_logger = logging.getLogger("adella_chatbot.security_audit")
security_logger.setLevel(logging.INFO)

# File handler for security events
_log_dir = os.path.join(config.BASE_DIR, 'logs')
os.makedirs(_log_dir, exist_ok=True)
_security_handler = logging.FileHandler(os.path.join(_log_dir, 'security_audit.log'))
_security_handler.setFormatter(logging.Formatter(
    '%(asctime)s | %(levelname)s | %(message)s'
))
security_logger.addHandler(_security_handler)


def init_security_audit_table(db_service: DatabaseService):
    """Initialize security audit log table."""
    try:
        db_service.execute_query("""
            CREATE TABLE IF NOT EXISTS security_audit_log (
                id SERIAL PRIMARY KEY,
                event_type VARCHAR(50) NOT NULL,
                ip_address VARCHAR(45),
                phone_number VARCHAR(20),
                user_agent TEXT,
                details TEXT,
                severity VARCHAR(20) DEFAULT 'INFO',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        db_service.execute_query("CREATE INDEX IF NOT EXISTS idx_security_audit_type ON security_audit_log(event_type)")
        db_service.execute_query("CREATE INDEX IF NOT EXISTS idx_security_audit_ip ON security_audit_log(ip_address)")
        db_service.execute_query("CREATE INDEX IF NOT EXISTS idx_security_audit_created ON security_audit_log(created_at DESC)")
        db_service.execute_query("CREATE INDEX IF NOT EXISTS idx_security_audit_severity ON security_audit_log(severity)")
        logger.info("Security audit log table initialized")
    except Exception as e:
        logger.warning(f"Failed to initialize security audit table: {e}")


def log_security_event(
    event_type: str,
    ip_address: str | None = None,
    phone_number: str | None = None,
    user_agent: str | None = None,
    details: str | None = None,
    severity: str = "INFO",
    db_service: DatabaseService | None = None
):
    """Log a security event to both file and database.
    
    Args:
        event_type: Type of event (e.g., 'LOGIN_ATTEMPT', 'RATE_LIMIT_EXCEEDED')
        ip_address: Client IP address
        phone_number: Associated phone number if applicable
        user_agent: Client user agent string
        details: Additional event details
        severity: Event severity (INFO, WARNING, CRITICAL)
    """
    # Log to file
    log_msg = f"{event_type} | IP:{ip_address} | Phone:{phone_number} | {details}"
    if severity == "CRITICAL":
        security_logger.critical(log_msg)
    elif severity == "WARNING":
        security_logger.warning(log_msg)
    else:
        security_logger.info(log_msg)
    
    # Log to database if available
    if db_service:
        try:
            db_service.execute_query(
                """INSERT INTO security_audit_log
                   (event_type, ip_address, phone_number, user_agent, details, severity)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (event_type, ip_address, phone_number, user_agent, details, severity)
            )
        except Exception as e:
            security_logger.error(f"Failed to log security event to DB: {e}")


def add_security_headers(response):
    """Add security headers to Flask response."""
    # Content Security Policy
    # 'unsafe-inline' is required for admin template inline scripts/styles.
    # Restricting default-src to 'self' still prevents data exfiltration via
    # unknown origins even while inline execution is permitted.
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://maps.googleapis.com https://maps.gstatic.com https://cdn.jsdelivr.net; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: https:; "
        "font-src 'self' data: https://fonts.gstatic.com; "
        "connect-src 'self' https://maps.googleapis.com; "
        "frame-ancestors 'none';"
    )
    
    # X-Frame-Options
    response.headers['X-Frame-Options'] = 'DENY'
    
    # X-Content-Type-Options
    response.headers['X-Content-Type-Options'] = 'nosniff'
    
    # X-XSS-Protection
    response.headers['X-XSS-Protection'] = '1; mode=block'
    
    # Referrer-Policy
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    
    # Permissions-Policy
    response.headers['Permissions-Policy'] = 'geolocation=(), microphone=(), camera=()'
    
    return response


# AI Input Sanitization Patterns
INJECTION_PATTERNS = [
    # Direct instruction overrides
    (r'(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|directions?)', '[FILTERED]'),
    (r'(?i)disregard\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)', '[FILTERED]'),
    (r'(?i)forget\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)', '[FILTERED]'),
    (r'(?i)override\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)', '[FILTERED]'),
    
    # System prompt injection attempts
    (r'(?i)```\s*system\s*:', '```[BLOCKED]:'),
    (r'(?i)\[system\s*\]', '[BLOCKED]'),
    (r'(?i)<system>', '<BLOCKED>'),
    (r'(?i)system\s*prompt\s*:', '[FILTERED]:'),
    
    # Role switching attempts
    (r'(?i)you\s+are\s+now\s+(a\s+)?different', 'you are the same'),
    (r'(?i)pretend\s+(to\s+be|you\s+are)\s+(a\s+)?different', '[FILTERED]'),
    (r'(?i)act\s+as\s+(a\s+)?different\s+(assistant|ai|bot)', '[FILTERED]'),
    
    # Jailbreak attempts
    (r'(?i)do\s+anything\s+now', '[FILTERED]'),
    (r'(?i)DAN\s+mode', '[FILTERED]'),
    (r'(?i)developer\s+mode\s+(enabled|on|active)', '[FILTERED]'),
    (r'(?i)bypass\s+(your\s+)?(safety|content|filter|restriction)', '[FILTERED]'),
    
    # Unicode/encoding tricks
    (r'[\u200b\u200c\u200d\ufeff]', ''),  # Zero-width characters
    (r'[\u2060\u2061\u2062\u2063]', ''),  # Word joiners
    
    # Command injection
    (r'(?i)\$\{.*?\}', '[FILTERED]'),  # Template injection
    (r'(?i)\{\{.*?\}\}', '[FILTERED]'),  # Jinja-style injection
]

MAX_MESSAGE_LENGTH = 4000
SUSPICIOUS_LENGTH = 2000


def sanitize_user_input(text: str, context: str = "general") -> str:
    """Truncate + strip control chars + filter known prompt-injection regexes.

    **Not** an XSS escape. The previous implementation did blind denylist
    character stripping (``[<>"';&]``, ``javascript:``) which is trivially
    bypassed by HTML entities, case variants, and unicode homoglyphs — it
    offered false security and mangled legitimate punctuation in SMS text.
    Browser-facing rendering must use jinja's autoescape or
    ``markupsafe.escape`` at template render time instead.
    """
    if not text:
        return ""

    original_length = len(text)

    if len(text) > MAX_MESSAGE_LENGTH:
        logger.warning(f"Input truncated from {len(text)} to {MAX_MESSAGE_LENGTH} chars in {context}")
        text = text[:MAX_MESSAGE_LENGTH] + "... [truncated]"

    if original_length > SUSPICIOUS_LENGTH:
        logger.info(f"Long input ({original_length} chars) in {context}")

    injection_detected = False
    for pattern, replacement in INJECTION_PATTERNS:
        if re.search(pattern, text):
            injection_detected = True
            text = re.sub(pattern, replacement, text)

    if injection_detected:
        logger.warning(f"Potential prompt injection detected and filtered in {context}")
        log_security_event(
            "PROMPT_INJECTION_DETECTED",
            ip_address=request.remote_addr if hasattr(request, 'remote_addr') else None,
            details=f"Context: {context}",
            severity="WARNING"
        )

    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r' {3,}', '  ', text)
    text = ''.join(char for char in text if char == '\n' or char == '\t' or (ord(char) >= 32 and ord(char) != 127))
    return text.strip()


sanitize_input = sanitize_user_input


def escape_for_html(text: str) -> str:
    """Escape user-supplied text for safe HTML rendering.

    Use this at the render boundary (template or response body). Jinja's
    autoescape handles the common case; call this when you're assembling HTML
    strings manually or passing user data through ``|safe``-like filters.
    """
    return str(_markupsafe_escape(text or ""))


def is_suspicious_message(text: str) -> tuple[bool, list[str]]:
    """Check if a message contains suspicious patterns.
    
    Args:
        text: Message to check
        
    Returns:
        Tuple of (is_suspicious, list of detected patterns)
    """
    if not text:
        return False, []
    
    detected = []
    for pattern, _ in INJECTION_PATTERNS:
        if re.search(pattern, text):
            detected.append(pattern)
    
    return len(detected) > 0, detected


def generate_csrf_token() -> str:
    """Generate a CSRF token for form protection."""
    return secrets.token_urlsafe(32)


def get_client_ip() -> str:
    """Get client IP address from request. Delegates to utils.net for correct proxy handling."""
    from utils.net import get_client_ip as _net_get_client_ip
    return _net_get_client_ip(request)


def get_user_agent() -> str:
    """Get user agent from request."""
    if hasattr(request, 'user_agent'):
        return str(request.user_agent)
    return 'Unknown'
