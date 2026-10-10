"""round4 #2: T4 subcommand (rtf_check.py t4 / det) with fake ASR, fake draft and fake llama-server."""
import json
import time
import wave
from pathlib import Path

import pytest

from app.asr import AsrResult
from app.draft_asr import DraftPartial
from app.translate import TranslateResult
from tools import rtf_check, t4

EXAMPLE = Path(__file__).resolve().parents[1] / "tools" / "t4.example.json"


def _wav(path: Path, seconds: float = 0.5) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x01\x00" * int(16000 * seconds))
    return path


@pytest.fixture
def clips(tmp_path):
    a, r = tmp_path / "audio", tmp_path / "ref"
    a.mkdir()
    r.mkdir()
    for i in range(3):
        _wav(a / f"{i:03d}.wav")
        (r / f"{i:03d}.txt").write_text("我們開始上課", encoding="utf-8")
    return a, r


class FakeAsr:
    def __init__(self, row):
        self.row, self.closed, self.last_stats = row, False, {}

    def transcribe(self, wav, prompt=""):
        time.sleep(0.01)
        self.last_stats = {"fallbacks": 1 if not self.row.get("no_fallback") else 0, "encode_ms": 5, "decode_ms": 3}
        return AsrResult(ok=True, text="我們開始上課")

    def close(self):
        self.closed = True


class FakeDraft:
    def __init__(self, unstable=False):
        self.unstable, self.n = unstable, 0

    def reset(self):
        self.n += 1
        self.fed = 0

    def feed(self, pcm):
        self.fed += len(pcm)
        if self.fed >= 3200 and self.fed - len(pcm) < 3200:
            return [DraftPartial("我們", False, 100)]
        return []

    def finish(self):
        return [DraftPartial("我們開始" + ("屏ugh" * (self.n % 2) if self.unstable else ""), True, 500)]


class FakeMt:
    def translate(self, zh, tgt_lang="en"):
        time.sleep(0.005)
        return TranslateResult("Let us begin." if tgt_lang == "en" else "始めましょう。", "ok", completion_tokens=4)


def engines(**kw):
    base = dict(asr=FakeAsr, draft=lambda row: FakeDraft(), mt=lambda row: (FakeMt(), None),
                sleep=lambda s: time.sleep(min(s, 0.01)), power_plan=lambda: "Balanced")
    base.update(kw)
    return t4.Engines(**base)


def small_plan():
    p = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    p.update(segments=3, reps=1, cooldown_s=0, segment_s=0.05)
    p["concurrent"]["minutes"] = 0.005
    return p


def test_example_plan_validates_and_expands_env():
    plan = t4.load_plan(EXAMPLE, env={"LOCALAPPDATA": r"C:\U\AppData\Local", "MODELS": r"D:\m"})
    assert len(plan["asr"]) == 6 and len(plan["draft"]) == 2 and len(plan["mt"]) == 2
    assert plan["mt"][0]["gguf"].startswith("D:\\m/")
    assert plan["draft"][0]["dir"].startswith("C:\\U\\AppData\\Local/ZenBridge/models/draft/")


@pytest.mark.parametrize("mutate,msg", [
    (lambda p: p.pop("segments"), "缺少欄位 segments"),
    (lambda p: p["asr"][0].pop("model"), "缺少欄位 model"),
    (lambda p: p["asr"][0].update(bogus=1), "未知欄位 bogus"),
    (lambda p: p.update(extra=1), "未知欄位 extra"),
    (lambda p: p["concurrent"]["pairs"].append(["nope", "xasr-int8-t1", "hymt2-q4"]), "id 不存在：nope"),
    (lambda p: p["mt"][0].update(langs=["fr"]), "en／ja"),
    (lambda p: p["asr"].append(dict(p["asr"][0])), "id 重複"),
    (lambda p: p.update(reps=0), ">= 1"),
])
def test_plan_errors(mutate, msg):
    p = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    mutate(p)
    with pytest.raises(t4.PlanError, match=msg):
        t4.load_plan(p)


