import asyncio

import httpx
import pytest

from errors import UpstreamError
from http_client import request_json
from index import build_server
from logging_util import JsonFormatter


def test_http_retry_then_ok():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 2 else httpx.Response(200, json={"ok": 1})

    out = asyncio.run(request_json("GET", "http://x/y", backoff=0,
                                   transport=httpx.MockTransport(h)))
    assert out == {"ok": 1}


def test_http_404_raises():
    with pytest.raises(UpstreamError):
        asyncio.run(request_json("GET", "http://x/y",
                                 transport=httpx.MockTransport(lambda r: httpx.Response(404))))


def test_log_redacts():
    import logging
    r = logging.LogRecord("n", 20, "", 0, "m", None, None)
    r.fields = {"api_key": "s3cret"}
    assert "s3cret" not in JsonFormatter().format(r)


def _run(handler, **kw):
    return asyncio.run(request_json("GET", "http://x/y", backoff=0,
                                    transport=httpx.MockTransport(handler), **kw))


def test_non_json_200_wrapped():
    with pytest.raises(UpstreamError):
        _run(lambda r: httpx.Response(200, text="<html>nope"))


def test_response_cap():
    with pytest.raises(UpstreamError, match="exceeded"):
        _run(lambda r: httpx.Response(200, content=b"x" * 5000), max_bytes=1000)


def test_from_status_redacts():
    from errors import from_status
    e = from_status(500, 'oops Bearer abcdef1234567890 token=hunter2secret "api_key": "zzz999"')
    m = str(e)
    assert "abcdef1234567890" not in m and "hunter2secret" not in m and "zzz999" not in m


def test_log_redacts_msg_and_exc():
    import logging
    import sys
    try:
        raise ValueError("bad Authorization: Bearer topsecrettoken123")
    except ValueError:
        r = logging.LogRecord("n", 40, "", 0, "got token=abc123xyz", None, sys.exc_info())
    out = JsonFormatter().format(r)
    assert "abc123xyz" not in out and "topsecrettoken123" not in out
