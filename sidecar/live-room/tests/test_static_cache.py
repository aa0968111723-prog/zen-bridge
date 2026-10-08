"""Host and listener pages, and /static, must revalidate. Secrets and exports stay no-store."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.server import create_app
from app.settings import Settings
from app.translate import Translator


class IdleAsr:
    def health(self) -> bool:
        return True


def make_app():
    return create_app(
        Settings(allow_testclient=True, translate=False),
        asr=IdleAsr(),
        translator=Translator(enabled=False),
    )


def auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}", "origin": "http://127.0.0.1:8780"}


@pytest.mark.anyio
async def test_pages_and_static_files_revalidate():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        pages = {
            "/": "禪譯主持",
            "/r/class": "禪譯聽眾",
            "/static/room_client.js": "export function createCaptionView",
        }
        for path, marker in pages.items():
            resp = await client.get(path)
            assert resp.status_code == 200, path
            assert marker in resp.text, path
            assert resp.headers["cache-control"] == "no-cache", path
            assert resp.headers["etag"], path
            assert resp.headers["last-modified"], path
            etag = resp.headers["etag"]
            cached = await client.get(path, headers={"if-none-match": etag})
            assert cached.status_code == 304, path
            assert cached.content == b"", path
            assert cached.headers["cache-control"] == "no-cache", path
            assert cached.headers["etag"] == etag, path
            mismatch = await client.get(path, headers={"if-none-match": '"not-the-file"'})
            assert mismatch.status_code == 200, path
            assert mismatch.headers["cache-control"] == "no-cache", path


@pytest.mark.anyio
async def test_host_token_readiness_and_export_stay_no_store():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        token_resp = await client.get("/api/host-token")
        assert token_resp.status_code == 200, token_resp.text
        assert token_resp.headers["cache-control"] == "no-store"
        health = await client.get("/api/health")
        assert health.headers["cache-control"] == "no-store"
        exported = await client.get(
            "/api/export",
            params={"room_id": "class", "kind": "srt"},
            headers=auth(token_resp.json()["token"]),
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["cache-control"] == "no-store"
