"""Desktop shortcut install: non-ASCII TEMP and required-shortcut failures.

Windows end-to-end coverage lives in test_shortcut_windows.py. This module keeps
the checks that run on every OS, including A-14 and A-15.
"""
from __future__ import annotations

import re
import subprocess

import pytest

from scripts.install_runtime import ROOT, install_shortcut

SHORTCUT_ARGS = [
    "powershell",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    "-File",
    ".\\install-shortcut.ps1",
]
FAILURE_MESSAGE = "桌面捷徑未建立，仍可雙擊 start.bat。"
POWERSHELL_MISSING_MESSAGE = "無法執行 powershell.exe。"
LINK_NAMES = ("Breeze Live Room", "Breeze Update", "Breeze Doctor")
BAT_NAMES = ("start.bat", "update.bat", "doctor.bat")


def _runner(returncode):
    calls = []

    def runner(args, cwd=None, **kwargs):
        calls.append((args, cwd, kwargs))
        return subprocess.CompletedProcess(args, returncode)

    return calls, runner


def _shortcut_text() -> str:
    return (ROOT / "install-shortcut.ps1").read_text(encoding="utf-8-sig")


def test_install_shortcut_success_invokes_powershell(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    calls, runner = _runner(0)
    assert install_shortcut(tmp_path, runner=runner) == 0
    assert len(calls) == 1
    args, cwd, kwargs = calls[0]
    assert args == SHORTCUT_ARGS
    assert args[0] == "powershell"
    assert args[args.index("-File") + 1] == ".\\install-shortcut.ps1"
    assert cwd == tmp_path
    assert kwargs == {}
    assert FAILURE_MESSAGE not in capsys.readouterr().out


def test_install_shortcut_failure_is_fatal_when_required(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")
    calls, runner = _runner(3)
    assert install_shortcut(tmp_path, runner=runner) != 0
    assert calls[0][0] == SHORTCUT_ARGS
    assert calls[0][1] == tmp_path
    assert FAILURE_MESSAGE in capsys.readouterr().out


def test_install_shortcut_failure_without_require_is_nonfatal(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    calls, runner = _runner(1)
    assert install_shortcut(tmp_path, runner=runner) == 0
    assert calls[0][0] == SHORTCUT_ARGS
    assert calls[0][1] == tmp_path
    assert FAILURE_MESSAGE in capsys.readouterr().out


def test_install_shortcut_skip_does_not_call_runner(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("BREEZE_SKIP_SHORTCUT", "1")
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")

    def runner(*args, **kwargs):
        raise AssertionError("runner should not be called when BREEZE_SKIP_SHORTCUT=1")

    assert install_shortcut(tmp_path, runner=runner) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(FileNotFoundError(2, "powershell"), id="file-not-found"),
        pytest.param(OSError("powershell"), id="os-error"),
    ],
)
def test_install_shortcut_handles_missing_powershell(monkeypatch, tmp_path, capsys, raised):
    """A-14: a missing powershell.exe does not raise; only BREEZE_REQUIRE_SHORTCUT=1 is fatal."""
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)

    def runner(*args, **kwargs):
        raise raised

    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    assert install_shortcut(tmp_path, runner=runner) == 0
    captured = capsys.readouterr()
    assert POWERSHELL_MISSING_MESSAGE in captured.out
    assert FAILURE_MESSAGE in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err

    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")
    assert install_shortcut(tmp_path, runner=runner) == 1
    captured = capsys.readouterr()
    assert POWERSHELL_MISSING_MESSAGE in captured.out
    assert FAILURE_MESSAGE in captured.out
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("value", "fatal"),
    [
        ("1", True),
        ("true", False),
        ("0", False),
        ("", False),
    ],
)
def test_require_shortcut_values(monkeypatch, tmp_path, capsys, value, fatal):
    """A-15: only BREEZE_REQUIRE_SHORTCUT=1 turns a failed shortcut into a non-zero install."""
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", value)
    calls, runner = _runner(1)
    code = install_shortcut(tmp_path, runner=runner)
    assert calls[0][0] == SHORTCUT_ARGS
    assert calls[0][1] == tmp_path
    assert FAILURE_MESSAGE in capsys.readouterr().out
    if fatal:
        assert code != 0
    else:
        assert code == 0


