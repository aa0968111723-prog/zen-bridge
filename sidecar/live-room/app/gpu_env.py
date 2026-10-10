"""round4 #9 (D7): Vulkan safety for the native ASR / llama processes we start.

* Third-party *implicit* Vulkan layers (OBS game capture, Steam overlay, RTSS, ...) are a
  common cause of Vulkan crashes on Windows. Every worker we spawn gets
  ``VK_LOADER_LAYERS_DISABLE=~implicit~`` unless the user already set that variable or
  opted out with ``BREEZE_VK_IMPLICIT_LAYERS=1``. On a CPU-only build the variable is inert.
* ggml prints the device's ``shared memory`` size at start-up. Adrenalin 26.5.2 reports
  32768 instead of 65536 on 5800H/Vega (llama.cpp #26163, ~17 % slower), so we keep the
  value from the worker's stderr and log it as ``BREEZE_GGML shared_memory=<bytes>``.
"""
from __future__ import annotations

import os
import re
from typing import Mapping

VK_DISABLE_VAR = "VK_LOADER_LAYERS_DISABLE"
VK_DISABLE_VALUE = "~implicit~"
_SHARED = re.compile(r"shared[ _]memory\b[^0-9]{0,8}(\d+)", re.I)


def worker_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    if (env.get("BREEZE_VK_IMPLICIT_LAYERS") or "").strip() == "1":
        return env
    env.setdefault(VK_DISABLE_VAR, VK_DISABLE_VALUE)
    return env


def shared_memory_from(line: str) -> int | None:
    """``ggml_vulkan: 0 = AMD Radeon(TM) Graphics ... | shared memory: 32768 | ...`` -> 32768."""
    m = _SHARED.search(line or "")
    return int(m.group(1)) if m else None
