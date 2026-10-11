"""Native (pywhispercpp) worker tuning — optimization-round2 §0/§2 (ASR RTF ~7 is config).

Everything is env-configurable; defaults are the research recommendations:

  BREEZE_ASR_FLASH_ATTN=0          flash attention off (CJK ~15% faster on Breeze, better punct.)
  BREEZE_ASR_TEMPERATURE_INC=0     no temperature fallback (= whisper-cli -nf)
  BREEZE_ASR_BEST_OF=0 -> 1        greedy best_of 1 (= -bo 1); an explicit value still wins
  BREEZE_ASR_AUDIO_CONTEXT=0       0 = derive from clip length (native worker only), else fixed
  BREEZE_ASR_AUDIO_CONTEXT_MIN=640 floor for the derived value (6 s clip -> 640)
  BREEZE_ASR_MAX_TOKENS=0          0 = derive from clip length, else fixed cap
  BREEZE_ASR_REPEAT_GUARD=1        a repetition loop is retried once with audio_ctx 1500
  BREEZE_ASR_TIMINGS=1             print_timings() after every clip + system_info() at start
  BREEZE_ASR_PRIORITY=above_normal worker process priority (normal | above_normal), Windows only
  BREEZE_ASR_ECOQOS_OFF=1          opt the worker out of EcoQoS power throttling, Windows only
  BREEZE_ASR_LOG=<path>|off        worker stderr -> rotating log (default <data>/logs/asr-worker.log)
"""
from __future__ import annotations

import logging
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

FULL_CTX = 1500            # 30 s window = 1500 encoder positions (50 per second)
CTX_STEP = 64

log = logging.getLogger("breeze.asr")


def _int(env, name, default, lo, hi):
    raw = (env.get(name) or "").strip()
    try:
        v = int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default
    return max(lo, min(hi, v))


def _float(env, name, default, lo, hi):
    raw = (env.get(name) or "").strip()
    try:
        v = float(raw) if raw else default
    except ValueError:
        return default
    if not math.isfinite(v):
        return default
    return max(lo, min(hi, v))


def _bool(env, name, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "off", "false", "no")


@dataclass(frozen=True)
class NativeTuning:
    flash_attn: bool = False
    temperature_inc: float = 0.0
    ctx_min: int = 640
    max_tokens: int = 0
    repeat_guard: bool = True
    timings: bool = True
    priority: str = "above_normal"
    ecoqos_off: bool = True

    @classmethod
    def from_env(cls, env=None) -> "NativeTuning":
        env = os.environ if env is None else env
        prio = (env.get("BREEZE_ASR_PRIORITY") or "above_normal").strip().lower()
        if prio not in ("normal", "above_normal"):
            log.warning("BREEZE_ASR_PRIORITY=%s unknown; using above_normal", prio)
            prio = "above_normal"
        return cls(
            flash_attn=_bool(env, "BREEZE_ASR_FLASH_ATTN", False),
            temperature_inc=_float(env, "BREEZE_ASR_TEMPERATURE_INC", 0.0, 0.0, 1.0),
            ctx_min=_int(env, "BREEZE_ASR_AUDIO_CONTEXT_MIN", 640, 128, FULL_CTX),
            max_tokens=_int(env, "BREEZE_ASR_MAX_TOKENS", 0, 0, 448),
            repeat_guard=_bool(env, "BREEZE_ASR_REPEAT_GUARD", True),
            timings=_bool(env, "BREEZE_ASR_TIMINGS", True),
            priority=prio,
            ecoqos_off=_bool(env, "BREEZE_ASR_ECOQOS_OFF", True),
        )

    def argv(self) -> list[str]:
        return ["--flash-attn", "1" if self.flash_attn else "0",
                "--temperature-inc", repr(self.temperature_inc),
                "--context-min", str(self.ctx_min), "--max-tokens", str(self.max_tokens),
                "--repeat-guard", "1" if self.repeat_guard else "0",
                "--timings", "1" if self.timings else "0",
                "--priority", self.priority, "--ecoqos-off", "1" if self.ecoqos_off else "0"]


