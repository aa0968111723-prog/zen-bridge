"""Fine-tune data prep (app/training, docs/finetune-data.md): schema, consent + withdrawal, studio, harvest,
review, import, split leakage, export format."""
import json
import sqlite3
import time
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from app.training import audioq, dataset, harvest, importer, prompts, recordings, registry, review, studio  # noqa: E402
from app.training import db as tdb  # noqa: E402
from tests.training_fakes import speechy, wav_bytes  # noqa: E402


@pytest.fixture
def tr(tmp_path, monkeypatch):
    root = tmp_path / "training"
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(root))
    harvest._cache.update(at=0.0, active=False, swept=time.monotonic())
    c = tdb.connect(root)
    yield c, root
    c.close()


def consented(c, label="師兄", **kw):
    s = registry.new_speaker(c, label)
    args = dict(scope_asr=True, scope_mt=True, harvest_live=True, recorded_by="社長", confirm_text=True)
    args.update(kw)
    registry.record_consent(c, s["id"], **args)
    return s["id"]


def studio_takes(c, root, sid, n=3, seed=1):
    prompts.sync(c, None)
    rec = studio.start(c, root, sid)
    ps = prompts.next_prompts(c, sid, n=n, seed=seed)
    out = []
    for i, p in enumerate(ps):
        r = studio.take(c, root, rec["id"], wav_bytes(speechy(2.5 + i * 0.3, seed=seed + i)), prompt_id=p["id"])
        assert r["saved"], r
        out.append(r["clip"])
    recordings.finish(c, root, rec["id"])
    return rec, out


# ------------------------------------------------------------------ schema
def test_schema_tables_and_constraints(tr):
    c, root = tr
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"speakers", "consents", "recordings", "clips", "prompts", "mt_pairs", "harvest_optins", "exports",
            "audit"} <= names
    assert c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
    sid = consented(c)
    with pytest.raises(sqlite3.IntegrityError):    # local_only must be 1
        c.execute("INSERT INTO consents(speaker_id, scope_asr, scope_mt, local_only, consent_version, consent_date, "
                  "recorded_by, granted_at) VALUES (?,1,1,0,'v','2026-10-12','x',0)", (sid,))
    with pytest.raises(sqlite3.IntegrityError):    # at least one scope
        c.execute("INSERT INTO consents(speaker_id, scope_asr, scope_mt, consent_version, consent_date, recorded_by, "
                  "granted_at) VALUES (?,0,0,'v','2026-10-12','x',0)", (sid,))
    cols = {r[1] for r in c.execute("PRAGMA table_info(speakers)")}
    assert not cols & {"student_id", "phone", "email", "real_name"}


def test_speaker_label_refuses_pii(tr):
    c, _ = tr
    for bad in ("410123456", "0912-345-678", "a@b.tw", ""):
        with pytest.raises(registry.RegistryError):
            registry.new_speaker(c, bad)
    assert registry.new_speaker(c, "小華師姐")["label"] == "小華師姐"


def test_consent_requires_confirmation_and_scope(tr):
    c, _ = tr
    s = registry.new_speaker(c, "阿德")
    with pytest.raises(registry.RegistryError):
        registry.record_consent(c, s["id"], scope_asr=True, scope_mt=False, recorded_by="社長", confirm_text=False)
    with pytest.raises(registry.RegistryError):
        registry.record_consent(c, s["id"], scope_asr=False, scope_mt=False, recorded_by="社長", confirm_text=True)
    assert not registry.has_consent(c, s["id"])
    registry.record_consent(c, s["id"], scope_asr=True, scope_mt=False, recorded_by="社長", confirm_text=True)
    assert registry.has_consent(c, s["id"], "asr") and not registry.has_consent(c, s["id"], "mt")
    assert not registry.has_consent(c, s["id"], "asr", live=True)


