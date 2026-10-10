"""app/hw_tune.py: Windows priority / EcoQoS / affinity / power detection, all with fakes.

No real Win32 call is made: a fake kernel32 + powrprof stand in for ctypes.WinDLL and a fake
psutil stands in for process discovery, so this runs on Linux CI.
"""
import ctypes
import struct
import uuid

import pytest

from app import hw_tune as hw


# ------------------------------------------------------------------ fakes
def core_record(mask: int, smt: bool = True, group: int = 0) -> bytes:
    head = struct.pack("<II", hw.RelationProcessorCore, 48) + bytes([1 if smt else 0, 0]) + b"\0" * 20
    return head + struct.pack("<H", 1) + struct.pack("<QHHHH", mask, group, 0, 0, 0)


def cache_record() -> bytes:                       # RelationCache: must be skipped
    return struct.pack("<II", 2, 40) + b"\0" * 32


def non_adjacent_5600h() -> bytes:
    """6 cores whose SMT siblings are (0,6) (1,7) ... — not adjacent on purpose."""
    return cache_record() + b"".join(core_record((1 << c) | (1 << (c + 6))) for c in range(6))


class Proc:
    def __init__(self, prio=0x20, affinity=0xFFF):
        self.prio, self.affinity, self.throttle = prio, affinity, None


class FakeK32:
    """Subset of kernel32 with Win32 calling shapes (ctypes byref args)."""

    def __init__(self, procs=None, denied=(), power=(1, 1, 80), cores=None, has_spi=True, timer_flag_ok=True):
        self.procs = procs if procs is not None else {}
        self.denied, self.power, self.cores = set(denied), power, cores
        self.timer_flag_ok, self.err, self.calls = timer_flag_ok, 0, []
        if has_spi:
            self.SetProcessInformation = self._spi

    def OpenProcess(self, access, inherit, pid):
        self.calls.append(("OpenProcess", pid))
        if pid in self.denied:
            self.err = hw.ERROR_ACCESS_DENIED
            return 0
        if pid not in self.procs:
            self.err = hw.ERROR_INVALID_PARAMETER
            return 0
        return 1000 + pid

    def CloseHandle(self, h):
        return 1

    def _p(self, h):
        return self.procs[h - 1000]

    def GetPriorityClass(self, h):
        return self._p(h).prio

    def SetPriorityClass(self, h, cls):
        self.calls.append(("SetPriorityClass", h - 1000, cls))
        self._p(h).prio = cls
        return 1

    def _spi(self, h, info_class, ref, size):
        s = ref._obj
        self.calls.append(("SetProcessInformation", h - 1000, info_class, s.Version, s.ControlMask, s.StateMask))
        if s.ControlMask & hw.POWER_THROTTLING_IGNORE_TIMER_RESOLUTION and not self.timer_flag_ok:
            return 0
        self._p(h).throttle = (s.ControlMask, s.StateMask)
        return 1

    def GetProcessAffinityMask(self, h, pref, sref):
        pref._obj.value, sref._obj.value = self._p(h).affinity, 0xFFF
        return 1

    def SetProcessAffinityMask(self, h, mask):
        self.calls.append(("SetProcessAffinityMask", h - 1000, mask))
        self._p(h).affinity = mask
        return 1

    def GetCurrentThread(self):
        return -2

    def SetThreadPriority(self, h, mode):
        self.calls.append(("SetThreadPriority", h, mode))
        return 1

    def GetSystemPowerStatus(self, ref):
        if self.power is None:
            return 0
        s = ref._obj
        s.ACLineStatus, s.BatteryFlag, s.BatteryLifePercent = self.power
        return 1

    def GetLogicalProcessorInformationEx(self, rel, buf, nref):
        data = self.cores or b""
        if buf is None:
            nref._obj.value = len(data)
            return 0
        ctypes.memmove(buf, data, len(data))
        return 1

    def LocalFree(self, p):
        return 0


class FakePowr:
    def __init__(self, scheme="381b4222-f694-41f0-9685-ff5bb260df2e", effective=None, actual=None):
        self._scheme = ctypes.create_string_buffer(uuid.UUID(scheme).bytes_le, 16) if scheme else None
        if effective is not None:
            self.PowerGetEffectiveOverlayScheme = self._overlay(effective)
        if actual is not None:
            self.PowerGetActualOverlayScheme = self._overlay(actual)

    def PowerGetActiveScheme(self, root, pref):
        if self._scheme is None:
            return 2
        pref._obj.value = ctypes.addressof(self._scheme)
        return 0

    @staticmethod
    def _overlay(guid):
        def fn(buf):
            ctypes.memmove(buf, uuid.UUID(guid).bytes_le, 16)
            return 0
        return fn