def audio_ctx_for(seconds: float, fixed: int = 0, ctx_min: int = 640) -> int:
    """fixed > 0 wins. Otherwise 50 positions/s plus 25% and one step of headroom, rounded up
    to 64, never below ctx_min, never above 1500 (6 s -> 640, 12 s -> 832, 30 s -> 1500)."""
    if fixed > 0:
        return min(FULL_CTX, fixed)
    need = max(0.0, float(seconds or 0)) * 50 * 1.25 + CTX_STEP
    ctx = int(math.ceil(need / CTX_STEP) * CTX_STEP)
    return max(min(ctx_min, FULL_CTX), min(FULL_CTX, ctx))


def max_tokens_for(seconds: float, fixed: int = 0) -> int:
    """Cap decoded tokens so a loop cannot run to 448: ~10 tokens/s + 8 (6 s -> 68)."""
    if fixed > 0:
        return fixed
    return max(32, min(224, int(math.ceil(max(0.0, float(seconds or 0)) * 10)) + 8))


_SPACE = re.compile(r"\s+")


def repetition_loop(text: str, *, min_repeats: int = 3, max_unit: int = 12) -> bool:
    """True when some 1..max_unit-char unit repeats back-to-back >= min_repeats times and the
    run covers at least 40% of the text (a whisper loop, not '謝謝謝謝' once)."""
    t = _SPACE.sub("", text or "")
    if len(t) < 6:
        return False
    for unit in range(1, max_unit + 1):
        if unit * min_repeats > len(t):
            break
        pat = re.compile(r"(.{%d})\1{%d,}" % (unit, min_repeats - 1), re.DOTALL)
        for m in pat.finditer(t):
            if len(m.group(0)) >= max(6, 0.4 * len(t)):
                return True
    return False


def default_log_path(env=None) -> Path | None:
    env = os.environ if env is None else env
    raw = (env.get("BREEZE_ASR_LOG") or "").strip()
    if raw.lower() in ("off", "0", "none"):
        return None
    if raw:
        return Path(raw)
    base = (env.get("ZEN_DATA_DIR") or "").strip()
    if base:
        root = Path(base)
    elif (env.get("LOCALAPPDATA") or "").strip():
        root = Path(env["LOCALAPPDATA"]) / "ZenBridge"
    else:
        root = Path.home() / ".local" / "share" / "ZenBridge"
    return root / "logs" / "asr-worker.log"


def rotating_logger(path: Path | None, name: str = "breeze.asr.worker") -> logging.Logger | None:
    """5 MB x 3 rotating file for the worker's stderr (whisper timings, system_info, errors).
    The worker never prints transcripts to stderr (print_realtime/progress are off)."""
    if path is None:
        return None
    from logging.handlers import RotatingFileHandler
    lg = logging.getLogger(f"{name}.{abs(hash(str(path)))}")
    if not lg.handlers:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            h = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        except OSError:
            log.warning("cannot open ASR worker log %s; worker stderr is discarded", path)
            return None
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False
    return lg


# ---------------------------------------------------------------- process priority (Windows)
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
NORMAL_PRIORITY_CLASS = 0x00000020
ProcessPowerThrottling = 4
PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def boost_current_process(priority: str = "above_normal", ecoqos_off: bool = True, *, platform: str | None = None,
                          kernel32=None) -> dict:
    """Raise this process and opt it out of EcoQoS. A no-op (and never raises) off Windows."""
    platform = platform or sys.platform
    out = {"platform": platform, "priority": None, "ecoqos_off": None}
    if platform != "win32":
        return out
    try:
        import ctypes
        from ctypes import wintypes
        k32 = kernel32 or ctypes.windll.kernel32
        try:
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
        except AttributeError:
            pass
        handle = k32.GetCurrentProcess()
        cls = ABOVE_NORMAL_PRIORITY_CLASS if priority == "above_normal" else NORMAL_PRIORITY_CLASS
        out["priority"] = bool(k32.SetPriorityClass(handle, cls))
        if ecoqos_off:
            class PPTS(ctypes.Structure):
                _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                            ("StateMask", wintypes.ULONG)]
            state = PPTS(PROCESS_POWER_THROTTLING_CURRENT_VERSION, PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)
            out["ecoqos_off"] = bool(k32.SetProcessInformation(handle, ProcessPowerThrottling,
                                                              ctypes.byref(state), ctypes.sizeof(state)))
    except Exception as exc:          # pragma: no cover - depends on the OS build
        out["error"] = type(exc).__name__
    return out
