"""Small helpers for the inbound SMS webhook (reduces cognitive complexity in application.webhook)."""

from __future__ import annotations

from typing import Any

from flask import Request, jsonify

from utils.log_sanitize import LOG_SUPPRESSED_FMT

from .log import logger
from .webhook_monitor import record_webhook_monitor


def _parse_twilio_form(form) -> dict[str, Any]:
    """Extract a normalized payload dict from a Twilio-style form submission."""
    _from = (form.get("From") or "").strip()
    _body = (form.get("Body") or "").strip()
    if not (_from or _body):
        return {}
    _media_urls: list[str] = []
    try:
        _num_media = int((form.get("NumMedia") or "0").strip())
    except (TypeError, ValueError):
        _num_media = 0
    for idx in range(max(_num_media, 0)):
        _url = (form.get(f"MediaUrl{idx}") or "").strip()
        if _url:
            _media_urls.append(_url)
    _data: dict[str, Any] = {"contact": _from, "content": _body}
    if _media_urls:
        _data["media_urls"] = _media_urls
    return {"data": _data}


def extract_webhook_contact_phone(request: Request) -> str:
    """Best-effort caller phone for error-path SMS (JSON ``data.contact``, then Twilio form)."""
    payload = request.get_json(force=True, silent=True)
    if isinstance(payload, dict):
        nested = payload.get("data")
        data = nested if isinstance(nested, dict) else payload
        phone = (data.get("contact") or "").strip()
        if phone:
            return phone
    try:
        if request.form:
            return (request.form.get("From") or "").strip()
    except Exception:
        pass
    return ""


def normalize_webhook_payload(request: Request) -> dict[str, Any]:
    """Parse JSON body or Twilio-style form into a dict (possibly nested under ``data``)."""
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = {}
    if not payload and request.form:
        payload = _parse_twilio_form(request.form)
    return payload


def _collect_media_urls_from_list(items: list) -> list[str]:
    """Extract URL strings from a list of raw media items (str or dict)."""
    media_urls: list[str] = []
    for item in items:
        if isinstance(item, str):
            _u = item.strip()
            if _u:
                media_urls.append(_u)
        elif isinstance(item, dict):
            _u = (item.get("url") or item.get("media_url") or "").strip()
            if _u:
                media_urls.append(_u)
    return media_urls


def collect_media_urls(msg_data: dict[str, Any]) -> list[str]:
    """Normalize ``media_urls`` / ``media`` / ``attachments`` from inbound message data."""
    _raw_media = (
        msg_data.get("media_urls")
        or msg_data.get("media")
        or msg_data.get("attachments")
        or []
    )
    if isinstance(_raw_media, str):
        return [_raw_media.strip()] if _raw_media.strip() else []
    if isinstance(_raw_media, dict):
        _url = (_raw_media.get("url") or "").strip()
        return [_url] if _url else []
    if isinstance(_raw_media, list):
        return _collect_media_urls_from_list(_raw_media)
    return []


def webhook_json_fastpath_reply(
    state_manager,
    phone_number: str,
    message_body: str,
    media_urls: list[Any],
    reply_text: str,
    request_id: str,
    *,
    send_error_label: str,
    fallback_on_send_fail: str | None = None,
) -> tuple[Any, int]:
    """Log inbound, send one outbound SMS, log outbound, record monitor; return JSON response."""
    from services.sms_service import send_sms

    sent, failed = 0, 0
    try:
        if not state_manager.get_state(phone_number):
            state_manager.create_state(phone_number, "NEW")
        state_manager.log_message(phone_number, "inbound", message_body, media_urls)
        state_manager.touch(phone_number)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        if send_sms(phone_number, reply_text):
            sent = 1
        else:
            failed = 1
    except Exception as send_err:
        logger.error("%s: %s", send_error_label, send_err)
        failed = 1
        if fallback_on_send_fail:
            try:
                if send_sms(phone_number, fallback_on_send_fail):
                    sent = 1
                    failed = 0
            except Exception as e:
                logger.warning(LOG_SUPPRESSED_FMT, e)
    try:
        state_manager.log_message(phone_number, "outbound", reply_text)
    except Exception as e:
        logger.warning(LOG_SUPPRESSED_FMT, e)
    record_webhook_monitor(request_id=request_id, messages_sent=sent, messages_failed=failed)
    return jsonify({"status": "success", "messages_sent": 1, "request_id": request_id}), 200
