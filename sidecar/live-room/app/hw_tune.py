"""Hardware tuning for the Windows desktop build (Ryzen 5 5600H, 6C/12T, no NVIDIA).

hardware.md §4-§5/§7 and QA 效能長 P1-9/P1-10: the processes that burn CPU are separate
programs (the native ASR worker or whisper.cpp, llama-server / the Ollama runner, an
embedding runner). Setting a priority on zen-bridge's own threads does not throttle them,
so this module acts on those PIDs directly and reports what happened per target.

* ``detect_topology()``     logical -> physical core map (GetLogicalProcessorInformationEx;
                             SMT siblings are read, never assumed adjacent).
* ``power_status()``        AC / battery / unknown, active plan, Win11 power mode. Read-only.
* ``recommend_threads()``   pure thread budget for ASR / MT / draft / embed.
* ``apply_profile()``       priority class, EcoQoS opt-out/in, optional affinity. Idempotent.
* ``revert()``              restore what apply_profile changed. ``status()`` is read-only.

Never REALTIME or HIGH. Never changes a power plan. Off Windows every call returns
status ``unsupported`` and touches nothing. psutil (pinned in requirements-lock.txt) is
optional: without it only explicit PIDs can be tuned.

Env (all optional; existing BREEZE_* names are reused where they already mean the same):
  ZEN_HW_POLICY        split        split = ASR + MT + draft <= physical cores (6C: 4+2);
                                    asr_first = ASR on all physical cores + MT 2 (oversubscribed,
                                    relies on priority; affinity is then never applied)
  ZEN_HW_AFFINITY      0            1 = pin ASR and MT to disjoint physical cores (can hurt; measure)
  BREEZE_ASR_PRIORITY  above_normal normal | above_normal
  BREEZE_ASR_ECOQOS_OFF 1           opt the ASR process out of EcoQoS
  ZEN_HW_TIMER_RES     0            1 = also opt ASR out of IGNORE_TIMER_RESOLUTION (Win11+)
  ZEN_HW_MT_PRIORITY   below_normal idle | below_normal | normal
  ZEN_HW_EMBED_ECOQOS  1            put the embedding process into EcoQoS (efficiency mode)
  ZEN_HW_EMBED_MATCH   ""           cmdline substring that identifies the embedding runner
  ZEN_HW_MT_EXCLUDE    ""           cmdline substring a runner must NOT have to count as MT
"""
from __future__ import annotations

import logging
import os
import struct
import sys
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable

log = logging.getLogger("zen.hw_tune")

# ------------------------------------------------------------------ Win32 constants
PRIORITY_CLASSES = {"idle": 0x40, "below_normal": 0x4000, "normal": 0x20, "above_normal": 0x8000}
FORBIDDEN_CLASSES = {0x80: "high", 0x100: "realtime"}
PROCESS_MODE_BACKGROUND_BEGIN = 0x00100000
THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
THREAD_MODE_BACKGROUND_END = 0x00020000
PROCESS_SET_INFORMATION = 0x0200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_QUERY_INFORMATION = 0x0400
ProcessPowerThrottling = 4
POWER_THROTTLING_CURRENT_VERSION = 1
POWER_THROTTLING_EXECUTION_SPEED = 0x1
POWER_THROTTLING_IGNORE_TIMER_RESOLUTION = 0x4
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
RelationProcessorCore = 0
LTP_PC_SMT = 0x1

SCHEMES = {"381b4222-f694-41f0-9685-ff5bb260df2e": "Balanced",
           "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c": "High performance",
           "a1841308-3541-4fab-bc81-f71556f20b4a": "Power saver",
           "e9a42b02-d5df-448d-aa00-03f14749eb61": "Ultimate Performance"}
# Win11 power modes ("overlays"): GUID_POWER_MODE_* in powrprof.h (PowerGetUserConfiguredACPowerMode docs).
OVERLAYS = {"00000000-0000-0000-0000-000000000000": "Balanced",
            "961cc777-2547-4f9d-8174-7d86181b8a7a": "Best power efficiency",
            "ded574b5-45a0-4f42-8737-46345c09c238": "Best performance"}