def test_main_delegates_shortcut_install():
    source = (ROOT / "scripts" / "install_runtime.py").read_text(encoding="utf-8")
    assert "def install_shortcut(root, runner=subprocess.run)" in source
    assert "install_shortcut(ROOT)" in source


def test_install_shortcut_script_has_utf8_bom():
    raw = (ROOT / "install-shortcut.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "install-shortcut.ps1 must start with a UTF-8 BOM"


def test_read_shortcuts_script_has_utf8_bom():
    """A-18: scripts/ci/read_shortcuts.ps1 starts with a UTF-8 BOM."""
    raw = (ROOT / "scripts" / "ci" / "read_shortcuts.ps1").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "read_shortcuts.ps1 must start with a UTF-8 BOM"
    text = raw.decode("utf-8-sig")
    assert text.isascii()
    assert "Shell.Application" in text
    assert "WScript.Shell" in text
    assert "wscript_target" in text
    assert "NoVerify" in text
    assert "$Out" in text
    assert "$InstallDir" in text
    assert "$DesktopPath" in text
    assert "PSScriptRoot" in text
    assert "UTF8Encoding $false" in text
    assert "exit 1" in text
    for name in (*LINK_NAMES, *BAT_NAMES):
        assert name in text
    for snippet in (
        "Shortcut not found:",
        "Shortcut target mismatch:",
        "Shortcut directory mismatch:",
        "Shortcut arguments are not empty:",
    ):
        assert snippet in text


def test_desktop_path_param_defaults_to_known_folder_when_empty():
    text = _shortcut_text()
    assert re.search(r"\[string\]\s*\$DesktopPath", text)
    assert text.count("GetFolderPath('Desktop')") == 1
    empty_at = text.index("IsNullOrEmpty($DesktopPath)")
    folder_at = text.index("GetFolderPath('Desktop')", empty_at)
    else_at = text.index("else", folder_at)
    given_at = text.index("$DesktopPath", else_at)
    assert empty_at < folder_at < else_at < given_at


def test_non_ascii_temp_guard_runs_before_add_type_and_temp_is_restored():
    text = _shortcut_text()
    add_at = text.index("Add-Type")
    before = text[:add_at]
    assert "Test-BreezeNonAsciiText" in before
    assert "$env:TEMP" in before
    assert "$env:TMP" in before
    here_end = text.index("'@", add_at)
    tail = text[here_end:]
    finally_at = tail.index("finally")
    restored = tail[finally_at:]
    assert re.search(r"\$env:TEMP\s*=", restored)
    assert re.search(r"\$env:TMP\s*=", restored)


def test_ascii_temp_candidates_use_programdata_public_and_systemdrive():
    text = _shortcut_text()
    for token in ("$env:ProgramData", "$env:PUBLIC", "$env:SystemDrive", r"BreezeLiveRoom\tmp", "BreezeLiveRoomTmp"):
        assert token in text


def test_ascii_fallback_is_announced_and_cleaned_up_after_temp_restore():
    text = _shortcut_text()
    assert "Using ASCII TEMP for shortcut helper:" in text
    here_end = text.index("'@", text.index("Add-Type"))
    restored = text[here_end:]
    finally_at = restored.index("finally")
    tail = restored[finally_at:]
    cleanup_at = tail.index("Remove-BreezeCreatedDirectory")
    assert tail.index("$env:TEMP") < cleanup_at
    assert tail.index("$env:TMP") < cleanup_at
    assert "Get-BreezeTopmostMissingAncestor" in text


def test_checkonly_exit_codes_for_missing_and_wrong_target():
    text = _shortcut_text()
    missing = text.index("Shortcut not found:")
    wrong = text.index("Shortcut target or directory is incorrect")
    assert text.index("$CheckOnly") < missing
    assert "exit 2" in text[missing:missing + 180]
    assert "exit 1" in text[wrong:wrong + 220]


def test_shortcut_names_and_targets():
    text = _shortcut_text()
    for name in (*LINK_NAMES, *BAT_NAMES):
        assert name in text


def test_shortcut_script_does_not_use_wscript_shell():
    text = _shortcut_text()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert "WScript.Shell" not in code
    assert "IBreezeShellLinkW" in text
