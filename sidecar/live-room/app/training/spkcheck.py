"""課後聲紋檢查（CPU、上課結束後才跑，不在直播時跑）：這段是不是主講者？

後端（自動選）：
* ``sherpa``：sherpa-onnx ``SpeakerEmbeddingExtractor`` ＋ 3D-Speaker CAM++ 中文模型
  （``3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx``，約 28 MB，Apache-2.0）。需要 ``pip install sherpa-onnx``
  與模型檔，放在 ``ZEN_SPEAKER_MODEL`` 指定的路徑、``training\\models\\`` 或 App 的 ``models\\speaker\\``。
* ``mfcc``：沒有模型時的退路，只用 numpy（MFCC 平均＋標準差，整場做標準化）。比較粗，所以門檻比較保守，
  不確定的段落會留給人看；搭配主持頁的「提問中」按鍵效果最好。

做法：用主講者「這堂課前幾分鐘」（排除提問中時段）的段落建一個聲紋中心；若之前已核准過他的片段，
就再混入儲存的聲紋（speaker_profiles，只存有同意的講者）。第二輪再用整場「確定是他」的段落重算中心
（主講者通常講了大半堂課）。每段算 cosine：低於 EXCLUDE_BELOW → 排除；介於中間 → 需要人看；
高於 CONFIDENT_AT → 可以一鍵核准。其他人的聲紋不保存。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODEL_NAMES = ("3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx", "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
# cosine 門檻（離線實測：CAM++ 同一講者 0.52–0.97、不同人 ≤ 0.34；MFCC 退路重疊多，所以大多留給人看）
# 另外在瀏覽器（Opus 壓縮＋同一支麥克風）實測：同一人 0.78–0.88、不同人 0.44–0.56 → 門檻取保守值。
# 收過幾堂真的課之後可用 ZEN_SPK_EXCLUDE / ZEN_SPK_CONFIDENT 調整。
EXCLUDE_BELOW = {"sherpa": float(os.environ.get("ZEN_SPK_EXCLUDE") or 0.50), "mfcc": -0.05}
CONFIDENT_AT = {"sherpa": float(os.environ.get("ZEN_SPK_CONFIDENT") or 0.70), "mfcc": 0.50}
LAST_ERROR = {"sherpa": None}


def find_model(root: Path | None = None) -> Path | None:
    env = (os.environ.get("ZEN_SPEAKER_MODEL") or "").strip()
    cands = [Path(env)] if env else []
    bases = []
    if root:
        bases.append(Path(root) / "models")
    bases.append(Path(__file__).resolve().parents[2] / "models" / "speaker")
    for b in bases:
        cands += [b / n for n in MODEL_NAMES]
        if b.exists():
            cands += sorted(b.glob("*.onnx"))
    return next((p for p in cands if p.is_file()), None)


class _Sherpa:
    name = "sherpa"

    def __init__(self, model: Path):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=1, debug=False, provider="cpu")
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)

    def embed(self, x: np.ndarray) -> np.ndarray | None:
        s = self.ex.create_stream()
        s.accept_waveform(16000, x.astype(np.float32))
        s.input_finished()
        if not self.ex.is_ready(s):
            return None
        return np.asarray(self.ex.compute(s), dtype=np.float32)


class _Mfcc:
    name = "mfcc"

    def __init__(self):
        n_fft, n_mel, sr = 512, 40, 16000
        mel = lambda f: 2595 * np.log10(1 + f / 700.0)  # noqa: E731
        imel = lambda m: 700 * (10 ** (m / 2595.0) - 1)  # noqa: E731
        pts = imel(np.linspace(mel(80), mel(7600), n_mel + 2))
        bins = np.floor((n_fft + 1) * pts / sr).astype(int)
        fb = np.zeros((n_mel, n_fft // 2 + 1), dtype=np.float32)
        for i in range(1, n_mel + 1):
            a, b, c = bins[i - 1], bins[i], bins[i + 1]
            if b > a:
                fb[i - 1, a:b] = (np.arange(a, b) - a) / (b - a)
            if c > b:
                fb[i - 1, b:c] = (c - np.arange(b, c)) / (c - b)
        self.fb = fb
        k = np.arange(n_mel)
        self.dct = np.cos(np.pi / n_mel * (k[None, :] + 0.5) * np.arange(1, 21)[:, None]).astype(np.float32)
        self.win = np.hamming(400).astype(np.float32)

    def embed(self, x: np.ndarray) -> np.ndarray | None:
        if len(x) < 16000:
            return None
        x = np.append(x[0], x[1:] - 0.97 * x[:-1])
        n = 1 + (len(x) - 400) // 160
        idx = np.arange(400)[None, :] + 160 * np.arange(n)[:, None]
        fr = x[idx] * self.win
        energy = (fr ** 2).mean(axis=1)
        keep = energy >= np.percentile(energy, 40)            # speech-ish frames only
        spec = np.abs(np.fft.rfft(fr[keep], 512)) ** 2
        logmel = np.log(spec @ self.fb.T + 1e-8)
        mf = logmel @ self.dct.T
        d = np.diff(mf, axis=0)
        return np.concatenate([mf.mean(0), mf.std(0), d.std(0)]).astype(np.float32)


def backend(root: Path | None = None, prefer: str | None = None):
    prefer = prefer or os.environ.get("ZEN_SPEAKER_BACKEND") or "auto"
    if prefer in ("auto", "sherpa"):
        m = find_model(root)
        if m:
            try:
                return _Sherpa(m)
            except Exception as exc:
                LAST_ERROR["sherpa"] = f"{type(exc).__name__}: {exc}"[:200]
                import logging
                logging.getLogger("zen.training").warning("speaker model unavailable, using MFCC fallback: %s", exc)
                if prefer == "sherpa":
                    raise
        elif prefer == "sherpa":
            raise RuntimeError("找不到聲紋模型（ZEN_SPEAKER_MODEL）")
    return _Mfcc()


def _unit(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


@dataclass
class Result:
    backend: str
    scores: list            # cosine per segment (None = too short to judge)
    threshold: float
    confident_at: float
    enrolled: int
    centroid: np.ndarray | None


def check(pcms: list[np.ndarray], enroll: list[bool], *, be=None, profile: np.ndarray | None = None) -> Result:
    """pcms: float32 mono 16 kHz per segment; enroll: which segments may build the lecturer's voiceprint."""
    be = be or backend()
    embs = [be.embed(x) if x is not None and len(x) >= 8000 else None for x in pcms]
    ok = [i for i, e in enumerate(embs) if e is not None]
    if not ok:
        return Result(be.name, [None] * len(pcms), 1.0, 1.0, 0, None)
    M = np.stack([embs[i] for i in ok])
    if be.name == "mfcc":                                   # per-session standardisation
        M = (M - M.mean(0)) / (M.std(0) + 1e-6)
    M = M / (np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    pos = {i: k for k, i in enumerate(ok)}
    en = [pos[i] for i in ok if enroll[i]] or list(range(min(len(ok), 10)))
    cen = _unit(M[en].mean(0))
    s = M @ cen
    good = [k for k in en if s[k] >= np.median(s[en])] or en       # drop the host's intro etc.
    cen = _unit(M[good].mean(0))
    if profile is not None and be.name == "sherpa" and profile.shape == cen.shape:
        cen = _unit(0.5 * cen + 0.5 * _unit(profile))
    s = M @ cen
    # pass 2: the lecturer talks most of the class, so re-centre on every confident segment
    conf = [k for k in range(len(ok)) if s[k] >= CONFIDENT_AT[be.name]]
    if len(conf) >= max(3, len(good)):
        cen2 = _unit(M[conf].mean(0))
        if profile is not None and be.name == "sherpa" and profile.shape == cen2.shape:
            cen2 = _unit(0.5 * cen2 + 0.5 * _unit(profile))
        s = M @ cen2
        good = [k for k in range(len(ok)) if s[k] >= CONFIDENT_AT[be.name]] or good
    thr = EXCLUDE_BELOW[be.name]
    scores = [None] * len(pcms)
    for i, k in pos.items():
        scores[i] = float(s[k])
    raw_cen = None
    if be.name == "sherpa":
        raw = np.stack([embs[ok[k]] for k in good])
        raw_cen = raw.mean(0).astype(np.float32)
    return Result(be.name, scores, float(thr), float(CONFIDENT_AT[be.name]), len(good), raw_cen)
