"""Static checks for the Windows PowerShell 5.1 scripts (DEFECTS.md D-001, D-003).

Windows PowerShell 5.1 reads a .ps1 *without* a BOM in the system ANSI code page
(cp1252 / cp950), so UTF-8 Chinese text breaks string literals and the whole script fails to
parse. Every script that contains non-ASCII bytes must therefore start with a UTF-8 BOM.
"""
import re
import subprocess
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = sorted(p for p in ROOT.rglob("*.ps1") if ".venv" not in p.parts and "node_modules" not in p.parts)
BOM = b"\xef\xbb\xbf"


def test_scripts_found():
    names = {p.name for p in SCRIPTS}
    assert {"setup_local.ps1", "start_backend.ps1", "bench_local.ps1", "collect_env.ps1"} <= names


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: str(p.relative_to(ROOT)))
def test_non_ascii_ps1_has_utf8_bom(path):
    data = path.read_bytes()
    body = data[3:] if data.startswith(BOM) else data
    body.decode("utf-8")                     # must be valid UTF-8 either way
    if any(b > 0x7F for b in body):
        assert data.startswith(BOM), f"{path.name}: non-ASCII without UTF-8 BOM breaks Windows PowerShell 5.1"


def _setup_text():
    return (ROOT / "scripts" / "setup_local.ps1").read_text(encoding="utf-8-sig")


def test_setup_local_finds_ollama_outside_path():
    text = _setup_text()
    assert "Resolve-Ollama" in text
    assert r"Programs\Ollama\ollama.exe" in text
    assert "OLLAMA_EXE" in text
    # the pulls go through the resolved path, never the bare command name
    assert not re.search(r"^\s*&\s*ollama\s+pull", text, re.M)


def test_setup_local_checks_every_pip_exit_code():
    lines = _setup_text().splitlines()
    for i, line in enumerate(lines):
        if "pip install" in line and line.lstrip().startswith("&"):
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            assert "$LASTEXITCODE" in nxt, f"line {i + 1}: pip install without exit-code check"


@pytest.mark.skipif(not shutil.which("pwsh"), reason="pwsh not installed")
@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_ps1_parses(path):
    cmd = ("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
           f"'{path}',[ref]$t,[ref]$e);$e.Count")
    out = subprocess.run(["pwsh", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "0", out.stdout + out.stderr


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_comment_help_not_glued_to_requires(path):
    """D-004: in PS 5.1 a '<#' help block directly after '#Requires' is not picked up by Get-Help."""
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    for i, line in enumerate(lines[:-1]):
        if line.lstrip().lower().startswith("#requires") and lines[i + 1].lstrip().startswith("<#"):
            pytest.fail(f"{path.name}:{i + 2} help block glued to #Requires; add a blank line")


def test_admin_window_never_hidden_without_login_path():
    """D-005: never start the admin console hidden unless the login URL opens in the browser and
    the once-only CLI token was already shown on an earlier start."""
    text = (ROOT / "scripts" / "start_backend.ps1").read_text(encoding="utf-8-sig")
    starts = [ln for ln in text.splitlines() if "Start-Process" in ln and "app.admin.run" in ln]
    assert starts, "admin start line missing"
    for ln in starts:
        assert "-WindowStyle Hidden" not in ln, "hard-coded hidden admin window loses the login URL/token"
    assert "HideAdminWindow" in text and "admin.token" in text and "-not $OpenAdmin" in text


def test_start_backend_thread_budget():
    """QA 效能長 P1-10: the launcher passes ASR 6 + LLM 2 by default and says priority != Ollama."""
    text = (ROOT / "scripts" / "start_backend.ps1").read_text(encoding="utf-8-sig")
    assert re.search(r"\[int\]\$Threads\s*=\s*2\b", text)
    assert "NOT the Ollama runner" in text
    doc = (ROOT / "app" / "runtime_tuning.py").read_text(encoding="utf-8")
    assert "does NOT throttle Ollama" in doc
