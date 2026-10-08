"""Windows end-to-end desktop shortcut tests (spec section A).

Skipped unless sys.platform == "win32". A-09 and A-10 also require the GitHub
Actions runner (or BREEZE_CI_REAL_DESKTOP=1). A-14 and A-15 run on every OS and
live in test_shortcut.py. A-17 is a workflow env change and is not a pytest.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from scripts.install_runtime import ROOT, install_shortcut

WIN = pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell 5.1")
CI_REAL = pytest.mark.skipif(
    not (
        sys.platform == "win32"
        and (os.getenv("GITHUB_ACTIONS") == "true" or os.getenv("BREEZE_CI_REAL_DESKTOP") == "1")
    ),
    reason="only on CI runner",
)
LINKS = ("Breeze Live Room", "Breeze Update", "Breeze Doctor")
TARGETS = ("start.bat", "update.bat", "doctor.bat")
LINK_FILES = tuple(sorted(f"{name}.lnk" for name in LINKS))
READ_SHORTCUTS = ROOT / "scripts" / "ci" / "read_shortcuts.ps1"
FAILURE_MESSAGE = "桌面捷徑未建立，仍可雙擊 start.bat。"
_COMPILE_SUFFIXES = {".cs", ".dll", ".cmdline", ".tmp"}
_SCRIPT_MARKERS = (
    "Using ASCII TEMP for shortcut helper:",
    "No ASCII-only writable directory is available for shortcut compilation.",
    "Shortcut not found:",
    "Shortcut target or directory is incorrect:",
)
_DESKTOP_QUERY = (
    "param([Parameter(Mandatory = $true)][string]$Out)\n"
    "$desktop = [Environment]::GetFolderPath('Desktop')\n"
    "$utf8 = New-Object System.Text.UTF8Encoding $false\n"
    "[System.IO.File]::WriteAllText($Out, $desktop, $utf8)\n"
)

pytestmark = WIN


def _powershell_pipe_text(value: str) -> str:
    # powershell.exe 5.1 writes UTF-16 LE into a pipe. Decoded as UTF-8, ASCII
    # text keeps NUL bytes between characters; drop them so assertions can read it.
    # Chinese in that stream is not recoverable. Assert ASCII fragments only.
    if value and "\x00" in value:
        return value.replace("\x00", "")
    return value


def _detail(proc: subprocess.CompletedProcess) -> str:
    return f"returncode={proc.returncode}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"


def _env_without(env: dict, *names: str) -> dict:
    blocked = {name.upper() for name in names}
    return {key: value for key, value in env.items() if key.upper() not in blocked}


def _child_env(base: dict, **overrides: object) -> dict:
    env = dict(base)
    for name, value in overrides.items():
        env = _env_without(env, name)
        env[name] = str(value)
    return env


def _run_powershell(command, *, cwd, env=None, timeout=180) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [str(part) for part in command],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    proc.stdout = _powershell_pipe_text(proc.stdout or "")
    proc.stderr = _powershell_pipe_text(proc.stderr or "")
    return proc


def run_ps(script_dir, *args, env=None, timeout=180, executable="powershell.exe"):
    """Run install-shortcut.ps1 with powershell.exe 5.1. Caller passes -DesktopPath."""
    return _run_powershell(
        [
            executable,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            Path(script_dir) / "install-shortcut.ps1",
            *args,
        ],
        cwd=script_dir,
        env=env,
        timeout=timeout,
    )


def _run_read_shortcuts(*args, env=None, timeout=180) -> subprocess.CompletedProcess:
    return _run_powershell(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            READ_SHORTCUTS,
            *args,
        ],
        cwd=ROOT,
        env=env,
        timeout=timeout,
    )


def _as_desktop(paths) -> Path:
    if isinstance(paths, (str, Path)):
        return Path(paths)
    items = [Path(item) for item in paths]
    if not items:
        raise AssertionError("read_links() requires a desktop directory or shortcut paths")
    parent = items[0].parent
    if any(item.parent != parent for item in items):
        raise AssertionError("read_links() paths must live in one directory")
    return parent


def read_links(paths) -> dict:
    """Read the three shortcuts via read_shortcuts.ps1 -NoVerify.

    `paths` is the desktop directory, or shortcut paths in that directory.
    JSON is loaded with utf-8-sig so Chinese paths are not taken from stdout.
    """
    desktop = _as_desktop(paths)
    fd, raw_name = tempfile.mkstemp(prefix="breeze-shortcuts-", suffix=".json")
    os.close(fd)
    out = Path(raw_name)
    try:
        proc = _run_read_shortcuts("-Out", str(out), "-DesktopPath", str(desktop), "-NoVerify")
        if proc.returncode != 0 or not out.is_file():
            raise AssertionError(_detail(proc))
        try:
            with out.open(encoding="utf-8-sig") as handle:
                rows = json.load(handle)
        except json.JSONDecodeError as exc:
            body = out.read_text(encoding="utf-8-sig", errors="replace")
            raise AssertionError(f"{exc}\n{body}\n{_detail(proc)}") from exc
    finally:
        out.unlink(missing_ok=True)
    if isinstance(rows, dict):
        rows = [rows]
    found = {}
    for row in rows:
        path_text = str(row.get("path", ""))
        for name in LINKS:
            if path_text.endswith(name + ".lnk"):
                found[name] = row
                break
    missing = [name for name in LINKS if name not in found]
    if missing:
        raise AssertionError(f"read_links did not return {missing}; rows={rows!r}")
    return found


def _assert_links(desktop: Path, detail: str) -> None:
    found = sorted(path.name for path in desktop.glob("*.lnk"))
    assert found == list(LINK_FILES), detail


def _tree(path: Path) -> list[str]:
    if not path.exists():
        return []
    return sorted(item.relative_to(path).as_posix() for item in path.rglob("*"))


# shell32 itself writes Microsoft\Windows\Caches\*.db under the overridden
# %ProgramData% when a shortcut is created. That cache is not this script.
_SHELL32_CACHE = "Microsoft/Windows/Caches"
_SHELL32_CACHE_DIRS = {"Microsoft", "Microsoft/Windows", _SHELL32_CACHE}
_BREEZE_TEMP_DIR_NAMES = {"BreezeLiveRoom", "BreezeLiveRoomTmp"}


def _assert_rejected_dir_has_no_breeze_temp(path: Path, before: list[str], detail: str) -> None:
    """A rejected non-ASCII dir must not keep a Breeze temp directory.

    The only permitted tree change is shell32's Microsoft\\Windows\\Caches
    files (and the ancestor directories that hold them). Nothing named
    BreezeLiveRoom or BreezeLiveRoomTmp may appear anywhere under ``path``.
    """
    after = _tree(path)
    for rel in after:
        overlap = set(rel.split("/")) & _BREEZE_TEMP_DIR_NAMES
        assert not overlap, f"{rel} names {sorted(overlap)}\n{detail}"
    before_set = set(before)
    after_set = set(after)

    def _is_shell32_cache(rel: str) -> bool:
        return rel in _SHELL32_CACHE_DIRS or rel.startswith(_SHELL32_CACHE + "/")

    for rel in after:
        if rel in before_set:
            continue
        assert _is_shell32_cache(rel), f"added outside Microsoft\\Windows\\Caches: {rel}\n{detail}"
    for rel in before:
        if rel in after_set:
            continue
        assert _is_shell32_cache(rel), f"removed outside Microsoft\\Windows\\Caches: {rel}\n{detail}"


def _compile_artifacts(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return [
        item
        for item in path.rglob("*")
        if item.is_file() and item.suffix.lower() in _COMPILE_SUFFIXES
    ]


def _assert_used_ascii_temp(proc: subprocess.CompletedProcess, fragment: str) -> None:
    lines = [line for line in proc.stdout.splitlines() if "Using ASCII TEMP for shortcut helper:" in line]
    assert lines, _detail(proc)
    assert any(fragment in line for line in lines), _detail(proc)


def _script_produced_output(proc: subprocess.CompletedProcess) -> bool:
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    return any(marker in combined for marker in _SCRIPT_MARKERS)


def _copy_install_tree(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src / "install-shortcut.ps1", dest / "install-shortcut.ps1")
    for name in TARGETS:
        shutil.copyfile(src / name, dest / name)


def _query_known_desktop(tmp_path: Path) -> Path:
    script = tmp_path / "query-desktop.ps1"
    out = tmp_path / "known-desktop.txt"
    script.write_bytes(b"\xef\xbb\xbf" + _DESKTOP_QUERY.encode("ascii"))
    proc = _run_powershell(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            "-Out",
            out,
        ],
        cwd=tmp_path,
        timeout=60,
    )
    assert proc.returncode == 0, _detail(proc)
    text = out.read_text(encoding="utf-8-sig").strip().strip("\ufeff")
    assert text, "Desktop folder path was empty"
    return Path(text)


@pytest.fixture
def install_dir(tmp_path):
    directory = tmp_path / "Breeze test 測試"
    directory.mkdir()
    shutil.copyfile(ROOT / "install-shortcut.ps1", directory / "install-shortcut.ps1")
    for name in TARGETS:
        (directory / name).write_bytes(b"@echo off\r\n")
    return directory


@pytest.fixture
def desktop(tmp_path):
    directory = tmp_path / "桌面 測試"
    directory.mkdir()
    return directory


@pytest.fixture
def non_ascii_temp_env(tmp_path):
    """TEMP/TMP under a non-ASCII directory, matching the previous Windows fixture."""
    temp_dir = tmp_path / "tmp 測試"
    temp_dir.mkdir()
    env = _env_without(os.environ.copy(), "TEMP", "TMP")
    env["TEMP"] = str(temp_dir)
    env["TMP"] = str(temp_dir)
    return env


def test_checkonly_exit2_when_one_link_missing(install_dir, desktop, non_ascii_temp_env):
    """A-01: -CheckOnly exits 2 when one shortcut is missing; the other two remain.

    Creation uses a non-ASCII TEMP, which is the case the previous end-to-end
    test covered before CheckOnly was asserted.
    """
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    detail = _detail(created)
    assert created.returncode == 0, detail
    _assert_links(desktop, detail)
    checked = run_ps(
        install_dir,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    assert checked.returncode == 0, _detail(checked)
    removed = desktop / "Breeze Update.lnk"
    removed.unlink()
    missing = run_ps(
        install_dir,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    detail = _detail(missing)
    assert missing.returncode == 2, detail
    # PS 5.1 stdout is the OEM code page, so the Chinese desktop directory is garbled.
    assert "Shortcut not found:" in missing.stdout, detail
    assert "Breeze Update.lnk" in missing.stdout, detail
    assert (desktop / "Breeze Live Room.lnk").is_file(), detail
    assert (desktop / "Breeze Doctor.lnk").is_file(), detail
    assert not removed.exists(), detail


def test_checkonly_exit1_when_target_points_to_old_install(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-02: -CheckOnly from a copied install dir reports a stale target."""
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert created.returncode == 0, _detail(created)
    relocated = tmp_path / "新位置 測試2"
    _copy_install_tree(install_dir, relocated)
    checked = run_ps(
        relocated,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    detail = _detail(checked)
    assert checked.returncode == 1, detail
    assert "Shortcut target or directory is incorrect:" in checked.stdout, detail
    assert "Breeze Live Room.lnk" in checked.stdout, detail


def test_create_twice_is_idempotent(install_dir, desktop, non_ascii_temp_env):
    """A-03: creating shortcuts twice leaves exactly the three link files."""
    first = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    second = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert first.returncode == 0, _detail(first)
    detail = _detail(second)
    assert second.returncode == 0, detail
    _assert_links(desktop, detail)
    checked = run_ps(
        install_dir,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    assert checked.returncode == 0, _detail(checked)


def test_rerun_from_new_location_overwrites_stale_target(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-04: rerunning from a new directory overwrites shortcut targets."""
    first = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert first.returncode == 0, _detail(first)
    relocated = tmp_path / "新位置 測試2"
    _copy_install_tree(install_dir, relocated)
    second = run_ps(relocated, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert second.returncode == 0, _detail(second)
    checked = run_ps(
        relocated,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    assert checked.returncode == 0, _detail(checked)
    links = read_links(desktop)
    prefix = str(relocated)
    for name in LINKS:
        assert links[name]["target"].startswith(prefix), links[name]


def test_fallback_public_when_programdata_unusable(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-05: a non-ASCII ProgramData falls through to an ASCII PUBLIC temp."""
    program_data = tmp_path / "資料 測試"
    public = tmp_path / "pub_ascii"
    program_data.mkdir()
    public.mkdir()
    before = _tree(program_data)
    env = _child_env(non_ascii_temp_env, ProgramData=program_data, PUBLIC=public)
    proc = run_ps(install_dir, "-DesktopPath", str(desktop), env=env)
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    _assert_links(desktop, detail)
    _assert_used_ascii_temp(proc, r"pub_ascii\BreezeLiveRoom\tmp")
    _assert_rejected_dir_has_no_breeze_temp(program_data, before, detail)
    assert not (program_data / "BreezeLiveRoom").exists(), detail
    assert not (public / "BreezeLiveRoom").exists(), detail


def test_fallback_systemdrive_when_programdata_and_public_unusable(
    install_dir, desktop, non_ascii_temp_env, tmp_path
):
    """A-06: non-ASCII ProgramData and PUBLIC fall through to SystemDrive.

    SystemDrive is overridden only in the child. PATH and SystemRoot stay
    inherited, and powershell.exe is started with -NoProfile, so pointing
    SystemDrive at a directory (not a drive letter) should not stop the host.
    If it fails to start, skip instead of pretending the fallback worked.
    """
    program_data = tmp_path / "資料 測試"
    public = tmp_path / "公共資料"
    system_drive = tmp_path / "sysdrv"
    program_data.mkdir()
    public.mkdir()
    system_drive.mkdir()
    program_before = _tree(program_data)
    public_before = _tree(public)
    env = _child_env(
        non_ascii_temp_env,
        ProgramData=program_data,
        PUBLIC=public,
        SystemDrive=system_drive,
    )
    proc = run_ps(install_dir, "-DesktopPath", str(desktop), env=env)
    if proc.returncode != 0 and not _script_produced_output(proc) and not (proc.stdout or "").strip():
        pytest.skip(
            "powershell.exe failed to start because SystemDrive was overridden: "
            + (proc.stderr or "").strip()[:500]
        )
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    _assert_links(desktop, detail)
    _assert_used_ascii_temp(proc, r"sysdrv\BreezeLiveRoomTmp")
    assert not (system_drive / "BreezeLiveRoomTmp").exists(), detail
    _assert_rejected_dir_has_no_breeze_temp(program_data, program_before, detail)
    _assert_rejected_dir_has_no_breeze_temp(public, public_before, detail)
    assert not (program_data / "BreezeLiveRoom").exists(), detail
    assert not (public / "BreezeLiveRoom").exists(), detail


def test_all_fallbacks_unusable_fails_clearly(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-07: no ASCII temp candidate fails before any shortcut is created."""
    program_data = tmp_path / "程式資料"
    public = tmp_path / "公共資料"
    system_drive = tmp_path / "系統碟"
    for path in (program_data, public, system_drive):
        path.mkdir()
    env = _child_env(
        non_ascii_temp_env,
        ProgramData=program_data,
        PUBLIC=public,
        SystemDrive=system_drive,
    )
    proc = run_ps(install_dir, "-DesktopPath", str(desktop), env=env)
    detail = _detail(proc)
    assert proc.returncode != 0, detail
    assert "No ASCII-only writable directory is available for shortcut compilation." in (
        proc.stdout + proc.stderr
    ), detail
    assert list(desktop.glob("*.lnk")) == [], detail


def test_temp_restored_and_compile_dir_cleaned(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-08: the script-created temp dir is removed; a pre-existing marker is kept."""
    program_data = tmp_path / "資料 測試"
    public = tmp_path / "pub_ascii"
    program_data.mkdir()
    public.mkdir()
    env = _child_env(non_ascii_temp_env, ProgramData=program_data, PUBLIC=public)
    before = _tree(public)
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=env)
    detail = _detail(created)
    assert created.returncode == 0, detail
    _assert_links(desktop, detail)
    _assert_used_ascii_temp(created, r"pub_ascii\BreezeLiveRoom\tmp")
    assert _compile_artifacts(public / "BreezeLiveRoom" / "tmp") == [], f"before={before}\n{detail}"
    assert not (public / "BreezeLiveRoom").exists(), f"before={before}\n{detail}"
    assert _tree(public) == before, detail

    target = public / "BreezeLiveRoom" / "tmp"
    target.mkdir(parents=True)
    marker = target / "marker.txt"
    marker.write_text("keep", encoding="utf-8")
    preexisting = _tree(public)
    again = run_ps(install_dir, "-DesktopPath", str(desktop), env=env)
    detail = _detail(again)
    assert again.returncode == 0, detail
    _assert_links(desktop, detail)
    _assert_used_ascii_temp(again, r"pub_ascii\BreezeLiveRoom\tmp")
    assert marker.is_file(), detail
    assert marker.read_text(encoding="utf-8") == "keep", detail
    assert _compile_artifacts(target) == [], f"before={preexisting}\n{detail}"
    assert _tree(public) == preexisting, detail


@CI_REAL
def test_default_desktop_without_desktoppath(install_dir, tmp_path):
    """A-09: omitting -DesktopPath writes the three shortcuts on the real desktop."""
    real_desktop = _query_known_desktop(tmp_path)
    backup = tmp_path / "desktop-lnk-backup"
    backup.mkdir()
    backed: list[str] = []
    started = False
    try:
        for name in LINKS:
            source = real_desktop / f"{name}.lnk"
            if not source.is_file():
                continue
            shutil.copy2(source, backup / source.name)
            source.unlink()
            backed.append(name)
        started = True
        created = run_ps(install_dir)
        detail = _detail(created)
        assert created.returncode == 0, detail
        for name in LINKS:
            assert (real_desktop / f"{name}.lnk").is_file(), detail
        checked = run_ps(install_dir, "-CheckOnly")
        assert checked.returncode == 0, _detail(checked)
    finally:
        for name in LINKS:
            link = real_desktop / f"{name}.lnk"
            if name in backed:
                if link.is_file():
                    link.unlink()
                shutil.copy2(backup / f"{name}.lnk", link)
            elif started and link.is_file():
                link.unlink()


@CI_REAL
def test_install_root_chinese_space_on_c_drive(tmp_path):
    """A-10: shortcuts keep a Chinese install path with a space on C:\\."""
    root = Path(r"C:\社課 測試")
    if root.exists():
        pytest.skip(r"C:\社課 測試 already exists")
    install = root / "breeze-live-room"
    desktop = tmp_path / "桌面 測試"
    desktop.mkdir()
    try:
        install.mkdir(parents=True)
        shutil.copyfile(ROOT / "install-shortcut.ps1", install / "install-shortcut.ps1")
        for name in TARGETS:
            (install / name).write_bytes(b"@echo off\r\n")
        proc = run_ps(install, "-DesktopPath", str(desktop))
        detail = _detail(proc)
        assert proc.returncode == 0, detail
        links = read_links(desktop)
        expected_dir = r"C:\社課 測試\breeze-live-room"
        for name, bat in zip(LINKS, TARGETS):
            assert links[name]["target"] == expected_dir + "\\" + bat, links[name]
            assert links[name]["workdir"] == expected_dir, links[name]
    finally:
        if root.exists():
            shutil.rmtree(root)


def test_links_read_back_unicode(install_dir, desktop, non_ascii_temp_env):
    """A-11: Shell.Application ShellLinkObject read-back keeps the Chinese target.

    WScript.Shell returns an empty TargetPath for Chinese targets on English
    Windows, as the spec's fallback clause anticipated, so read-back uses
    Shell.Application ShellLinkObject. IShellLinkW -CheckOnly is also asserted.
    Target and working directory match exactly, including Chinese, and args are
    empty. The install script sets no icon. The observed WScript value was
    ",0"; Shell.Application must report exactly "" or ",0".
    """
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert created.returncode == 0, _detail(created)
    checked = run_ps(
        install_dir,
        "-CheckOnly",
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    assert checked.returncode == 0, _detail(checked)
    links = read_links(desktop)
    for name, bat in zip(LINKS, TARGETS):
        row = links[name]
        assert row["reader"] == "Shell.Application", row
        assert "wscript_target" in row, row
        assert row["target"] == str(install_dir / bat), row
        assert row["workdir"] == str(install_dir), row
        assert row["args"] == "", row
        assert row["icon"] in ("", ",0"), f"observed icon={row['icon']!r} row={row!r}"


def test_missing_desktop_folder_is_created(install_dir, non_ascii_temp_env, tmp_path):
    """A-12: -DesktopPath creates a missing nested desktop folder."""
    desktop = tmp_path / "不存在 的桌面" / "子層"
    assert not desktop.exists()
    proc = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    detail = _detail(proc)
    assert proc.returncode == 0, detail
    assert desktop.is_dir(), detail
    _assert_links(desktop, detail)


def test_shortcut_failure_message_is_actionable(install_dir, desktop, tmp_path, monkeypatch, capsys):
    """A-13: install_shortcut stays non-fatal unless BREEZE_REQUIRE_SHORTCUT=1.

    The runner wraps subprocess.run, appends -DesktopPath, and passes an env
    copy. The test process environment is not modified, and no real desktop is used.
    """
    monkeypatch.delenv("BREEZE_SKIP_SHORTCUT", raising=False)
    monkeypatch.delenv("BREEZE_REQUIRE_SHORTCUT", raising=False)
    program_data = tmp_path / "程式資料"
    public = tmp_path / "公共資料"
    system_drive = tmp_path / "系統碟"
    temp_dir = tmp_path / "tmp 測試"
    for path in (program_data, public, system_drive, temp_dir):
        path.mkdir()
    preserved = ("TEMP", "TMP", "ProgramData", "PUBLIC", "SystemDrive")
    before_env = {name: os.environ.get(name) for name in preserved}
    seen = []

    def runner(args, cwd=None, **kwargs):
        env = _env_without(os.environ.copy(), *preserved)
        env["TEMP"] = str(temp_dir)
        env["TMP"] = str(temp_dir)
        env["ProgramData"] = str(program_data)
        env["PUBLIC"] = str(public)
        env["SystemDrive"] = str(system_drive)
        proc = subprocess.run(
            [*args, "-DesktopPath", str(desktop)],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        proc.stdout = _powershell_pipe_text(proc.stdout or "")
        proc.stderr = _powershell_pipe_text(proc.stderr or "")
        seen.append(proc)
        return proc

    assert install_shortcut(install_dir, runner=runner) == 0
    assert FAILURE_MESSAGE in capsys.readouterr().out
    assert seen[-1].returncode != 0, _detail(seen[-1])
    assert "No ASCII-only writable directory is available for shortcut compilation." in (
        seen[-1].stdout + seen[-1].stderr
    ), _detail(seen[-1])
    assert list(desktop.glob("*.lnk")) == []

    monkeypatch.setenv("BREEZE_REQUIRE_SHORTCUT", "1")
    assert install_shortcut(install_dir, runner=runner) != 0
    assert FAILURE_MESSAGE in capsys.readouterr().out
    assert {name: os.environ.get(name) for name in preserved} == before_env


def test_checkonly_pwsh7_and_ps51_agree(install_dir, desktop, non_ascii_temp_env):
    """A-16: powershell.exe and pwsh -CheckOnly agree before and after a link is removed."""
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh is not installed")
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert created.returncode == 0, _detail(created)
    for executable in ("powershell.exe", pwsh):
        checked = run_ps(
            install_dir,
            "-CheckOnly",
            "-DesktopPath",
            str(desktop),
            env=non_ascii_temp_env,
            executable=executable,
        )
        assert checked.returncode == 0, executable + "\n" + _detail(checked)
    (desktop / "Breeze Update.lnk").unlink()
    for executable in ("powershell.exe", pwsh):
        missing = run_ps(
            install_dir,
            "-CheckOnly",
            "-DesktopPath",
            str(desktop),
            env=non_ascii_temp_env,
            executable=executable,
        )
        assert missing.returncode == 2, executable + "\n" + _detail(missing)


def test_read_shortcuts_verifies_created_links(install_dir, desktop, non_ascii_temp_env, tmp_path):
    """A-18: read_shortcuts.ps1 accepts fresh shortcuts and rejects an InstallDir mismatch."""
    created = run_ps(install_dir, "-DesktopPath", str(desktop), env=non_ascii_temp_env)
    assert created.returncode == 0, _detail(created)
    out = tmp_path / "data" / "shortcut-readback.json"
    assert not out.parent.exists()
    verified = _run_read_shortcuts(
        "-Out",
        str(out),
        "-InstallDir",
        str(install_dir),
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    detail = _detail(verified)
    assert verified.returncode == 0, detail
    assert out.is_file(), detail
    raw = out.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), detail
    with out.open(encoding="utf-8-sig") as handle:
        rows = json.load(handle)
    assert isinstance(rows, list) and len(rows) == 3, rows
    by_name = {}
    for row in rows:
        for name in LINKS:
            if str(row.get("path", "")).endswith(name + ".lnk"):
                by_name[name] = row
    assert sorted(by_name) == sorted(LINKS), rows
    for name, bat in zip(LINKS, TARGETS):
        row = by_name[name]
        assert row["exists"] is True, row
        assert row["reader"] == "Shell.Application", row
        assert "wscript_target" in row, row
        assert row["target"] == str(install_dir / bat), row
        assert row["workdir"] == str(install_dir), row
        assert row["args"] == "", row

    copied = tmp_path / "搬移後 測試"
    _copy_install_tree(install_dir, copied)
    mismatch_out = tmp_path / "mismatch" / "readback.json"
    mismatched = _run_read_shortcuts(
        "-Out",
        str(mismatch_out),
        "-InstallDir",
        str(copied),
        "-DesktopPath",
        str(desktop),
        env=non_ascii_temp_env,
    )
    detail = _detail(mismatched)
    assert mismatched.returncode == 1, detail
    assert "Shortcut target mismatch:" in mismatched.stdout, detail
    assert "Breeze Live Room" in mismatched.stdout, detail
