"""Tests for tools/caption_gap_detector.py (queue item 36).

Every database and event log is a temp fixture written with explicit UTF-8; nothing
depends on host recordings, network, ports, POSIX permissions, path separators or locale.
"""
from __future__ import annotations

import ast
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "caption_gap_detector.py"

sys.path.insert(0, str(ROOT / "tools"))
import caption_gap_detector as cgd  # noqa: E402


REPORT_KEYS = {
    "tool", "schema_version", "status", "exit_code", "inputs", "thresholds", "sessions", "gaps",
    "drafts", "pending", "counts", "assumptions", "issues", "summary_zh",
}
T0 = 1_760_000_000.0          # wall clock base (epoch seconds)

# Same DDL as app/store.py CaptionStore._prepare (captions.sqlite3).
CAPTIONS_DDL = """
create table captions (
    id text primary key, room_id text not null, session_id text not null, seq integer not null,
    version integer not null, zh text, zh_raw text, en text, status text,
    t0_ms integer, t1_ms integer, updated_at real not null, session_ord integer, term_flags text
)
"""


# --------------------------------------------------------------------------- fixtures
def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def _jsonl(path: Path, events: list[dict], tail: str = "") -> Path:
    return _write(path, "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events) + tail)


def _live_events(n=10, slice_ms=3000, draft_lag=0.5, final_lag=2.0, drafts=True, end=True,
                 room="r1", session="s1") -> list[dict]:
    """Recording live.jsonl: continuous speech in ``slice_ms`` slices, drafts every second."""
    ev: list[dict] = [{"at": T0, "type": "start", "speaker_id": "spk_a", "room_id": room, "session_id": session}]
    for i in range(n):
        t0, t1 = i * slice_ms, (i + 1) * slice_ms
        if drafts:
            for k in range(1, slice_ms // 1000 + 1):
                ev.append({"at": T0 + (t0 + k * 1000) / 1000 + draft_lag, "type": "draft",
                           "t0_ms": t0, "t1_ms": t0 + k * 1000, "text": "禪" * k})
        ev.append({"at": T0 + t1 / 1000 + final_lag, "type": "final", "seq": i + 1, "t0_ms": t0, "t1_ms": t1,
                   "asr_text": f"第{i + 1}句", "text": f"第{i + 1}句，靜坐。", "tgt_lang": "en"})
    if end:
        ev.append({"at": T0 + n * slice_ms / 1000 + final_lag + 0.5, "type": "end", "reason": "host"})
    return ev


def _captions_db(path: Path, rows: list[tuple]) -> Path:
    """rows: (room, session, seq, status, zh, t0, t1, updated_at_seconds)"""
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(CAPTIONS_DDL)
        conn.executemany(
            "insert into captions (id, room_id, session_id, seq, version, zh, zh_raw, en, status, t0_ms, t1_ms,"
            " updated_at) values (?,?,?,?,1,?,?,'',?,?,?,?)",
            [(f"{r}:{s}:{q}", r, s, q, zh, zh, st, t0, t1, at) for r, s, q, st, zh, t0, t1, at in rows],
        )
        conn.commit()
    return path


def _slices(room="r1", session="s1", n=6, slice_ms=4000, lag=1.5, start=T0):
    return [(room, session, i + 1, "ready", f"第{i + 1}句", i * slice_ms, (i + 1) * slice_ms,
             start + (i + 1) * slice_ms / 1000 + lag) for i in range(n)]


def _run(argv: list[str]) -> int:
    return cgd.main([str(a) for a in argv])


def _report(tmp_path: Path, argv: list, name="r.json") -> tuple[int, dict]:
    out = tmp_path / name
    code = _run(list(argv) + ["--json", out])
    return code, json.loads(out.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- helpers
def test_nearest_rank():
    assert cgd.nearest_rank([], 0.5) is None
    assert cgd.nearest_rank([5, 1, 3], 0.5) == 3
    assert cgd.nearest_rank(list(range(1, 21)), 0.95) == 19
    assert cgd.nearest_rank([7], 0.95) == 7


def test_fmt_ms():
    assert cgd.fmt_ms(None) == "--:--:--"
    assert cgd.fmt_ms(3723004) == "01:02:03.004"
    assert cgd.fmt_ms(-1500) == "-00:00:01.500"


def test_wall_ms_keys_and_units():
    assert cgd.wall_ms({"at": 12.5}) == 12500
    assert cgd.wall_ms({"ts": "3"}) == 3000
    assert cgd.wall_ms({"at_ms": 42}) == 42
    assert cgd.wall_ms({"updated_at": 1_760_000_000_123}) == 1_760_000_000_123   # already ms
    assert cgd.wall_ms({"at": True}) is None
    assert cgd.wall_ms({"at": float("nan")}) is None
    assert cgd.wall_ms({}) is None


def test_key_from_seq_or_id():
    assert cgd._key({"seq": 7}) == 7
    assert cgd._key({"id": "room:sess:12"}) == 12
    assert cgd._key({"id": "abc"}) == "abc"
    assert cgd._key({}) is None


def test_merge_subtract_and_find_gaps():
    speech = cgd.merge([(0, 5), (4, 8), (10, 12), (3, 3)])
    assert speech == [[0, 8], [10, 12]]
    assert cgd.subtract(speech, [(2, 3), (11, 20)]) == [[0, 2], [3, 8], [10, 11]]
    gaps = cgd.find_gaps([[0, 8], [10, 12]], [(1, "draft"), (7, "caption"), (9, "caption"), (15, "end")])
    assert [(g["start"], g["end"], g["closed_by"]) for g in gaps] == [(0, 1, "draft"), (1, 7, "caption"),
                                                                      (7, 9, "caption"), (10, 15, "end")]
    assert cgd.find_gaps([], [(1, "caption"), (5, "end")]) == []


def test_pause_windows_unpaired_runs_to_end():
    assert cgd.pause_windows([(5, "resume"), (1, "pause"), (9, "pause")], 20) == [(1, 5), (9, 20)]


# --------------------------------------------------------------------------- JSONL: recording live.jsonl
def test_continuous_drafts_are_ok(tmp_path):
    p = _jsonl(tmp_path / "live.jsonl", _live_events())
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 0 and r["status"] == "ok", r["summary_zh"]
    assert set(r) == REPORT_KEYS
    assert r["gaps"]["count"] > 0 and r["gaps"]["p95_ms"] <= 1500
    assert r["gaps"]["offending_count"] == 0
    assert r["drafts"]["windows"] == 10 and r["drafts"]["stuck_count"] == 0
    assert r["drafts"]["p50_ms"] == 3500          # first draft at +1.5 s, final at +5 s of each slice
    s = r["sessions"][0]
    assert s["session"] == "r1/s1" and s["origin_method"] == "min_lag"
    assert s["speech_ms"] == 30000 and s["draft_updates"] == 30 and s["caption_updates"] == 10


def test_finals_only_gaps_follow_slice_length(tmp_path):
    p = _jsonl(tmp_path / "live.jsonl", _live_events(n=8, slice_ms=4000, drafts=False))
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 0, r["summary_zh"]
    assert r["gaps"]["p50_ms"] == 4000 and r["gaps"]["max_ms"] == 4000
    code, r = _report(tmp_path, ["--jsonl", p, "--max-gap-s", "3"], name="r2.json")
    assert code == 1 and r["gaps"]["offending_count"] >= 7


def test_long_silence_on_screen_during_speech_is_listed(tmp_path):
    ev = _live_events(n=5, end=False)
    # 15..35 s of speech: no drafts, one late final at +42 s
    ev.append({"at": T0 + 42.0, "type": "final", "seq": 6, "t0_ms": 15000, "t1_ms": 35000, "text": "很長的一段開示"})
    ev.append({"at": T0 + 45.0, "type": "end"})
    p = _jsonl(tmp_path / "live.jsonl", ev)
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 1 and r["status"] == "warn"
    worst = r["gaps"]["offending"][0]
    # origin = fastest draft (lag 0.5 s), last caption before the gap was final 5 at +17 s
    assert worst["gap_ms"] == 25000 and worst["closed_by"] == "caption"
    assert worst["audio_ms"] == 16500 and worst["open"] is False
    assert worst["start_at"] == T0 + 17 and worst["end_at"] == T0 + 42
    assert any(i["code"] == "gaps" for i in r["issues"])
    assert "沒有新字幕" in r["summary_zh"] and "錄音 00:00:16.500" in r["summary_zh"]


def test_speech_until_end_without_caption_is_open_gap(tmp_path):
    ev = _live_events(n=3, end=False)
    ev.append({"at": T0 + 11.5, "type": "vad", "t0_ms": 9000, "t1_ms": 11000})
    ev.append({"at": T0 + 30.0, "type": "end", "reason": "host"})
    p = _jsonl(tmp_path / "live.jsonl", ev)
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 1
    w = r["gaps"]["offending"][0]
    assert w["closed_by"] == "end" and w["open"] is True and w["gap_ms"] == 19000


def test_stuck_draft_resolved_late_and_never_finalized(tmp_path):
    ev = _live_events(n=2, end=False)
    ev += [
        {"at": T0 + 7.0, "type": "draft", "t0_ms": 6000, "t1_ms": 7000, "text": "慢"},
        {"at": T0 + 9.0, "type": "draft", "t0_ms": 6000, "t1_ms": 8500, "text": "慢慢"},
        {"at": T0 + 19.0, "type": "final", "seq": 3, "t0_ms": 6000, "t1_ms": 9000, "text": "慢慢來"},
        {"at": T0 + 20.0, "type": "draft", "t0_ms": 9000, "t1_ms": 10000, "text": "沒有定稿"},
        {"at": T0 + 40.0, "type": "end"},
    ]
    p = _jsonl(tmp_path / "live.jsonl", ev)
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 1
    stuck = {d["text"]: d for d in r["drafts"]["stuck"]}
    assert stuck["慢慢"]["duration_ms"] == 12000 and stuck["慢慢"]["drafts"] == 2
    assert stuck["慢慢"]["never_finalized"] is False and stuck["慢慢"]["audio_ms"] == 6000
    assert stuck["沒有定稿"]["never_finalized"] is True and stuck["沒有定稿"]["duration_ms"] == 20000
    assert r["drafts"]["stuck_count"] == 2 and r["drafts"]["never_finalized_count"] == 1
    assert "卡住的草稿" in r["summary_zh"] and "到紀錄結束都沒有定稿" in r["summary_zh"]


def test_short_trailing_draft_is_not_stuck(tmp_path):
    ev = _live_events(n=2, end=False)
    ev += [{"at": T0 + 8.0, "type": "draft", "t0_ms": 6000, "t1_ms": 7000, "text": "尾"},
           {"at": T0 + 9.0, "type": "end"}]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "live.jsonl", ev)])
    assert code == 0, r["summary_zh"]
    assert r["drafts"]["never_finalized_count"] == 1 and r["drafts"]["stuck_count"] == 0


