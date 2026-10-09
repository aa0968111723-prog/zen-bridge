"""Host and listener pages, and /static, must revalidate. Secrets and exports stay no-store.

Served pages stamp /static/*.js imports with ?v=<VERSION>-<sha256 prefix> so a
browser that cached the script before Cache-Control existed does not reuse it.
"""

import re
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient, Response

from app.server import (
    ROOT,
    STATIC,
    _static_import_target,
    app_version,
    create_app,
    stamp_static_imports,
    static_asset_token,
)
from app.settings import Settings
from app.translate import Translator

_VERSIONED_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport(?:\s*\(\s*|\s+))(?P<quote>["'])"""
    r"""/static/(?P<name>[^"'?#]+\.js)\?v=(?P<token>[^"'&#]+)(?P=quote)"""
)
_BARE_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport(?:\s*\(\s*|\s+))(?P<quote>["'])"""
    r"""(?P<url>/static/[^"'?#]+\.js)(?P=quote)"""
)


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


def stored_text(path: Path) -> str:
    """File text as stored. read_text() would hide a Windows CRLF checkout."""

    expected = path.read_bytes().decode("utf-8")
    return expected


def import_versions(text: str, static_dir: Path) -> dict[str, str]:
    bare = [match.group("url") for match in _BARE_IMPORT.finditer(text)]
    assert bare == [], bare
    found: dict[str, str] = {}
    for match in _VERSIONED_IMPORT.finditer(text):
        name = match.group("name")
        token = match.group("token")
        assert token == static_asset_token(static_dir / name), name
        found[name] = token
    assert found, "expected at least one /static/*.js import"
    return found


async def assert_revalidates(client: AsyncClient, path: str) -> Response:
    resp = await client.get(path)
    assert resp.status_code == 200, path
    assert resp.headers["cache-control"] == "no-cache", path
    assert resp.headers["referrer-policy"] == "no-referrer", path
    assert resp.headers["etag"], path
    assert resp.headers["last-modified"], path
    etag = resp.headers["etag"]
    cached = await client.get(path, headers={"if-none-match": etag})
    assert cached.status_code == 304, path
    assert cached.content == b"", path
    assert cached.headers["cache-control"] == "no-cache", path
    assert cached.headers["referrer-policy"] == "no-referrer", path
    assert cached.headers["etag"] == etag, path
    mismatch = await client.get(path, headers={"if-none-match": '"not-the-file"'})
    assert mismatch.status_code == 200, path
    assert mismatch.headers["cache-control"] == "no-cache", path
    assert mismatch.headers["referrer-policy"] == "no-referrer", path
    return resp


@pytest.mark.anyio
async def test_pages_and_static_files_revalidate():
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        pages = {
            "/": "禪譯主持",
            "/r/class": "禪譯聽眾",
        }
        scripts = sorted(STATIC.glob("*.js"))
        names = {script.name for script in scripts}
        assert {"room_client.js", "host_caption.js", "recorder_machine.js"} <= names
        for script in scripts:
            pages[f"/static/{script.name}"] = "export "
        for path, marker in pages.items():
            resp = await assert_revalidates(client, path)
            assert marker in resp.text, path
            assert int(resp.headers["content-length"]) == len(resp.content), path
            if path.startswith("/static/") and path.endswith(".js"):
                expected = stored_text(STATIC / path.removeprefix("/static/"))
                assert resp.text == expected


@pytest.mark.anyio
async def test_module_imports_carry_a_content_version():
    app = make_app()
    transport = ASGITransport(app=app)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        host = await assert_revalidates(client, "/")
        assert host.headers["content-type"].startswith("text/html")
        expected = stored_text(STATIC / "host.html")
        assert host.text == stamp_static_imports(expected, STATIC)
        host_versions = import_versions(host.text, STATIC)
        assert set(host_versions) == {
            "recorder_machine.js", "room_client.js", "host_caption.js", "host_glossary.js",
        }

        room = await assert_revalidates(client, "/r/class")
        assert room.headers["content-type"].startswith("text/html")
        expected = stored_text(STATIC / "room.html")
        assert room.text == stamp_static_imports(expected, STATIC)
        room_versions = import_versions(room.text, STATIC)
        assert set(room_versions) == {"room_client.js", "room_prefs.js", "room_view.js"}
        assert room_versions["room_client.js"] == host_versions["room_client.js"]

        for page in ("/static/host.html", "/static/room.html"):
            direct = await assert_revalidates(client, page)
            assert import_versions(direct.text, STATIC) == (
                host_versions if page.endswith("host.html") else room_versions
            )

        versions = dict(host_versions)
        versions.update(room_versions)
        for name, token in versions.items():
            prefix, suffix = token.rsplit("-", 1)
            assert prefix == version, token
            assert len(suffix) == 8 and all(ch in "0123456789abcdef" for ch in suffix)
            url = f"/static/{name}?v={token}"
            served = await assert_revalidates(client, url)
            expected = stored_text(STATIC / name)
            assert served.text == expected
            ranged = await client.get(url, headers={"range": "bytes=0-9"})
            assert ranged.status_code == 206, url
            assert ranged.headers["cache-control"] == "no-cache", url
            assert ranged.headers["referrer-policy"] == "no-referrer", url
            assert ranged.content == served.content[:10]
            ignored = await client.get(f"/static/{name}?v=not-a-real-token")
            assert ignored.status_code == 200
            assert ignored.text == served.text
            assert ignored.headers["etag"] == served.headers["etag"]
            assert ignored.headers["cache-control"] == "no-cache"


@pytest.mark.anyio
async def test_changed_module_changes_its_import_version(monkeypatch, tmp_path):
    import app.server as server_mod

    copied = tmp_path / "static"
    shutil.copytree(server_mod.STATIC, copied)
    client_js = copied / "room_client.js"
    client_js.write_text(
        'import { needsEnglishRetry } from "/static/host_caption.js?v=stale";\n'
        + client_js.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.setattr(server_mod, "STATIC", copied)
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        first = await client.get("/")
        assert first.status_code == 200
        before = import_versions(first.text, copied)
        assert set(before) == {
            "recorder_machine.js", "room_client.js", "host_caption.js", "host_glossary.js",
        }
        nested = await client.get("/static/room_client.js?v=" + before["room_client.js"])
        assert nested.status_code == 200
        assert nested.headers["cache-control"] == "no-cache"
        assert "v=stale" not in nested.text
        assert import_versions(nested.text, copied)["host_caption.js"] == before["host_caption.js"]
        nested_etag = nested.headers["etag"]

        caption = copied / "host_caption.js"
        caption.write_bytes(caption.read_bytes() + b"\n// touched\n")

        second = await client.get("/", headers={"if-none-match": first.headers["etag"]})
        assert second.status_code == 200
        assert second.headers["cache-control"] == "no-cache"
        assert second.headers["etag"] != first.headers["etag"]
        after = import_versions(second.text, copied)
        assert after["host_caption.js"] != before["host_caption.js"]
        assert after["recorder_machine.js"] == before["recorder_machine.js"]
        assert after["room_client.js"] == before["room_client.js"]
        fresh_page = await client.get("/", headers={"if-none-match": second.headers["etag"]})
        assert fresh_page.status_code == 304
        assert fresh_page.headers["etag"] == second.headers["etag"]

        url = "/static/room_client.js?v=" + after["room_client.js"]
        refreshed = await client.get(url)
        assert refreshed.status_code == 200
        assert refreshed.headers["etag"] != nested_etag
        assert import_versions(refreshed.text, copied)["host_caption.js"] == after["host_caption.js"]
        stale = await client.get(url, headers={"if-none-match": nested_etag})
        assert stale.status_code == 200
        renewed = await client.get(url, headers={"if-none-match": refreshed.headers["etag"]})
        assert renewed.status_code == 304
        assert renewed.content == b""
        assert renewed.headers["cache-control"] == "no-cache"
        assert renewed.headers["etag"] == refreshed.headers["etag"]

        caption_url = "/static/host_caption.js?v=" + after["host_caption.js"]
        caption_resp = await assert_revalidates(client, caption_url)
        assert "// touched" in caption_resp.text


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


def test_static_import_target_rejects_nul_and_overlong_names(tmp_path):
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "ok.js").write_bytes(b"export const ok = 1;\n")
    nul_name = "a\x00.js"
    long_name = "b" * 297 + ".js"
    assert len(long_name) == 300
    assert _static_import_target(nul_name, static_dir) is None
    assert _static_import_target(long_name, static_dir) is None
    found = _static_import_target("ok.js", static_dir)
    assert found is not None and found.is_file()
    source = (
        'import "/static/' + nul_name + '";\n'
        + 'import "/static/' + long_name + '";\n'
        + 'import "/static/ok.js";\n'
    )
    stamped = stamp_static_imports(source, static_dir)
    assert 'import "/static/' + nul_name + '";' in stamped
    assert 'import "/static/' + long_name + '";' in stamped
    assert 'import "/static/ok.js?v=' in stamped
    assert stamped.count("?v=") == 1


def test_app_version_rejects_backslash_and_script_markup(monkeypatch, tmp_path):
    import app.server as server_mod

    monkeypatch.setattr(server_mod, "ROOT", tmp_path)
    version = tmp_path / "VERSION"
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "room_client.js").write_bytes(b"export const x = 1;\n")
    page = 'import "/static/room_client.js";\n'

    version.write_bytes("1.0\\\n".encode("utf-8"))
    assert app_version() == "0"
    backslash = stamp_static_imports(page, static_dir)
    assert "\\" not in backslash
    assert "?v=0-" in backslash

    version.write_bytes(b"</script><script>alert(1)</script>\n")
    assert app_version() == "0"
    script = stamp_static_imports(page, static_dir)
    assert "</script>" not in script
    assert "?v=0-" in script

    version.write_bytes(b"1.2.3-rc.1+build\n")
    assert app_version() == "1.2.3-rc.1+build"
    assert "?v=1.2.3-rc.1+build-" in stamp_static_imports(page, static_dir)

    version.write_bytes(b"a" * 33 + b"\n")
    assert app_version() == "0"
    version.write_bytes(b"a" * 32 + b"\n")
    assert app_version() == "a" * 32

    version.write_bytes(b"0.2.1\n")
    assert app_version() == "0.2.1"


@pytest.mark.anyio
async def test_javascript_is_served_as_text_javascript(monkeypatch, tmp_path):
    import app.server as server_mod

    # A Windows registry often makes guess_type() return text/plain for .js.
    monkeypatch.setattr(server_mod, "guess_type", lambda *_args, **_kwargs: ("text/plain", None))
    monkeypatch.setattr(
        "starlette.responses.guess_type",
        lambda *_args, **_kwargs: ("text/plain", None),
    )
    copied = tmp_path / "static"
    shutil.copytree(server_mod.STATIC, copied)
    client_js = copied / "room_client.js"
    client_js.write_bytes(b'import "/static/host_caption.js";\n' + client_js.read_bytes())
    (copied / "extra.mjs").write_bytes(b'import "/static/host_caption.js";\nexport const extra = 1;\n')
    monkeypatch.setattr(server_mod, "STATIC", copied)
    app = make_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1:8780") as client:
        plain = await client.get("/static/host_caption.js")
        assert plain.status_code == 200
        assert plain.headers["content-type"] == "text/javascript; charset=utf-8"
        assert plain.headers["cache-control"] == "no-cache"
        assert b"?v=" not in plain.content
        for name in ("recorder_machine.js", "room_client.js", "extra.mjs"):
            resp = await client.get(f"/static/{name}")
            assert resp.status_code == 200, name
            assert resp.headers["content-type"] == "text/javascript; charset=utf-8", name
            assert resp.headers["cache-control"] == "no-cache", name
        rewritten = await client.get("/static/room_client.js")
        assert 'import "/static/host_caption.js?v=' in rewritten.text
        module = await client.get("/static/extra.mjs")
        assert 'import "/static/host_caption.js?v=' in module.text
        assert module.headers["content-type"] == "text/javascript; charset=utf-8"
