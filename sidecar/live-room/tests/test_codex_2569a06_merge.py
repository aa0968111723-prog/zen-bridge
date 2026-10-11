"""Merge guards for Codex 2569a06 onto the v2.5 line (see MERGE_NOTES.md)."""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_embed_on_is_refused_before_binding_the_port(monkeypatch):
    from app.admin import run
    monkeypatch.setenv("ZEN_EMBED", "1")
    called = []
    monkeypatch.setattr(run, "bind_socket", lambda *a: called.append(a))
    with pytest.raises(SystemExit) as e:
        run.main([])
    assert "Embedding" in str(e.value)
    assert called == []


def test_admin_run_never_builds_an_embed_handler_but_keeps_backup_and_retention():
    src = (ROOT / "app/admin/run.py").read_text(encoding="utf-8")
    assert "embed_handler=None, embed_scan_s=0.0" in src
    assert "backfill_handler" not in src
    assert "backup_every_s=" in src and "retention_every_s=" in src
    assert 'install_file_log("admin")' in src
    assert "live_url_from_env()" in src


def test_start_backend_keeps_login_url_visible_and_embed_off():
    src = (ROOT / "scripts/start_backend.ps1").read_text(encoding="utf-8-sig")
    assert "$env:ZEN_EMBED = '0'" in src
    assert "$style = 'Normal'" in src
    assert "-WindowStyle Hidden" not in src            # D-005
    assert "-WindowStyle $style" in src


def test_setup_runs_pinned_runtime_and_resolves_ollama():
    src = (ROOT / "scripts/setup_local.ps1").read_text(encoding="utf-8-sig")
    assert "fetch_windows_runtime.py" in src
    assert "function Resolve-Ollama" in src            # D-001
    assert "throw (\"ollama pull" in src


def test_bench_is_check_only_and_ps_scripts_have_bom():
    bench = (ROOT / "scripts/bench_local.ps1").read_bytes()
    assert bench.startswith(b"\xef\xbb\xbf")
    assert "if (-not $Check) {" in bench.decode("utf-8-sig")
    for name in ("setup_local.ps1", "start_backend.ps1", "bench_local.ps1"):
        assert (ROOT / "scripts" / name).read_bytes().startswith(b"\xef\xbb\xbf"), name


def test_admin_serves_pinned_mermaid_and_admin_logic(tmp_path):
    from fastapi.testclient import TestClient
    from app.admin.server import create_admin_app
    app = create_admin_app(tmp_path / "z.sqlite3", token_hash_hex="00" * 32, port=8791,
                           identity_path=tmp_path / "i.sqlite3")
    c = TestClient(app, client=("127.0.0.1", 5000), base_url="http://127.0.0.1:8791")
    for name in ("mermaid.min.js", "mermaid-safe.js", "admin_logic.js", "app.js"):
        assert c.get(f"/admin/static/{name}").status_code == 200, name
    assert c.get("/admin/static/vendor%2Fmermaid.min.js").status_code == 404
