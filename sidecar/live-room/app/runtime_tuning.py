"""Env-tunable scheduling knobs for local translation / embeddings (hardware.md §5, §7).

All values come from the environment (not Settings, so .env.example contract tests are
unchanged) and have conservative defaults for a Ryzen 5 5600H with no NVIDIA GPU:

  BREEZE_TRANSLATE_QUEUE          4      queue max (existing Settings knob, unchanged)
  BREEZE_TRANSLATE_STALE_S        8      oldest waiting line older than this ...
  BREEZE_TRANSLATE_STALE_POLICY   skip   ... is skipped (skipped_backlog) or merged into the next request
  BREEZE_TRANSLATE_PRIORITY       below_normal  translate worker threads (idle|below_normal|normal)
  BREEZE_TRANSLATE_NUM_THREAD     4      Ollama options.num_thread (native API only)
  BREEZE_TRANSLATE_NUM_CTX        2048   Ollama options.num_ctx
  BREEZE_TRANSLATE_KEEP_ALIVE     -1     Ollama keep_alive (model stays resident)
  ZEN_EMBED_IDLE_S                10     embeddings only after ASR+translate queues idle this long ...
  ZEN_EMBED_BATCH                 16     ... in batches of this size, or after the session ended
  ZEN_EMBED_NUM_THREAD            2      Ollama options.num_thread for embeddings
"""
from __future__ import annotations

import logging
import os
import sys

log = logging.getLogger("breeze.tuning")

STALE_POLICIES = ("skip", "merge", "off")
PRIORITIES = ("idle", "below_normal", "normal")


def env_float(name: str, default: float, env: dict | None = None, *, minimum: float = 0.0) -> float:
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default
    return max(minimum, value)


def env_int(name: str, default: int, env: dict | None = None, *, minimum: int = 0) -> int:
    return int(env_float(name, float(default), env, minimum=float(minimum)))


def env_choice(name: str, default: str, choices: tuple, env: dict | None = None) -> str:
    env = os.environ if env is None else env
    raw = (env.get(name) or "").strip().lower() or default
    if raw not in choices:
        log.warning("%s=%r not in %s; using %s", name, raw, choices, default)
        return default
    return raw


def stale_s(env=None) -> float:
    return env_float("BREEZE_TRANSLATE_STALE_S", 8.0, env)


def stale_policy(env=None) -> str:
    return env_choice("BREEZE_TRANSLATE_STALE_POLICY", "skip", STALE_POLICIES, env)


def translate_priority(env=None) -> str:
    return env_choice("BREEZE_TRANSLATE_PRIORITY", "below_normal", PRIORITIES, env)


def lower_current_thread(priority: str) -> bool:
    """Best effort: drop the calling thread below the ASR/event-loop threads. Never raises."""
    if priority == "normal":
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            level = {"below_normal": -1, "idle": -15}[priority]   # THREAD_PRIORITY_*
            k32 = ctypes.windll.kernel32
            return bool(k32.SetThreadPriority(k32.GetCurrentThread(), level))
        if sys.platform.startswith("linux") and hasattr(os, "setpriority"):
            nice = {"below_normal": 5, "idle": 19}[priority]
            os.setpriority(os.PRIO_PROCESS, threading_native_id(), nice)   # Linux: per-thread
            return True
    except Exception:
        log.debug("could not lower thread priority", exc_info=True)
    return False


def threading_native_id() -> int:
    import threading
    return threading.get_native_id()


def translate_thread_initializer():
    prio = translate_priority()

    def init() -> None:
        lower_current_thread(prio)
    return init