def test_adjacent_slice_final_does_not_resolve_next_draft(tmp_path):
    ev = [
        {"at": T0 + 3.5, "type": "draft", "t0_ms": 3000, "t1_ms": 3500, "text": "下一句"},
        {"at": T0 + 4.0, "type": "final", "seq": 1, "t0_ms": 0, "t1_ms": 3000, "text": "上一句"},
        {"at": T0 + 7.0, "type": "final", "seq": 2, "t0_ms": 3000, "t1_ms": 6000, "text": "下一句完成"},
    ]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "live.jsonl", ev)])
    assert r["drafts"]["windows"] == 1 and r["drafts"]["max_ms"] == 3500


def test_pause_excludes_speech_and_closes_gap(tmp_path):
    ev = _live_events(n=2, drafts=False, end=False)
    ev += [
        {"at": T0 + 9.0, "type": "pause", "reason": "no_recognition_5min"},
        {"at": T0 + 60.0, "type": "vad", "t0_ms": 6000, "t1_ms": 50000},       # inside the pause
        {"at": T0 + 70.0, "type": "resume"},
        {"at": T0 + 80.0, "type": "end"},
    ]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "live.jsonl", ev)])
    # origin T0+2 s; gaps: +2..+5, +5..+8, +8..+9 (closed by the pause); the rest is paused
    assert r["gaps"]["count"] == 3 and r["gaps"]["max_ms"] == 3000, r["summary_zh"]
    assert code == 0
    _, nopause = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "np.jsonl", [e for e in ev if e["type"] not in
                                                                              ("pause", "resume")])], name="np.json")
    assert nopause["gaps"]["max_ms"] > 60000


