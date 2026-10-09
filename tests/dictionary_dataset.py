"""Small-fixture tests for scripts/dictionary_dataset.py. Stdlib only."""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "dictionary_dataset.py"

spec = importlib.util.spec_from_file_location("dictionary_dataset", SCRIPT)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(mod)

CEDICT_BODY = """# CC-CEDICT
# comment stays a comment
中國 中国 [Zhong1 guo2] /China/Middle Kingdom/
你好 你好 [ni3 hao3] /Hello/Hi/

not a cedict line
U盤 U盘 [U pan2] /USB flash drive/
"""

ECDICT_HEADER = (
    "word,phonetic,definition,translation,pos,collins,oxford,tag,bnc,frq,"
    "exchange,detail,audio"
)


def _write(path: Path, data: str | bytes, *, gz: bool = False) -> None:
    raw = data if isinstance(data, bytes) else data.encode("utf-8")
    if gz:
        with path.open("wb") as raw_fp:
            with gzip.GzipFile(fileobj=raw_fp, mode="wb", mtime=0, filename="") as handle:
                handle.write(raw)
    else:
        path.write_bytes(raw)


def _read_ndjson(path: Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DictionaryDatasetTests(unittest.TestCase):
    def test_cedict_slash_definitions_and_scripts(self) -> None:
        parsed = mod.parse_cedict_line(
            "中國 中国 [Zhong1 guo2] /China/Middle Kingdom/"
        )
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["word"], "中國")
        self.assertEqual(parsed["alternative"], "中国")
        self.assertEqual(parsed["pronunciation"], "Zhong1 guo2")
        self.assertEqual(parsed["zh"], "")
        self.assertEqual(parsed["en"], "China/Middle Kingdom")
        entry = mod.build_entry("cc-cedict", parsed)
        assert entry is not None
        self.assertEqual(entry["zh"], "")
        self.assertIn("china", entry["keys"])
        self.assertIn("middle kingdom", entry["keys"])
        self.assertIn("中國", entry["keys"])
        self.assertIn("中国", entry["keys"])
        self.assertNotIn("the", entry["keys"])

    def test_cedict_gzip_skips_comments_rejects_bad_keeps_rest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "cedict.txt.gz"
            output = folder / "out.jsonl.gz"
            _write(source, CEDICT_BODY, gz=True)
            report = mod.convert("cedict", str(source), "cc-cedict", str(output))
            rows = _read_ndjson(output)
            self.assertEqual(report["entry_count"], 3)
            self.assertEqual(report["rejected"], 1)
            self.assertEqual(report["input_sha256"], _sha256(source))
            self.assertEqual(report["output_sha256"], _sha256(output))
            self.assertEqual(len(report["sample"]), 3)
            self.assertEqual([row["word"] for row in rows], ["中國", "你好", "U盤"])
            self.assertEqual(rows[0]["en"], "China/Middle Kingdom")
            self.assertEqual(rows[0]["alternative"], "中国")

    def test_ecdict_quoted_csv_chinese_phonetic_exchange(self) -> None:
        csv_text = "\n".join(
            [
                ECDICT_HEADER,
                'hello,həˈləʊ,"a greeting / hi","喂；哈罗",int,1,0,gk,1,1,s:hellos,,',
                '"foo,bar",fʊ,"def with, comma","逗號詞",n,0,0,zk,0,0,s:foo-bars,,',
                "walk,wɔːk,to walk,走,v,0,0,,0,0,s:walks/p:walked/i:walking/d:walked/3:walks,,",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "ecdict.csv"
            output = folder / "out.jsonl.gz"
            _write(source, csv_text)
            report = mod.convert("ecdict", str(source), "ecdict", str(output))
            rows = {row["word"]: row for row in _read_ndjson(output)}
            self.assertEqual(report["entry_count"], 3)
            self.assertEqual(report["rejected"], 0)
            hello = rows["hello"]
            self.assertEqual(hello["pronunciation"], "həˈləʊ")
            self.assertEqual(hello["zh"], "喂；哈罗")
            self.assertEqual(hello["en"], "a greeting / hi")
            self.assertIn("hellos", hello["keys"])
            quoted = rows["foo,bar"]
            self.assertEqual(quoted["en"], "def with, comma")
            self.assertEqual(quoted["zh"], "逗號詞")
            walk = rows["walk"]
            for form in ("walks", "walked", "walking"):
                self.assertIn(form, walk["keys"])
            self.assertNotIn("to", walk["keys"])
            self.assertIn("walk", walk["keys"])

    def test_empty_and_malformed_csv_rows_do_not_drop_file(self) -> None:
        csv_text = "\n".join(
            [
                "word,phonetic,definition,translation,pos,tag,exchange",
                "",
                ",missing-word,def,譯,n,gk,",
                'ok,o,"plain",好,n,gk,s:oks',
                'broken,"unclosed',
                "still,s,after break,仍,n,gk,",
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "bad.csv"
            output = folder / "out.jsonl.gz"
            _write(source, csv_text)
            report = mod.convert("ecdict", str(source), "ecdict", str(output))
            rows = _read_ndjson(output)
            words = [row["word"] for row in rows]
            self.assertIn("ok", words)
            self.assertGreaterEqual(report["rejected"], 1)
            self.assertGreaterEqual(report["entry_count"], 1)
            self.assertNotIn("", words)

    def test_control_characters_and_length_limits(self) -> None:
        self.assertIsNone(
            mod.build_entry(
                "cc-cedict",
                {
                    "word": "bad\x00word",
                    "alternative": "x",
                    "pronunciation": "a",
                    "zh": "",
                    "en": "ok",
                    "exchange": "",
                },
            )
        )
        self.assertIsNone(
            mod.build_entry(
                "ecdict",
                {
                    "word": "ok",
                    "alternative": "",
                    "pronunciation": "a",
                    "zh": "譯",
                    "en": "line\x01feed",
                    "exchange": "",
                },
            )
        )
        self.assertIsNone(
            mod.build_entry(
                "ecdict",
                {
                    "word": "w" * (mod.WORD_MAX + 1),
                    "alternative": "",
                    "pronunciation": "",
                    "zh": "短",
                    "en": "short",
                    "exchange": "",
                },
            )
        )
        self.assertIsNone(
            mod.build_entry(
                "ecdict",
                {
                    "word": "ok",
                    "alternative": "",
                    "pronunciation": "",
                    "zh": "長" * (mod.DEF_MAX + 1),
                    "en": "short",
                    "exchange": "",
                },
            )
        )
        allowed = mod.build_entry(
            "ecdict",
            {
                "word": "ok",
                "alternative": "",
                "pronunciation": "",
                "zh": "第一行\n第二行",
                "en": "first\nsecond",
                "exchange": "",
            },
        )
        self.assertIsNotNone(allowed)

    def test_stable_ids_match_source_and_fields(self) -> None:
        parsed = mod.parse_cedict_line("你好 你好 [ni3 hao3] /Hello/Hi/")
        assert parsed is not None
        first = mod.build_entry("cc-cedict", parsed)
        second = mod.build_entry("cc-cedict", parsed)
        assert first is not None and second is not None
        self.assertEqual(first["id"], second["id"])
        expected = hashlib.sha256(
            "\0".join(
                ["cc-cedict", "你好", "你好", "ni3 hao3", "", "Hello/Hi"]
            ).encode("utf-8")
        ).hexdigest()[:32]
        self.assertEqual(first["id"], expected)
        other_source = mod.build_entry("other", parsed)
        assert other_source is not None
        self.assertNotEqual(first["id"], other_source["id"])

    def test_does_not_invent_traditional_or_definitions(self) -> None:
        parsed = mod.parse_ecdict_row(
            {
                "word": "china",
                "phonetic": "",
                "definition": "a country",
                "translation": "中国",
                "exchange": "",
            }
        )
        assert parsed is not None
        entry = mod.build_entry("ecdict", parsed)
        assert entry is not None
        self.assertEqual(entry["zh"], "中国")
        self.assertNotIn("中國", entry["zh"])
        self.assertNotIn("中國", entry["keys"])
        self.assertEqual(entry["alternative"], "")
        cedict = mod.parse_cedict_line("中国 中国 [Zhong1 guo2] /China/")
        assert cedict is not None
        built = mod.build_entry("cc-cedict", cedict)
        assert built is not None
        self.assertEqual(built["word"], "中国")
        self.assertEqual(built["alternative"], "中国")
        self.assertEqual(built["zh"], "")

    def test_dictionary_text_is_not_executed(self) -> None:
        parsed = mod.parse_ecdict_row(
            {
                "word": "__import__",
                "phonetic": "",
                "definition": "__import__('os').system('echo pwned')",
                "translation": "不要執行",
                "exchange": "",
            }
        )
        assert parsed is not None
        entry = mod.build_entry("ecdict", parsed)
        assert entry is not None
        self.assertIn("__import__", entry["en"])
        self.assertEqual(entry["word"], "__import__")

    def test_keys_cap_and_stopwords(self) -> None:
        words = " ".join(f"term{i}" for i in range(80))
        entry = mod.build_entry(
            "ecdict",
            {
                "word": "widget",
                "alternative": "",
                "pronunciation": "",
                "zh": "",
                "en": "the of and " + words,
                "exchange": "",
            },
        )
        assert entry is not None
        self.assertLessEqual(len(entry["keys"]), 64)
        self.assertEqual(len(entry["keys"]), 64)
        self.assertIn("widget", entry["keys"])
        self.assertNotIn("the", entry["keys"])
        self.assertNotIn("of", entry["keys"])
        self.assertNotIn("and", entry["keys"])
        self.assertIn("term0", entry["keys"])

    def test_cli_stdout_json_and_repeatable_output_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            source = folder / "mini.txt.gz"
            first_out = folder / "a.jsonl.gz"
            second_out = folder / "b.jsonl.gz"
            _write(source, "中國 中国 [Zhong1 guo2] /China/\n", gz=True)
            command = [
                sys.executable,
                str(SCRIPT),
                "--format",
                "cedict",
                "--input",
                str(source),
                "--source-id",
                "cc-cedict",
                "--output",
            ]
            one = subprocess.run(
                command + [str(first_out)],
                check=True,
                capture_output=True,
                text=True,
            )
            two = subprocess.run(
                command + [str(second_out)],
                check=True,
                capture_output=True,
                text=True,
            )
            report = json.loads(one.stdout)
            self.assertEqual(report["entry_count"], 1)
            self.assertEqual(report["rejected"], 0)
            self.assertEqual(report["input_sha256"], _sha256(source))
            self.assertEqual(report["output_sha256"], _sha256(first_out))
            self.assertEqual(len(report["sample"]), 1)
            self.assertEqual(report["sample"][0]["word"], "中國")
            self.assertEqual(_sha256(first_out), _sha256(second_out))
            self.assertEqual(one.stderr, "")
            self.assertEqual(json.loads(two.stdout)["output_sha256"], report["output_sha256"])


if __name__ == "__main__":
    unittest.main()