def make_win(k32, pp=None, pid=4242):
    return hw._Win(k32, pp, last_error=lambda: k32.err, current_pid=pid)


class FakePsutil:
    class Error(Exception):
        pass

    class AccessDenied(Error):
        pass

    class NoSuchProcess(Error):
        pass

    class ZombieProcess(NoSuchProcess):
        pass

    def __init__(self, procs, physical=6, logical=12):
        self._procs, self.physical, self.logical = procs, physical, logical

    def process_iter(self, attrs):
        for pid, name, cmd in self._procs:
            p = type("P", (), {})()
            p.info = {"pid": pid, "name": name}

            def cmdline(cmd=cmd):
                if cmd is None:
                    raise FakePsutil.AccessDenied()
                return cmd
            p.cmdline = cmdline
            yield p

    def cpu_count(self, logical=True):
        return self.logical if logical else self.physical


@pytest.fixture(autouse=True)
def _clean_registry():
    hw._ORIGINAL.clear()
    yield
    hw._ORIGINAL.clear()


# ------------------------------------------------------------------ non-Windows
def test_everything_is_unsupported_off_windows():
    assert {r.status for r in hw.apply_profile(platform="linux")} == {"unsupported"}
    assert hw.revert(platform="linux")[0].status == "unsupported"
    assert hw.power_status(platform="linux").status == "unsupported"
    assert hw.enter_background_thread(platform="linux").status == "unsupported"
    st = hw.status(platform="linux", psutil_mod=FakePsutil([]))
    assert st["supported"] is False and st["topology"]["physical"] == 6 and st["plan"]["asr"] == 4


def test_topology_without_psutil_falls_back_to_os(monkeypatch):
    monkeypatch.setattr(hw, "_psutil", lambda: None)
    t = hw.detect_topology(platform="linux")
    assert t.source == "os" and t.physical == t.logical >= 1 and t.cores == ()


# ------------------------------------------------------------------ topology
def test_core_map_detects_non_adjacent_smt_siblings():
    t = hw.detect_topology(win=make_win(FakeK32(cores=non_adjacent_5600h())), platform="win32")
    assert t.source == "glpi_ex" and t.physical == 6 and t.logical == 12
    assert t.cores[0] == (0, 6) and t.cores[5] == (5, 11)
    assert t.as_dict()["smt"] is True


def test_core_map_adjacent_and_no_smt():
    adj = b"".join(core_record(0b11 << (2 * c)) for c in range(6))
    assert hw.detect_topology(win=make_win(FakeK32(cores=adj)), platform="win32").cores[1] == (2, 3)
    nosmt = b"".join(core_record(1 << c, smt=False) for c in range(4))
    t = hw.detect_topology(win=make_win(FakeK32(cores=nosmt)), platform="win32")
    assert t.physical == t.logical == 4 and t.as_dict()["smt"] is False


def test_core_map_api_failure_falls_back_to_psutil():
    t = hw.detect_topology(win=make_win(FakeK32(cores=None)), platform="win32", psutil_mod=FakePsutil([]))
    assert (t.source, t.physical, t.logical, t.cores) == ("psutil", 6, 12, ())


def test_parser_ignores_truncated_record():
    assert hw.parse_core_records(core_record(0b11)[:40]) == []


# ------------------------------------------------------------------ power
@pytest.mark.parametrize("ac,flag,source,has_battery", [
    (1, 1, "ac", True), (0, 0, "battery", True), (255, 1, "unknown", True),
    (255, 128, "ac", False),          # desktop: no system battery -> AC even if ACLineStatus unknown
    (0, 128, "ac", False), (255, 255, "unknown", None)])
def test_classify_power(ac, flag, source, has_battery):
    assert hw.classify_power(ac, flag) == (source, has_battery)


def test_power_status_scheme_and_effective_overlay():
    win = make_win(FakeK32(power=(1, 8, 100)), FakePowr("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
                                                          effective="ded574b5-45a0-4f42-8737-46345c09c238"))
    ps = hw.power_status(win=win, platform="win32")
    assert ps.status == "ok" and ps.source == "ac" and ps.scheme_name == "High performance"
    assert ps.overlay_name == "Best performance" and ps.battery_percent == 100