# --------------------------------------------------------------------------- JSONL: live-room bus events
def _bus(seq, at, status, zh="", t0=None, t1=None, version=1, typ=None, **extra):
    ev = {"type": typ or ("final" if status in ("ready", "silent") else "update"), "id": f"r9:s9:{seq}",
          "room_id": "r9", "session_id": "s9", "seq": seq, "version": version, "zh": zh, "en": "",
          "status": status, "at": at}
    if t0 is not None:
        ev.update(t0_ms=t0, t1_ms=t1)
    ev.update(extra)
    return ev


def test_bus_events_keyed_drafts_and_t1_wall(tmp_path):
    w = T0 * 1000
    ev = [
        _bus(1, T0 + 2.1, "queued", t0=0, t1=2000, t1_wall_ms=w + 2000),
        {"type": "draft", "room_id": "r9", "session_id": "s9", "seq": 1, "id": "r9:s9:1", "zh": "大",
         "status": "draft", "at": T0 + 1.0},
        _bus(1, T0 + 3.0, "zh_ready", "大家好", 0, 2000, version=2),
        _bus(1, T0 + 3.5, "ready", "大家好", 0, 2000, version=3),
        {"type": "draft", "room_id": "r9", "session_id": "s9", "seq": 1, "id": "r9:s9:1", "zh": "舊",
         "status": "draft", "at": T0 + 4.0},                                                     # stale
        _bus(2, T0 + 4.1, "queued", t0=2000, t1=4000, t1_wall_ms=w + 4000),
        _bus(2, T0 + 5.0, "ready", "請坐", 2000, 4000, version=2),
    ]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "bus.jsonl", ev)])
    assert code == 0, r["summary_zh"]
    s = r["sessions"][0]
    assert s["session"] == "r9/s9" and s["origin_method"] == "t1_wall_ms"
    assert r["drafts"]["windows"] == 1 and r["drafts"]["max_ms"] == 2000 and r["drafts"]["stale_count"] == 1
    # speech 0..4 s on the wall clock; screen: draft +1.0, zh_ready +3.0, ready +3.5, seq 2 +5.0
    assert r["gaps"]["max_ms"] == 2000 and r["gaps"]["count"] == 4


