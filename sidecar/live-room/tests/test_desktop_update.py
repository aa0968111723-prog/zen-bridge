import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.desktop_update import (
    cleanup_downloads,
    SETUP_NAME,
    assert_https,
    parse_sha256_sidecar,
    parse_version,
    plan,
    stream_verified,
)

def test_cleanup_removes_only_old_app_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr('app.desktop_update.tempfile.gettempdir', lambda: str(tmp_path))
    for name, old in [('BreezeUpdate-old', True), ('BreezeUpdate-recent', False)]:
        folder = tmp_path / name
        folder.mkdir()
        (folder / SETUP_NAME).write_bytes(b'installer')
        (folder / 'breeze-update.json').write_text(json.dumps({'repository': 'aa0968111723-prog/zen-bridge', 'filename': SETUP_NAME, 'created_at': time.time() - (7200 if old else 0)}))
    unrelated = tmp_path / 'BreezeUpdate-unrelated'
    unrelated.mkdir()
    (unrelated / 'notes.txt').write_text('user file')
    assert cleanup_downloads() == 1
    assert not (tmp_path / 'BreezeUpdate-old').exists()
    assert (tmp_path / 'BreezeUpdate-recent' / SETUP_NAME).exists()
    assert (unrelated / 'notes.txt').read_text() == 'user file'


def release(tag="v0.4.0", **extra):
    setup = {
        "name": SETUP_NAME,
        "browser_download_url": "https://github.com/aa0968111723-prog/zen-bridge/releases/download/v0.4.0/" + SETUP_NAME,
        "size": 1200,
    }
    digest = {
        "name": SETUP_NAME + ".sha256",
        "browser_download_url": "https://github.com/aa0968111723-prog/zen-bridge/releases/download/v0.4.0/" + SETUP_NAME + ".sha256",
        "size": 80,
    }
    body = {"tag_name": tag, "draft": False, "prerelease": False, "assets": [setup, digest]}
    body.update(extra)
    return body


def test_plan_reports_current_and_refuses_downgrade():
    assert plan(release("v0.3.0"), "0.3.0")["status"] == "current"
    assert plan(release("v0.2.0"), "0.3.0")["status"] == "current"


def test_plan_requires_both_official_assets():
    missing = release()
    missing["assets"] = [missing["assets"][0]]
    assert plan(missing, "0.3.0")["status"] == "unavailable"
    chosen = plan(release(), "0.3.0")
    assert chosen["status"] == "update" and chosen["version"] == "0.4.0"


@pytest.mark.parametrize("value", ["1.2", "v1.2.3-beta", "latest", ""])
def test_version_must_be_stable(value):
    with pytest.raises(ValueError):
        parse_version(value)


def test_plan_rejects_non_https_and_foreign_hosts():
    bad = release()
    bad["assets"][0]["browser_download_url"] = "http://github.com/setup.exe"
    with pytest.raises(ValueError, match="HTTPS"):
        plan(bad, "0.3.0")
    foreign = release()
    foreign["assets"][0]["browser_download_url"] = "https://evil.example/setup.exe"
    with pytest.raises(ValueError, match="允許"):
        plan(foreign, "0.3.0")
    with pytest.raises(ValueError):
        assert_https("https://example.com/file")


def test_update_cannot_use_another_repository_or_release_tag():
    for replacement in ('another-owner/another-repo/releases/download/v0.4.0', 'aa0968111723-prog/zen-bridge/releases/download/v9.9.9'):
        bad = release()
        bad['assets'][0]['browser_download_url'] = 'https://github.com/' + replacement + '/' + SETUP_NAME
        with pytest.raises(ValueError):
            plan(bad, '0.3.0')


def test_sidecar_binds_the_installer_name():
    digest = "a" * 64
    assert parse_sha256_sidecar(digest + "  " + SETUP_NAME) == digest
    with pytest.raises(ValueError, match="檔名"):
        parse_sha256_sidecar(digest + "  other.exe")
    with pytest.raises(ValueError):
        parse_sha256_sidecar("zz")