def test_power_status_falls_back_to_actual_overlay_then_unknown():
    ps = hw.power_status(win=make_win(FakeK32(), FakePowr(actual="961cc777-2547-4f9d-8174-7d86181b8a7a")),
                         platform="win32")
    assert ps.overlay_name == "Best power efficiency"
    ps = hw.power_status(win=make_win(FakeK32(power=(255, 1, 255)), FakePowr()), platform="win32")
    assert ps.overlay_name == "UNKNOWN" and ps.source == "unknown"
    assert any("UNKNOWN" in n for n in ps.notes) and any("255" in n for n in ps.notes)


def test_power_status_custom_scheme_and_missing_powrprof():
    ps = hw.power_status(win=make_win(FakeK32(), FakePowr("11111111-2222-3333-4444-555555555555")), platform="win32")
    assert ps.scheme_name == "Custom"
    ps = hw.power_status(win=make_win(FakeK32(power=None), None), platform="win32")
    assert ps.scheme_name == "UNKNOWN" and ps.source == "unknown" and "GetSystemPowerStatus failed" in ps.notes


def test_power_status_never_writes():
    k32, pp = FakeK32(), FakePowr(effective="00000000-0000-0000-0000-000000000000")
    hw.power_status(win=make_win(k32, pp), platform="win32")
    assert not [c for c in k32.calls if c[0].startswith("Set")]
    assert not [n for n in dir(pp) if n.startswith("PowerSet")]


# ------------------------------------------------------------------ thread plan (pure)
def test_5600h_default_split_is_4_plus_2():
    p = hw.recommend_threads(6, 12)
    assert (p.asr, p.mt, p.draft, p.embed, p.oversubscribed) == (4, 2, 0, 0, False)
    assert p.env()["BREEZE_ASR_THREADS"] == "4" and p.args()["whisper_cpp"] == ["-t", "4"]
    assert p.args()["llama_server"] == ["-t", "2", "-tb", "2"] and p.args()["ollama_options"] == {"num_thread": 2}
    assert p.affinity is None and p.rationale


def test_draft_asr_keeps_breeze_at_four():
    p = hw.recommend_threads(6, 12, draft_threads=1)
    assert (p.asr, p.mt, p.draft) == (4, 1, 1) and p.asr + p.mt + p.draft <= 6


def test_asr_first_is_oversubscribed_and_never_pins():
    p = hw.recommend_threads(6, 12, policy="asr_first", affinity=True,
                             topology=hw.detect_topology(win=make_win(FakeK32(cores=non_adjacent_5600h())),
                                                         platform="win32"))
    assert (p.asr, p.mt, p.oversubscribed, p.affinity) == (6, 2, True, None)
    assert p.warnings


@pytest.mark.parametrize("live,power,embed", [(True, "ac", 0), (False, "ac", 4), (False, "battery", 0)])
def test_embed_threads_only_when_idle_on_ac(live, power, embed):
    assert hw.recommend_threads(6, 12, live=live, power=power, embed_enabled=True).embed == embed


def test_battery_and_unknown_power_warn():
    assert any("battery" in w for w in hw.recommend_threads(6, 12, power="battery").warnings)
    assert any("UNKNOWN" in w for w in hw.recommend_threads(6, 12, power="unknown").warnings)


def test_small_machines_and_bad_policy():
    assert hw.recommend_threads(2).asr >= 1 and hw.recommend_threads(1).mt == 1
    with pytest.raises(ValueError):
        hw.recommend_threads(6, policy="realtime")


def test_affinity_uses_detected_siblings():
    topo = hw.detect_topology(win=make_win(FakeK32(cores=non_adjacent_5600h())), platform="win32")
    p = hw.recommend_threads(6, 12, affinity=True, topology=topo)
    assert sorted(p.affinity["asr"]) == [0, 1, 2, 3, 6, 7, 8, 9]
    assert sorted(p.affinity["mt"]) == [4, 5, 10, 11]
    assert not set(p.affinity["asr"]) & set(p.affinity["mt"])


def test_affinity_without_core_map_is_refused():
    p = hw.recommend_threads(6, 12, affinity=True, topology=hw.Topology(12, 6, (), "psutil"))
    assert p.affinity is None and any("affinity" in w for w in p.warnings)