# ------------------------------------------------------------------ studio + transcript files
def test_studio_saves_audio_with_live_and_full_transcript(tr):
    c, root = tr
    sid = consented(c)
    rec, clips = studio_takes(c, root, sid, n=3)
    d = root / rec["dir"]
    for k in clips:
        w = root / k["audio_path"]
        assert w.exists() and audioq.read_pcm(w)                    # 16 kHz mono PCM16
    live = [json.loads(x) for x in (d / "live.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e["type"] for e in live] == ["final", "final", "final", "end"]
    assert all("t0_ms" in e and "speaker" in e for e in live[:3])
    doc = json.loads((d / "transcript.json").read_text(encoding="utf-8"))
    assert doc["audio"] == "recording.wav" and len(doc["segments"]) == 3
    assert [s["text"] for s in doc["segments"]] == [k["text"] for k in clips]
    assert doc["segments"][1]["start_ms"] > doc["segments"][0]["end_ms"]
    srt = (d / "transcript.srt").read_text(encoding="utf-8")
    assert "00:00:00,000 --> " in srt and "Speaker 0: " in srt
    assert (d / "transcript.vtt").read_text(encoding="utf-8").startswith("WEBVTT")
    assert "<v Speaker 0>" in (d / "transcript.vtt").read_text(encoding="utf-8")
    assert len((d / "transcript.txt").read_text(encoding="utf-8").splitlines()) == 3
    full = audioq.read_pcm(d / "recording.wav")
    assert len(full) / 16000 >= sum(k["duration_s"] for k in clips)
    assert registry.get_speaker(c, sid)["minutes_total"] > 0.1


def test_studio_rejects_bad_takes_and_rerecord_replaces(tr):
    c, root = tr
    sid = consented(c)
    prompts.sync(c, None)
    rec = studio.start(c, root, sid)
    p = prompts.next_prompts(c, sid, n=1)[0]
    short = studio.take(c, root, rec["id"], wav_bytes(speechy(0.5)), prompt_id=p["id"])
    assert short["saved"] is False and any("太短" in x for x in short["problems"])
    loud = studio.take(c, root, rec["id"], wav_bytes(speechy(3, clip=True)), prompt_id=p["id"])
    assert loud["saved"] and any("削波" in x for x in loud["problems"]) and loud["clip"]["status"] == "unverified"
    good = studio.take(c, root, rec["id"], wav_bytes(speechy(3)), prompt_id=p["id"], replace=loud["clip"]["id"])
    assert good["saved"] and good["clip"]["status"] == "approved"
    assert not (root / loud["clip"]["audio_path"]).exists()
    assert c.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
    with pytest.raises(audioq.AudioFormatError):
        audioq.read_pcm(b"RIFF....not a wav")


def test_studio_requires_consent(tr):
    c, root = tr
    s = registry.new_speaker(c, "沒同意的人")
    with pytest.raises(registry.RegistryError) as e:
        studio.start(c, root, s["id"])
    assert e.value.status == 403


# ------------------------------------------------------------------ harvest
def _seg_wav(tmp, name, seconds=3.0, seed=3):
    p = tmp / name
    p.write_bytes(wav_bytes(speechy(seconds, seed=seed)))
    return p


def test_harvest_only_with_host_optin_and_live_consent(tr, tmp_path):
    c, root = tr
    sid = consented(c)
    nolive = consented(c, "只錄音室", harvest_live=False)
    w = _seg_wav(tmp_path, "a.wav")
    assert harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=w, zh_raw="今天講因緣", zh="今天講因緣") is None
    with pytest.raises(registry.RegistryError):
        harvest.enable(c, room_id="r1", speaker_id=nolive, enabled_by="host")
    o = harvest.enable(c, room_id="r1", speaker_id=sid, enabled_by="host")
    cid = harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=w, zh_raw="今天講音緣", zh="今天講因緣",
                          t0_ms=0, t1_ms=3000, tgt_lang="en")
    assert cid
    k = recordings.clip(c, cid)
    assert k["status"] == "unverified" and k["asr_text"] == "今天講音緣" and k["segment_id"] == "r1:s1:1"
    # retry of the same segment replaces; other rooms are ignored
    harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=w, zh_raw="今天講因緣", zh="今天講因緣")
    assert c.execute("SELECT COUNT(*) FROM clips").fetchone()[0] == 1
    assert harvest.capture(room_id="r2", session_id="x", seq=1, wav_path=w, zh_raw="a", zh="a") is None
    rec_id = harvest.optin(c, o["id"])["recording_id"]
    assert (root / recordings.get(c, rec_id)["dir"] / "live.jsonl").exists()
    # a new session in the same room ends the per-session opt-in and writes the full transcript
    assert harvest.capture(room_id="r1", session_id="s2", seq=1, wav_path=w, zh_raw="b", zh="b") is None
    assert harvest.optin(c, o["id"])["stopped_at"] is not None
    rec = recordings.get(c, rec_id)
    assert rec["status"] == "ended" and (root / rec["dir"] / "transcript.srt").exists()