def test_stream_publishes_only_after_checksum(tmp_path):
    body = b"official-setup"
    digest = hashlib.sha256(body).hexdigest()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/setup"
    dest = tmp_path / SETUP_NAME
    try:
        with pytest.raises(ValueError, match="SHA256"):
            stream_verified(url, dest, "b" * 64, limit=len(body), urlopen=lambda request, timeout: __import__("urllib.request", fromlist=["urlopen"]).urlopen(request, timeout=timeout))
        assert not dest.exists() and not (tmp_path / (SETUP_NAME + ".part")).exists()
        saved = stream_verified(url, dest, digest, limit=len(body), urlopen=lambda request, timeout: __import__("urllib.request", fromlist=["urlopen"]).urlopen(request, timeout=timeout))
        assert saved.read_bytes() == body
    finally:
        server.shutdown()


def test_prerelease_is_not_installed():
    assert plan(release(prerelease=True), "0.3.0")["status"] == "unavailable"


def compact_release():
    from app.desktop_update import UPDATE_NAME, RUNTIME_NAME
    body = release()
    prefix = 'https://github.com/aa0968111723-prog/zen-bridge/releases/download/v0.4.0/'
    body['assets'] += [{'name': name, 'browser_download_url': prefix + name, 'size': 100} for name in (UPDATE_NAME, UPDATE_NAME + '.sha256', RUNTIME_NAME)]
    return body


def test_compact_requires_matching_healthy_runtime(monkeypatch, tmp_path):
    from app.desktop_update import select_compatible, UPDATE_NAME
    chosen = plan(compact_release(), '0.3.0')
    monkeypatch.setattr('app.desktop_update.fetch_text', lambda url, **kwargs: json.dumps(compact_descriptor()))
    monkeypatch.setattr('scripts.update_runtime.compatible', lambda root, expected: False)
    assert select_compatible(chosen, tmp_path)['mode'] == 'full'
    monkeypatch.setattr('scripts.update_runtime.compatible', lambda root, expected: expected['fingerprint'] == 'a' * 64)
    compact = select_compatible(chosen, tmp_path)
    assert compact['mode'] == 'compact' and compact['source_name'] == UPDATE_NAME
    assert compact['bytes'] == 100 and compact['setup_url'].endswith(UPDATE_NAME)


def test_compact_metadata_cannot_point_outside_official_tag():
    from app.desktop_update import RUNTIME_NAME
    body = compact_release()
    next(a for a in body['assets'] if a['name'] == RUNTIME_NAME)['browser_download_url'] = 'https://github.com/another/repo/releases/download/v0.4.0/' + RUNTIME_NAME
    with pytest.raises(ValueError): plan(body, '0.3.0')


@pytest.mark.parametrize('descriptor', [{'schema': 2, 'fingerprint': 'a' * 64}, {'schema': 1, 'fingerprint': None}, {}])
def test_bad_compact_metadata_falls_back_to_full(monkeypatch, tmp_path, descriptor):
    from app.desktop_update import select_compatible
    monkeypatch.setattr('app.desktop_update.fetch_text', lambda url, **kwargs: json.dumps(descriptor))
    assert select_compatible(plan(compact_release(), '0.3.0'), tmp_path)['mode'] == 'full'


def compact_descriptor():
    return {'schema': 1, 'gate_version': 1, 'fingerprint': 'a' * 64, 'tools': {name: {'size': 4, 'sha256': 'b' * 64} for name in ('ffmpeg.exe', 'whisper-cli.exe')}}


def test_check_is_cheap_but_download_revalidates(monkeypatch, tmp_path):
    from app.desktop_update import select_compatible
    monkeypatch.setattr('app.desktop_update.fetch_text', lambda url, **kwargs: json.dumps(compact_descriptor()))
    monkeypatch.setattr('scripts.update_runtime.probably_compatible', lambda root, value: True)
    def deep(root, value): raise AssertionError('deep hashing should not happen on --check')
    monkeypatch.setattr('scripts.update_runtime.compatible', deep)
    assert select_compatible(plan(compact_release(), '0.3.0'), tmp_path, verify=False)['mode'] == 'compact'
    monkeypatch.setattr('scripts.update_runtime.compatible', lambda root, value: False)
    assert select_compatible(plan(compact_release(), '0.3.0'), tmp_path, verify=True)['mode'] == 'full'
