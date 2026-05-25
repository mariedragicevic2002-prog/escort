"""
Request Context Management
Manages request IDs and context for tracing requests through the system.
"""

import uuid

from flask import g, request

from utils.structured_logging import get_request_id, set_request_id


def init_request_context():
    """Initialize request context (call at start of each request)."""
    # Generate or get request ID from header
    request_id = request.headers.get('X-Request-ID')
    if not request_id:
        request_id = str(uuid.uuid4())[:8]  # Short 8-char ID
    
    # Store in Flask g and structured logging
    g.request_id = request_id
    set_request_id(request_id)
    
    return request_id


def get_current_request_id() -> str:
    """Get current request ID from Flask g or structured logging."""
    if hasattr(g, 'request_id'):
        return g.request_id
    return get_request_id() or 'unknown'