def test_harvest_idle_warns_at_4_and_stops_at_5_minutes(tr, tmp_path):
    c, root = tr
    sid = consented(c)
    o = harvest.enable(c, room_id="r9", speaker_id=sid, enabled_by="host")
    harvest.capture(room_id="r9", session_id="s1", seq=1, wav_path=_seg_wav(tmp_path, "b.wav"), zh_raw="x", zh="x")
    now = time.time()
    o = harvest.optin(c, o["id"])
    assert harvest.idle_state(c, o, now + 100)["warn"] is False
    assert harvest.idle_state(c, o, now + 245)["warn"] is True
    assert harvest.sweep(c, root, now + 200) == []
    assert harvest.sweep(c, root, now + 301) == [o["id"]]
    rec = recordings.get(c, harvest.optin(c, o["id"])["recording_id"])
    assert rec["status"] == "ended" and (root / rec["dir"] / "transcript.json").exists()


def test_pipeline_hook_is_noop_without_training_db(tmp_path, monkeypatch):
    import asyncio
    from app import pipeline
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(tmp_path / "none"))
    harvest._cache.update(at=0.0, active=False)

    class S:
        room_id, session_id, seq, zh_raw, zh, t0_ms, t1_ms, tgt_lang = "r", "s", 1, "a", "a", 0, 1, "en"
    asyncio.run(pipeline._training_harvest(S(), tmp_path / "x.wav"))
    assert not (tmp_path / "none").exists()


# ------------------------------------------------------------------ review + MT pairs
def test_review_approve_creates_mt_pair_and_reject_deletes_audio(tr, tmp_path):
    c, root = tr
    sid = consented(c)
    harvest.enable(c, room_id="r1", speaker_id=sid, enabled_by="host")
    a = harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=_seg_wav(tmp_path, "1.wav"), zh_raw="諸行無嘗",
                        zh="諸行無嘗", tgt_lang="en")
    b = harvest.capture(room_id="r1", session_id="s1", seq=2, wav_path=_seg_wav(tmp_path, "2.wav", seed=5),
                        zh_raw="旁人講話", zh="旁人講話")
    out = review.decide(c, root, a, action="approve", actor="t", text="諸行無常", mt_text="All conditioned things are impermanent.")
    assert out["status"] == "approved" and out["text"] == "諸行無常" and out["mt_pair"]["tgt_lang"] == "en"
    bp = recordings.clip(c, b)["audio_path"]
    review.decide(c, root, b, action="reject", actor="t")
    assert not (root / bp).exists() and recordings.clip(c, b)["audio_path"] is None
    assert [x["id"] for x in review.queue(c, status="approved")] == [a]


