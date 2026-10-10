"""後台「即時效能」頁（uiux-a）：路由、靜態白名單、CSP 安全的 HTML、metrics 轉接，以及 Node 純函式測試。

掛載方式比照整合者要做的：在 create_admin_app() 內、zinfo.register(app, ctx) 之後呼叫
perf_routes.register(app, ctx)。這裡用 monkeypatch 包住 info.register 做到同樣效果，
不修改 app/admin/server.py。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.admin import info as zinfo
from app.admin import perf_routes, security
from app.admin.live_client import LiveDown
from app.admin.server import API, create_admin_app

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "admin" / "static"
TOKEN = "test-admin-token-0123456789"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"
BEARER = {"authorization": f"Bearer {TOKEN}"}

def _real_metrics():
    """8780 /api/metrics 的形狀：pipeline.stats()（含 latency.snapshot()）＋其他欄位。"""
    from app.latency import StageLatency
    lat = StageLatency(window=64)
    seg = type("Seg", (), {"lat": {}})()
    for ms in (100, 120, 140, 400):
        lat.note(seg, "A2", ms)
    for ms in (3000, 3500, 5000):
        lat.note(seg, "A5", ms)
    return {
        "pending": 1, "inflight": 1, "backlog_s": 2.0, "asr_samples": 4,
        "asr_rtf_p50": 0.5, "asr_rtf_p95": 0.8, "asr_ms_p50": 3000, "asr_ms_p95": 4800,
        "tokens_used": 999, "price": "secret-ish", "listeners": 3,
        "latency": lat.snapshot(),
        "rtf": {"window": {"count": 4}, "session": {"count": 4, "limit": 200, "rtf": {"p50": 0.5, "p95": 0.8, "max": 1},
                                                     "asr_ms": {"p50": 3000, "p95": 4800, "max": 5000}},
                "sessions": [{"room_id": "class", "session_id": "s1"}]},
    }


class FakeLive:
    def __init__(self):
        self.data = _real_metrics()
        self.down = False

    def metrics(self):
        if self.down:
            raise LiveDown("down")
        return self.data


@pytest.fixture
def env(tmp_path, monkeypatch):
    original = zinfo.register

    def register_with_perf(app, ctx):
        n = original(app, ctx)
        perf_routes.register(app, ctx)          # ← 整合者要加的那一行
        return n

    monkeypatch.setattr(zinfo, "register", register_with_perf)
    live = FakeLive()
    app = create_admin_app(tmp_path / "zen.sqlite3", token_hash_hex=security.token_hash(TOKEN), port=PORT,
                           identity_path=tmp_path / "zen-identity.sqlite3", probes={"live": lambda: "up"},
                           live_client=live, backup_dir=lambda: tmp_path / "backups", start_worker=False,
                           sse_interval_s=0.0, sse_max_events=1)
    client = TestClient(app, base_url=BASE, client=("127.0.0.1", 50000))
    with client:
        yield {"app": app, "client": client, "live": live}


def assert_csp_safe_html(html: str):
    scripts = re.findall(r"<script\b[^>]*>", html, re.I)
    assert scripts, "page must load its script"
    for tag in scripts:
        assert re.search(r"\ssrc=\"/admin/perf/static/[a-z_]+\.js\"", tag), tag
    # no inline script bodies
    assert not re.search(r"<script\b[^>]*>\s*[^<\s]", html, re.I)
    assert not re.search(r"\son[a-z]+\s*=", html, re.I), "inline event handler"
    assert not re.search(r"\sstyle\s*=", html, re.I), "style attribute"
    assert "<style" not in html.lower()
    assert "javascript:" not in html.lower()


def test_page_served_with_csp_and_safe_html(env):
    r = env["client"].get("/admin/perf")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "style-src 'self'" in csp
    assert r.headers.get("cache-control") == "no-store"
    html = r.text
    assert_csp_safe_html(html)
    assert '<meta name="viewport"' in html
    assert 'lang="zh-Hant"' in html
    assert html.count('role="status"') == 1           # 只有一個播報區
    assert "<table" in html and "<details" in html     # 表格版數字
    assert "即時效能" in html


def test_static_allowlist(env):
    c = env["client"]
    for name, ctype in (("perf.js", "javascript"), ("perf_logic.js", "javascript"),
                        ("perf.css", "text/css"), ("perf_fixture.json", "application/json")):
        r = c.get(f"/admin/perf/static/{name}")
        assert r.status_code == 200, name
        assert ctype in r.headers["content-type"], (name, r.headers["content-type"])
        assert "script-src 'self'" in r.headers["content-security-policy"]
    for bad in ("perf.html", "app.js", "server.py", "..%2Fperf_routes.py", "%2e%2e%2fsecurity.py", "vendor"):
        r = c.get(f"/admin/perf/static/{bad}")
        assert r.status_code == 404, bad
        assert r.headers["content-type"].startswith("application/problem+json")
    # the original allowlist is untouched
    assert c.get("/admin/static/perf.js").status_code == 404
    assert c.get("/admin/static/app.js").status_code == 200


def test_static_js_css_have_no_unsafe_constructs():
    js = (STATIC / "perf.js").read_text(encoding="utf-8") + (STATIC / "perf_logic.js").read_text(encoding="utf-8")
    for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "setAttribute(\"style",
                "document.write", "http://", "https://", "EventSource(\"http", "WebSocket("):
        assert bad not in js, bad
    assert "credentials: \"same-origin\"" in js
    assert "document.hidden" in js                    # 分頁隱藏時暫停
    css = (STATIC / "perf.css").read_text(encoding="utf-8")
    assert "@import" not in css and "url(" not in css
    assert ":focus-visible" in css
    fixture = json.loads((STATIC / "perf_fixture.json").read_text(encoding="utf-8"))
    # 示範資料必須和真實 latency.snapshot() 同形狀
    from app.latency import NAMES, STAGES, StageLatency
    real = StageLatency(window=16).snapshot()
    assert set(fixture["latency"]) == set(STAGES) == set(real)
    for sid in STAGES:
        assert set(fixture["latency"][sid]) == set(real[sid])
        assert fixture["latency"][sid]["name"] == NAMES[sid]


def test_metrics_requires_login(env):
    r = env["client"].get(f"{API}/perf/metrics")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["detail"]


def test_metrics_trims_real_shape(env):
    r = env["client"].get(f"{API}/perf/metrics", headers=BEARER)
    assert r.status_code == 200
    assert r.headers.get("cache-control") == "no-store"
    body = r.json()
    lat = body["latency"]
    assert set(lat) == {"A2", "A3", "A4", "A5", "A6"}
    assert lat["A2"]["name"] == "upload" and lat["A2"]["n"] == 4
    assert lat["A2"]["p50_ms"] == 130 and lat["A2"]["p95_ms"] is not None
    assert lat["A3"] == {"name": "queue", "n": 0, "p50_ms": None, "p95_ms": None}
    assert lat["A5"]["p50_ms"] == 3500
    assert body["asr_rtf_p95"] == 0.8 and body["backlog_s"] == 2.0
    assert body["rtf"] == {"session": {"count": 4, "rtf": {"p50": 0.5, "p95": 0.8}}}
    for leaked in ("tokens_used", "price", "listeners", "asr_ms_p50"):
        assert leaked not in body
    assert "class" not in json.dumps(body) and "s1" not in json.dumps(body)   # 不回傳 room/session id
    assert isinstance(body["updated_at"], (int, float))


def test_metrics_without_latency_block(env):
    env["live"].data = {"asr_rtf_p95": 0.4, "latency": {"A2": {"name": "upload", "n": 1, "p50_ms": 5, "p95_ms": 5,
                                                               "secret": "x"}, "A9": {"n": 1}}}
    body = env["client"].get(f"{API}/perf/metrics", headers=BEARER).json()
    assert body["latency"] == {"A2": {"name": "upload", "n": 1, "p50_ms": 5, "p95_ms": 5}}
    env["live"].data = {"asr_rtf_p95": 0.4}
    body = env["client"].get(f"{API}/perf/metrics", headers=BEARER).json()
    assert "latency" not in body and body["asr_rtf_p95"] == 0.4


def test_metrics_live_down_is_problem_json(env):
    env["live"].down = True
    r = env["client"].get(f"{API}/perf/metrics", headers=BEARER)
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    assert "8780" in r.json()["detail"]


def test_trim_metrics_legacy_number_and_garbage():
    assert perf_routes.trim_metrics({"rtf": 0.4, "pending": 0}) == {"rtf": 0.4, "pending": 0}
    assert perf_routes.trim_metrics(None) == {}
    assert perf_routes.trim_metrics({"rtf": True}) == {}


def _node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    raw = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True).stdout.strip()
    try:
        major = int(raw.split(".", 1)[0])
    except ValueError:
        pytest.skip(f"could not parse node version {raw!r}")
    if major < 18:                       # 只用 ES module 與 node:assert，18 以上即可
        pytest.skip(f"node {raw} is older than 18")
    return node


def test_node_perf_logic_suite():
    proc = subprocess.run([_node(), str(ROOT / "tests" / "admin_perf.test.mjs")], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
    assert "admin_perf.test.mjs: ok" in proc.stdout