# ------------------------------------------------------------------ profile from env
def test_profile_from_env_reuses_breeze_names_and_rejects_bad_values():
    p = hw.Profile.from_env({})
    assert (p.policy, p.affinity, p.asr_priority, p.mt_priority) == ("split", False, "above_normal", "below_normal")
    p = hw.Profile.from_env({"BREEZE_ASR_PRIORITY": "realtime", "ZEN_HW_MT_PRIORITY": "high",
                             "ZEN_HW_AFFINITY": "1", "BREEZE_ASR_ECOQOS_OFF": "0", "ZEN_HW_POLICY": "ASR_FIRST"})
    assert p.asr_priority == "above_normal" and p.mt_priority == "below_normal"     # never HIGH / REALTIME
    assert p.affinity is True and p.asr_ecoqos_off is False and p.policy == "asr_first"


# ------------------------------------------------------------------ apply
def fleet():
    return [(10, "python.exe", ["python", "-m", "app.native_worker", "--model", "m"]),
            (11, "python.exe", ["python", "-m", "app.desktop_service"]),         # zen-bridge itself: untouched
            (20, "ollama.exe", ["C:\\ollama.exe", "serve"]),                     # server, not the runner
            (21, "ollama.exe", ["C:\\ollama.exe", "runner", "--model", "C:\\blobs\\sha256-aaa", "--port", "5"]),
            (22, "llama-server.exe", ["llama-server.exe", "-m", "hy-mt2.gguf", "--port", "8081"]),
            (30, "ollama.exe", ["C:\\ollama.exe", "runner", "--model", "C:\\blobs\\sha256-emb"])]


def test_apply_sets_priorities_on_the_cpu_heavy_processes():
    procs = {pid: Proc() for pid in (10, 11, 20, 21, 22, 30)}
    k32 = FakeK32(procs)
    res = hw.apply_profile(hw.Profile(embed_match="sha256-emb"), platform="win32", win=make_win(k32),
                           psutil_mod=FakePsutil(fleet()))
    assert procs[10].prio == hw.PRIORITY_CLASSES["above_normal"]
    assert procs[21].prio == procs[22].prio == hw.PRIORITY_CLASSES["below_normal"]
    assert procs[30].prio == hw.PRIORITY_CLASSES["idle"]
    assert procs[11].prio == procs[20].prio == 0x20                      # app process and ollama serve untouched
    assert procs[10].throttle == (hw.POWER_THROTTLING_EXECUTION_SPEED, 0)              # EcoQoS off for ASR
    assert procs[30].throttle == (hw.POWER_THROTTLING_EXECUTION_SPEED,) * 2           # EcoQoS on for embedding
    bg = [r for r in res if r.action == "background"]
    assert bg and bg[0].status == "unsupported" and "calling process" in bg[0].detail
    assert not [c for c in k32.calls if c[0] == "SetPriorityClass" and c[2] in hw.FORBIDDEN_CLASSES]
    assert not [c for c in k32.calls if c[0] == "SetProcessAffinityMask"]               # affinity off by default


@pytest.mark.parametrize("name,cmd,role", [
    ("whisper-server.exe", ["whisper-server.exe", "-m", "b.bin"], "asr"),
    ("whisper-cli.exe", ["whisper-cli.exe"], "asr"),
    ("ollama_llama_server.exe", ["ollama_llama_server.exe", "--model", "x"], "mt"),
    ("ollama.exe", ["ollama.exe", "serve"], None),
    ("ollama app.exe", ["ollama app.exe"], None),
    ("python.exe", ["python", "-m", "app.run"], None)])
def test_role_matching(name, cmd, role):
    assert hw._role_of(name, cmd, hw.Profile()) == role


def test_mt_exclude_keeps_embedding_runner_out_of_mt():
    prof = hw.Profile(mt_exclude="sha256-emb")
    assert hw._role_of("ollama.exe", ["ollama.exe", "runner", "--model", "sha256-emb"], prof) is None