# ------------------------------------------------------------------ withdrawal
def test_withdraw_deletes_audio_pairs_recordings_and_exports(tr, tmp_path):
    c, root = tr
    keep = consented(c, "留下的")
    gone = consented(c, "要撤回的")
    studio_takes(c, root, keep, n=3, seed=1)
    rec, clips = studio_takes(c, root, gone, n=3, seed=7)
    harvest.enable(c, room_id="r1", speaker_id=gone, enabled_by="host")
    h = harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=_seg_wav(tmp_path, "w.wav"), zh_raw="放下", zh="放下")
    review.decide(c, root, h, action="approve", actor="t", mt_text="Let go.")
    ext = tmp_path / "outside-export"
    ex = dataset.build(c, root, out_dir=ext)
    assert gone in ex["speakers"] and ext.exists()
    ex2 = dataset.build(c, root)
    audio = [root / k["audio_path"] for k in clips]
    assert all(p.exists() for p in audio)

    res = registry.withdraw(c, root, gone)
    assert res["clips_deleted"] == 4 and res["exports_deleted"] == 2
    assert not any(p.exists() for p in audio)
    assert not (root / rec["dir"]).exists()
    assert not ext.exists() and not Path(ex2["dir"]).exists()
    assert c.execute("SELECT COUNT(*) FROM clips WHERE speaker_id=?", (gone,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM mt_pairs").fetchone()[0] == 0
    sp = c.execute("SELECT label, withdrawn_at FROM speakers WHERE id=?", (gone,)).fetchone()
    assert sp[0] == registry.WITHDRAWN_LABEL and sp[1]
    assert not registry.has_consent(c, gone)
    # nothing of the withdrawn speaker anywhere on disk under the training dir
    assert not [p for p in root.rglob("*") if gone in str(p)]
    # future exports exclude them, even if a stale row were left behind
    ex3 = dataset.build(c, root)
    assert ex3["speakers"] == [keep]
    with pytest.raises(registry.RegistryError):
        studio.start(c, root, gone)
    with pytest.raises(registry.RegistryError):
        registry.record_consent(c, gone, scope_asr=True, scope_mt=True, recorded_by="x", confirm_text=True)
    assert harvest.capture(room_id="r1", session_id="s1", seq=9, wav_path=_seg_wav(tmp_path, "z.wav"), zh_raw="a", zh="a") is None
    assert registry.withdraw(c, root, gone)["clips_deleted"] == 0          # idempotent


# ------------------------------------------------------------------ split leakage + export format
def _many(c, root, tmp_path):
    spk = [consented(c, f"講者{i}") for i in range(3)]
    for i, sid in enumerate(spk):
        for sess in range(3):
            room = f"room{i}"
            harvest.enable(c, room_id=room, speaker_id=sid, enabled_by="host")
            for seq in range(4):
                w = _seg_wav(tmp_path, f"{i}{sess}{seq}.wav", 2.0 + seq * 0.2, seed=i * 31 + sess * 7 + seq)
                text = "諸行無常" if seq == 0 else f"第{i}位第{sess}場第{seq}句"
                cid = harvest.capture(room_id=room, session_id=f"s{sess}", seq=seq, wav_path=w, zh_raw=text, zh=text,
                                      tgt_lang="en")
                review.decide(c, root, cid, action="approve", actor="t", mt_text=f"line {i}-{sess}-{seq}")
            harvest.stop(c, root, c.execute("SELECT MAX(id) FROM harvest_optins").fetchone()[0])
    return spk


def test_split_has_no_group_or_text_leakage(tr, tmp_path):
    c, root = tr
    _many(c, root, tmp_path)
    ex = dataset.build(c, root)
    out = Path(ex["dir"])
    groups, texts = {}, {}
    for s in dataset.SPLITS:
        meta = out / "asr" / s / "metadata.jsonl"
        rows = [json.loads(x) for x in meta.read_text(encoding="utf-8").splitlines()] if meta.exists() else []
        assert rows, f"{s} is empty"
        for r in rows:
            assert groups.setdefault(r["group"], s) == s, "a speaker×session group spans two splits"
            assert texts.setdefault(r["transcription"], s) == s, "same sentence in two splits"
    assert ex["asr"]["leakage_removed"] >= 1          # "諸行無常" said in many sessions
    mt_src = {}
    for s in dataset.SPLITS:
        for line in (out / "mt" / f"zh-en.{s}.jsonl").read_text(encoding="utf-8").splitlines():
            src = json.loads(line)["messages"][1]["content"]
            assert mt_src.setdefault(src, s) == s
    # deterministic
    again = dataset.build(c, root)
    assert again["asr"]["clips"] == ex["asr"]["clips"]


def test_assign_splits_unit():
    items = [{"g": f"g{i}", "w": 1.0} for i in range(20)]
    m = dataset.assign_splits(items, key=lambda x: x["g"], weight=lambda x: x["w"])
    counts = {s: sum(1 for v in m.values() if v == s) for s in dataset.SPLITS}
    assert counts["train"] >= 14 and counts["validation"] >= 1 and counts["test"] >= 1
    assert dataset.assign_splits(items[:2], key=lambda x: x["g"], weight=lambda x: 1)  # tiny: no crash


def test_export_format_audiofolder_jsonl_and_card(tr, tmp_path):
    c, root = tr
    _many(c, root, tmp_path)
    # a bad pair (Breeze output far from the text) and a too-short clip are filtered
    sid = consented(c, "濾掉")
    harvest.enable(c, room_id="rx", speaker_id=sid, enabled_by="host")
    bad = harvest.capture(room_id="rx", session_id="s", seq=1, wav_path=_seg_wav(tmp_path, "bad.wav"),
                          zh_raw="完全不同的一句話", zh="完全不同的一句話")
    review.decide(c, root, bad, action="approve", actor="t", text="今天天氣很好我們去散步")
    short = harvest.capture(room_id="rx", session_id="s", seq=2, wav_path=_seg_wav(tmp_path, "s.wav", 0.6),
                            zh_raw="短", zh="短")
    review.decide(c, root, short, action="approve", actor="t")
    unv = harvest.capture(room_id="rx", session_id="s", seq=3, wav_path=_seg_wav(tmp_path, "u.wav"), zh_raw="未審", zh="未審")
    ex = dataset.build(c, root)
    out = Path(ex["dir"])
    d = ex["asr"]["dropped"]
    assert d["high_cer"] == 1 and d["duration"] == 1 and d["unverified"] == 1
    assert any(f["clip_id"] == bad for f in ex["asr"]["flagged_high_cer"])
    row = json.loads((out / "asr" / "train" / "metadata.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert {"file_name", "transcription", "speaker_id", "duration"} <= set(row)
    assert (out / "asr" / "train" / row["file_name"]).exists()
    audioq.read_pcm(out / "asr" / "train" / row["file_name"])
    man = json.loads((out / "asr" / "train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert (out / "asr" / man["audio"]).exists()
    chat = json.loads((out / "mt" / "zh-en.train.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert [m["role"] for m in chat["messages"]] == ["system", "user", "assistant"]
    card = (out / "README.md").read_text(encoding="utf-8")
    assert "每位講者時數" in card and "術語涵蓋" in card and "只限社團本機使用" in card
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_id"] == ex["export_id"] and set(manifest["asr"]["hours_per_speaker"]) <= set(ex["speakers"])
    assert unv and not list(out.rglob(f"{unv}.wav"))


def test_export_term_coverage_uses_glossary_read_only(tr, tmp_path):
    c, root = tr
    zen = tmp_path / "zen.sqlite3"
    z = sqlite3.connect(zen)
    z.execute("CREATE TABLE glossary_terms (id INTEGER PRIMARY KEY, zh TEXT, en TEXT, status TEXT, hit_count INT)")
    z.executemany("INSERT INTO glossary_terms(zh, en, status, hit_count) VALUES (?,?,'active',0)",
                  [("禪學社", "Zen Club"), ("止觀", "calm and insight")])
    z.commit()
    z.close()
    before = zen.read_bytes()
    assert prompts.sync(c, zen) > 0
    assert c.execute("SELECT COUNT(*) FROM prompts WHERE source='glossary'").fetchone()[0] == 2
    sid = consented(c)
    rec = studio.start(c, root, sid)
    p = c.execute("SELECT id FROM prompts WHERE term='禪學社'").fetchone()[0]
    studio.take(c, root, rec["id"], wav_bytes(speechy(3)), prompt_id=p)
    ex = dataset.build(c, root, options={"zen_db": str(zen)})
    assert "禪學社" not in ex["terms"]["missing"] and "止觀" in ex["terms"]["missing"]
    assert zen.read_bytes() == before


# ------------------------------------------------------------------ speaker labels + import
def test_rename_and_map_speaker_labels(tr, tmp_path):
    c, root = tr
    sid = consented(c)
    harvest.enable(c, room_id="r1", speaker_id=sid, enabled_by="host")
    a = harvest.capture(room_id="r1", session_id="s1", seq=1, wav_path=_seg_wav(tmp_path, "1.wav"), zh_raw="法師", zh="法師")
    b = harvest.capture(room_id="r1", session_id="s1", seq=2, wav_path=_seg_wav(tmp_path, "2.wav"), zh_raw="提問", zh="提問")
    rid = recordings.clip(c, a)["recording_id"]
    recordings.set_clip_tag(c, root, b, "Speaker 1")
    recordings.rename_tag(c, root, rid, "Speaker 0", "法師")
    recordings.finish(c, root, rid)
    srt = (root / recordings.get(c, rid)["dir"] / "transcript.srt").read_text(encoding="utf-8")
    assert "法師: 法師" in srt and "Speaker 1: 提問" in srt
    bp = root / recordings.clip(c, b)["audio_path"]
    recordings.map_tag(c, root, rid, "Speaker 1", None)          # the questioner has no consent: exclude
    assert not bp.exists() and recordings.clip(c, b)["status"] == "needs_review"
    import zipfile, io
    z = zipfile.ZipFile(io.BytesIO(recordings.bundle(c, root, rid)))
    names = {Path(n).name for n in z.namelist()}
    assert {"transcript.txt", "transcript.srt", "transcript.vtt", "transcript.json", "recording.wav"} <= names


def test_import_parsers():
    srt = "1\n00:00:01,000 --> 00:00:03,500\n講者A: 今天講止觀\n\n2\n00:00:04,000 --> 00:00:06,000\n好的\n"
    s = importer.parse_cues(srt)
    assert [(x.start_ms, x.end_ms, x.speaker, x.text) for x in s] == [(1000, 3500, "講者A", "今天講止觀"),
                                                                       (4000, 6000, None, "好的")]
    vtt = "WEBVTT\n\n00:01.000 --> 00:02.000\n<v Speaker 1>你好\n"
    assert importer.parse_cues(vtt)[0].speaker == "Speaker 1"
    txt = "[00:00:05] 小明：大家好\n00:00:09 Speaker 2\n我們開始靜坐。\n"
    t = importer.parse_txt(txt)
    assert [(x.start_ms, x.speaker, x.text) for x in t] == [(5000, "小明", "大家好"), (9000, "Speaker 2", "我們開始靜坐。")]
    j = importer.parse_json(json.dumps({"segments": [{"start": 1.5, "end": 2, "text": "a", "speaker": "S0"}]}))
    assert (j[0].start_ms, j[0].end_ms) == (1500, 2000)
    plain = importer.parse_txt("第一段話。\n\n第二段比較長的話。")
    segs, kind = importer.align(plain, 10000)
    assert kind == "forced" and segs[0].start_ms == 0 and segs[-1].end_ms == 10000


def test_import_excludes_unmapped_speakers(tr, tmp_path):
    c, root = tr
    if not audioq.ffmpeg_bin():
        pytest.skip("ffmpeg not available")
    sid = consented(c)
    audio = tmp_path / "class.wav"
    audio.write_bytes(wav_bytes(speechy(8.0)))
    tx = tmp_path / "class.srt"
    tx.write_text("1\n00:00:00,500 --> 00:00:03,500\n老師: 今天講止觀\n\n2\n00:00:04,000 --> 00:00:07,000\n同學: 我有問題\n",
                  encoding="utf-8")
    r = importer.import_recording(c, root, audio, tx, speaker_map={"老師": sid})
    tags = {x["tag"]: x for x in r["speakers"]}
    assert tags["老師"]["cut"] == 1 and tags["同學"]["cut"] == 0
    rows = {k["speaker_tag"]: k for k in map(dict, c.execute("SELECT * FROM clips"))}
    assert rows["老師"]["status"] == "unverified" and rows["同學"]["status"] == "needs_review"
    assert rows["同學"]["audio_path"] is None and rows["同學"]["speaker_id"] is None
    ex = dataset.build(c, root, options={"include_unverified": True})
    assert ex["asr"]["dropped"].get("no_consent") == 1
    importer.purge_source(c, root, r["id"])
    assert not (root / r["dir"] / "source.wav").exists()
