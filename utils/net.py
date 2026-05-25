"""Network helpers shared by the webhook app and standalone utilities.

Keeps the client-IP resolution rules in one place so admin auth, the admin
rate limiter, and the deploy endpoint agree on what "the client IP" means.
Previously each call site had its own variant — one validated XFF but
trusted any peer, one trusted XFF only from known proxies, one did a naive
split. Unifying here removes an entire class of bypasses where a
trusted-proxy-style limiter could be defeated by whichever consumer happened
to forget the proxy check.
"""

from __future__ import annotations

import ipaddress
import logging
import os

logger = logging.getLogger("adella_chatbot.utils.net")


def _parse_trusted_proxy_ips() -> frozenset[str]:
    return frozenset(
        ip.strip()
        for ip in (os.environ.get("TRUSTED_PROXY_IPS") or "").split(",")
        if ip.strip()
    )


def get_client_ip(flask_request, *, trust_proxy: bool = True) -> str:
    """Return the best-guess client IP for ``flask_request``.

    Rules:
      * If ``trust_proxy`` is False (or ``TRUSTED_PROXY_IPS`` is unset),
        always return ``remote_addr``. Defeats XFF forgery when the app is
        exposed directly.
      * Else only honour ``X-Forwarded-For`` when ``remote_addr`` is in the
        trusted-proxy allowlist. The first hop is validated with
        ``ipaddress.ip_address`` — garbage values fall back to
        ``remote_addr`` so they can't be used as a fresh cache key per
        request (which would effectively disable rate limiting for the
        attacker).
      * Always returns a non-empty string; ``"unknown"`` when neither
        source yields a usable IP.
    """
    remote = (flask_request.remote_addr or "").strip()
    fallback = remote or "unknown"

    trusted = _parse_trusted_proxy_ips()
    if not trust_proxy or not trusted or remote not in trusted:
        return fallback

    fwd = (flask_request.headers.get("X-Forwarded-For") or "").strip()
    if not fwd:
        return fallback

    first = fwd.split(",", 1)[0].strip()
    if not first:
        return fallback
    try:
        ipaddress.ip_address(first)
    except ValueError:
        logger.warning("Ignoring invalid X-Forwarded-For value for client-IP resolution")
        return fallback
    return first
