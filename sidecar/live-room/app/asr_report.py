"""Report the engine under a fast-final wrapper, not the wrapper itself."""
from __future__ import annotations


def reported_asr(engine):
    from app.fast_final import FastFinalAsr
    return engine.primary if isinstance(engine, FastFinalAsr) else engine


def asr_mode_label(engine) -> str:
    from app.asr import ResidentAsr
    from app.asr_gpu import GpuWithCpuFallback
    from app.native_asr import NativeResidentAsr
    reported = reported_asr(engine)
    if isinstance(reported, GpuWithCpuFallback):
        return "native-gpu" if reported.on_gpu else "native"
    if isinstance(reported, NativeResidentAsr):
        return "native"
    if isinstance(reported, ResidentAsr):
        return "resident"
    return "cli"
