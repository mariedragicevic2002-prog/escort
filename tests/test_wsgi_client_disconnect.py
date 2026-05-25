"""WSGI middleware for client disconnect must call start_response when handling early errors."""

import errno

import pytest

from main_v2.wsgi_client_disconnect import install_ignore_client_disconnect_middleware


class _FakeApp:
    __slots__ = ("wsgi_app",)

    def __init__(self, inner):
        self.wsgi_app = inner


def _sr_sink():
    calls = []

    def start_response(status, headers, exc_info=None):
        calls.append((status, list(headers)))

    return calls, start_response


def test_disconnect_before_start_response_invokes_start_response_once():
    def inner(environ, start_response):
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    app = _FakeApp(inner)
    install_ignore_client_disconnect_middleware(app)  # type: ignore[arg-type]

    calls, sr = _sr_sink()
    body = list(app.wsgi_app({}, sr))

    assert len(calls) == 1
    assert calls[0][0] == "200 OK"
    assert dict(calls[0][1])["Content-Length"] == "0"
    assert body == [b""]


def test_disconnect_after_start_response_no_second_call():
    def inner(environ, start_response):
        start_response("200 OK", [("Content-Length", "99")])
        raise BrokenPipeError(errno.EPIPE, "Broken pipe")

    app = _FakeApp(inner)
    install_ignore_client_disconnect_middleware(app)  # type: ignore[arg-type]

    calls, sr = _sr_sink()
    body = list(app.wsgi_app({}, sr))

    assert len(calls) == 1
    assert body == [b""]


def test_disconnect_during_body_suppressed():
    def inner(environ, start_response):
        start_response("200 OK", [("Content-Length", "10")])

        def gen():
            raise BrokenPipeError(errno.EPIPE, "Broken pipe")
            yield b"x"

        return gen()

    app = _FakeApp(inner)
    install_ignore_client_disconnect_middleware(app)  # type: ignore[arg-type]

    calls, sr = _sr_sink()
    iterable = app.wsgi_app({}, sr)
    assert len(calls) == 1
    out = list(iterable)
    assert out == []


def test_non_disconnect_oserror_propagates():
    def inner(environ, start_response):
        raise OSError(errno.ENOSPC, "No space")

    app = _FakeApp(inner)
    install_ignore_client_disconnect_middleware(app)  # type: ignore[arg-type]

    calls, sr = _sr_sink()
    with pytest.raises(OSError) as ei:
        list(app.wsgi_app({}, sr))
    assert ei.value.errno == errno.ENOSPC
    assert len(calls) == 0
