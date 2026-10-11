"""QA 後端 B1 (500 text/plain -> problem+json, LiveError mapping) and B2 (typed, signed cursors)."""
import base64
import json

import pytest
from starlette.testclient import TestClient

from app.admin import security
from app.admin.live_client import LiveError
from app.admin.server import API, create_admin_app, dec_cursor, enc_cursor, Problem
from tests.test_admin_api import BASE, BEARER, TOKEN, FakeLive


@pytest.fixture
def mk(tmp_path):
    def build(live):
        app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=security.token_hash(TOKEN), port=8791,
                               probes={}, live_client=live, start_worker=False)
        return TestClient(app, base_url=BASE, client=("127.0.0.1", 50000), raise_server_exceptions=False)
    return build


class Raising(FakeLive):
    def __init__(self, exc):
        super().__init__()
        self.exc = exc

    def metrics(self):
        raise self.exc

    def room_glossary(self, room_id):
        raise self.exc


@pytest.mark.parametrize("exc,status,code", [(LiveError(401, None), 502, "live_auth_failed"),
                                             (LiveError(404, None), 404, "live_error"),
                                             (LiveError(500, None), 502, "live_error"),
                                             (RuntimeError("boom SECRET"), 500, "internal")])
def test_errors_are_problem_json(mk, exc, status, code):
    with mk(Raising(exc)) as c:
        for r in (c.get(f"{API}/live/metrics", headers=BEARER),
                  c.post(f"{API}/rooms/class/glossary/push", json={"dry_run": True}, headers=BEARER)):
            assert r.status_code == status, r.text
            assert r.headers["content-type"].startswith("application/problem+json")
            assert r.json()["code"] == code and "SECRET" not in r.text


def _raw(values):
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


@pytest.mark.parametrize("cursor", [_raw([{"a": 1}, "x"]), _raw([[1], 1]), _raw([1e18, "zzzz"]),
                                    _raw([1.0, "a"]) + ".deadbeefdeadbeef", "garbage", "a.b"])
def test_unsigned_or_mistyped_cursor_400(mk, cursor):
    with mk(FakeLive()) as c:
        for path in ("sessions", "jobs"):
            r = c.get(f"{API}/{path}", params={"cursor": cursor}, headers=BEARER)
            assert r.status_code == 400, (path, r.text)
            assert r.json()["code"] == "bad_cursor"


def test_signed_cursor_roundtrip_and_type_check():
    assert dec_cursor(enc_cursor([1.5, "a"]), 2) == [1.5, "a"]
    with pytest.raises(Problem):
        dec_cursor(enc_cursor([{"a": 1}, "x"]), 2)        # signed but wrong types
    with pytest.raises(Problem):
        dec_cursor(enc_cursor([True]), 1)
