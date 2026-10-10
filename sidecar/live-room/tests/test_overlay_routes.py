"""fullstack: OBS overlay router (app/overlay_routes.py) and its node suite."""

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.overlay_routes import OVERLAY_HTML, router

ROOT = Path(__file__).resolve().parents[1]


def _bare_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def _real_app() -> FastAPI:
    # Mount the router on the real app the way the integrator will (server.py is not edited).
    from app.server import create_app
    from app.settings import Settings

    app = create_app(Settings(allow_testclient=True))
    app.include_router(router)
    return app


def _assert_page(resp):
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "charset=utf-8" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"
    csp = resp.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert 'id="zen-overlay"' in resp.text
    assert "/static/overlay.js" in resp.text


@pytest.mark.anyio
async def test_overlay_page_bare_router():
    async with AsyncClient(transport=ASGITransport(app=_bare_app()), base_url="http://127.0.0.1:8780") as client:
        _assert_page(await client.get("/overlay"))
        _assert_page(await client.get("/overlay?room=hall&lang=ja&size=9999&lines=7&color=%3Cx%3E"))
        _assert_page(await client.get("/overlay/hall-2_b"))
        head = await client.head("/overlay")
        assert head.status_code in (200, 405)


@pytest.mark.anyio
@pytest.mark.parametrize("bad", ["a" * 65, "bad%20room", "%E4%B8%AD", "x.y"])
async def test_overlay_room_id_is_validated(bad):
    async with AsyncClient(transport=ASGITransport(app=_bare_app()), base_url="http://127.0.0.1:8780") as client:
        resp = await client.get("/overlay/" + bad)
        assert resp.status_code == 400


@pytest.mark.anyio
async def test_query_params_are_not_reflected():
    marker = "<script>alert(1)</script>"
    async with AsyncClient(transport=ASGITransport(app=_bare_app()), base_url="http://127.0.0.1:8780") as client:
        resp = await client.get("/overlay", params={"room": marker, "color": marker, "k": marker})
        assert resp.status_code == 200
        assert marker not in resp.text
        assert resp.content == OVERLAY_HTML.read_bytes()


@pytest.mark.anyio
async def test_overlay_mounted_on_real_app(monkeypatch):
    from app import share

    monkeypatch.setattr(share, "lan_ip", lambda: None)
    app = _real_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
        _assert_page(await client.get("/overlay/class?lang=en&size=56"))
        js = await client.get("/static/overlay.js")
        assert js.status_code == 200
        assert js.headers["content-type"].startswith("text/javascript")
        assert js.headers["cache-control"] == "no-cache"
        assert "export function parseParams" in js.text
        # Existing pages are unaffected and the global referrer policy still applies.
        room = await client.get("/r/class")
        assert room.status_code == 200
        overlay = await client.get("/overlay")
        assert overlay.headers.get("referrer-policy") == room.headers.get("referrer-policy")


def test_overlay_static_files_have_no_external_urls():
    for name in ("overlay.html", "overlay.js"):
        text = (ROOT / "app" / "static" / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text
        assert "innerHTML" not in text


def test_overlay_node_suite():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    raw = subprocess.run([node, "-p", "process.versions.node"], capture_output=True, text=True, check=False).stdout.strip()
    try:
        major = int(raw.split(".", 1)[0])
    except ValueError:
        pytest.skip(f"could not parse node version {raw!r}")
    if major < 18:
        pytest.skip(f"node {raw} is older than 18 (node:test)")
    proc = subprocess.run(
        [node, "--test", str(ROOT / "tests" / "overlay.test.mjs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120, check=False,
    )
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr
