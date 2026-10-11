from __future__ import annotations

from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tools.glossary_gap_candidates import FIELDS, collect_candidates, main
from tools.glossary_term_stats import _open_readonly


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "glossary_gap_candidates.py"


@pytest.fixture
def inputs(tmp_path):
    database = tmp_path / "captions # 禪.sqlite3"
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "CREATE TABLE captions(id TEXT PRIMARY KEY, session_id TEXT, zh TEXT, "
            "zh_raw TEXT, en TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO captions VALUES (?,?,?,?,?,?)",
            [
                ("c1", "s1", "菩提心，菩提心。禪修！我們，所以。", "原始辨識", "", "ready"),
                ("c2", "s2", "菩提心。禪修，我們，所以。", "原始辨識", "", "ready"),
                ("c3", "s2", "菩提心。禪修，我們，所以。", "原始辨識", "", "ready"),
                ("draft", "s1", "未完成未完成未完成", "", "", "asr"),
                ("failed", "s1", "失敗失敗失敗", "", "", "error"),
                ("silent", "s1", "靜音靜音靜音", "", "", "silent"),
                ("empty", "s1", None, "原始辨識原始辨識原始辨識", "", "ready"),
            ],
        )
        conn.commit()
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"terms": [{"zh": "禪修", "ja": "禅修"}]}), encoding="utf-8")
    return database, glossary


def add_caption(database, text, caption_id="extra", session="s3"):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute(
            "INSERT INTO captions VALUES (?,?,?,?,?,?)",
            (caption_id, session, text, "", "", "ready"),
        )
        conn.commit()


def test_counts_pruning_stoplist_final_source_and_readonly(inputs):
    database, glossary = inputs
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in inputs}
    assert collect_candidates(database, glossary) == [{
        "term": "菩提心", "count": 4, "sessions": 2, "example_caption_id": "c1",
        "example_text": "菩提心，菩提心。禪修！我們，所以。",
    }]
    with closing(_open_readonly(database)) as conn:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM captions")
    for path in inputs:
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before[path]


def test_shorter_candidate_retained_when_count_differs(inputs):
    database, glossary = inputs
    add_caption(database, "菩提")
    rows = collect_candidates(database, glossary)
    assert [(row["term"], row["count"]) for row in rows] == [("菩提", 5), ("菩提心", 4)]


def test_session_and_threshold(inputs):
    database, glossary = inputs
    assert collect_candidates(database, glossary, session="s1") == []
    assert collect_candidates(database, glossary, min_count=2, session="s1")[0]["count"] == 2
    assert collect_candidates(database, glossary, session="' OR 1=1 --") == []
    assert collect_candidates(database, glossary, min_count=5) == []


@pytest.mark.parametrize("suffix", [".json", ".csv"])
def test_glossary_exact_substrings_blank_targets_and_bom(inputs, tmp_path, suffix):
    database, _ = inputs
    glossary = tmp_path / ("export" + suffix)
    glossary.write_text(
        '[{"zh":"菩提心","en":""},{"zh":"禪修法門","ja":"禅修"}]' if suffix == ".json"
        else "zh,en,ja\n菩提心,,\n禪修法門,,禅修\n",
        encoding="utf-8-sig",
    )
    before = glossary.read_bytes()
    assert collect_candidates(database, glossary) == []
    assert glossary.read_bytes() == before


def test_sqlite_glossary_is_readonly(inputs, tmp_path):
    database, _ = inputs
    glossary = tmp_path / "glossary.sqlite3"
    with closing(sqlite3.connect(glossary)) as conn:
        conn.execute("CREATE TABLE room_glossary(room_id TEXT, terms_json TEXT)")
        conn.execute("INSERT INTO room_glossary VALUES (?,?)", (
            "room", json.dumps([{"zh": "菩提心", "ja": "菩提心"}, {"zh": "禪修", "en": "Zen"}]),
        ))
        conn.commit()
    before = glossary.read_bytes()
    assert collect_candidates(database, glossary) == []
    assert glossary.read_bytes() == before


def test_han_runs_lengths_overlaps_and_example_truncation(inputs):
    database, glossary = inputs
    text = "a" * 61 + "。佛性真如，佛性真如！佛性真如；無無無無；公1案 公a案 公 あ案"
    add_caption(database, text)
    rows = {row["term"]: row for row in collect_candidates(database, glossary)}
    assert rows["佛性真如"]["count"] == 3
    assert rows["佛性真如"]["example_text"] == "a" * 60
    assert rows["無無"]["count"] == 3
    assert "公案" not in rows
    assert "佛性" not in rows
    assert all(2 <= len(term) <= 4 for term in rows)


