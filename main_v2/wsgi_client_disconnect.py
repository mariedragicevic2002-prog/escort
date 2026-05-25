"""Helpers to soften client-disconnect socket errors on WSGI.

Not registered from ``application.py`` by default: wrapping ``app.wsgi_app`` and
proxying Flask's response iterable has caused blank pages / bad gateway responses
on PythonAnywhere (uWSGI). Import and call ``install_ignore_client_disconnect_middleware``
only where you have verified the stack (e.g. local devserver).

For PA, prefer tolerating log noise or gateway-level ignore-write-errors if available.
"""

from __future__ import annotations

import errno
import logging
from typing import TYPE_CHECKING, Any

from werkzeug.wsgi import ClosingIterator

if TYPE_CHECKING:
    from flask import Flask

_log = logging.getLogger(__name__)


def _is_client_disconnect(exc: BaseException) -> bool:
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        err = getattr(exc, "errno", None)
        if err in (errno.EPIPE, errno.ECONNRESET):
            return True
        lowered = str(exc).lower()
        if "broken pipe" in lowered or "write error" in lowered:
            return True
    return False


def install_ignore_client_disconnect_middleware(app: Any) -> None:
    """
    Wrap WSGI app so benign client disconnects don't bubble as uncaught OSError.

    WSGI requires ``start_response`` to be invoked exactly once before returning an
    iterable that may yield body bytes. Returning ``[]`` without calling
    ``start_response`` breaks servers (e.g. uWSGI blank/502 responses).

    We wrap ``start_response`` to detect whether headers were already committed,
    then on disconnect either complete a minimal response or return an empty
    iterable without calling ``start_response`` twice.
    """
    original_wsgi = app.wsgi_app

    _DISCONNECT_HTTP_STATUS = "200 OK"
    _DISCONNECT_HEADERS = [("Content-Length", "0")]

    def middleware(environ, start_response):
        response_started = False

        def capturing_start_response(status, headers, exc_info=None):
            nonlocal response_started
            response_started = True
            return start_response(status, headers, exc_info)

        try:
            iterable = original_wsgi(environ, capturing_start_response)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            if _is_client_disconnect(e):
                _log.warning("Client disconnected before response iterable: %s", e)
                if not response_started:
                    try:
                        start_response(_DISCONNECT_HTTP_STATUS, list(_DISCONNECT_HEADERS))
                    except Exception:
                        pass
                # Headers may already be sent; empty iterable ends the request cleanly.
                return [b""]
            raise

        def body_iter():
            try:
                yield from iterable
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                if _is_client_disconnect(e):
                    _log.warning("Client disconnected during response body: %s", e)
                    return
                raise

        # ClosingIterator ensures generator.close() runs inner iterable cleanup once (WSGI pattern).
        return ClosingIterator(body_iter())

    app.wsgi_app = middleware
