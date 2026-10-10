"""Deterministic recommendations only; no hardware probing or runtime changes.

Existing env keys are returned at the top level as strings. Unsupported knobs
are isolated in ``proposed_new_keys`` and must not be applied as runtime config.
"""
from __future__ import annotations


def suggest(hw: dict, target_lang: str, *, on_battery: bool | None = None) -> dict:
    """Suggest settings for one ``en`` or ``ja`` session without mutating inputs.

    At least two physical cores are needed for a positive ASR/MT thread budget.
    Reserve one core for an optional draft model where possible; its one-thread
    setting does not enable a draft model. On two cores, draft must run serially.
    ``on_battery`` overrides ``on_ac_power``; unknown power is not assumed AC.
    Quantisation and backend keys are proposals, not existing runtime controls.
    """
    if target_lang not in ("en", "ja"):
        raise ValueError("target_lang must be 'en' or 'ja'")
    cores = hw.get("physical_cores")
    if isinstance(cores, bool) or not isinstance(cores, int) or cores < 2:
        raise ValueError("physical_cores must be an integer >= 2")

    warnings = []
    reasons = []
    battery = on_battery if on_battery is not None else hw.get("on_ac_power") is False
    budget = cores - 1 if cores >= 3 else cores
    mt_threads = 2 if budget >= 7 else 1
    asr_threads = min(5, budget - mt_threads)
    if battery:
        asr_threads = max(1, asr_threads - 1)
        mt_threads = 1
        warnings.append("On battery/not on AC: latency measurements are not trustworthy.")
        reasons.append("Lower thread counts reduce battery power and thermal pressure.")
    elif on_battery is None and hw.get("on_ac_power") is None:
        warnings.append("Power source is unknown; verify AC power before measuring latency.")
    reasons.append(
        f"Use {asr_threads} ASR + {mt_threads} MT threads within {cores} physical cores; "
        "logical SMT threads are not extra physical cores."
    )
    reasons.append("Keep optional draft ASR at one thread; no draft model is enabled here.")
    if cores == 2:
        warnings.append("On two cores, run optional draft ASR serially, not alongside ASR and MT.")

    quantisation = "Q4_K_M"
    if target_lang == "ja":
        if hw.get("ram_available_gb", 0) >= 6:
            quantisation = "Q8_0"
            reasons.append("Japanese uses Q8_0 with at least 6 GB of available RAM.")
        else:
            warnings.append("Japanese Q8_0 needs at least 6 GB available RAM; suggest Q4_K_M.")
    else:
        reasons.append("English uses Q4_K_M to keep local MT memory usage low.")

    backend = "vulkan" if hw.get("vulkan_devices") else "cpu"
    reasons.append(f"Suggest {backend} ASR offload based only on reported Vulkan devices.")
    reasons.append("Quantisation and backend selection are proposed keys, not runtime switches.")
    reasons.append("Embeddings remain off to preserve the live ASR/MT budget.")
    return {
        "BREEZE_ASR_THREADS": str(asr_threads),
        "BREEZE_DRAFT_THREADS": "1",
        "BREEZE_TRANSLATE_NUM_THREAD": str(mt_threads),
        "ZEN_EMBED": "0",
        "proposed_new_keys": {
            "BREEZE_TRANSLATE_QUANTIZATION": quantisation,
            "BREEZE_ASR_BACKEND": backend,
        },
        "reasons": reasons,
        "warnings": warnings,
    }