ROLES = ("asr", "mt", "embed")
ASR_NAMES = {"whisper-cli.exe", "whisper-server.exe", "whisper-cli", "whisper-server"}
ASR_CMDLINE = ("app.native_worker", "native_worker.py")
MT_NAMES = {"llama-server.exe", "llama-server", "ollama_llama_server.exe", "ollama_llama_server"}


# ------------------------------------------------------------------ results
@dataclass
class Result:
    role: str
    action: str                      # priority | ecoqos | affinity | background | find
    status: str                      # applied | denied | not_found | unsupported | skipped | error
    pid: int | None = None
    name: str | None = None
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Topology:
    logical: int
    physical: int
    cores: tuple[tuple[int, ...], ...]   # logical CPU ids per physical core
    source: str                          # glpi_ex | psutil | os | unknown

    def as_dict(self) -> dict:
        return {"logical": self.logical, "physical": self.physical, "cores": [list(c) for c in self.cores],
                "smt": any(len(c) > 1 for c in self.cores), "source": self.source}


@dataclass
class PowerStatus:
    status: str = "unsupported"          # ok | unsupported | error
    source: str = "unknown"              # ac | battery | unknown
    ac_line_status: int | None = None    # 0 offline, 1 online, 255 unknown
    battery_flag: int | None = None      # 128 = no system battery
    battery_percent: int | None = None   # 255 = unknown
    has_battery: bool | None = None
    scheme_guid: str | None = None
    scheme_name: str = "UNKNOWN"
    overlay_guid: str | None = None
    overlay_name: str = "UNKNOWN"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ThreadPlan:
    asr: int
    mt: int
    draft: int
    embed: int
    policy: str
    oversubscribed: bool
    affinity: dict[str, tuple[int, ...]] | None
    rationale: list[str]
    warnings: list[str]

    def env(self) -> dict[str, str]:
        """Suggested values for the existing knobs (nothing is set here)."""
        return {"BREEZE_ASR_THREADS": str(self.asr), "BREEZE_TRANSLATE_NUM_THREAD": str(self.mt),
                "BREEZE_DRAFT_THREADS": str(self.draft), "ZEN_EMBED_NUM_THREAD": str(self.embed)}

    def args(self) -> dict[str, Any]:
        return {"whisper_cpp": ["-t", str(self.asr)], "llama_server": ["-t", str(self.mt), "-tb", str(self.mt)],
                "ollama_options": {"num_thread": self.mt}, "embed_num_thread": self.embed}

    def as_dict(self) -> dict:
        d = asdict(self)
        d["affinity"] = None if self.affinity is None else {k: list(v) for k, v in self.affinity.items()}
        d["env"], d["args"] = self.env(), self.args()
        return d


# ------------------------------------------------------------------ topology (pure parser + probes)
def parse_core_records(buf: bytes) -> list[tuple[int, int, bool]]:
    """SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX records -> [(group, mask, smt)] per physical core.

    Layout (64-bit): Relationship u32, Size u32, then PROCESSOR_RELATIONSHIP: Flags u8,
    EfficiencyClass u8, Reserved[20], GroupCount u16, GROUP_AFFINITY[GroupCount] at +32
    (Mask u64, Group u16, Reserved u16[3]; 16 bytes each).
    """
    out, off = [], 0
    while off + 8 <= len(buf):
        rel, size = struct.unpack_from("<II", buf, off)
        if size <= 0 or off + size > len(buf):
            break
        if rel == RelationProcessorCore and size >= 48:
            flags = buf[off + 8]
            count = struct.unpack_from("<H", buf, off + 30)[0]
            for i in range(max(1, count)):
                base = off + 32 + 16 * i
                if base + 10 > off + size:
                    break
                mask, group = struct.unpack_from("<QH", buf, base)
                out.append((group, mask, bool(flags & LTP_PC_SMT)))
        off += size
    return out


def cores_from_records(records: Iterable[tuple[int, int, bool]]) -> tuple[tuple[int, ...], ...]:
    cores = []
    for group, mask, _smt in records:
        ids = tuple(group * 64 + bit for bit in range(64) if mask >> bit & 1)
        if ids:
            cores.append(ids)
    return tuple(sorted(cores))