def test_apply_is_idempotent_and_reports_unchanged():
    procs = {10: Proc(), 21: Proc()}
    k32 = FakeK32(procs)
    kw = dict(pids={"asr": [10], "mt": [21]}, platform="win32", win=make_win(k32))
    hw.apply_profile(hw.Profile(), **kw)
    n = len([c for c in k32.calls if c[0] == "SetPriorityClass"])
    again = hw.apply_profile(hw.Profile(), **kw)
    assert len([c for c in k32.calls if c[0] == "SetPriorityClass"]) == n
    assert {r.detail for r in again if r.action == "priority"} == {"above_normal (unchanged)", "below_normal (unchanged)"}
    assert hw._ORIGINAL[10]["priority"] == 0x20                          # the first original is kept


def test_access_denied_is_reported_not_swallowed():
    procs = {10: Proc()}
    res = hw.apply_profile(hw.Profile(), pids={"asr": [10], "mt": [21]}, platform="win32",
                           win=make_win(FakeK32(procs, denied={21})))
    mt = [r for r in res if r.role == "mt"]
    assert [(r.status, r.pid) for r in mt] == [("denied", 21)]


def test_unreadable_cmdline_and_missing_targets():
    ps = FakePsutil([(10, "python.exe", ["python", "-m", "app.native_worker"]), (40, "ollama.exe", None)])
    res = hw.apply_profile(hw.Profile(), platform="win32", win=make_win(FakeK32({10: Proc()})), psutil_mod=ps)
    st = {(r.role, r.action): r for r in res}
    assert st[("mt", "find")].status == "denied" and st[("mt", "find")].pid == 40
    assert st[("embed", "find")].status == "not_found" and "ZEN_HW_EMBED_MATCH" in st[("embed", "find")].detail


def test_vanished_process_is_not_found():
    res = hw.apply_profile(hw.Profile(), pids={"asr": [99]}, platform="win32", win=make_win(FakeK32({})))
    assert [r.status for r in res if r.role == "asr"] == ["not_found"]


def test_no_psutil_needs_explicit_pids(monkeypatch):
    monkeypatch.setattr(hw, "_psutil", lambda: None)
    res = hw.apply_profile(hw.Profile(), platform="win32", win=make_win(FakeK32({})))
    assert {r.status for r in res} == {"unsupported"} and all("psutil" in r.detail for r in res)


def test_high_or_realtime_process_is_left_alone():
    procs = {10: Proc(prio=0x100)}
    res = hw.apply_profile(hw.Profile(), pids={"asr": [10]}, platform="win32", win=make_win(FakeK32(procs)))
    assert procs[10].prio == 0x100 and [r.status for r in res if r.action == "priority"] == ["skipped"]
    with pytest.raises(ValueError):
        make_win(FakeK32(procs)).set_priority(1010, 0x100)


def test_ecoqos_missing_api_is_unsupported():
    procs = {10: Proc()}
    res = hw.apply_profile(hw.Profile(), pids={"asr": [10]}, platform="win32",
                           win=make_win(FakeK32(procs, has_spi=False)))
    eco = [r for r in res if r.action == "ecoqos"][0]
    assert eco.status == "unsupported" and procs[10].prio == hw.PRIORITY_CLASSES["above_normal"]


def test_timer_resolution_flag_falls_back_on_older_windows():
    procs = {10: Proc()}
    res = hw.apply_profile(hw.Profile(asr_timer_res=True), pids={"asr": [10]}, platform="win32",
                           win=make_win(FakeK32(procs, timer_flag_ok=False)))
    eco = [r for r in res if r.action == "ecoqos"][0]
    assert eco.status == "applied" and "not supported" in eco.detail
    assert procs[10].throttle == (hw.POWER_THROTTLING_EXECUTION_SPEED, 0)
    procs = {10: Proc()}
    hw.apply_profile(hw.Profile(asr_timer_res=True), pids={"asr": [10]}, platform="win32", win=make_win(FakeK32(procs)))
    assert procs[10].throttle == (hw.POWER_THROTTLING_EXECUTION_SPEED | hw.POWER_THROTTLING_IGNORE_TIMER_RESOLUTION, 0)


def test_affinity_split_and_revert():
    procs = {10: Proc(), 22: Proc()}
    k32 = FakeK32(procs, cores=non_adjacent_5600h())
    win = make_win(k32)
    hw.apply_profile(hw.Profile(affinity=True), pids={"asr": [10], "mt": [22]}, platform="win32", win=win)
    assert procs[10].affinity == 0b1111001111 and procs[22].affinity == 0b110000110000
    out = hw.revert(platform="win32", win=win)
    assert procs[10].affinity == procs[22].affinity == 0xFFF
    assert procs[10].prio == procs[22].prio == 0x20
    assert procs[10].throttle == (0, 0)                                  # EcoQoS handed back to Windows
    assert all(r.status == "applied" for r in out) and not hw._ORIGINAL