@pytest.mark.parametrize("excel", [False, True])
@pytest.mark.parametrize("to_file", [False, True])
def test_cli_csv_and_bom_and_unchanged_inputs(inputs, tmp_path, excel, to_file):
    database, glossary = inputs
    before = {path: path.read_bytes() for path in inputs}
    args = [sys.executable, str(SCRIPT), "--db", str(database), "--glossary", str(glossary)]
    if excel:
        args.append("--excel")
    output = tmp_path / "candidates.csv"
    if to_file:
        args += ["--out", str(output)]
    result = subprocess.run(args, capture_output=True, check=True)
    payload = output.read_bytes() if to_file else result.stdout
    assert payload.startswith(b"\xef\xbb\xbf") is excel
    reader = csv.DictReader(io.StringIO(payload.decode("utf-8-sig")))
    assert reader.fieldnames == list(FIELDS)
    assert list(reader)[0]["term"] == "菩提心"
    for path in inputs:
        assert path.read_bytes() == before[path]


@pytest.mark.parametrize("destination", ["db", "glossary", "wal", "existing", "symlink", "hardlink"])
def test_out_never_overwrites_inputs_or_other_files(inputs, tmp_path, destination, capsys, monkeypatch):
    database, glossary = inputs
    output = tmp_path / "output.csv"
    if destination == "db":
        output = database
    elif destination == "glossary":
        output = glossary
    elif destination == "wal":
        output = Path(str(database) + "-wal")
    elif destination == "symlink":
        try:
            output.symlink_to(glossary)
        except OSError as exc:
            if getattr(exc,'winerror',None)!=1314:
                raise
            # Unprivileged Windows cannot create file symlinks. Exercise the
            # resolved-input boundary without changing OS security settings.
            original=Path.resolve
            def resolved(path,*args,**kwargs):
                return glossary if path==output else original(path,*args,**kwargs)
            monkeypatch.setattr(Path,'resolve',resolved)
    elif destination == "hardlink":
        output.hardlink_to(glossary)
    else:
        output.write_bytes(b"keep this dictionary")
    before = {path: path.read_bytes() for path in inputs}
    output_before = output.read_bytes() if output.exists() else None
    assert main(["--db", str(database), "--glossary", str(glossary), "--out", str(output)]) == 2
    assert "glossary_gap_candidates:" in capsys.readouterr().err
    for path in inputs:
        assert path.read_bytes() == before[path]
    assert (output.read_bytes() if output.exists() else None) == output_before


@pytest.mark.parametrize("bad", ["db", "glossary", "schema", "json", "zh", "count", "out"])
def test_bad_input_exits_two_without_creating_output(inputs, tmp_path, bad):
    database, glossary = inputs
    extra = []
    if bad == "db":
        database = tmp_path / "missing.sqlite3"
    elif bad == "glossary":
        glossary = tmp_path / "missing.json"
    elif bad == "schema":
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("DROP TABLE captions")
            conn.commit()
    elif bad in {"json", "zh"}:
        glossary.write_text("{" if bad == "json" else '[{"zh":""}]', encoding="utf-8")
    elif bad == "count":
        extra = ["--min-count", "0"]
    output = tmp_path / "missing-parent" / "out.csv" if bad == "out" else tmp_path / "out.csv"
    before = {path: path.read_bytes() for path in (database, glossary) if path.exists()}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(database), "--glossary", str(glossary),
         "--out", str(output), *extra], capture_output=True,
    )
    assert result.returncode == 2
    assert result.stderr
    assert not output.exists()
    for path, payload in before.items():
        assert path.read_bytes() == payload
    if bad in {"db", "glossary"}:
        assert not (database if bad == "db" else glossary).exists()


def test_empty_csv_has_header(inputs, capsys):
    database, glossary = inputs
    assert main(["--db", str(database), "--glossary", str(glossary), "--min-count", "99"]) == 0
    assert capsys.readouterr().out.strip() == ",".join(FIELDS)


def test_committed_wal_is_read_without_changes(inputs):
    database, glossary = inputs
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO captions VALUES ('wal','s3','菩提心','','','ready')")
        writer.commit()
        wal = Path(str(database) + "-wal")
        before = {path: path.read_bytes() for path in (database, glossary, wal)}
        assert collect_candidates(database, glossary)[0]["count"] == 5
        for path, payload in before.items():
            assert path.read_bytes() == payload