def detect_topology(*, win: "_Win | None" = None, platform: str | None = None, psutil_mod: Any = None) -> Topology:
    platform = platform or sys.platform
    if platform == "win32":
        try:
            cores = cores_from_records(parse_core_records((win or _Win.load()).core_info()))
            if cores:
                return Topology(sum(len(c) for c in cores), len(cores), cores, "glpi_ex")
        except Exception as exc:
            log.info("GetLogicalProcessorInformationEx unavailable (%s); falling back", type(exc).__name__)
    ps = _psutil() if psutil_mod is None else psutil_mod
    logical = os.cpu_count() or 1
    if ps is not None:
        try:
            logical = ps.cpu_count() or logical
            physical = ps.cpu_count(logical=False) or logical
            return Topology(logical, physical, (), "psutil")    # counts only: sibling ids unknown
        except Exception:
            pass
    return Topology(logical, logical, (), "os")


# ------------------------------------------------------------------ power (read-only)
def guid_str(raw: bytes) -> str:
    """A Win32 GUID struct (little-endian Data1-3) as the usual lowercase string."""
    return str(uuid.UUID(bytes_le=bytes(raw[:16])))


def classify_power(ac_line: int | None, battery_flag: int | None) -> tuple[str, bool | None]:
    """(source, has_battery). BatteryFlag 128 = no battery: a desktop is on AC."""
    has_battery = None if battery_flag in (None, 255) else not (battery_flag & 128)
    if has_battery is False:
        return "ac", False
    if ac_line == 1:
        return "ac", has_battery
    if ac_line == 0:
        return "battery", has_battery
    return "unknown", has_battery


def power_status(*, win: "_Win | None" = None, platform: str | None = None) -> PowerStatus:
    platform = platform or sys.platform
    ps = PowerStatus()
    if platform != "win32":
        ps.notes.append("not Windows")
        return ps
    try:
        win = win or _Win.load()
    except Exception as exc:
        ps.status, ps.notes = "error", [f"win32 unavailable: {type(exc).__name__}"]
        return ps
    ps.status = "ok"
    sps = win.system_power_status()
    if sps is None:
        ps.notes.append("GetSystemPowerStatus failed")
    else:
        ps.ac_line_status, ps.battery_flag, ps.battery_percent = sps
        ps.source, ps.has_battery = classify_power(ps.ac_line_status, ps.battery_flag)
        if ps.source == "unknown":
            ps.notes.append("ACLineStatus unknown (255): treat as not plugged in for benchmarks")
    ps.scheme_guid = win.active_scheme()
    ps.scheme_name = SCHEMES.get(ps.scheme_guid or "", "UNKNOWN" if ps.scheme_guid is None else "Custom")
    for fn in ("PowerGetEffectiveOverlayScheme", "PowerGetActualOverlayScheme"):
        g = win.overlay(fn)
        if g is not None:
            ps.overlay_guid, ps.overlay_name = g, OVERLAYS.get(g, "Custom")
            ps.notes.append(f"power mode via {fn}")
            break
    else:
        ps.notes.append("power mode API unavailable (older Windows): UNKNOWN")
    return ps


