"""Shared token generation utilities used by webform_security and deposit_upload_tokens."""

import secrets
import string


def generate_short_code(length: int = 6) -> str:
    """Generate a short alphanumeric code for URLs."""
    chars = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))
