"""Tests for tools/transcript_pair_check.py (queue item 35).

Every transcript is a temp fixture written with explicit UTF-8; nothing depends on
host recordings, network, ports, POSIX permissions, path separators or locale.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "transcript_pair_check.py"

sys.path.insert(0, str(ROOT / "tools"))
import transcript_pair_check as tpc  # noqa: E402


REPORT_KEYS = {
    "tool", "schema_version", "status", "exit_code", "match_mode", "offset_ms", "thresholds",
    "live", "complete", "missing", "timing", "divergence", "issues", "summary_zh",
}

LINES = [
    (0, 2000, "大家好，歡迎來到禪學社。"),
    (2100, 4200, "今天我們談坐禪的姿勢。"),
    (4300, 6500, "請先調整呼吸，放鬆肩膀。"),
    (6600, 9000, "接下來靜坐五分鐘。"),
]


# --------------------------------------------------------------------------- fixtures
def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8"))
    return path


def _live_jsonl(path: Path, lines=LINES, *, draft=True, extra: list[dict] | None = None,
                truncated_tail=False, key="seq") -> Path:
    events: list[dict] = [{"at": 1.0, "type": "start", "speaker_id": "spk_a"}]
    for i, (t0, t1, text) in enumerate(lines, 1):
        if draft:
            events.append({"at": 1.0 + i, "type": "draft", "t0_ms": t0, "t1_ms": t0 + 500, "text": text[:2]})
        ev = {"at": 1.5 + i, "type": "final", "t0_ms": t0, "t1_ms": t1, "asr_text": text, "text": text,
              "tgt_lang": "en"}
        if key == "seq":
            ev["seq"] = i
        elif key == "clip":
            ev["clip_id"] = f"c_{i}"
        events.append(ev)
    events.extend(extra or [])
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    if truncated_tail:
        body += '{"at": 99.0, "type": "fin'
    return _write(path, body)


def _transcript_json(path: Path, lines=LINES, *, live_times=True, audio_offset=0, clip=False) -> Path:
    segs = []
    for i, (t0, t1, text) in enumerate(lines, 1):
        segs.append({
            "index": i, "clip_id": f"c_{i}" if clip else None,
            "start_ms": t0 + audio_offset, "end_ms": t1 + audio_offset,
            "live_t0_ms": t0 if live_times else None, "live_t1_ms": t1 if live_times else None,
            "speaker_id": None, "speaker_tag": "Speaker 0", "text": text, "status": "unverified",
            "excluded": False,
        })
    doc = {"recording_id": "rh_20261017_abcd1234", "kind": "harvest", "audio": "audio/", "segments": segs}
    return _write(path, json.dumps(doc, ensure_ascii=False, indent=1))


def _ts(ms: int, sep: str) -> str:
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _srt(path: Path, lines=LINES, *, tag="Speaker 0", newline="\n") -> Path:
    out = []
    for i, (t0, t1, text) in enumerate(lines, 1):
        out += [str(i), f"{_ts(t0, ',')} --> {_ts(t1, ',')}", f"{tag}: {text}" if tag else text, ""]
    return _write(path, newline.join(out))


def _vtt(path: Path, lines=LINES, tag="Speaker 0") -> Path:
    out = ["WEBVTT", ""]
    for t0, t1, text in lines:
        out += [f"{_ts(t0, '.')} --> {_ts(t1, '.')}", f"<v {tag}>{text}", ""]
    return _write(path, "\n".join(out))


def _txt(path: Path, lines=LINES, *, mark="【未校對】", tag="Speaker 0") -> Path:
    body = "".join(f"[{_ts(t0, '.')}] {tag}: {mark}{text}\n" for t0, _t1, text in lines)
    return _write(path, body)


def _check(live: Path, comp: Path, **kw) -> dict:
    return tpc.analyze(tpc.load(live), tpc.load(comp), **kw)


@pytest.fixture
def rec_dir(tmp_path: Path) -> Path:
    d = tmp_path / "recordings" / "harvest" / "mixed" / "rh_20261017_abcd1234"
    d.mkdir(parents=True)
    _live_jsonl(d / "live.jsonl")
    _transcript_json(d / "transcript.json")
    _srt(d / "transcript.srt")
    _vtt(d / "transcript.vtt")
    _txt(d / "transcript.txt")
    return d


# --------------------------------------------------------------------------- tokenize / helpers
def test_tokenize_cjk_one_token_per_char_and_ignores_punct():
    assert tpc.tokenize("大家好，歡迎！") == ["大", "家", "好", "歡", "迎"]


def test_tokenize_latin_words_case_and_fullwidth():
    assert tpc.tokenize("Hello  ＷＯＲＬＤ 2026, zen-bridge") == ["hello", "world", "2026", "zen", "bridge"]


def test_tokenize_kana_mixed_with_latin():
    assert tpc.tokenize("坐禅はZenです") == ["坐", "禅", "は", "zen", "で", "す"]


def test_tokenize_keep_punct():
    assert tpc.tokenize("好，", keep_punct=True) == ["好", ","]  # NFKC maps the fullwidth comma


def test_fmt_ms():
    assert tpc.fmt_ms(3723004) == "01:02:03.004"
    assert tpc.fmt_ms(None) == "--:--:--"


# --------------------------------------------------------------------------- parsing
def test_parse_live_jsonl_finals_only_and_drafts_ignored(tmp_path):
    p = tpc.load(_live_jsonl(tmp_path / "live.jsonl"))
    assert p.fmt == "jsonl"
    assert [s.text for s in p.segments] == [t for _a, _b, t in LINES]
    assert p.drafts_ignored == len(LINES)
    assert p.other_events == 1  # the "start" event
    assert p.segments[1].start_ms == 2100 and p.segments[1].end_ms == 4200


def test_parse_live_jsonl_last_revision_wins(tmp_path):
    extra = [{"at": 50.0, "type": "final", "seq": 2, "t0_ms": 2100, "t1_ms": 4200, "text": "今天我們談坐禪的姿勢與呼吸。"}]
    p = tpc.load(_live_jsonl(tmp_path / "live.jsonl", extra=extra))
    assert p.revisions == 1
    assert len(p.segments) == len(LINES)
    assert p.segments[1].text == "今天我們談坐禪的姿勢與呼吸。"


def test_parse_live_jsonl_truncated_last_line_counted(tmp_path):
    p = tpc.load(_live_jsonl(tmp_path / "live.jsonl", truncated_tail=True))
    assert len(p.segments) == len(LINES)
    assert len(p.corrupt_lines) == 1


def test_parse_generic_jsonl_seconds(tmp_path):
    f = _write(tmp_path / "seg.jsonl", '{"start": 1.5, "end": 2.25, "text": "你好"}\n'
                                       '{"start": "00:00:03.000", "end": 4, "text": "再見"}\n')
    p = tpc.load(f)
    assert [(s.start_ms, s.end_ms, s.text) for s in p.segments] == [(1500, 2250, "你好"), (3000, 4000, "再見")]


def test_parse_transcript_json_uses_live_timeline(tmp_path):
    p = tpc.load(_transcript_json(tmp_path / "transcript.json", audio_offset=777))
    assert p.fmt == "json" and p.timeline == "live"
    assert p.segments[0].start_ms == 777
    assert p.segments[0].align_start == 0


def test_parse_transcript_json_without_live_times(tmp_path):
    p = tpc.load(_transcript_json(tmp_path / "transcript.json", live_times=False))
    assert p.timeline == "native"


def test_parse_json_plain_list(tmp_path):
    f = _write(tmp_path / "x.json", json.dumps([{"start_ms": 0, "end_ms": 900, "text": "一"}], ensure_ascii=False))
    assert [s.text for s in tpc.load(f).segments] == ["一"]


def test_parse_json_invalid_raises_input_error(tmp_path):
    with pytest.raises(tpc.InputError):
        tpc.load(_write(tmp_path / "bad.json", "{not json"))


def test_parse_json_without_segments_raises(tmp_path):
    with pytest.raises(tpc.InputError):
        tpc.load(_write(tmp_path / "bad.json", '{"foo": 1}'))


def test_parse_srt_crlf_and_speaker_tag_stripped(tmp_path):
    p = tpc.load(_srt(tmp_path / "t.srt", newline="\r\n"))
    assert p.fmt == "srt"
    assert [s.text for s in p.segments] == [t for _a, _b, t in LINES]
    assert p.speaker_tags_stripped == len(LINES)
    assert (p.segments[2].start_ms, p.segments[2].end_ms) == (4300, 6500)


def test_parse_srt_multiline_cue(tmp_path):
    f = _write(tmp_path / "m.srt", "1\n00:00:00,000 --> 00:00:01,000\n第一行\n第二行\n\n2\n00:00:01,000 --> 00:00:02,000\n下一句\n")
    p = tpc.load(f)
    assert [s.text for s in p.segments] == ["第一行 第二行", "下一句"]


def test_parse_vtt_voice_tag(tmp_path):
    p = tpc.load(_vtt(tmp_path / "t.vtt"))
    assert p.fmt == "vtt"
    assert p.segments[0].text == LINES[0][2]
    assert p.speaker_tags_stripped == len(LINES)


def test_parse_txt_marks_and_tags(tmp_path):
    p = tpc.load(_txt(tmp_path / "t.txt"))
    assert p.fmt == "txt"
    assert [s.text for s in p.segments] == [t for _a, _b, t in LINES]
    assert p.segments[0].end_ms is None


def test_parse_txt_short_timestamps_and_continuation(tmp_path):
    f = _write(tmp_path / "t.txt", "[00:05] 第一句\n接續的文字\n[01:02:03] 第二句\n")
    p = tpc.load(f)
    assert [(s.start_ms, s.text) for s in p.segments] == [(5000, "第一句 接續的文字"), (3723000, "第二句")]


def test_speaker_tag_not_stripped_without_majority(tmp_path):
    lines = [(0, 1000, "老師: 請坐"), (1000, 2000, "大家好"), (2000, 3000, "開始")]
    p = tpc.load(_srt(tmp_path / "t.srt", lines, tag=""))
    assert p.segments[0].text == "老師: 請坐"


def test_keep_speaker_tags_option(tmp_path):
    p = tpc.load(_srt(tmp_path / "t.srt"), strip_speaker_tags=False)
    assert p.segments[0].text.startswith("Speaker 0: ")


def test_detect_format_by_content_without_extension(tmp_path):
    assert tpc.load(_live_jsonl(tmp_path / "a")).fmt == "jsonl"
    assert tpc.load(_srt(tmp_path / "b")).fmt == "srt"
    assert tpc.load(_vtt(tmp_path / "c")).fmt == "vtt"
    assert tpc.load(_txt(tmp_path / "d")).fmt == "txt"
    assert tpc.load(_transcript_json(tmp_path / "e")).fmt == "json"


def test_unknown_format_raises(tmp_path):
    with pytest.raises(tpc.InputError):
        tpc.load(_write(tmp_path / "x.dat", "只是一段沒有時間的文字\n"))


def test_utf8_bom_accepted(tmp_path):
    f = tmp_path / "t.srt"
    f.write_bytes(b"\xef\xbb\xbf" + "1\n00:00:00,000 --> 00:00:01,000\n你好\n".encode("utf-8"))
    assert tpc.load(f).segments[0].text == "你好"


def test_utf16_bom_accepted(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes("[00:00:01] 你好\r\n".encode("utf-16"))
    assert tpc.load(f).segments[0].text == "你好"


def test_non_utf8_raises_input_error(tmp_path):
    f = tmp_path / "t.txt"
    f.write_bytes("[00:00:01] 你好\n".encode("big5"))
    with pytest.raises(tpc.InputError):
        tpc.load(f)


def test_missing_file_raises_input_error(tmp_path):
    with pytest.raises(tpc.InputError):
        tpc.load(tmp_path / "nope.jsonl")


# --------------------------------------------------------------------------- analysis
@pytest.mark.parametrize("name", ["transcript.json", "transcript.srt", "transcript.vtt", "transcript.txt"])
def test_identical_pair_is_ok_for_every_complete_format(rec_dir, name):
    r = _check(rec_dir / "live.jsonl", rec_dir / name)
    assert r["status"] == "ok", r["issues"]
    assert r["exit_code"] == 0
    assert r["divergence"]["matched_divergence"] == 0.0
    assert r["divergence"]["paired_groups"] == len(LINES)
    assert r["missing"]["in_complete_count"] == 0 and r["missing"]["in_live_count"] == 0
    assert set(r) == REPORT_KEYS


def test_missing_segment_in_complete_fails(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    comp = _transcript_json(tmp_path / "transcript.json", LINES[:2] + LINES[3:])
    r = _check(live, comp)
    assert r["status"] == "fail" and r["exit_code"] == 2
    assert r["missing"]["in_complete_count"] == 1
    assert r["missing"]["in_complete"][0]["start_ms"] == 4300
    assert any(i["code"] == "missing_in_complete" and i["level"] == "fail" for i in r["issues"])


def test_small_missing_below_threshold_warns(tmp_path):
    lines = [(i * 2000, i * 2000 + 1800, f"第{i}段很長的內容用來測試缺段比例門檻是否正確計算") for i in range(30)]
    lines.append((60000, 60500, "嗯"))
    live = _live_jsonl(tmp_path / "live.jsonl", lines)
    comp = _transcript_json(tmp_path / "transcript.json", lines[:-1])
    r = _check(live, comp)
    assert r["status"] == "warn" and r["exit_code"] == 1
    assert r["missing"]["in_complete_count"] == 1
    assert r["missing"]["in_complete_token_ratio"] < tpc.DEFAULT_FAIL_MISSING


def test_segment_only_in_complete_reported(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl", LINES[:3])
    comp = _transcript_json(tmp_path / "transcript.json")
    r = _check(live, comp)
    assert r["missing"]["in_live_count"] == 1
    assert r["missing"]["in_live"][0]["start_ms"] == 6600
    assert r["status"] == "fail"  # 9 of ~44 tokens > 5%


def test_text_divergence_warn_and_fail(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    small = list(LINES)
    small[1] = (2100, 4200, "今天我們來談坐禪的姿勢。")
    r = _check(live, _transcript_json(tmp_path / "a.json", small), warn_divergence=0.01)
    assert r["status"] == "warn"
    assert 0 < r["divergence"]["matched_divergence"] < 0.05
    assert r["divergence"]["worst"][0]["start_ms"] == 2100

    big = [(a, b, "完全不同的另一段話語內容") for a, b, _t in LINES]
    r2 = _check(live, _transcript_json(tmp_path / "b.json", big))
    assert r2["status"] == "fail"
    assert r2["divergence"]["matched_divergence"] >= tpc.DEFAULT_FAIL_DIVERGENCE
    assert any(i["code"] == "divergence" and i["level"] == "fail" for i in r2["issues"])


def test_punctuation_and_whitespace_only_difference_is_ok(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    variant = [(a, b, t.replace("，", " ").replace("。", "")) for a, b, t in LINES]
    r = _check(live, _transcript_json(tmp_path / "t.json", variant))
    assert r["status"] == "ok"
    r2 = _check(live, _transcript_json(tmp_path / "t2.json", variant), keep_punct=True)
    assert r2["divergence"]["matched_divergence"] > 0


def test_merged_segments_compare_as_one_group(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    merged = [(0, 4200, LINES[0][2] + LINES[1][2])] + LINES[2:]
    r = _check(live, _transcript_json(tmp_path / "t.json", merged))
    assert r["status"] == "ok", r["issues"]
    assert r["divergence"]["paired_groups"] == 3
    assert r["divergence"]["worst"] == []


def test_complete_backwards_fails_live_backwards_warns(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    swapped = [LINES[0], LINES[2], LINES[1], LINES[3]]
    r = _check(live, _srt(tmp_path / "t.srt", swapped))
    assert r["timing"]["complete"]["backwards_count"] == 1
    assert any(i["code"] == "complete_backwards" and i["level"] == "fail" for i in r["issues"])
    assert r["status"] == "fail"

    live2 = _live_jsonl(tmp_path / "live2.jsonl", swapped)
    r2 = _check(live2, _transcript_json(tmp_path / "t.json"))
    assert r2["timing"]["live"]["backwards_count"] == 1
    assert r2["status"] == "warn"
    assert r2["missing"]["in_complete_count"] == 0


def test_overlap_warns(tmp_path):
    lines = list(LINES)
    lines[1] = (1500, 4200, LINES[1][2])
    r = _check(_live_jsonl(tmp_path / "live.jsonl"), _srt(tmp_path / "t.srt", lines))
    assert r["timing"]["complete"]["overlaps_count"] == 1
    assert r["timing"]["complete"]["overlaps"][0]["overlap_ms"] == 500
    assert r["status"] == "warn"


def test_small_overlap_within_tolerance_ignored(tmp_path):
    lines = list(LINES)
    lines[1] = (1980, 4200, LINES[1][2])
    r = _check(_live_jsonl(tmp_path / "live.jsonl"), _srt(tmp_path / "t.srt", lines))
    assert r["timing"]["complete"]["overlaps_count"] == 0
    assert r["status"] == "ok"


def test_negative_duration_in_complete_fails(tmp_path):
    lines = list(LINES)
    lines[2] = (4300, 4000, LINES[2][2])
    r = _check(_live_jsonl(tmp_path / "live.jsonl"), _transcript_json(tmp_path / "t.json", lines, live_times=False))
    assert r["timing"]["complete"]["negative_count"] == 1
    assert r["status"] == "fail"


def test_truncated_live_tail_only_warns(tmp_path):
    r = _check(_live_jsonl(tmp_path / "live.jsonl", truncated_tail=True), _transcript_json(tmp_path / "t.json"))
    assert r["status"] == "warn"
    assert r["live"]["corrupt_line_count"] == 1


def test_studio_clip_id_pairing_with_different_audio_timeline(tmp_path):
    # studio: transcript start_ms is the offset in the concatenated recording.wav, not the live timeline
    live = _live_jsonl(tmp_path / "live.jsonl", key="clip")
    comp = _transcript_json(tmp_path / "t.json", live_times=False, audio_offset=-1500, clip=True)
    r = _check(live, comp)
    assert r["match_mode"] == "key"
    assert r["status"] == "ok", r["issues"]


def test_time_mismatch_reports_nothing_paired_then_offset_fixes(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl")
    shifted = [(a + 600000, b + 600000, t) for a, b, t in LINES]
    comp = _srt(tmp_path / "t.srt", shifted)
    r = _check(live, comp)
    assert r["status"] == "fail"
    assert any(i["code"] == "nothing_paired" for i in r["issues"])
    r2 = _check(live, comp, offset_ms=-600000)
    assert r2["status"] == "ok", r2["issues"]


def test_txt_without_end_times_pairs_by_start(tmp_path):
    jittered = [(a + 300, b, t) for a, b, t in LINES]
    r = _check(_live_jsonl(tmp_path / "live.jsonl"), _txt(tmp_path / "t.txt", jittered))
    assert r["status"] == "ok", r["issues"]
    assert r["divergence"]["paired_groups"] == len(LINES)


def test_japanese_text_divergence(tmp_path):
    lines = [(0, 2000, "皆さん、こんにちは。"), (2100, 4000, "座禅を始めます。")]
    live = _live_jsonl(tmp_path / "live.jsonl", lines)
    changed = [(0, 2000, "皆さん、こんばんは。"), (2100, 4000, "座禅を始めます。")]
    r = _check(live, _transcript_json(tmp_path / "t.json", changed))
    assert 0 < r["divergence"]["matched_divergence"] < 0.2


def test_empty_live_side_fails(tmp_path):
    live = _write(tmp_path / "live.jsonl", json.dumps({"at": 1, "type": "start"}) + "\n")
    r = _check(live, _transcript_json(tmp_path / "t.json"))
    assert r["status"] == "fail"
    assert any(i["code"] == "live_empty" for i in r["issues"])


def test_summary_is_traditional_chinese(rec_dir):
    r = _check(rec_dir / "live.jsonl", rec_dir / "transcript.json")
    s = r["summary_zh"]
    assert "逐字稿比對：通過" in s and "文字落差率" in s and "缺段" in s and "時間戳" in s


# --------------------------------------------------------------------------- CLI
def test_main_dir_ok(rec_dir, capsys):
    assert tpc.main(["--dir", str(rec_dir)]) == 0
    out = capsys.readouterr().out
    assert "通過" in out and "transcript.json" in out


def test_main_missing_segment_exit_2(tmp_path, capsys):
    live = _live_jsonl(tmp_path / "live.jsonl")
    comp = _transcript_json(tmp_path / "t.json", LINES[:2])
    assert tpc.main(["--live", str(live), "--complete", str(comp)]) == 2
    assert "完整稿缺" in capsys.readouterr().out


def test_main_warn_exit_1(tmp_path):
    live = _live_jsonl(tmp_path / "live.jsonl", truncated_tail=True)
    comp = _transcript_json(tmp_path / "t.json")
    assert tpc.main(["--live", str(live), "--complete", str(comp)]) == 1


def test_main_input_errors_exit_3(tmp_path, capsys):
    comp = _transcript_json(tmp_path / "t.json")
    assert tpc.main(["--live", str(tmp_path / "nope.jsonl"), "--complete", str(comp)]) == 3
    assert "輸入錯誤" in capsys.readouterr().err
    empty = tmp_path / "empty"
    empty.mkdir()
    assert tpc.main(["--dir", str(empty)]) == 3
    assert tpc.main(["--dir", str(tmp_path / "no-such-dir")]) == 3
    e1 = _write(tmp_path / "e1.jsonl", "")
    e2 = _write(tmp_path / "e2.srt", "")
    assert tpc.main(["--live", str(e1), "--complete", str(e2)]) == 3


@pytest.mark.parametrize("argv", [
    [],
    ["--live", "a.jsonl"],
    ["--bogus"],
    ["--warn-divergence", "1.5", "--live", "a", "--complete", "b"],
    ["--warn-divergence", "0.5", "--fail-divergence", "0.1", "--live", "a", "--complete", "b"],
    ["--min-overlap-ms", "-1", "--live", "a", "--complete", "b"],
])
def test_main_bad_options_exit_3_not_2(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        tpc.main(argv)
    assert exc.value.code == 3


def test_main_dir_with_live_rejected(rec_dir, capsys):
    with pytest.raises(SystemExit) as exc:
        tpc.main(["--dir", str(rec_dir), "--live", str(rec_dir / "live.jsonl")])
    assert exc.value.code == 3


def test_json_report_written_to_new_file(rec_dir, tmp_path):
    out = tmp_path / "report.json"
    assert tpc.main(["--dir", str(rec_dir), "--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) == REPORT_KEYS
    assert data["tool"] == "transcript_pair_check" and data["status"] == "ok"
    assert data["live"]["format"] == "jsonl" and data["complete"]["format"] == "json"


def test_json_never_overwrites_existing(rec_dir, tmp_path, capsys):
    out = tmp_path / "report.json"
    _write(out, "keep me")
    assert tpc.main(["--dir", str(rec_dir), "--json", str(out)]) == 4
    assert out.read_text(encoding="utf-8") == "keep me"
    assert "拒絕覆蓋" in capsys.readouterr().err


def test_json_unwritable_parent_exit_4(rec_dir, tmp_path):
    out = tmp_path / "no-such-dir" / "report.json"
    assert tpc.main(["--dir", str(rec_dir), "--json", str(out)]) == 4
    assert not out.exists()


def test_json_not_created_on_input_error(tmp_path):
    out = tmp_path / "report.json"
    assert tpc.main(["--live", str(tmp_path / "x.jsonl"), "--complete", str(tmp_path / "y.json"),
                     "--json", str(out)]) == 3
    assert not out.exists()


def test_inputs_are_read_only(rec_dir, tmp_path):
    before = {p.name: p.read_bytes() for p in rec_dir.iterdir()}
    assert tpc.main(["--dir", str(rec_dir), "--json", str(tmp_path / "r.json")]) == 0
    after = {p.name: p.read_bytes() for p in rec_dir.iterdir()}
    assert before == after


def test_subprocess_cli_utf8_output(rec_dir):
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--live", str(rec_dir / "live.jsonl"), "--complete", str(rec_dir / "transcript.srt")],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "逐字稿比對：通過" in proc.stdout


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
    assert not mods & {"socket", "urllib", "http", "requests", "subprocess", "ssl"}
    assert "8645" not in src


def test_shebang_and_docstring_document_exit_codes():
    src = TOOL.read_text(encoding="utf-8")
    assert src.startswith("#!/usr/bin/env python3")
    doc = ast.get_docstring(ast.parse(src)) or ""
    for code in ("``ok``", "``warn``", "``fail``", "(input)", "(output)"):
        assert code in doc
    assert (tpc.EXIT_OK, tpc.EXIT_WARN, tpc.EXIT_FAIL, tpc.EXIT_INPUT, tpc.EXIT_OUTPUT) == (0, 1, 2, 3, 4)
