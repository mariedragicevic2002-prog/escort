"""
utils/sms_crypto.py

Symmetric encryption for SMS gateway payloads between the Raspberry Pi
(sms_receive.py) and the Flask chatbot (/sms/incoming endpoint).

Uses Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package.
Both sides must share the same SMS_ENCRYPTION_KEY env var.

Generate a key:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Then set on both the Pi and the Flask host:
    export SMS_ENCRYPTION_KEY="<the key>"
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

_KEY = (os.environ.get("SMS_ENCRYPTION_KEY") or "").strip()

# Fernet token TTL — reject tokens older than this (seconds).
# Prevents replay attacks with captured payloads.
TOKEN_TTL_SECONDS = int(os.environ.get("SMS_CRYPTO_TTL", "120"))


def is_encryption_enabled() -> bool:
    """True when an encryption key is configured."""
    return bool(_KEY)


def _get_fernet():
    """Lazy-load Fernet instance."""
    if not _KEY:
        raise RuntimeError(
            "SMS_ENCRYPTION_KEY not set. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    from cryptography.fernet import Fernet
    return Fernet(_KEY.encode("utf-8"))


def encrypt_payload(data: dict) -> str:
    """
    Encrypt a JSON-serialisable dict → base64 Fernet token string.
    Includes a timestamp for TTL enforcement on the receiver side.
    """
    f = _get_fernet()
    plaintext = json.dumps(data).encode("utf-8")
    token = f.encrypt(plaintext)
    return token.decode("utf-8")


def decrypt_payload(token: str, ttl: int | None = None) -> dict:
    """
    Decrypt a Fernet token string → dict.

    Args:
        token: The encrypted base64 Fernet token.
        ttl: Max age in seconds (default: TOKEN_TTL_SECONDS).
             Set to None to use the default; 0 to disable TTL check.

    Raises:
        cryptography.fernet.InvalidToken on bad key, expired, or tampered data.
    """
    f = _get_fernet()
    if ttl is None:
        ttl = TOKEN_TTL_SECONDS
    plaintext = f.decrypt(token.encode("utf-8"), ttl=ttl if ttl > 0 else None)
    return json.loads(plaintext.decode("utf-8"))
