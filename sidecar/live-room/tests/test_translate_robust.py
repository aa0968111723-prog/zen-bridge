"""QA AI代理 P2-1 (malformed replies never raise), P2-3 (total deadline / size cap on the reply),
P2-8 (EXTRA_BODY cannot turn on streaming)."""
import http.client
import json
import threading
import time

import pytest

from app import translate as tmod
from app.translate import Translator


class Resp:
    def __init__(self, data=b"", exc=None, drip=None):
        self.data, self.exc, self.drip = data, exc, drip
        self.pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass

    def read1(self, n):
        if self.exc:
            raise self.exc
        if self.drip:
            time.sleep(self.drip)
            if self.pos >= len(self.data):
                return b""
            self.pos += 1
            return self.data[self.pos - 1:self.pos]
        out = self.data[self.pos:self.pos + n]
        self.pos += len(out)
        return out


def tr(resp, **kw):
    seen = {}

    def opener(req, timeout=0):
        seen["body"] = json.loads(req.data)
        return resp
    t = Translator(enabled=True, key="k", opener=opener, sleeper=lambda s: None, **kw)
    return t, seen


OK = json.dumps({"choices": [{"message": {"content": "Hello."}}]}).encode()


@pytest.mark.parametrize("resp,status", [
    (Resp(b"\xff\xfe"), "bad_response"),
    (Resp(b"[" * 100000), "bad_response"),
    (Resp(exc=http.client.IncompleteRead(b"x", 10)), "network"),
    (Resp(exc=http.client.BadStatusLine("x")), "network"),
    (Resp(json.dumps({"choices": [{"message": {"content": "Hi."}}], "usage": [1]}).encode()), "ok"),
    (Resp(json.dumps(["not", "a", "dict"]).encode()), "bad_response"),
])
def test_malformed_never_raises(resp, status):
    t, _ = tr(resp)
    assert t.translate("你好").status == status


def test_reply_size_cap():
    t, _ = tr(Resp(b"x" * (tmod.MAX_REPLY_BYTES + 10)))
    assert t.translate("你好").status == "bad_response"


def test_drip_reply_respects_total_deadline():
    t, _ = tr(Resp(OK, drip=0.05))
    t0 = time.monotonic()
    out = t.translate("你好", deadline=time.monotonic() + 0.4)
    assert out.status == "timeout" and time.monotonic() - t0 < 1.5


def test_drip_reply_respects_cancel():
    t, _ = tr(Resp(OK, drip=0.05))
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    t0 = time.monotonic()
    assert t.translate("你好", cancel=cancel).status == "timeout"
    assert time.monotonic() - t0 < 1.5


def test_extra_body_cannot_enable_stream():
    t, seen = tr(Resp(OK), extra_body={"stream": True, "temperature": 0.1})
    assert t.translate("你好").status == "ok"
    assert seen["body"]["stream"] is False and seen["body"]["temperature"] == 0.1
