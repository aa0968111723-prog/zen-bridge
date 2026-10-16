"""Tests for tools/ram_headroom_guard.py (queue item 32).

Every memory reading is mocked (sample /proc/meminfo text, fake kernel32, or
--available-gb); nothing depends on the host's real RAM, network or ports.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ram_headroom_guard.py"

sys.path.insert(0, str(ROOT / "tools"))
import ram_headroom_guard as guard  # noqa: E402

GIB = 1024**3

SAMPLE_MEMINFO = """\
MemTotal:       16151480 kB
MemFree:         1203456 kB
MemAvailable:    7340032 kB
Buffers:          204800 kB
Cached:          5242880 kB
SwapCached:            0 kB
Active:          6291456 kB
Shmem:            524288 kB
SReclaimable:     409600 kB
HugePages_Total:       0
Hugepagesize:       2048 kB
"""

# Old kernel (< 3.14): no MemAvailable line.
OLD_MEMINFO = """\
MemTotal:        8388608 kB
MemFree:         1048576 kB
Buffers:          262144 kB
Cached:          2097152 kB
Shmem:            524288 kB
SReclaimable:     262144 kB
"""

REPORT_KEYS = {
    "tool",
    "schema_version",
    "status",
    "exit_code",
    "os",
    "method",
    "available_gb",
    "total_gb",
    "memory_load_pct",
    "budget_gb",
    "margin_gb",
    "required_gb",
    "headroom_gb",
    "shortfall_gb",
    "summary_zh",
    "advice_zh",
    "notes",
}


def _reading(avail_gb: float | None, total_gb: float | None = 15.4, **extra) -> dict:
    data = {
        "os": "Linux",
        "method": "mock",
        "available_bytes": None if avail_gb is None else int(avail_gb * GIB),
        "total_bytes": None if total_gb is None else int(total_gb * GIB),
    }
    data.update(extra)
    return data


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )


# ---------------------------------------------------------------- meminfo


def test_parse_meminfo_sample_units():
    info = guard.parse_meminfo(SAMPLE_MEMINFO)
    assert info["MemTotal"] == 16151480 * 1024
    assert info["MemAvailable"] == 7340032 * 1024
    # unit-less counters stay raw
    assert info["HugePages_Total"] == 0
    assert info["Hugepagesize"] == 2048 * 1024


def test_parse_meminfo_ignores_malformed_lines():
    info = guard.parse_meminfo("garbage\nMemTotal: abc kB\n: 5 kB\nMemFree: 10 kB\n\n")
    assert info == {"MemFree": 10 * 1024}


def test_memory_from_meminfo_prefers_memavailable():
    mem = guard.memory_from_meminfo(guard.parse_meminfo(SAMPLE_MEMINFO))
    assert mem["method"] == "proc_meminfo:MemAvailable"
    assert mem["available_bytes"] == 7340032 * 1024
    assert mem["total_bytes"] == 16151480 * 1024


def test_memory_from_meminfo_old_kernel_estimate():
    mem = guard.memory_from_meminfo(guard.parse_meminfo(OLD_MEMINFO))
    assert mem["method"] == "proc_meminfo:estimate"
    expected_kib = 1048576 + 262144 + 2097152 + 262144 - 524288
    assert mem["available_bytes"] == expected_kib * 1024


def test_memory_from_meminfo_missing_fields_is_unknown():
    mem = guard.memory_from_meminfo(guard.parse_meminfo("MemTotal: 100 kB\n"))
    assert mem["available_bytes"] is None
    report = guard.evaluate({**mem, "os": "Linux"})
    assert report["status"] == "unknown"
    assert report["exit_code"] == guard.EXIT_UNKNOWN


def test_read_linux_from_sample_file(tmp_path: Path):
    path = tmp_path / "meminfo"
    path.write_text(SAMPLE_MEMINFO, encoding="utf-8")
    mem = guard.read_memory("Linux", meminfo_path=path)
    assert mem["os"] == "Linux"
    assert mem["available_bytes"] == 7340032 * 1024


def test_read_linux_unreadable_file_is_unknown(tmp_path: Path):
    mem = guard.read_memory("Linux", meminfo_path=tmp_path / "missing")
    assert mem["available_bytes"] is None
    assert "cannot read" in mem["error"]
    report = guard.evaluate(mem)
    assert report["status"] == "unknown"
    assert report["notes"] and "cannot read" in report["notes"][0]


# ---------------------------------------------------------------- windows


class _FakeKernel32:
    def __init__(self, *, ok: int = 1, total: int, avail: int, load: int = 55):
        self.ok, self.total, self.avail, self.load = ok, total, avail, load
        self.calls = 0

    def GlobalMemoryStatusEx(self, ref):  # noqa: N802 - Win32 name
        self.calls += 1
        status = ref._obj  # ctypes.byref -> CArgObject._obj is the struct
        assert status.dwLength == ctypes.sizeof(type(status))
        status.ullTotalPhys = self.total
        status.ullAvailPhys = self.avail
        status.dwMemoryLoad = self.load
        return self.ok


def test_read_windows_with_fake_kernel32():
    fake = _FakeKernel32(total=int(15.4 * GIB), avail=int(6.5 * GIB), load=58)
    mem = guard.read_memory("Windows", kernel32=fake)
    assert fake.calls == 1
    assert mem["method"] == "GlobalMemoryStatusEx"
    assert mem["available_bytes"] == int(6.5 * GIB)
    assert mem["total_bytes"] == int(15.4 * GIB)
    assert mem["memory_load_pct"] == 58
    report = guard.evaluate(mem)
    assert report["status"] == "ok"
    assert report["memory_load_pct"] == 58


def test_read_windows_call_returns_zero_is_unknown():
    fake = _FakeKernel32(ok=0, total=0, avail=0)
    mem = guard.read_windows(fake)
    assert mem["available_bytes"] is None
    assert "returned 0" in mem["error"]
    assert guard.evaluate(mem)["status"] == "unknown"


def test_read_windows_without_windll_is_unknown():
    class _Broken:
        def GlobalMemoryStatusEx(self, ref):  # noqa: N802
            raise OSError("boom")

    mem = guard.read_windows(_Broken())
    assert mem["available_bytes"] is None
    assert "unavailable" in mem["error"]


@pytest.mark.parametrize("system", ["Darwin", "FreeBSD", ""])
def test_other_os_reports_unknown(system):
    mem = guard.read_memory(system or "Plan9")
    assert mem["method"] == "unsupported"
    assert mem["available_bytes"] is None
    report = guard.evaluate(mem)
    assert report["status"] == "unknown"
    assert report["exit_code"] == 3
    assert any("工作管理員" in tip for tip in report["advice_zh"])


# ---------------------------------------------------------------- classify


@pytest.mark.parametrize(
    "avail,expected",
    [
        (12.0, "ok"),
        (5.8, "ok"),  # exactly budget + margin
        (5.79, "warn"),
        (4.8, "warn"),  # exactly budget
        (4.79, "fail"),
        (0.0, "fail"),
        (None, "unknown"),
    ],
)
def test_classify_boundaries_default_budget(avail, expected):
    assert guard.classify(avail) == expected


def test_classify_custom_budget_and_margin():
    assert guard.classify(3.0, budget_gb=2.5, margin_gb=0.5) == "ok"
    assert guard.classify(2.7, budget_gb=2.5, margin_gb=0.5) == "warn"
    assert guard.classify(2.4, budget_gb=2.5, margin_gb=0.5) == "fail"
    assert guard.classify(2.5, budget_gb=2.5, margin_gb=0.0) == "ok"


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_classify_rejects_bad_budget(bad):
    with pytest.raises(guard.GuardInputError):
        guard.classify(5.0, budget_gb=bad)


def test_default_budget_is_4_8():
    assert guard.DEFAULT_BUDGET_GB == 4.8
    assert guard.EXIT_CODES == {"ok": 0, "warn": 1, "fail": 2, "unknown": 3}
    assert guard.EXIT_USAGE == 4


# ---------------------------------------------------------------- report / advice


@pytest.mark.parametrize(
    "avail,status,code",
    [(8.0, "ok", 0), (5.0, "warn", 1), (3.0, "fail", 2), (None, "unknown", 3)],
)
def test_evaluate_json_shape_all_statuses(avail, status, code):
    report = guard.evaluate(_reading(avail))
    assert set(report) == REPORT_KEYS
    assert report["tool"] == "ram_headroom_guard"
    assert report["schema_version"] == 1
    assert report["status"] == status
    assert report["exit_code"] == code
    assert report["budget_gb"] == 4.8
    assert report["margin_gb"] == 1.0
    assert report["required_gb"] == 5.8
    assert isinstance(report["advice_zh"], list) and report["advice_zh"]
    assert all(isinstance(t, str) for t in report["advice_zh"])
    assert isinstance(report["summary_zh"], str) and status in report["summary_zh"]
    json.dumps(report, ensure_ascii=False)  # serialisable


def test_evaluate_numbers_for_fail():
    report = guard.evaluate(_reading(3.0, 15.4))
    assert report["available_gb"] == pytest.approx(3.0, abs=1e-3)
    assert report["total_gb"] == pytest.approx(15.4, abs=1e-3)
    assert report["shortfall_gb"] == pytest.approx(1.8, abs=1e-3)
    assert report["headroom_gb"] == pytest.approx(-1.8, abs=1e-3)


def test_evaluate_numbers_for_ok_have_no_shortfall():
    report = guard.evaluate(_reading(10.0))
    assert report["shortfall_gb"] == 0.0
    assert report["headroom_gb"] == pytest.approx(5.2, abs=1e-3)


def test_unknown_report_has_null_numbers():
    report = guard.evaluate(_reading(None, None))
    assert report["available_gb"] is None
    assert report["total_gb"] is None
    assert report["shortfall_gb"] is None
    assert report["headroom_gb"] is None


def test_advice_fail_mentions_background_apps_and_q4():
    text = "\n".join(guard.evaluate(_reading(2.0))["advice_zh"])
    assert "背景程式" in text
    assert "Q4" in text
    assert "Chrome" in text
    assert "還差約 2.8 GB" in text


def test_advice_warn_mentions_q4_but_not_shortfall():
    text = "\n".join(guard.evaluate(_reading(5.2))["advice_zh"])
    assert "Q4" in text
    assert "背景程式" in text
    assert "還差" not in text


def test_advice_ok_is_short_traditional_chinese():
    advice = guard.evaluate(_reading(9.0))["advice_zh"]
    assert len(advice) == 1
    assert "足夠" in advice[0]
    assert "Q4" not in advice[0]


def test_format_text_contains_summary_and_exit_code():
    text = guard.format_text(guard.evaluate(_reading(3.0)))
    assert text.splitlines()[0].startswith("RAM 檢查：不足（fail）")
    assert "exit code: 2" in text


# ---------------------------------------------------------------- CLI (in-process)


@pytest.mark.parametrize(
    "avail,code", [(8.0, 0), (5.0, 1), (3.0, 2), (None, 3)]
)
def test_main_with_injected_reader_exit_codes(avail, code, capsys):
    rc = guard.main(["--json"], reader=lambda: _reading(avail))
    assert rc == code
    data = json.loads(capsys.readouterr().out)
    assert data["exit_code"] == code


def test_main_text_mode(capsys):
    rc = guard.main([], reader=lambda: _reading(5.0))
    out = capsys.readouterr().out
    assert rc == 1
    assert "偏緊（warn）" in out
    assert not out.lstrip().startswith("{")


def test_main_budget_override(capsys):
    rc = guard.main(
        ["--json", "--budget-gb", "2.0", "--margin-gb", "0.5"],
        reader=lambda: _reading(3.0),
    )
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["budget_gb"] == 2.0
    assert data["required_gb"] == 2.5


def test_main_negative_budget_is_usage_error(capsys):
    rc = guard.main(["--budget-gb", "-1"], reader=lambda: _reading(8.0))
    assert rc == guard.EXIT_USAGE
    assert "budget" in capsys.readouterr().err


def test_main_conflicting_inputs_is_usage_error(tmp_path: Path, capsys):
    path = tmp_path / "meminfo"
    path.write_text(SAMPLE_MEMINFO, encoding="utf-8")
    rc = guard.main(["--available-gb", "5", "--meminfo", str(path)])
    assert rc == guard.EXIT_USAGE


def test_main_missing_meminfo_file_is_usage_error(tmp_path: Path, capsys):
    rc = guard.main(["--meminfo", str(tmp_path / "nope")])
    assert rc == guard.EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_main_unknown_option_exits_usage():
    with pytest.raises(SystemExit) as exc:
        guard.main(["--no-such-flag"])
    assert exc.value.code == guard.EXIT_USAGE


def test_main_does_not_probe_host_when_reader_given(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("host RAM must not be read in tests")

    monkeypatch.setattr(guard, "read_memory", _boom)
    assert guard.main(["--json"], reader=lambda: _reading(8.0)) == 0


# ---------------------------------------------------------------- CLI (subprocess)


def test_cli_meminfo_file_json(tmp_path: Path):
    path = tmp_path / "meminfo"
    path.write_text(SAMPLE_MEMINFO, encoding="utf-8")  # 7.0 GiB available
    proc = _run("--meminfo", str(path), "--json")
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert set(data) == REPORT_KEYS
    assert data["status"] == "ok"
    assert data["method"] == "proc_meminfo:MemAvailable"
    assert data["available_gb"] == pytest.approx(7.0, abs=1e-3)


@pytest.mark.parametrize(
    "avail,code,status", [("9", 0, "ok"), ("5.3", 1, "warn"), ("1.5", 2, "fail")]
)
def test_cli_available_override_exit_codes(avail, code, status):
    proc = _run("--available-gb", avail, "--total-gb", "15.4", "--json")
    assert proc.returncode == code, proc.stderr
    data = json.loads(proc.stdout)
    assert data["status"] == status
    assert data["method"] == "manual"
    assert data["total_gb"] == pytest.approx(15.4, abs=1e-3)


def test_cli_meminfo_without_available_is_unknown(tmp_path: Path):
    path = tmp_path / "meminfo"
    path.write_text("MemTotal: 1000 kB\n", encoding="utf-8")
    proc = _run("--meminfo", str(path), "--json")
    assert proc.returncode == 3
    assert json.loads(proc.stdout)["status"] == "unknown"


def test_cli_bad_budget_type_exits_4():
    proc = _run("--budget-gb", "lots", "--available-gb", "5")
    assert proc.returncode == 4
    assert "error" in proc.stderr