# ------------------------------------------------------------------ thread budget (pure)
def recommend_threads(physical: int, logical: int | None = None, *, power: str = "ac", live: bool = True,
                      draft_threads: int = 0, embed_enabled: bool = False, policy: str = "split",
                      affinity: bool = False, topology: Topology | None = None) -> ThreadPlan:
    """Thread budget. Heavy threads share DDR4 bandwidth, so SMT siblings are not counted.

    hardware.md §4.1: whisper.cpp / llama.cpp never above physical cores (5800HS medium:
    8 t 8,421 ms vs 16 t 10,889 ms); §5.3 + QA P1-10: ASR + MT + draft <= physical cores
    because the Ollama runner / llama-server cannot reliably be pushed below ASR.
    """
    if policy not in ("split", "asr_first"):
        raise ValueError("policy must be split or asr_first")
    physical = max(1, int(physical))
    logical = max(physical, int(logical or physical))
    draft = max(0, int(draft_threads))
    why, warn = [], []
    if policy == "asr_first":
        asr, mt = physical, min(2, max(1, physical // 3))
        why.append(f"asr_first: ASR uses all {physical} physical cores, MT {mt}; relies on MT priority "
                   "below ASR, so CPU affinity is not applied")
    else:
        mt = 2 if physical >= 6 else 1
        asr = physical - mt - draft
        if asr < 4 <= physical - draft - 1 and mt > 1:
            mt, asr = 1, physical - 1 - draft
            why.append("draft ASR takes a core, MT drops to 1 thread to keep Breeze at >= 4")
        asr = max(1, asr)
        why.append(f"split: ASR {asr} + MT {mt} + draft {draft} on {physical} physical cores "
                   "(SMT siblings left for the OS, event loop and HTTP)")
    total = asr + mt + draft
    over = total > physical
    if over:
        warn.append(f"{total} heavy threads on {physical} physical cores: measure subtitle latency (layer 3)")
    if logical > physical:
        why.append(f"{logical} logical CPUs but only {physical} physical: whisper.cpp/llama.cpp -t counts physical")
    embed = 0
    if embed_enabled and not live and power == "ac":
        embed = max(1, physical - 2)
        why.append(f"not live and on AC: embedding may use {embed} threads at IDLE priority")
    elif embed_enabled:
        why.append("embedding deferred (live session or not on AC)")
    if power == "battery":
        warn.append("on battery: Windows may throttle; RTF measured now is not comparable")
    elif power == "unknown":
        warn.append("power source unknown: treat results as UNKNOWN")
    pins = None
    if affinity and policy == "split" and topology and len(topology.cores) >= 2 and not over:
        cores = list(topology.cores)
        asr_cores = cores[:asr]
        rest = cores[asr:] or cores[-1:]
        pins = {"asr": tuple(i for c in asr_cores for i in c), "mt": tuple(i for c in rest for i in c)}
        why.append("affinity on: ASR and MT pinned to disjoint physical cores (both SMT siblings each)")
    elif affinity:
        warn.append("affinity requested but not applied (needs policy split, a real core map and no oversubscription)")
    return ThreadPlan(asr, mt, draft, embed, policy, over, pins, why, warn)


# ------------------------------------------------------------------ profile
@dataclass
class Profile:
    policy: str = "split"
    affinity: bool = False
    asr_priority: str = "above_normal"
    asr_ecoqos_off: bool = True
    asr_timer_res: bool = False
    mt_priority: str = "below_normal"
    embed_priority: str = "idle"
    embed_ecoqos: bool = True
    embed_match: str = ""
    mt_exclude: str = ""

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Profile":
        env = os.environ if env is None else env

        def pick(name, default, choices):
            raw = (env.get(name) or "").strip().lower() or default
            if raw not in choices:
                log.warning("%s=%r not in %s; using %s", name, raw, choices, default)
                return default
            return raw

        def flag(name, default):
            raw = (env.get(name) or "").strip().lower()
            return default if not raw else raw not in ("0", "off", "false", "no")
        return cls(policy=pick("ZEN_HW_POLICY", "split", ("split", "asr_first")),
                   affinity=flag("ZEN_HW_AFFINITY", False),
                   asr_priority=pick("BREEZE_ASR_PRIORITY", "above_normal", ("normal", "above_normal")),
                   asr_ecoqos_off=flag("BREEZE_ASR_ECOQOS_OFF", True),
                   asr_timer_res=flag("ZEN_HW_TIMER_RES", False),
                   mt_priority=pick("ZEN_HW_MT_PRIORITY", "below_normal", ("idle", "below_normal", "normal")),
                   embed_ecoqos=flag("ZEN_HW_EMBED_ECOQOS", True),
                   embed_match=(env.get("ZEN_HW_EMBED_MATCH") or "").strip(),
                   mt_exclude=(env.get("ZEN_HW_MT_EXCLUDE") or "").strip())


# ------------------------------------------------------------------ process discovery
def _psutil():
    try:
        import psutil
        return psutil
    except ImportError:
        return None


def _role_of(name: str, cmdline: list[str], profile: Profile) -> str | None:
    name = (name or "").lower()
    joined = " ".join(cmdline or []).lower()
    if name in ASR_NAMES or any(k in joined for k in ASR_CMDLINE):
        return "asr"
    if profile.embed_match and profile.embed_match.lower() in joined:
        return "embed"
    is_runner = name in MT_NAMES or (name.startswith("ollama") and " runner" in f" {joined}")
    if is_runner and not (profile.mt_exclude and profile.mt_exclude.lower() in joined):
        return "mt"
    return None


def find_targets(profile: Profile, psutil_mod: Any = None) -> tuple[dict[str, list[tuple[int, str]]], list[Result]]:
    """{role: [(pid, name)]} plus a Result for every candidate whose cmdline could not be read."""
    ps = _psutil() if psutil_mod is None else psutil_mod
    found: dict[str, list[tuple[int, str]]] = {r: [] for r in ROLES}
    notes: list[Result] = []
    if ps is None:
        return found, [Result(r, "find", "unsupported", detail="psutil not installed; pass pids") for r in ROLES]
    for p in ps.process_iter(["pid", "name"]):
        name = (p.info.get("name") or "")
        lname = name.lower()
        if not (lname in ASR_NAMES or lname in MT_NAMES or lname.startswith(("ollama", "python", "whisper", "llama"))):
            continue
        try:
            cmd = p.cmdline()
        except ps.AccessDenied:
            if lname.startswith("ollama") or lname in MT_NAMES:
                notes.append(Result("mt", "find", "denied", p.info.get("pid"), name,
                                    "cmdline unreadable (Ollama service / other user?)"))
            continue
        except (ps.NoSuchProcess, ps.ZombieProcess):
            continue
        role = _role_of(name, cmd, profile)
        if role:
            found[role].append((p.info.get("pid"), name))
    return found, notes


# ------------------------------------------------------------------ win32 wrapper (single mock seam)
class _Win:
    """Thin ctypes layer. Tests pass fake kernel32/powrprof objects and a last_error callable."""

    def __init__(self, kernel32: Any, powrprof: Any = None, last_error: Callable[[], int] | None = None,
                 current_pid: int | None = None):
        import ctypes
        self.ct = ctypes
        self.k32, self.pp = kernel32, powrprof
        self.last_error = last_error or ctypes.get_last_error
        self.current_pid = os.getpid() if current_pid is None else current_pid

    @classmethod
    def load(cls) -> "_Win":
        import ctypes
        from ctypes import wintypes as w
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        for fn, args, res in (("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
                              ("CloseHandle", [w.HANDLE], w.BOOL),
                              ("GetPriorityClass", [w.HANDLE], w.DWORD),
                              ("SetPriorityClass", [w.HANDLE, w.DWORD], w.BOOL),
                              ("GetCurrentProcess", [], w.HANDLE),
                              ("GetCurrentThread", [], w.HANDLE),
                              ("SetThreadPriority", [w.HANDLE, ctypes.c_int], w.BOOL),
                              ("GetProcessAffinityMask", [w.HANDLE, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
                              ("SetProcessAffinityMask", [w.HANDLE, ctypes.c_size_t], w.BOOL),
                              ("GetSystemPowerStatus", [ctypes.c_void_p], w.BOOL),
                              ("GetLogicalProcessorInformationEx", [ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p], w.BOOL)):
            f = getattr(k32, fn)
            f.argtypes, f.restype = args, res
        if hasattr(k32, "SetProcessInformation"):       # Windows 8+
            k32.SetProcessInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
            k32.SetProcessInformation.restype = w.BOOL
        try:
            pp = ctypes.WinDLL("powrprof")
        except OSError:
            pp = None
        return cls(k32, pp)

    # -- process handles
    def open(self, pid: int) -> tuple[Any, str | None]:
        access = PROCESS_SET_INFORMATION | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_QUERY_INFORMATION
        h = self.k32.OpenProcess(access, False, int(pid))
        if h:
            return h, None
        err = self.last_error()
        return None, {ERROR_ACCESS_DENIED: "denied", ERROR_INVALID_PARAMETER: "not_found"}.get(err, f"error:{err}")

    def close(self, h: Any) -> None:
        try:
            self.k32.CloseHandle(h)
        except Exception:
            pass

    def get_priority(self, h) -> int:
        return int(self.k32.GetPriorityClass(h))

    def set_priority(self, h, cls: int) -> bool:
        if cls in FORBIDDEN_CLASSES:
            raise ValueError(f"refusing {FORBIDDEN_CLASSES[cls]} priority")
        return bool(self.k32.SetPriorityClass(h, cls))

    def set_throttling(self, h, control: int, state: int) -> bool | None:
        if not hasattr(self.k32, "SetProcessInformation"):
            return None
        ct = self.ct

        class PPTS(ct.Structure):
            _fields_ = [("Version", ct.c_ulong), ("ControlMask", ct.c_ulong), ("StateMask", ct.c_ulong)]
        s = PPTS(POWER_THROTTLING_CURRENT_VERSION, control, state)
        return bool(self.k32.SetProcessInformation(h, ProcessPowerThrottling, ct.byref(s), ct.sizeof(s)))

    def get_affinity(self, h) -> tuple[int, int] | None:
        ct = self.ct
        proc, system = ct.c_size_t(0), ct.c_size_t(0)
        if not self.k32.GetProcessAffinityMask(h, ct.byref(proc), ct.byref(system)):
            return None
        return proc.value, system.value

    def set_affinity(self, h, mask: int) -> bool:
        return bool(self.k32.SetProcessAffinityMask(h, mask))

    def background_thread(self, begin: bool) -> bool:
        mode = THREAD_MODE_BACKGROUND_BEGIN if begin else THREAD_MODE_BACKGROUND_END
        return bool(self.k32.SetThreadPriority(self.k32.GetCurrentThread(), mode))

    # -- read-only system info
    def system_power_status(self) -> tuple[int, int, int] | None:
        ct = self.ct

        class SPS(ct.Structure):
            _fields_ = [("ACLineStatus", ct.c_ubyte), ("BatteryFlag", ct.c_ubyte), ("BatteryLifePercent", ct.c_ubyte),
                        ("SystemStatusFlag", ct.c_ubyte), ("BatteryLifeTime", ct.c_ulong),
                        ("BatteryFullLifeTime", ct.c_ulong)]
        s = SPS()
        if not self.k32.GetSystemPowerStatus(ct.byref(s)):
            return None
        return s.ACLineStatus, s.BatteryFlag, s.BatteryLifePercent

    def active_scheme(self) -> str | None:
        if self.pp is None or not hasattr(self.pp, "PowerGetActiveScheme"):
            return None
        ct = self.ct
        ptr = ct.c_void_p()
        if self.pp.PowerGetActiveScheme(None, ct.byref(ptr)) != 0 or not ptr.value:
            return None
        try:
            return guid_str(ct.string_at(ptr.value, 16))
        finally:
            if hasattr(self.k32, "LocalFree"):
                self.k32.LocalFree(ptr)

    def overlay(self, fn: str) -> str | None:
        if self.pp is None or not hasattr(self.pp, fn):
            return None
        buf = self.ct.create_string_buffer(16)
        try:
            ok = getattr(self.pp, fn)(buf) == 0
        except Exception:
            return None
        return guid_str(buf.raw) if ok else None

    def core_info(self) -> bytes:
        ct = self.ct
        n = ct.c_ulong(0)
        self.k32.GetLogicalProcessorInformationEx(RelationProcessorCore, None, ct.byref(n))
        if not n.value:
            raise OSError("no size")
        buf = ct.create_string_buffer(n.value)
        if not self.k32.GetLogicalProcessorInformationEx(RelationProcessorCore, buf, ct.byref(n)):
            raise OSError(f"GetLogicalProcessorInformationEx failed ({self.last_error()})")
        return buf.raw[: n.value]


# ------------------------------------------------------------------ apply / revert / status
_ORIGINAL: dict[int, dict[str, Any]] = {}     # pid -> what we changed, for revert()


def _mask(cpus: Iterable[int]) -> int:
    m = 0
    for c in cpus:
        if 0 <= c < 64:                         # single processor group (5600H: 12 logical)
            m |= 1 << c
    return m


def _apply_one(win: _Win, role: str, pid: int, name: str, profile: Profile, pins) -> list[Result]:
    out: list[Result] = []
    h, err = win.open(pid)
    if h is None:
        return [Result(role, "priority", err if err in ("denied", "not_found") else "error", pid, name,
                       "OpenProcess failed" + ("" if err in ("denied", "not_found") else f" ({err})"))]
    try:
        orig = _ORIGINAL.setdefault(pid, {"role": role, "name": name})
        prio_name = {"asr": profile.asr_priority, "mt": profile.mt_priority, "embed": profile.embed_priority}[role]
        want = PRIORITY_CLASSES[prio_name]
        cur = win.get_priority(h)
        if cur in FORBIDDEN_CLASSES:
            out.append(Result(role, "priority", "skipped", pid, name,
                              f"process is {FORBIDDEN_CLASSES[cur]}; left alone (set by someone else)"))
        elif cur == want:
            out.append(Result(role, "priority", "applied", pid, name, f"{prio_name} (unchanged)"))
        else:
            orig.setdefault("priority", cur)
            ok = win.set_priority(h, want)
            out.append(Result(role, "priority", "applied" if ok else "error", pid, name,
                              prio_name if ok else f"SetPriorityClass failed ({win.last_error()})"))
        if role == "asr" and profile.asr_ecoqos_off or role == "embed" and profile.embed_ecoqos:
            control = POWER_THROTTLING_EXECUTION_SPEED
            if role == "asr" and profile.asr_timer_res:
                control |= POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
            state = 0 if role == "asr" else POWER_THROTTLING_EXECUTION_SPEED
            ok = win.set_throttling(h, control, state)
            detail = "EcoQoS off" if role == "asr" else "EcoQoS on"
            if ok is False and control != POWER_THROTTLING_EXECUTION_SPEED:
                ok = win.set_throttling(h, POWER_THROTTLING_EXECUTION_SPEED, state)   # pre-Win11: no timer flag
                detail += " (timer-resolution flag not supported)"
            if ok:
                orig["ecoqos"] = True
            out.append(Result(role, "ecoqos", {None: "unsupported", True: "applied", False: "error"}[ok], pid, name,
                              "SetProcessInformation missing (pre-Windows 8)" if ok is None else detail))
        if role == "embed":
            out.append(Result(role, "background", "unsupported", pid, name,
                              "PROCESS_MODE_BACKGROUND_BEGIN only works on the calling process; "
                              "use enter_background_thread() inside an in-process embedding worker"))
        if pins and role in pins:
            got = win.get_affinity(h)
            mask = _mask(pins[role])
            if got is None:
                out.append(Result(role, "affinity", "error", pid, name, "GetProcessAffinityMask failed"))
            else:
                cur_mask, system = got
                mask &= system
                if not mask:
                    out.append(Result(role, "affinity", "skipped", pid, name, "requested CPUs not in system mask"))
                elif cur_mask == mask:
                    out.append(Result(role, "affinity", "applied", pid, name, f"{mask:#x} (unchanged)"))
                else:
                    orig.setdefault("affinity", cur_mask)
                    ok = win.set_affinity(h, mask)
                    out.append(Result(role, "affinity", "applied" if ok else "error", pid, name, f"{mask:#x}"))
    except ValueError as exc:
        out.append(Result(role, "priority", "error", pid, name, str(exc)))
    finally:
        win.close(h)
    return out


def apply_profile(profile: Profile | None = None, *, pids: dict[str, list[int]] | None = None,
                  env: dict | None = None, platform: str | None = None, win: _Win | None = None,
                  psutil_mod: Any = None, topology: Topology | None = None, draft_threads: int = 0) -> list[Result]:
    """Tune the CPU-heavy processes. Safe to call again (e.g. after the Ollama runner restarts)."""
    profile = profile or Profile.from_env(env)
    platform = platform or sys.platform
    if platform != "win32":
        return [Result(r, "priority", "unsupported", detail="not Windows") for r in ROLES]
    try:
        win = win or _Win.load()
    except Exception as exc:
        return [Result(r, "priority", "error", detail=f"win32 unavailable: {type(exc).__name__}") for r in ROLES]
    results: list[Result] = []
    if pids is None:
        found, results = find_targets(profile, psutil_mod)
    else:
        found = {r: [(int(p), "") for p in pids.get(r, [])] for r in ROLES}
    pins = None
    if profile.affinity:
        topo = topology or detect_topology(win=win, platform=platform, psutil_mod=psutil_mod)
        pins = recommend_threads(topo.physical, topo.logical, policy=profile.policy, affinity=True, topology=topo,
                                 draft_threads=draft_threads).affinity
        if pins is None:
            results.append(Result("asr", "affinity", "skipped", detail="no usable core map or policy asr_first"))
    for role in ROLES:
        if not found.get(role):
            if not any(r.role == role and r.action == "find" for r in results):
                results.append(Result(role, "find", "not_found",
                                      detail="set ZEN_HW_EMBED_MATCH to tune embedding" if role == "embed" else ""))
            continue
        for pid, name in found[role]:
            results.extend(_apply_one(win, role, pid, name, profile, pins))
    for r in results:
        log.info("hw_tune %s %s pid=%s %s: %s %s", r.role, r.action, r.pid, r.name or "", r.status, r.detail)
    return results


def revert(*, platform: str | None = None, win: _Win | None = None) -> list[Result]:
    """Restore priority / affinity we changed and hand EcoQoS back to the system."""
    if (platform or sys.platform) != "win32":
        return [Result("all", "revert", "unsupported", detail="not Windows")]
    win = win or _Win.load()
    out = []
    for pid, orig in list(_ORIGINAL.items()):
        role, name = orig.get("role", "?"), orig.get("name")
        h, err = win.open(pid)
        if h is None:
            out.append(Result(role, "revert", "not_found" if err == "not_found" else (err or "error"), pid, name))
            _ORIGINAL.pop(pid, None)
            continue
        try:
            if "priority" in orig:
                out.append(Result(role, "priority", "applied" if win.set_priority(h, orig["priority"]) else "error",
                                  pid, name, f"restored {orig['priority']:#x}"))
            if orig.get("ecoqos"):
                ok = win.set_throttling(h, 0, 0)      # ControlMask 0: let Windows decide again
                out.append(Result(role, "ecoqos", {None: "unsupported", True: "applied", False: "error"}[ok], pid,
                                  name, "system managed"))
            if "affinity" in orig:
                out.append(Result(role, "affinity", "applied" if win.set_affinity(h, orig["affinity"]) else "error",
                                  pid, name, f"restored {orig['affinity']:#x}"))
        finally:
            win.close(h)
        _ORIGINAL.pop(pid, None)
    return out


def enter_background_thread(begin: bool = True, *, platform: str | None = None, win: _Win | None = None) -> Result:
    """For an in-process embedding worker: THREAD_MODE_BACKGROUND_BEGIN/END on the calling thread."""
    if (platform or sys.platform) != "win32":
        return Result("embed", "background", "unsupported", detail="not Windows")
    try:
        ok = (win or _Win.load()).background_thread(begin)
    except Exception as exc:
        return Result("embed", "background", "error", detail=type(exc).__name__)
    return Result("embed", "background", "applied" if ok else "error", detail="begin" if begin else "end")


def status(*, env: dict | None = None, platform: str | None = None, win: _Win | None = None,
           psutil_mod: Any = None, live: bool = True, draft_threads: int = 0) -> dict:
    """Read-only snapshot: topology, power, recommended plan, what apply_profile changed."""
    platform = platform or sys.platform
    profile = Profile.from_env(env)
    if platform == "win32" and win is None:
        try:
            win = _Win.load()
        except Exception:
            win = None
    topo = detect_topology(win=win, platform=platform, psutil_mod=psutil_mod)
    power = power_status(win=win, platform=platform) if win is not None or platform != "win32" else PowerStatus(
        status="error", notes=["win32 unavailable"])
    plan = recommend_threads(topo.physical, topo.logical, power=power.source if power.status == "ok" else "ac",
                             live=live, draft_threads=draft_threads, policy=profile.policy,
                             affinity=profile.affinity, topology=topo)
    if power.overlay_name == "Best power efficiency" or power.scheme_name == "Power saver":
        plan.warnings.append(f"Windows power mode is '{power.overlay_name}' / plan '{power.scheme_name}': "
                             "ASR will be slower; switch to Balanced or Best performance before a session "
                             "(zen-bridge never changes it)")
    return {"platform": platform, "supported": platform == "win32", "profile": asdict(profile),
            "topology": topo.as_dict(), "power": power.as_dict(), "plan": plan.as_dict(),
            "changed": {str(k): {kk: vv for kk, vv in v.items()} for k, v in _ORIGINAL.items()}}


if __name__ == "__main__":       # read-only: python -m app.hw_tune
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=1))