def test_runner_restart_gets_tuned_again():
    procs = {21: Proc()}
    k32 = FakeK32(procs)
    hw.apply_profile(hw.Profile(), pids={"mt": [21]}, platform="win32", win=make_win(k32))
    procs[23] = Proc()                                                   # Ollama restarted its runner
    del procs[21]
    hw.apply_profile(hw.Profile(), pids={"mt": [23]}, platform="win32", win=make_win(k32))
    assert procs[23].prio == hw.PRIORITY_CLASSES["below_normal"]
    out = hw.revert(platform="win32", win=make_win(k32))
    assert {(r.pid, r.status) for r in out if r.action == "revert"} == {(21, "not_found")}


def test_background_thread_mode():
    k32 = FakeK32()
    assert hw.enter_background_thread(True, platform="win32", win=make_win(k32)).status == "applied"
    hw.enter_background_thread(False, platform="win32", win=make_win(k32))
    modes = [c[2] for c in k32.calls if c[0] == "SetThreadPriority"]
    assert modes == [hw.THREAD_MODE_BACKGROUND_BEGIN, hw.THREAD_MODE_BACKGROUND_END]


def test_status_is_read_only_snapshot():
    k32 = FakeK32({}, cores=non_adjacent_5600h(), power=(0, 0, 40))
    st = hw.status(platform="win32", win=make_win(k32, FakePowr(effective="ded574b5-45a0-4f42-8737-46345c09c238")))
    assert st["topology"]["source"] == "glpi_ex" and st["power"]["source"] == "battery"
    assert st["plan"]["asr"] == 4 and any("battery" in w for w in st["plan"]["warnings"])
    assert not [c for c in k32.calls if c[0].startswith("Set") or c[0] == "OpenProcess"]


def test_status_warns_on_power_efficiency_mode_but_never_changes_it():
    k32 = FakeK32({}, cores=non_adjacent_5600h())
    pp = FakePowr(effective="961cc777-2547-4f9d-8174-7d86181b8a7a")
    st = hw.status(platform="win32", win=make_win(k32, pp))
    assert any("Best power efficiency" in w for w in st["plan"]["warnings"])
    assert not [n for n in dir(pp) if n.startswith("PowerSet")]


# ------------------------------------------------------------------ real Windows (skipped elsewhere)
WINDOWS = pytest.mark.skipif(__import__("sys").platform != "win32", reason="real Win32 APIs")


@WINDOWS
def test_real_status_is_read_only_and_sane():
    st = hw.status()
    assert st["supported"] is True and st["topology"]["physical"] >= 1
    assert st["power"]["status"] == "ok" and st["power"]["source"] in ("ac", "battery", "unknown")
    assert st["plan"]["asr"] + st["plan"]["mt"] <= max(st["topology"]["physical"], 2) or st["plan"]["oversubscribed"]


@WINDOWS
def test_real_apply_and_revert_on_a_throwaway_child():
    """Only touches a child process this test starts; never Ollama or the app."""
    import subprocess
    import sys
    psutil = pytest.importorskip("psutil")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        p = psutil.Process(child.pid)
        before_prio, before_aff = p.nice(), p.cpu_affinity()
        res = hw.apply_profile(hw.Profile(affinity=True), pids={"mt": [child.pid]})
        prio = [r for r in res if r.role == "mt" and r.action == "priority"]
        assert prio and prio[0].status == "applied", res
        assert p.nice() == psutil.BELOW_NORMAL_PRIORITY_CLASS
        aff = [r for r in res if r.action == "affinity"]
        if aff and aff[0].status == "applied":
            assert set(p.cpu_affinity()) < set(before_aff)
        hw.revert()
        assert p.nice() == before_prio and p.cpu_affinity() == before_aff
        res = hw.apply_profile(hw.Profile(), pids={"asr": [child.pid]})
        eco = [r for r in res if r.action == "ecoqos"]
        assert eco and eco[0].status in ("applied", "unsupported"), res
        assert p.nice() == psutil.ABOVE_NORMAL_PRIORITY_CLASS
        hw.revert()
        assert p.nice() == before_prio
    finally:
        child.kill()
        child.wait(10)
