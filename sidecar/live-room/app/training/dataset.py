"""資料集建置：品質篩選 → 去重 → 依講者＋場次分組切 train/validation/test（無洩漏）→
Hugging Face audiofolder（ASR）＋ JSONL 對話格式（MT）＋ 資料集卡。

每次匯出前都重新檢查同意：撤回或沒勾該用途的講者一律不會出現在輸出裡。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.training import prompts as tprompts
from app.training import registry, textnorm
from app.training import db as tdb

SPLITS = ("train", "validation", "test")
SYSTEM = {
    "en": "You translate Traditional Chinese dharma talks and Zen club announcements into natural English. "
          "Keep Buddhist terms consistent with the club glossary. Output only the translation.",
    "ja": "あなたは繁体字中国語の法話と禅サークルの案内を自然な日本語に翻訳します。"
          "仏教用語はサークルの用語集に合わせ、訳文だけを出力してください。",
}


@dataclass
class Options:
    min_s: float = 1.0
    max_s: float = 30.0
    min_snr_db: float = 10.0
    max_clip_ratio: float = 0.001
    max_silence_ratio: float = 0.8
    max_cer: float = 0.5                 # Breeze 輸出 vs 修正稿：差太多 = 可能對錯段
    include_unverified: bool = False
    ratios: tuple = (0.8, 0.1, 0.1)
    seed: str = "zen-ft-v1"
    tm_min_quality: int = 4
    include_tm: bool = True
    include_ledger_human: bool = True
    zen_db: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Options":
        d = dict(d or {})
        o = cls()
        for k, v in d.items():
            if hasattr(o, k) and k != "zen_db":
                cur = getattr(o, k)
                if isinstance(cur, bool):
                    v = bool(v)
                elif isinstance(cur, float):
                    v = float(v)
                elif isinstance(cur, int):
                    v = int(v)
                elif isinstance(cur, tuple):
                    v = tuple(float(x) for x in v)
                    if len(v) != 3 or abs(sum(v) - 1) > 1e-6:
                        raise ValueError("ratios 需三個數字且加總為 1")
                setattr(o, k, v)
        return o


@dataclass
class Report:
    kept: int = 0
    dropped: dict = field(default_factory=lambda: defaultdict(int))
    flagged_cer: list = field(default_factory=list)
    leakage_moved: int = 0


def _h(*parts) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()


def group_key(clip: dict) -> str:
    """Leakage unit: one speaker in one session (live) or one recording (studio/import)."""
    sess = clip.get("session_id") or clip["recording_id"]
    return f"{clip['speaker_id']}::{sess}"


def select_clips(c: sqlite3.Connection, root: Path, opt: Options) -> tuple[list[dict], Report]:
    rep = Report()
    rows = [dict(r) for r in c.execute(
        "SELECT k.*, r.session_id, r.kind FROM clips k JOIN recordings r ON r.id=k.recording_id ORDER BY k.created_at")]
    consent_ok: dict[str, bool] = {}
    out = []
    for r in rows:
        sid = r["speaker_id"]
        if sid not in consent_ok:
            consent_ok[sid] = registry.has_consent(c, sid, "asr")
        reason = None
        if not sid or not consent_ok[sid]:
            reason = "no_consent"
        elif not r["audio_path"] or not (Path(root) / r["audio_path"]).exists():
            reason = "no_audio"
        elif r["status"] == "rejected":
            reason = "rejected"
        elif r["status"] == "needs_review" or (r["status"] == "unverified" and not opt.include_unverified):
            reason = "unverified"
        elif not (r["text"] or "").strip():
            reason = "no_text"
        elif not (opt.min_s <= (r["duration_s"] or 0) <= opt.max_s):
            reason = "duration"
        elif (r["clip_ratio"] or 0) > opt.max_clip_ratio:
            reason = "clipping"
        elif (r["snr_db"] if r["snr_db"] is not None else 99) < opt.min_snr_db:
            reason = "low_snr"
        elif (r["silence_ratio"] or 0) > opt.max_silence_ratio:
            reason = "silence"
        else:
            e = textnorm.cer(r["asr_text"], r["text"]) if r["asr_text"] else None
            r["cer"] = e
            if e is not None and e > opt.max_cer:
                reason = "high_cer"
                rep.flagged_cer.append({"clip_id": r["id"], "cer": round(e, 3)})
        if reason:
            rep.dropped[reason] += 1
            continue
        out.append(r)
    # dedupe: identical audio, then same speaker reading the same text (keep best SNR)
    seen_sha, best = set(), {}
    for r in out:
        if r["sha256"] in seen_sha:
            rep.dropped["dup_audio"] += 1
            continue
        seen_sha.add(r["sha256"])
        k = (r["speaker_id"], textnorm.norm(r["text"]), r["origin"] == "studio")
        if r["origin"] == "studio" and k in best:
            rep.dropped["dup_text"] += 1
            if (r["snr_db"] or 0) > (best[k]["snr_db"] or 0):
                best[k] = r
            continue
        best[k if r["origin"] == "studio" else ("u", r["id"])] = r
    kept = list(best.values())
    rep.kept = len(kept)
    return kept, rep


def assign_splits(items: list[dict], *, key, weight, ratios=(0.8, 0.1, 0.1), seed="zen-ft-v1") -> dict[str, str]:
    """Group-level split: every group goes wholly to one split. Deterministic (hash order), greedy by
    weight so small datasets still get validation/test when there are >= 3 groups. Returns group->split."""
    groups: dict[str, float] = defaultdict(float)
    for it in items:
        groups[key(it)] += weight(it)
    order = sorted(groups, key=lambda g: _h(seed, g))
    total = sum(groups.values()) or 1.0
    got = {s: 0.0 for s in SPLITS}
    out = {}
    if len(order) >= 3:                      # guarantee one group each for validation and test first
        for s, g in zip(("test", "validation"), order[:2]):
            out[g] = s
            got[s] += groups[g]
        order = order[2:]
    for g in order:
        deficit = {s: ratios[i] * total - got[s] for i, s in enumerate(SPLITS)}
        s = max(SPLITS, key=lambda x: (deficit[x], x == "train"))
        out[g] = s
        got[s] += groups[g]
    return out


def remove_text_leakage(items: list[dict], split_of, text_of) -> tuple[list[dict], int]:
    """Same normalised sentence must not sit in two splits: test wins, then validation, then train."""
    rank = {"test": 0, "validation": 1, "train": 2}
    owner: dict[str, str] = {}
    for it in items:
        t = textnorm.norm(text_of(it))
        s = split_of(it)
        if t not in owner or rank[s] < rank[owner[t]]:
            owner[t] = s
    kept, moved = [], 0
    for it in items:
        if owner[textnorm.norm(text_of(it))] != split_of(it):
            moved += 1
            continue
        kept.append(it)
    return kept, moved


def _zen_ro(path):
    if not path or not Path(path).exists():
        return None
    c = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=5)
    c.execute("PRAGMA query_only=ON")
    return c


def collect_mt(c: sqlite3.Connection, opt: Options) -> list[dict]:
    pairs = []
    for r in c.execute("SELECT * FROM mt_pairs"):
        r = dict(r)
        if r["speaker_id"] and not registry.has_consent(c, r["speaker_id"], "mt"):
            continue
        pairs.append({"src": r["src_text"], "tgt": r["tgt_text"], "tgt_lang": r["tgt_lang"], "origin": r["origin"],
                      "group": f"{r['speaker_id']}::{r['session_id']}", "tm_unit_id": r["tm_unit_id"]})
    z = _zen_ro(opt.zen_db)
    if z is not None:
        try:
            if opt.include_tm:
                for tid, src, tl, tgt, q, seg in z.execute(
                        "SELECT id, src_text, tgt_lang, tgt_text, quality, segment_id FROM tm_units WHERE quality >= ? "
                        "AND tgt_lang IN ('en','ja')", (opt.tm_min_quality,)):
                    sess = seg.split(":")[1] if seg and seg.count(":") >= 2 else None
                    pairs.append({"src": src, "tgt": tgt, "tgt_lang": tl, "origin": "tm",
                                  "group": f"tm::{sess or _h(textnorm.norm(src))[:8]}", "tm_unit_id": tid})
            if opt.include_ledger_human:
                for seg, sess, src, tl, tgt in z.execute(
                        "SELECT s.id, s.session_id, t.text, tr.tgt_lang, tr.text FROM translations tr "
                        "JOIN segments s ON s.id=tr.segment_id JOIN transcripts t ON t.segment_id=s.id AND t.is_current=1 "
                        "WHERE tr.is_current=1 AND tr.origin IN ('human','post_edit') AND tr.tgt_lang IN ('en','ja')"):
                    pairs.append({"src": src, "tgt": tgt, "tgt_lang": tl, "origin": "ledger",
                                  "group": f"ledger::{sess}", "tm_unit_id": None})
        except sqlite3.Error:
            pass
        finally:
            z.close()
    seen, out = set(), []
    for p in pairs:
        k = (textnorm.norm(p["src"]), p["tgt_lang"], " ".join(p["tgt"].split()))
        if not k[0] or not k[2] or k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


def _write_jsonl(path: Path, rows) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            n += 1
    return n


def build(c: sqlite3.Connection, root: Path, *, out_dir: Path | None = None, options: Options | dict | None = None,
          actor: str = "cli") -> dict:
    root = Path(root)
    opt = options if isinstance(options, Options) else Options.from_dict(options)
    if opt.zen_db is None and isinstance(options, dict) and options.get("zen_db"):
        opt.zen_db = options["zen_db"]
    eid = "ds_" + time.strftime("%Y%m%d_%H%M%S") + "_" + hashlib.sha1(str(time.time()).encode()).hexdigest()[:4]
    out = Path(out_dir) if out_dir else root / "exports" / eid
    if out.exists() and any(out.iterdir()):
        raise registry.RegistryError("輸出資料夾已存在且不是空的")
    tmp = out.with_name(out.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)

    clips, rep = select_clips(c, root, opt)
    gsplit = assign_splits(clips, key=group_key, weight=lambda r: r["duration_s"] or 0, ratios=opt.ratios, seed=opt.seed)
    for r in clips:
        r["split"] = gsplit[group_key(r)]
    clips, rep.leakage_moved = remove_text_leakage(clips, lambda r: r["split"], lambda r: r["text"])
    asr_rows = defaultdict(list)
    for r in clips:
        name = f"{r['id']}.wav"
        dst = tmp / "asr" / r["split"] / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / r["audio_path"], dst)
        asr_rows[r["split"]].append({
            "file_name": name, "transcription": r["text"], "speaker_id": r["speaker_id"], "group": group_key(r),
            "origin": r["origin"], "duration": r["duration_s"], "snr_db": r["snr_db"], "asr_text": r["asr_text"],
            "cer_breeze": None if r.get("cer") is None else round(r["cer"], 4), "status": r["status"]})
    for s in SPLITS:
        rows = asr_rows.get(s, [])
        if rows:
            _write_jsonl(tmp / "asr" / s / "metadata.jsonl", rows)
        _write_jsonl(tmp / "asr" / f"{s}.jsonl", ({**x, "audio": f"{s}/{x['file_name']}"} for x in rows))

    mt = collect_mt(c, opt)
    msplit = assign_splits(mt, key=lambda p: p["group"], weight=lambda p: 1.0, ratios=opt.ratios, seed=opt.seed)
    for p in mt:
        p["split"] = msplit[p["group"]]
    mt_counts = {}
    for lang in ("en", "ja"):
        sub = [p for p in mt if p["tgt_lang"] == lang]
        sub, moved = remove_text_leakage(sub, lambda p: p["split"], lambda p: p["src"])
        rep.leakage_moved += moved
        for s in SPLITS:
            rows = [{"messages": [{"role": "system", "content": SYSTEM[lang]}, {"role": "user", "content": p["src"]},
                                  {"role": "assistant", "content": p["tgt"]}],
                     "meta": {"origin": p["origin"], "group": p["group"], "tm_unit_id": p["tm_unit_id"]}}
                    for p in sub if p["split"] == s]
            mt_counts[f"zh-{lang}.{s}"] = _write_jsonl(tmp / "mt" / f"zh-{lang}.{s}.jsonl", rows)

    speakers = sorted({r["speaker_id"] for r in clips} | {p["group"].split("::")[0] for p in mt
                                                           if p["group"].startswith("spk_")})
    stats = _stats(c, clips, rep, mt_counts, opt)
    stats.update(export_id=eid, created_at=time.time(), speakers=speakers, options=_opt_dict(opt))
    (tmp / "manifest.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding="utf-8")
    (tmp / "README.md").write_text(dataset_card(stats), encoding="utf-8")
    if out.exists():
        out.rmdir()
    tmp.replace(out)
    try:
        d = tdb.rel(root, out)
    except ValueError:
        d = str(out)               # outside the training dir: recorded, but withdrawal can only delete what it can reach
    c.execute("INSERT INTO exports(id, dir, speakers, stats, created_at) VALUES (?,?,?,?,?)",
              (eid, d, json.dumps(speakers), json.dumps({k: stats[k] for k in ("asr", "mt")}, ensure_ascii=False),
               time.time()))
    tdb.audit(c, "export", actor=actor, export=eid, clips=len(clips))
    return {"export_id": eid, "dir": str(out), **stats}


def _opt_dict(opt: Options) -> dict:
    d = asdict(opt)
    d.pop("zen_db", None)
    return d


def _stats(c, clips, rep: Report, mt_counts: dict, opt: Options) -> dict:
    per = defaultdict(lambda: {s: 0.0 for s in SPLITS})
    for r in clips:
        per[r["speaker_id"]][r["split"]] += (r["duration_s"] or 0) / 3600
    hours = {s: round(sum(v[s] for v in per.values()), 4) for s in SPLITS}
    # term coverage: glossary + prompt terms that appear in the ASR transcripts
    terms = {t["zh"] for t in tprompts.glossary_terms(Path(opt.zen_db) if opt.zen_db else None)}
    terms |= {r[0] for r in c.execute("SELECT DISTINCT term FROM prompts WHERE term IS NOT NULL")}
    text = "\n".join(r["text"] for r in clips)
    hit = sorted(t for t in terms if t and t in text)
    return {
        "asr": {"clips": {s: sum(1 for r in clips if r["split"] == s) for s in SPLITS}, "hours": hours,
                "hours_per_speaker": {k: {s: round(v[s], 4) for s in SPLITS} for k, v in sorted(per.items())},
                "dropped": dict(rep.dropped), "flagged_high_cer": rep.flagged_cer[:200],
                "leakage_removed": rep.leakage_moved},
        "mt": mt_counts,
        "terms": {"total": len(terms), "covered": len(hit), "missing": sorted(terms - set(hit))[:200]},
    }


DROP_ZH = {"no_consent": "沒有有效同意", "no_audio": "沒有音檔", "rejected": "審核退回", "unverified": "尚未校對",
           "no_text": "沒有文字", "duration": "長度不在範圍", "clipping": "削波", "low_snr": "SNR 太低",
           "silence": "靜音太多", "high_cer": "Breeze 與修正稿差異過大", "dup_audio": "重複音檔", "dup_text": "同人同句重錄"}


def dataset_card(st: dict) -> str:
    a = st["asr"]
    lines = ["---", "language: [zh, en, ja]", "license: other", "pretty_name: 淡江禪學社 本機訓練資料",
             "task_categories: [automatic-speech-recognition, translation]", "---", "",
             "# 淡江禪學社 本機訓練資料集", "",
             f"匯出編號：`{st['export_id']}`　建立時間：{time.strftime('%Y-%m-%d %H:%M', time.localtime(st['created_at']))}",
             "", "> 只限社團本機使用。所有講者都有本機訓練同意；撤回同意的講者會連同這份匯出一起刪除。"
             "請勿上傳到雲端或 Hugging Face Hub。", "",
             "## 語音辨識（ASR）", "",
             "| 切分 | 片段數 | 時數 |", "| --- | ---: | ---: |"]
    for s in SPLITS:
        lines.append(f"| {s} | {a['clips'][s]} | {a['hours'][s]:.3f} |")
    lines += ["", "### 每位講者時數（依 ID，不放暱稱）", "", "| 講者 | train | validation | test |", "| --- | ---: | ---: | ---: |"]
    for k, v in a["hours_per_speaker"].items():
        lines.append(f"| `{k}` | {v['train']:.3f} | {v['validation']:.3f} | {v['test']:.3f} |")
    lines += ["", "### 篩掉的片段", ""]
    lines += [f"- {DROP_ZH.get(k, k)}：{v}" for k, v in sorted(a["dropped"].items())] or ["- （無）"]
    lines += [f"- 跨切分重複句移除：{a['leakage_removed']}", "",
              "切分以「講者 × 場次（或錄音）」為單位，同一組不會跨 train／validation／test；同一句文字只會出現在一個切分。", "",
              "## 翻譯（MT）配對", "", "| 檔案 | 筆數 |", "| --- | ---: |"]
    lines += [f"| `mt/{k}.jsonl` | {v} |" for k, v in sorted(st["mt"].items())]
    t = st["terms"]
    lines += ["", "## 術語涵蓋", "", f"詞表與提示詞共 {t['total']} 個，出現在逐字稿裡的有 {t['covered']} 個。", ""]
    if t["missing"]:
        lines += ["還沒錄到的詞（前 200 個）：" + "、".join(t["missing"]), ""]
    lines += ["## 格式", "", "- `asr/{train,validation,test}/` 是 Hugging Face `audiofolder`："
              "`load_dataset(\"audiofolder\", data_dir=\"asr\")`，欄位 `transcription`。",
              "- `asr/{split}.jsonl` 是同內容的 manifest（`audio` 為相對路徑）。",
              "- `mt/zh-en.*.jsonl`、`mt/zh-ja.*.jsonl` 是 chat 格式（system／user／assistant）。",
              "- 訓練腳本見 repo 的 `training/`。", ""]
    return "\n".join(lines)


def delete_export(c, root: Path, eid: str, reason: str = "manual") -> bool:
    r = c.execute("SELECT * FROM exports WHERE id=?", (eid,)).fetchone()
    if not r:
        return False
    registry._remove_export_dir(root, eid, r["dir"])
    c.execute("UPDATE exports SET deleted_at=?, deleted_reason=? WHERE id=?", (time.time(), reason, eid))
    return True