def test_silent_and_zero_speech_ratio_are_not_speech(tmp_path):
    ev = [
        _bus(1, T0 + 3.0, "ready", "開始", 0, 2000),
        _bus(2, T0 + 30.0, "silent", "", 2000, 20000),
        _bus(3, T0 + 31.0, "ready", "", 20000, 28000, speech_ratio=0.0),
        _bus(4, T0 + 32.0, "ready", "結束", 28000, 30000),
    ]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "bus.jsonl", ev)])
    assert code == 0, r["summary_zh"]
    assert r["sessions"][0]["speech_segments"] == 2


def test_failed_asr_slice_counts_as_uncaptioned_speech(tmp_path):
    ev = [
        _bus(1, T0 + 3.0, "ready", "開始", 0, 2000),
        _bus(2, T0 + 20.0, "timeout", "", 2000, 16000, typ="caption"),
        _bus(3, T0 + 21.0, "ready", "回來了", 16000, 18000),
    ]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "bus.jsonl", ev)])
    assert code == 1
    assert r["gaps"]["offending"][0]["gap_ms"] == 18000            # +3 s .. +21 s


def test_room_level_paused_note_applies_to_session(tmp_path):
    ev = [
        _bus(1, T0 + 3.0, "ready", "開始", 0, 2000),
        {"type": "paused", "room_id": "r9", "at": T0 + 3.5},
        _bus(2, T0 + 30.0, "queued", "", 2000, 20000),
        {"type": "resumed", "room_id": "r9", "at": T0 + 40.0},
        _bus(3, T0 + 41.0, "ready", "回來了", 39000, 40000),
    ]
    p = _jsonl(tmp_path / "bus.jsonl", ev)
    code, r = _report(tmp_path, ["--jsonl", p])
    assert r["gaps"]["max_ms"] <= 2000, r["summary_zh"]
    assert r["pending"]["count"] == 1 and code == 1                 # seq 2 never got a caption
    _, filtered = _report(tmp_path, ["--jsonl", p, "--session", "s9"], name="f.json")
    assert filtered["gaps"]["max_ms"] <= 2000 and [s["session"] for s in filtered["sessions"]] == ["r9/s9"]


