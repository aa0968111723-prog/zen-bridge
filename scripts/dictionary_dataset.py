"""Stream CC-CEDICT / ECDICT files into gzip NDJSON. Stdlib only.

Does not download sources, invent Traditional Chinese, add unsourced
definitions, or claim a license. IDs hash source-id plus stored fields.
Dictionary text is never eval'd, exec'd, or unpickled.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import sys
import unicodedata
from typing import Iterable, Iterator, Mapping, TextIO

WORD_MAX = 160
DEF_MAX = 8000
PRON_MAX = 160
KEYS_MAX = 64
SAMPLE_MAX = 5
GLOSS_KEY_MAX = 160

STOPWORDS = frozenset(
    """
    a an the of and or to in on for with at by from as
    is are was were be been being am
    it its this that these those
    not no nor but if then than
    into onto upon over under
    i we you he she they them his her their
    """.split()
)

CEDICT_RE = re.compile(r"^(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+/(.*)/$")
EN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*(?:['-][A-Za-z0-9]+)*")
CONTROL_ANY = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
CONTROL_STRICT = re.compile(r"[\x00-\x1f\x7f]")
ECDICT_FIELDS = (
    "word",
    "phonetic",
    "definition",
    "translation",
    "pos",
    "tag",
    "exchange",
)


class _HashingIn(io.RawIOBase):
    def __init__(self, fp):
        self._fp = fp
        self.sha256 = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        view = memoryview(b)
        readinto = getattr(self._fp, "readinto", None)
        if readinto is not None:
            n = readinto(view)
        else:
            chunk = self._fp.read(len(view))
            view[: len(chunk)] = chunk
            n = len(chunk)
        if n:
            self.sha256.update(view[:n])
        return n

    def close(self) -> None:
        if not self.closed:
            self._fp.close()
        super().close()


class _HashingOut:
    def __init__(self, fp):
        self._fp = fp
        self.sha256 = hashlib.sha256()

    def write(self, b) -> int:
        if isinstance(b, memoryview):
            b = b.tobytes()
        elif not isinstance(b, (bytes, bytearray)):
            b = bytes(b)
        self._fp.write(b)
        self.sha256.update(b)
        return len(b)

    def flush(self) -> None:
        self._fp.flush()


def nfkc_lower(value: str) -> str:
    return unicodedata.normalize("NFKC", value).lower().strip()


def entry_id(
    source_id: str,
    word: str,
    alternative: str,
    pronunciation: str,
    zh: str,
    en: str,
) -> str:
    blob = "\0".join(
        [source_id, word, alternative, pronunciation, zh, en]
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def _illegal(value: str, *, allow_breaks: bool) -> bool:
    if allow_breaks:
        return CONTROL_ANY.search(value) is not None
    return CONTROL_STRICT.search(value) is not None


def _too_long(word: str, alternative: str, pronunciation: str, zh: str, en: str) -> bool:
    return (
        len(word) > WORD_MAX
        or len(alternative) > WORD_MAX
        or len(pronunciation) > PRON_MAX
        or len(zh) > DEF_MAX
        or len(en) > DEF_MAX
    )


def _add_key(keys: list[str], seen: set[str], value: str) -> None:
    if len(keys) >= KEYS_MAX:
        return
    folded = nfkc_lower(value)
    if not folded or folded in seen or _illegal(folded, allow_breaks=False):
        return
    if len(folded) > WORD_MAX:
        return
    seen.add(folded)
    keys.append(folded)


def _english_glosses(en: str) -> list[str]:
    text = en.replace("\\n", "\n")
    parts: list[str] = []
    for chunk in re.split(r"[\n/]+", text):
        item = chunk.strip()
        if item:
            parts.append(item)
    return parts


def build_keys(
    word: str,
    alternative: str,
    en: str,
    exchange: str = "",
) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    _add_key(keys, seen, word)
    if alternative:
        _add_key(keys, seen, alternative)
    for gloss in _english_glosses(en):
        if len(gloss) <= GLOSS_KEY_MAX:
            _add_key(keys, seen, gloss)
        for token in EN_WORD_RE.findall(gloss):
            folded = nfkc_lower(token)
            if folded in STOPWORDS:
                continue
            _add_key(keys, seen, token)
    if exchange:
        for part in exchange.split("/"):
            item = part.strip()
            if not item:
                continue
            form = item.split(":", 1)[1] if ":" in item else item
            _add_key(keys, seen, form)
    return keys


def parse_cedict_line(line: str) -> dict[str, str] | None:
    match = CEDICT_RE.match(line.rstrip("\r\n"))
    if not match:
        return None
    traditional, simplified, pinyin, defs = match.groups()
    return {
        "word": traditional,
        "alternative": simplified,
        "pronunciation": pinyin,
        "zh": "",
        "en": defs,
        "exchange": "",
    }


def parse_ecdict_row(row: Mapping[str, str]) -> dict[str, str] | None:
    word = (row.get("word") or "").strip()
    if not word:
        return None
    return {
        "word": word,
        "alternative": "",
        "pronunciation": (row.get("phonetic") or "").strip(),
        "zh": row.get("translation") or "",
        "en": row.get("definition") or "",
        "exchange": row.get("exchange") or "",
    }


def build_entry(source_id: str, parsed: Mapping[str, str]) -> dict | None:
    word = parsed["word"]
    alternative = parsed.get("alternative") or ""
    pronunciation = parsed.get("pronunciation") or ""
    zh = parsed.get("zh") or ""
    en = parsed.get("en") or ""
    if not word:
        return None
    if _illegal(word, allow_breaks=False) or _illegal(alternative, allow_breaks=False):
        return None
    if _illegal(pronunciation, allow_breaks=False):
        return None
    if _illegal(zh, allow_breaks=True) or _illegal(en, allow_breaks=True):
        return None
    if _too_long(word, alternative, pronunciation, zh, en):
        return None
    return {
        "id": entry_id(source_id, word, alternative, pronunciation, zh, en),
        "word": word,
        "alternative": alternative,
        "pronunciation": pronunciation,
        "zh": zh,
        "en": en,
        "keys": build_keys(word, alternative, en, parsed.get("exchange") or ""),
    }


def _open_text_input(path: str):
    fp = open(path, "rb")
    hashing = _HashingIn(fp)
    buf = io.BufferedReader(hashing)
    magic = buf.peek(2)[:2]
    binary: io.BufferedIOBase | gzip.GzipFile
    if magic == b"\x1f\x8b":
        binary = gzip.GzipFile(fileobj=buf, mode="rb")
    else:
        binary = buf
    text = io.TextIOWrapper(binary, encoding="utf-8-sig", errors="replace", newline="")
    return text, hashing


def _iter_cedict(text: TextIO) -> Iterator[dict[str, str] | None]:
    for raw in text:
        line = raw.rstrip("\n\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\ufffd" in line:
            yield None
            continue
        parsed = parse_cedict_line(line)
        yield parsed


def _header_map(row: list[str]) -> dict[str, int] | None:
    names = [cell.strip().lower() for cell in row]
    if not names or names[0] != "word":
        return None
    return {name: index for index, name in enumerate(names) if name}


def _row_dict(row: list[str], header: dict[str, int] | None) -> dict[str, str]:
    out = {name: "" for name in ECDICT_FIELDS}
    if header is None:
        for index, name in enumerate(ECDICT_FIELDS):
            if index < len(row):
                out[name] = row[index]
        return out
    for name in ECDICT_FIELDS:
        index = header.get(name)
        if index is not None and index < len(row):
            out[name] = row[index]
    return out


def _iter_csv_rows(text: TextIO) -> Iterator[list[str] | None]:
    reader = csv.reader(text)
    while True:
        try:
            yield next(reader)
        except StopIteration:
            return
        except csv.Error:
            yield None


def _iter_ecdict(text: TextIO) -> Iterator[dict[str, str] | None]:
    header: dict[str, int] | None = None
    seen_header = False
    for row in _iter_csv_rows(text):
        if row is None:
            yield None
            continue
        if not row or all(not cell.strip() for cell in row):
            continue
        if any("\ufffd" in cell for cell in row):
            yield None
            continue
        if not seen_header:
            mapped = _header_map(row)
            seen_header = True
            if mapped is not None:
                header = mapped
                continue
        yield parse_ecdict_row(_row_dict(row, header))


def convert(fmt: str, input_path: str, source_id: str, output_path: str) -> dict:
    if fmt not in {"cedict", "ecdict"}:
        raise ValueError("format must be cedict or ecdict")
    if not source_id:
        raise ValueError("source-id is required")

    text, hashing_in = _open_text_input(input_path)
    entry_count = 0
    rejected = 0
    sample: list[dict] = []

    try:
        rows: Iterable[dict[str, str] | None]
        if fmt == "cedict":
            rows = _iter_cedict(text)
        else:
            rows = _iter_ecdict(text)

        with open(output_path, "wb") as out_fp:
            hashing_out = _HashingOut(out_fp)
            with gzip.GzipFile(
                fileobj=hashing_out, mode="wb", mtime=0, filename=""
            ) as gz:
                for parsed in rows:
                    if parsed is None:
                        rejected += 1
                        continue
                    entry = build_entry(source_id, parsed)
                    if entry is None:
                        rejected += 1
                        continue
                    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                    gz.write(line.encode("utf-8") + b"\n")
                    entry_count += 1
                    if len(sample) < SAMPLE_MAX:
                        sample.append(entry)
            output_sha256 = hashing_out.sha256.hexdigest()
    finally:
        text.close()
        hashing_in.close()

    return {
        "entry_count": entry_count,
        "rejected": rejected,
        "input_sha256": hashing_in.sha256.hexdigest(),
        "output_sha256": output_sha256,
        "sample": sample,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stream dictionaries to gzip NDJSON")
    parser.add_argument("--format", required=True, choices=("cedict", "ecdict"))
    parser.add_argument("--input", required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    report = convert(args.format, args.input, args.source_id, args.output)
    json.dump(report, sys.stdout, ensure_ascii=False, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