def test_whole_flow_with_fakes_is_fast_and_rows_complete(clips, tmp_path):
    a, r = clips
    out = tmp_path / "t4.jsonl"
    t0 = time.monotonic()
    code, rows = t4.run_t4(t4.load_plan(small_plan()), a, r, out, engines())
    assert time.monotonic() - t0 < 5
    assert code == 0
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 6 + 2 + 4 + 1                    # mt rows are per language
    for row in lines:
        assert set(t4.ROW_KEYS) <= set(row), row["id"]
        assert row["on_ac"] is True and row["power_plan"] == "Balanced"
    asr = {x["id"]: x for x in lines if x["layer"] == "asr"}
    assert asr["now"]["fallbacks"] == 3 and asr["nf"]["fallbacks"] == 0
    assert asr["now"]["cer"] == 0.0 and asr["now"]["n"] == 3 and asr["now"]["rtf_p95"] > 0
    draft = [x for x in lines if x["layer"] == "draft"]
    assert all(x["deterministic"] for x in draft) and draft[0]["first_partial_p50"] == 0.1
    assert draft[1]["threads"] == 2
    mt = {x["id"]: x for x in lines if x["layer"] == "mt"}
    assert set(mt) == {"hymt2-q4:en", "hymt2-q4:ja", "hymt2-q8:en", "hymt2-q8:ja"}
    assert mt["hymt2-q4:ja"]["gate_fail"] == 0 and mt["hymt2-q4:ja"]["n"] == 20
    conc = [x for x in lines if x["layer"] == "concurrent"][0]
    assert conc["id"] == "nf-ac640+xasr-int8-t1+hymt2-q4" and conc["a3_ok"] is True and conc["n"] >= 1
    md = t4.markdown(rows)
    assert "| asr | now |" in md and "同時跑" in md and "Balanced" in md
    assert "我們開始上課" not in out.read_text(encoding="utf-8")       # no transcript in results


def test_on_battery_exits_3_and_busy_exits_4(clips, tmp_path):
    a, r = clips
    code, rows = t4.run_t4(t4.load_plan(small_plan()), a, r, tmp_path / "x.jsonl", engines(on_battery=lambda: True))
    assert code == t4.EXIT_BATTERY == 3 and rows == []
    code, _ = t4.run_t4(t4.load_plan(small_plan()), a, r, tmp_path / "y.jsonl",
                        engines(busy=lambda: (True, "listeners=3")))
    assert code == t4.EXIT_BUSY == 4


def test_ev37_aborts(clips, tmp_path):
    a, r = clips
    code, rows = t4.run_t4(t4.load_plan(small_plan()), a, r, tmp_path / "z.jsonl", engines(ev37=lambda s: 2))
    assert code == 4 and len(rows) == 1 and rows[0]["ev37"] == 2


def test_unstable_draft_is_flagged(clips, tmp_path):
    a, r = clips
    p = small_plan()
    p["asr"], p["mt"], p["concurrent"] = [], [], {}
    p["draft"] = p["draft"][:1]
    code, rows = t4.run_t4(t4.load_plan(p), a, r, tmp_path / "d.jsonl", engines(draft=lambda row: FakeDraft(unstable=True)))
    assert rows[0]["deterministic"] is False and rows[0]["deterministic_clips"] == "0/3"


def test_cli_hold_dry_run_and_missing(tmp_path, clips, capsys):
    plan = tmp_path / "p.json"
    plan.write_text(json.dumps(small_plan()), encoding="utf-8")
    # real engines path: files missing -> exit 6, nothing downloaded
    assert rtf_check.main(["t4", "--plan", str(plan), "--models", str(tmp_path), "--dry-run"]) == t4.EXIT_MISSING
    bad = tmp_path / "bad.json"
    bad.write_text("{}", encoding="utf-8")
    assert rtf_check.main(["t4", "--plan", str(bad)]) == t4.EXIT_PLAN

    class A:
        pass
    args = A()
    args.plan, args.audio, args.ref, args.out = plan, clips[0], clips[1], tmp_path / "o.jsonl"
    args.models, args.llama_server, args.report, args.dry_run, args.approved = tmp_path, None, tmp_path / "R.md", False, False
    assert rtf_check._cmd_t4(args, engines=engines()) == t4.EXIT_HOLD          # benchmark hold
    args.approved = True
    assert rtf_check._cmd_t4(args, engines=engines()) == 0
    assert "| draft | xasr-int8-t1 |" in args.report.read_text(encoding="utf-8")


def test_det_subcommand(clips, tmp_path):
    a, _ = clips

    class A:
        pass
    args = A()
    args.dir, args.threads, args.audio, args.n, args.precision, args.out = tmp_path, "1,2", a, 30, "auto", tmp_path / "det.json"
    assert rtf_check._cmd_det(args, factory=lambda th: FakeDraft(unstable=th > 1)) == 1
    rows = json.loads(args.out.read_text(encoding="utf-8"))
    assert rows == [{"threads": 1, "clips": 3, "identical": 3, "deterministic": True},
                    {"threads": 2, "clips": 3, "identical": 0, "deterministic": False}]