def test_p95_over_fail_threshold_exit_2(tmp_path):
    ev = [_bus(i + 1, T0 + (i + 1) * 12 + 0.5, "ready", f"句{i}", i * 12000, (i + 1) * 12000) for i in range(25)]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "bus.jsonl", ev)])
    assert code == 2 and r["status"] == "fail"
    assert r["gaps"]["count"] == 25 and r["gaps"]["p95_ms"] == 12000
    assert any(i["code"] == "gap_p95" and i["level"] == "fail" for i in r["issues"])
    assert "不通過" in r["summary_zh"]


def test_high_p95_from_few_gaps_only_warns(tmp_path):
    ev = [_bus(i + 1, T0 + (i + 1) * 12 + 0.5, "ready", f"句{i}", i * 12000, (i + 1) * 12000) for i in range(5)]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "bus.jsonl", ev)])
    assert code == 1 and r["status"] == "warn"
    assert any(i["code"] == "gap_p95_few" and i["level"] == "warn" for i in r["issues"])


def test_min_lag_option_lengthens_first_wait(tmp_path):
    ev = [_bus(1, T0 + 6.0, "ready", "一", 0, 4000), _bus(2, T0 + 10.0, "ready", "二", 4000, 8000)]
    p = _jsonl(tmp_path / "bus.jsonl", ev)
    _, base = _report(tmp_path, ["--jsonl", p])
    _, lag = _report(tmp_path, ["--jsonl", p, "--min-lag-ms", "1500"], name="lag.json")
    assert base["gaps"]["max_ms"] == 4000 and lag["gaps"]["max_ms"] == 5500


def test_corrupt_tail_warns_and_untimed_counted(tmp_path):
    ev = _live_events(n=3) + [{"type": "final", "seq": 99, "t0_ms": 0, "t1_ms": 1, "text": "無時間"}]
    p = _jsonl(tmp_path / "live.jsonl", ev, tail='{"at": 99, "type": "fin')
    code, r = _report(tmp_path, ["--jsonl", p])
    assert code == 1
    assert r["counts"]["corrupt_lines"] == 1 and r["counts"]["untimed_events"] == 1
    assert any(i["code"] == "corrupt_lines" for i in r["issues"])


def test_session_and_room_filters(tmp_path):
    ev = _live_events(n=3, room="a", session="x") + _live_events(n=3, room="b", session="y")
    p = _jsonl(tmp_path / "live.jsonl", ev)
    _, both = _report(tmp_path, ["--jsonl", p])
    assert [s["session"] for s in both["sessions"]] == ["a/x", "b/y"]
    _, one = _report(tmp_path, ["--jsonl", p, "--session", "y"], name="one.json")
    assert [s["session"] for s in one["sessions"]] == ["b/y"]
    _, room = _report(tmp_path, ["--jsonl", p, "--room", "a"], name="room.json")
    assert [s["session"] for s in room["sessions"]] == ["a/x"]


