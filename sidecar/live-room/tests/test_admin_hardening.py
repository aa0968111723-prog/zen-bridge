"""Regression tests for QA defects found during laptop integration (DEFECTS.md)."""
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from starlette.testclient import TestClient  # noqa: E402

from app.admin import security  # noqa: E402
from app.admin.server import create_admin_app  # noqa: E402

TOKEN = "test-admin-token-0123456789"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
BEARER = {"authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=security.token_hash(TOKEN), port=PORT,
                           identity_path=tmp_path / "zen-identity.sqlite3", probes={"live": lambda: "up"},
                           backup_dir=lambda: tmp_path / "backups", start_worker=False)
    return TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))


# D-002: credential-looking query parameters must be refused even when encoded/renamed.
@pytest.mark.parametrize("query", [
    "%74oken=x", "%54OKEN=x", "t%6Fken=x", "%2574oken=x", "a=1;token=x", "+token=x", "token%20=x",
    "Api-Key=x", "apikey=x", "password=x", "bearer=x", "session=x", "jwt=x", "refresh_token=x",
    "my_token=x", "client_secret=x", "token[]=x", "zen_admin_sid=x", "csrf=x",
])
def test_encoded_or_renamed_secret_query_refused(client, query):
    r = client.get("/admin/api/v1/health?" + query, headers=BEARER)
    assert r.status_code == 400, query
    assert r.headers["content-type"].startswith("application/problem+json")


@pytest.mark.parametrize("query", ["", "limit=5", "q=%E4%BD%9B&scope=transcript", "cursor=abc&room_id=r1",
                                   "status=applied", "kind=embed&state=queued", "flagged=1"])
def test_ordinary_query_parameters_still_allowed(client, query):
    r = client.get("/admin/api/v1/health?" + query, headers=BEARER)
    assert r.status_code == 200, query


def test_query_has_secret_unit():
    assert security.query_has_secret(b"%74oken=1")
    assert security.query_has_secret("x=1;Access-Token=2")
    assert not security.query_has_secret(b"limit=10&cursor=eyJ0b2tlbiI6MX0")
    assert not security.query_has_secret(b"")
    assert not security.query_has_secret(b"tokenizer=bpe")
