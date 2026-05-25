"""
Pure webhook payload parsing — no Flask context side effects, no infrastructure deps.
Lives in app/ingress/ so upper layers (app/) don't depend on main_v2/.
"""
from __future__ import annotations

from typing import Any

from flask import Request


def _parse_twilio_form(form: Any) -> dict[str, Any]:
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


def normalize_webhook_payload(request: Request) -> dict[str, Any]:
    """Parse JSON body or Twilio-style form into a normalised dict."""
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = {}
    if not payload and request.form:
        payload = _parse_twilio_form(request.form)
    return payload