def test_no_timeline_warns(tmp_path):
    ev = [{"type": "final", "seq": 1, "text": "沒有時間軸", "at": T0 + 1}]
    code, r = _report(tmp_path, ["--jsonl", _jsonl(tmp_path / "x.jsonl", ev)])
    assert code == 1 and any(i["code"] == "no_speech_timeline" for i in r["issues"])


def test_utf8_bom_and_crlf_accepted(tmp_path):
    body = "".join(json.dumps(e, ensure_ascii=False) + "\r\n" for e in _live_events(n=2))
    p = tmp_path / "bom.jsonl"
    p.write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    assert _run(["--jsonl", p]) == 0


# --------------------------------------------------------------------------- SQLite
def test_captions_db_gap_late_rewrite_and_pending(tmp_path):
    rows = _slices(n=5)
    rows.append(("r1", "s1", 6, "ready", "停很久", 20000, 24000, T0 + 34.0))      # 10 s after its audio
    rows.append(("r1", "s1", 7, "ready", "課後修正", 24000, 28000, T0 + 9000.0))  # edited much later
    rows.append(("r1", "s1", 8, "transcribing", "", 28000, 32000, T0 + 32.2))   # never finished
    rows.append(("r1", "s1", 9, "silent", "", 32000, 36000, T0 + 36.5))
    db = _captions_db(tmp_path / "captions.sqlite3", rows)
    code, r = _report(tmp_path, ["--db", db])
    assert code == 1
    assert r["inputs"][0]["schema"] == "captions" and r["inputs"][0]["rows"] == 9
    assert r["counts"]["late_rewrites"] == 1 and r["sessions"][0]["late_rewrites"] == 1
    assert r["pending"]["count"] == 1 and r["pending"]["items"][0]["status"] == "transcribing"
    worst = r["gaps"]["offending"][0]
    # origin = fastest row (seq 8: written 0.2 s after its audio) -> gap +21.5 s .. +34 s on the wall clock
    assert worst["gap_ms"] == 12500 and worst["audio_ms"] == 21300
    assert r["sessions"][0]["duration_ms"] < 60000                  # the late edit does not stretch the session
    assert any("updated_at" in a for a in r["assumptions"])
    assert any("草稿" in a for a in r["assumptions"])
    assert r["drafts"]["windows"] == 0


def test_captions_db_session_filter_and_ok(tmp_path):
    db = _captions_db(tmp_path / "c.sqlite3", _slices(session="s1") + _slices(session="s2", lag=9.0))
    code, r = _report(tmp_path, ["--db", db, "--session", "s1"])
    assert code == 0 and [s["session_id"] for s in r["sessions"]] == ["s1"], r["summary_zh"]
    assert r["gaps"]["max_ms"] == 4000


def test_db_opened_read_only_without_sidecar_files(tmp_path):
    db = _captions_db(tmp_path / "c.sqlite3", _slices())
    before = db.read_bytes()
    assert _run(["--db", db, "--json", tmp_path / "r.json"]) == 0
    assert db.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.sqlite3", "r.json"]


def test_wal_without_shm_is_input_error(tmp_path, capsys):
    db = _captions_db(tmp_path / "c.sqlite3", _slices())
    (tmp_path / "c.sqlite3-wal").write_bytes(b"")
    assert _run(["--db", db]) == 3
    assert "checkpoint" in capsys.readouterr().err


