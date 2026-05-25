"""
httpSMS outbound adapter — implements core/ports/sms_gateway.SmsGateway.

Infrastructure adapter: handles HTTP I/O for SMS delivery.
Business logic must NOT live here — this is a pure transport adapter.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


class HttpSmsAdapter:
    """Sends SMS via the httpSMS API."""

    def __init__(self, api_key: Optional[str] = None, phone_id: Optional[str] = None) -> None:
        self._api_key = api_key or os.environ.get("HTTPSMS_API_KEY", "")
        self._phone_id = phone_id or os.environ.get("HTTPSMS_PHONE_ID") or os.environ.get("HTTPSMS_PHONE_NUMBER", "")
        self._base_url = os.environ.get("HTTPSMS_BASE_URL", "https://api.httpsms.com").rstrip("/")

    def send_message(self, phone: str, text: str) -> bool:
        """
        Send an SMS to the given phone number.
        Returns True on success, False on failure.
        Never raises — failures are logged.
        """
        masked_phone = f"{phone[:4]}****" if phone else "unknown"
        if not self._api_key or not self._phone_id:
            logger.error(
                "sms_adapter.not_configured",
                extra={"phone": masked_phone, "api_key_present": bool(self._api_key), "phone_id_present": bool(self._phone_id)},
            )
            return False

        try:
            import requests

            response = requests.post(
                f"{self._base_url}/v1/messages/send",
                json={"to": phone, "content": text, "from": self._phone_id},
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                timeout=10,
            )
            ok = response.status_code in (200, 201, 202)
            if ok:
                logger.info("sms_adapter.sent", extra={"phone": masked_phone, "status": response.status_code})
            else:
                logger.error(
                    "sms_adapter.failed",
                    extra={"phone": masked_phone, "status": response.status_code, "body": response.text[:200]},
                )
            return ok
        except Exception:
            logger.exception("sms_adapter.exception", extra={"phone": masked_phone})
            return False