def test_real_caption_store_schema_is_readable(tmp_path):
    sys.path.insert(0, str(ROOT))
    from app.store import CaptionStore

    path = tmp_path / "captions.sqlite3"
    store = CaptionStore(path)
    try:
        for seq, status, zh in ((1, "ready", "大家好"), (2, "ready", "今天打坐"), (3, "queued", "")):
            store.save({"id": f"room:sess:{seq}", "room_id": "room", "session_id": "sess", "seq": seq,
                        "version": 1, "zh": zh, "zh_raw": zh, "en": "", "status": status,
                        "t0_ms": (seq - 1) * 2000, "t1_ms": seq * 2000})
    finally:
        store.close()
    code, r = _report(tmp_path, ["--db", path])
    assert r["inputs"][0]["schema"] == "captions" and r["inputs"][0]["rows"] == 3
    assert r["sessions"][0]["session"] == "room/sess"
    assert r["pending"]["count"] == 1 and code == 1


def test_real_ledger_schema_uses_first_transcript_time(tmp_path):
    sys.path.insert(0, str(ROOT))
    from app.admin.db import ensure_migrated

    path = tmp_path / "zen.sqlite3"
    ensure_migrated(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("INSERT INTO rooms(id, created_at) VALUES ('room', ?)", (T0,))
        conn.execute("INSERT INTO sessions(id, room_id, started_at) VALUES ('sess', 'room', ?)", (T0,))
        segs = [(1, 0, 3000, "translated", None, 3.2, 4.0), (2, 3000, 6000, "translated", 0.8, 6.2, 7.0),
                (3, 6000, 9000, "silent", 0.0, 9.2, None), (4, 9000, 12000, "translated", 0.9, 12.2, 25.0)]
        for seq, t0, t1, status, ratio, created, first in segs:
            sid = f"room:sess:{seq}"
            conn.execute("INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, speech_ratio, status,"
                         " created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (sid, "sess", "room", seq, t0, t1, ratio, status, T0 + created))
            if first is not None:
                conn.execute("INSERT INTO transcripts(segment_id, version, is_current, text, created_at)"
                             " VALUES (?,?,0,?,?)", (sid, 1, f"第{seq}句", T0 + first))
                conn.execute("INSERT INTO transcripts(segment_id, version, is_current, text, created_at)"
                             " VALUES (?,?,1,?,?)", (sid, 2, f"第{seq}句（修）", T0 + first + 500))
        conn.commit()
    code, r = _report(tmp_path, ["--db", path])
    assert r["inputs"][0]["schema"] == "ledger"
    assert r["sessions"][0]["speech_segments"] == 3
    worst = r["gaps"]["offending"][0]
    # origin = created_at - t1 = T0 + 0.2 s; speech 9..12 s -> wall +9.2; first transcript at +25 s
    assert worst["gap_ms"] == 15800 and code == 1


def test_unsupported_schema_and_missing_db_exit_3(tmp_path, capsys):
    db = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("create table foo(x)")
    assert _run(["--db", db]) == 3
    assert _run(["--db", tmp_path / "nope.sqlite3"]) == 3
    bad = _write(tmp_path / "bad.sqlite3", "這不是資料庫" * 50)
    assert _run(["--db", bad]) == 3
    assert "輸入錯誤" in capsys.readouterr().err


def test_db_and_jsonl_combined(tmp_path):
    db = _captions_db(tmp_path / "c.sqlite3", _slices(room="r1", session="s1", n=3))
    drafts = [{"at": T0 + 1.6 + i * 4, "type": "draft", "room_id": "r1", "session_id": "s1", "seq": i + 1,
               "zh": "草", "t0_ms": i * 4000, "t1_ms": i * 4000 + 1000} for i in range(3)]
    p = _jsonl(tmp_path / "d.jsonl", drafts)
    code, r = _report(tmp_path, ["--db", db, "--jsonl", p])
    assert [i["kind"] for i in r["inputs"]] == ["db", "jsonl"]
    assert len(r["sessions"]) == 1 and r["sessions"][0]["draft_updates"] == 3
    # keyed drafts (seq) are resolved by the DB rows with the same seq: +1.6 s -> +5.5 s
    assert r["drafts"]["windows"] == 3 and r["drafts"]["never_finalized_count"] == 0
    assert r["drafts"]["max_ms"] == 3900 and code == 0, r["summary_zh"]


# --------------------------------------------------------------------------- CLI / output
def test_input_errors_exit_3(tmp_path, capsys):
    assert _run(["--jsonl", tmp_path / "missing.jsonl"]) == 3
    assert _run(["--jsonl", tmp_path]) == 3
    p = tmp_path / "big5.jsonl"
    p.write_bytes('{"type": "final", "text": "禪"}'.encode("big5"))
    assert _run(["--jsonl", p]) == 3
    assert _run(["--jsonl", _write(tmp_path / "empty.jsonl", "")]) == 3
    untimed = _jsonl(tmp_path / "u.jsonl", [{"type": "final", "seq": 1, "t0_ms": 0, "t1_ms": 1, "text": "x"}])
    assert _run(["--jsonl", untimed]) == 3
    assert "輸入錯誤" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [
    [],
    ["--bogus"],
    ["--jsonl", "a", "--max-gap-s", "0"],
    ["--jsonl", "a", "--max-draft-s", "-1"],
    ["--jsonl", "a", "--max-gap-s", "12", "--fail-p95-s", "10"],
    ["--jsonl", "a", "--min-lag-ms", "-5"],
    ["--jsonl", "a", "--late-rewrite-s", "nan"],
    ["--jsonl", "a", "--max-gap-s", "abc"],
])
def test_bad_options_exit_3_not_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        cgd.main(argv)
    assert exc.value.code == 3
    assert "參數錯誤" in capsys.readouterr().err


def test_json_written_to_new_file_only(tmp_path, capsys):
    p = _jsonl(tmp_path / "live.jsonl", _live_events(n=2))
    out = tmp_path / "report.json"
    _write(out, "keep me")
    assert _run(["--jsonl", p, "--json", out]) == 4
    assert out.read_text(encoding="utf-8") == "keep me"
    assert "拒絕覆蓋" in capsys.readouterr().err


def test_json_unwritable_parent_exit_4(tmp_path):
    p = _jsonl(tmp_path / "live.jsonl", _live_events(n=2))
    out = tmp_path / "no-such-dir" / "report.json"
    assert _run(["--jsonl", p, "--json", out]) == 4
    assert not out.exists()


def test_json_not_created_on_input_error(tmp_path):
    out = tmp_path / "report.json"
    assert _run(["--jsonl", tmp_path / "x.jsonl", "--json", out]) == 3
    assert not out.exists()


def test_inputs_are_not_modified(tmp_path):
    p = _jsonl(tmp_path / "live.jsonl", _live_events())
    before = p.read_bytes()
    assert _run(["--jsonl", p, "--json", tmp_path / "r.json"]) == 0
    assert p.read_bytes() == before


def test_summary_is_traditional_chinese(tmp_path, capsys):
    p = _jsonl(tmp_path / "live.jsonl", _live_events())
    assert _run(["--jsonl", p]) == 0
    out = capsys.readouterr().out
    assert out.startswith("字幕空窗檢查：通過（exit 0）")
    for word in ("語音活動", "空窗", "草稿→定稿", "假設", "沒有超過門檻的空窗"):
        assert word in out


def test_subprocess_cli_utf8_output(tmp_path):
    p = _jsonl(tmp_path / "live.jsonl", _live_events())
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--jsonl", str(p)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "字幕空窗檢查：通過" in proc.stdout


# --------------------------------------------------------------------------- hygiene
def test_stdlib_only_and_no_network():
    src = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(src)
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert mods <= stdlib | {"__future__"}, mods - stdlib
    assert not mods & {"socket", "urllib", "http", "requests", "subprocess", "ssl", "app"}
    assert "8645" not in src
    assert "mode=ro" in src


def test_shebang_and_docstring_document_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for code in ("``ok``", "``warn``", "``fail``", "(input)", "(output)"):
        assert code in doc
    assert (cgd.EXIT_OK, cgd.EXIT_WARN, cgd.EXIT_FAIL, cgd.EXIT_INPUT, cgd.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
