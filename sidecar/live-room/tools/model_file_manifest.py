#!/usr/bin/env python3
"""Offline model file manifest: sha256 + size inventory of a local model folder (queue item 37).

  python tools/model_file_manifest.py generate --root models --out models.manifest.json
  python tools/model_file_manifest.py verify --root models --manifest models.manifest.json
  python tools/model_file_manifest.py verify --manifest models/manifest.json --quick --json OUT.json

zen-bridge runs everything from model files that sit on the laptop: the
whisper.cpp / Breeze ASR ``ggml-*.bin`` weights, the MT ``*.gguf`` files for
llama.cpp / Ollama, the Silero VAD ``*.onnx`` / ``*.jit`` file and their small
config / vocabulary files.  A truncated copy, a half-finished download or a
bit-flipped USB transfer usually shows up only as a crash or garbage captions
in the middle of a class.  This tool catches that beforehand:

* ``generate`` walks the model root and writes a manifest (JSON) to a **new**
  file: every regular file with its size and sha256, paths relative to the root
  in POSIX form (``asr/ggml-breeze-asr-25-q5_0.bin``) so a manifest made on
  Windows verifies on Linux / macOS and vice versa.
* ``verify`` compares the folder with an existing manifest and reports
  **missing** files, **corrupt** files (size or sha256 mismatch), files that
  cannot be read and **extra** files that are not in the manifest, with
  Traditional Chinese suggestions (which file to fetch again, how much disk
  space that needs, which leftovers such as ``*.part`` can be cleaned up).
  ``--quick`` compares sizes only (no hashing).

Read-only, stdlib only, never downloads anything and makes no network calls:
the suggestions only say *what* to copy back; fetching it is up to you.
Files are hashed as a stream in 1 MiB chunks, so multi-GB GGUF files never sit
in memory.  Symbolic links / junctions that resolve inside the root are
followed; links that escape the root (or are broken) are skipped and reported.
Paths are compared after Unicode NFC normalisation; a file whose name differs
from the manifest only by letter case is matched and reported as a warning.

Manifest format (``schema_version`` 1)::

  {"kind": "zen-bridge-model-manifest", "schema_version": 1,
   "algorithm": "sha256", "chunk_size": 1048576, "root_name": "models",
   "file_count": 2, "total_bytes": 123,
   "files": [{"path": "asr/ggml-breeze-asr-25-q5_0.bin", "size": 100, "sha256": "<64 hex>"},
             {"path": "vad/silero_vad.onnx", "size": 23, "sha256": "<64 hex>"}]}

Hand-written manifests may omit ``sha256`` for an entry (size-only check, with
a warning).  Paths with backslashes are accepted and normalised to ``/``;
absolute paths, ``..`` components and duplicate paths are rejected.

==========  ==================================================================  ====
status      condition                                                           exit
==========  ==================================================================  ====
``ok``      every manifest file present with matching size (and sha256 unless    0
            ``--quick``), no extra files, no skipped links
``warn``    extra files (unless ``--allow-extra``), case-only name differences,  1
            links escaping the root / broken links / unreadable folders
            (skipped), entries without sha256, or too little free disk space
            to fetch the missing files again
``fail``    missing file, size mismatch, sha256 mismatch, or unreadable file      2
(input)     model root missing / not a folder, manifest missing / not UTF-8 /   3
            not valid JSON / bad schema, nothing to put in a new manifest,
            unreadable file while generating, or invalid options (argparse
            errors use 3, not 2)
(output)    ``--out`` / ``--json`` path already exists (never overwritten) or    4
            cannot be written
==========  ==================================================================  ====

``generate`` returns 0, or 1 when links had to be skipped (the manifest is
still written).  Human output is Traditional Chinese.  ``--out`` and ``--json
OUT`` only ever create *new* files (exclusive create; an existing path is
refused with exit 4).  The manifest and report files themselves (and any other
JSON written by this tool inside the root) are never counted as model files.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TOOL = "model_file_manifest"
SCHEMA_VERSION = 1
MANIFEST_KIND = "zen-bridge-model-manifest"
ALGORITHM = "sha256"
CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks

EXIT_OK = 0
EXIT_WARN = 1
EXIT_FAIL = 2
EXIT_INPUT = 3
EXIT_OUTPUT = 4

MAX_LISTED = 50
DEFAULT_EXCLUDES = (".DS_Store", "Thumbs.db", "desktop.ini")
PARTIAL_SUFFIXES = (".part", ".partial", ".tmp", ".download", ".crdownload", ".incomplete", ".aria2")
DISK_HEADROOM = 1.05  # want 5% more free space than the bytes to fetch again
_HEX64 = re.compile(r"[0-9a-f]{64}")
_DRIVE = re.compile(r"^[A-Za-z]:")


class InputError(Exception):
    """Unreadable / invalid input (exit 3)."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # argparse would exit 2, which means "fail" here
        self.print_usage(sys.stderr)
        sys.stderr.write(f"{self.prog}: 參數錯誤：{message}\n")
        raise SystemExit(EXIT_INPUT)


@dataclass
class DiskFile:
    rel: str            # POSIX, NFC, relative to the root
    path: str           # OS path used to open the file
    size: int


@dataclass
class Scan:
    root: str
    files: dict[str, DiskFile] = field(default_factory=dict)
    excluded: int = 0
    escaped_links: list[dict] = field(default_factory=list)
    broken_links: list[str] = field(default_factory=list)
    unreadable_dirs: list[str] = field(default_factory=list)
    skipped_special: list[str] = field(default_factory=list)
    own_outputs: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n < 1024:
        return f"{n} B"
    v = float(n)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        v /= 1024.0
        if v < 1024.0 or unit == "TiB":
            return f"{v:.1f} {unit}"
    return f"{n} B"  # pragma: no cover


def _nfc(s: str) -> str:
    return unicodedata.normalize("NFC", s)


def normalize_rel(raw: str) -> str:
    """Manifest path → canonical POSIX relative path; raises ValueError when unsafe."""
    if not isinstance(raw, str):
        raise ValueError("path 必須是字串")
    s = _nfc(raw.replace("\\", "/")).strip()
    if not s:
        raise ValueError("path 是空的")
    if s.startswith("/") or _DRIVE.match(s):
        raise ValueError(f"path 必須是相對於模型根目錄的路徑，不能是絕對路徑：{raw}")
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts:
        raise ValueError(f"path 無效：{raw}")
    if any(p == ".." for p in parts):
        raise ValueError(f"path 不能含「..」（會跑出模型根目錄）：{raw}")
    return "/".join(parts)


def _real(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def _inside(real: str, root_real: str) -> bool:
    try:
        return os.path.commonpath([real, root_real]) == root_real
    except ValueError:  # different drives on Windows
        return False


def _excluded(rel: str, patterns: tuple[str, ...]) -> bool:
    name = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatchcase(name, pat) or fnmatch.fnmatchcase(rel, pat):
            return True
    return False


def sha256_file(path: str | os.PathLike, chunk_size: int = CHUNK_SIZE) -> tuple[str, int]:
    """Stream the file in ``chunk_size`` blocks; returns (hex digest, bytes read)."""
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


def kind_of(rel: str) -> str:
    low = rel.lower()
    name = low.rsplit("/", 1)[-1]
    if "silero" in low or name.startswith("vad") or "/vad" in low or low.startswith("vad"):
        return "Silero VAD"
    if name.endswith(".gguf"):
        return "MT GGUF（llama.cpp／Ollama 翻譯模型）"
    if "ggml" in name or "whisper" in low or "breeze-asr" in low or (name.endswith(".bin") and "asr" in low):
        return "ASR（whisper.cpp／Breeze ASR）"
    if name.endswith((".onnx", ".jit", ".pt", ".safetensors", ".bin")):
        return "模型權重"
    if name.endswith((".json", ".txt", ".model", ".tiktoken", ".yaml", ".yml", ".vocab")):
        return "設定／詞表檔"
    return "其他"


_OWN_MARKERS = (f'"kind": "{MANIFEST_KIND}"'.encode(), f'"tool": "{TOOL}"'.encode())


def _is_own_output(path: str, size: int) -> bool:
    """A manifest / JSON report written by this tool (never a model file)."""
    if not path.lower().endswith(".json") or size > 64 * 1024 * 1024:
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    head = head.removeprefix(b"\xef\xbb\xbf").lstrip()
    return head.startswith(b"{") and any(m in head[:200] for m in _OWN_MARKERS)


def _is_partial(rel: str) -> bool:
    return rel.lower().endswith(PARTIAL_SUFFIXES)


# --------------------------------------------------------------------------- scanning
def scan_root(root: Path, excludes: tuple[str, ...] = (), skip_real: set[str] | None = None) -> Scan:
    """Walk ``root`` (read-only). Links resolving inside the root are followed, others reported."""
    if not root.exists():
        raise InputError(f"找不到模型根目錄：{root}")
    if not root.is_dir():
        raise InputError(f"模型根目錄不是資料夾：{root}")
    root_real = _real(str(root))
    scan = Scan(root=str(root))
    skip_real = skip_real or set()
    visited: set[str] = {root_real}
    stack: list[tuple[str, tuple[str, ...]]] = [(str(root), ())]
    while stack:
        dir_path, rel_parts = stack.pop()
        try:
            with os.scandir(dir_path) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            scan.unreadable_dirs.append("/".join(rel_parts) or ".")
            continue
        subdirs: list[tuple[str, tuple[str, ...]]] = []
        for entry in entries:
            parts = rel_parts + (_nfc(entry.name),)
            rel = "/".join(parts)
            try:
                is_link = entry.is_symlink() or bool(getattr(os.path, "isjunction", lambda _p: False)(entry.path))
            except OSError:
                is_link = False
            real = _real(entry.path)
            if is_link or not _inside(real, root_real):
                if not os.path.exists(entry.path):
                    scan.broken_links.append(rel)
                    continue
                if not _inside(real, root_real):
                    try:
                        target = os.readlink(entry.path)
                    except (OSError, ValueError):
                        target = ""
                    scan.escaped_links.append({"path": rel, "target": target})
                    continue
            try:
                if entry.is_dir(follow_symlinks=True):
                    if real in visited:
                        continue  # link loop / second link to the same folder
                    visited.add(real)
                    subdirs.append((entry.path, parts))
                    continue
                if not entry.is_file(follow_symlinks=True):
                    scan.skipped_special.append(rel)
                    continue
                size = os.stat(entry.path).st_size
            except OSError:
                scan.unreadable_dirs.append(rel)
                continue
            if real in skip_real or _is_own_output(entry.path, size):
                scan.own_outputs.append(rel)
                continue  # the manifest / report files themselves
            if _excluded(rel, excludes):
                scan.excluded += 1
                continue
            scan.files[rel] = DiskFile(rel=rel, path=entry.path, size=size)
        stack.extend(reversed(subdirs))
    return scan


def _link_issues(scan: Scan, add) -> None:
    if scan.escaped_links:
        add("warn", "symlink_escape",
            f"有 {len(scan.escaped_links)} 個符號連結／連接點指到模型根目錄外面，已略過不計"
            "（請把真正的模型檔複製進根目錄，或改用根目錄內的路徑）。")
    if scan.broken_links:
        add("warn", "broken_symlink", f"有 {len(scan.broken_links)} 個符號連結指向不存在的目標，已略過。")
    if scan.unreadable_dirs:
        add("warn", "unreadable_dir", f"有 {len(scan.unreadable_dirs)} 個資料夾／項目無法讀取，已略過（權限？）。")
    if scan.skipped_special:
        add("warn", "special_file", f"有 {len(scan.skipped_special)} 個非一般檔案（裝置、FIFO 等）已略過。")


def _scan_report(scan: Scan) -> dict[str, Any]:
    return {
        "escaped": scan.escaped_links[:MAX_LISTED], "escaped_count": len(scan.escaped_links),
        "broken": scan.broken_links[:MAX_LISTED], "broken_count": len(scan.broken_links),
        "unreadable": scan.unreadable_dirs[:MAX_LISTED], "unreadable_count": len(scan.unreadable_dirs),
        "special": scan.skipped_special[:MAX_LISTED], "special_count": len(scan.skipped_special),
        "excluded_count": scan.excluded,
        "own_outputs": scan.own_outputs[:MAX_LISTED],
    }


def _disk_usage(root: Path) -> dict[str, int] | None:
    try:
        du = shutil.disk_usage(str(root))
    except OSError:
        return None
    return {"free_bytes": int(du.free), "total_bytes": int(du.total)}


# --------------------------------------------------------------------------- manifest
def build_manifest(root: Path, scan: Scan, chunk_size: int = CHUNK_SIZE) -> dict[str, Any]:
    files = []
    for rel in sorted(scan.files):
        df = scan.files[rel]
        try:
            digest, n = sha256_file(df.path, chunk_size)
        except OSError as exc:
            raise InputError(f"無法讀取模型檔：{rel}（{exc.__class__.__name__}），manifest 未產生") from exc
        files.append({"path": rel, "size": n, "sha256": digest})
    name = Path(os.path.abspath(str(root))).name
    return {
        "kind": MANIFEST_KIND,
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "algorithm": ALGORITHM,
        "chunk_size": chunk_size,
        "root_name": _nfc(name),
        "file_count": len(files),
        "total_bytes": sum(f["size"] for f in files),
        "files": files,
    }


def read_text(path: Path) -> str:
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise InputError(f"找不到 manifest：{path}") from exc
    except IsADirectoryError as exc:
        raise InputError(f"manifest 是資料夾不是檔案：{path}") from exc
    except OSError as exc:
        raise InputError(f"無法讀取 manifest：{path}（{exc.__class__.__name__}）") from exc
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):  # PowerShell `>` writes UTF-16
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise InputError(f"manifest UTF-16 解碼失敗：{path}") from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"manifest 不是 UTF-8 文字檔：{path}（位元組 {exc.start}）") from exc


def load_manifest(path: Path) -> list[dict[str, Any]]:
    text = read_text(path)
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise InputError(f"manifest 不是有效的 JSON：{path}（第 {getattr(exc, 'lineno', '?')} 行）") from exc
    if isinstance(doc, dict):
        if doc.get("kind") not in (None, MANIFEST_KIND):
            raise InputError(f"不是模型 manifest（kind={doc.get('kind')!r}）：{path}")
        ver = doc.get("schema_version", SCHEMA_VERSION)
        if isinstance(ver, bool) or not isinstance(ver, int) or ver > SCHEMA_VERSION or ver < 1:
            raise InputError(f"不支援的 manifest schema_version：{ver!r}（本工具支援 {SCHEMA_VERSION}）")
        algo = doc.get("algorithm", ALGORITHM)
        if not isinstance(algo, str) or algo.lower() != ALGORITHM:
            raise InputError(f"不支援的雜湊演算法：{algo!r}（只支援 sha256）")
        items = doc.get("files")
    else:
        items = doc
    if not isinstance(items, list):
        raise InputError(f"manifest 裡找不到 files 陣列：{path}")
    if not items:
        raise InputError(f"manifest 沒有任何檔案：{path}")
    out: list[dict[str, Any]] = []
    seen: dict[str, int] = {}
    for i, obj in enumerate(items):
        where = f"files[{i}]"
        if not isinstance(obj, dict):
            raise InputError(f"manifest {where} 不是物件")
        try:
            rel = normalize_rel(obj.get("path"))
        except ValueError as exc:
            raise InputError(f"manifest {where}：{exc}") from exc
        size = obj.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InputError(f"manifest {where}（{rel}）的 size 必須是非負整數")
        digest = obj.get("sha256")
        if digest is not None:
            if not isinstance(digest, str) or not _HEX64.fullmatch(digest.strip().lower()):
                raise InputError(f"manifest {where}（{rel}）的 sha256 必須是 64 位十六進位字串")
            digest = digest.strip().lower()
        if rel in seen:
            raise InputError(f"manifest 有重複路徑：{rel}（files[{seen[rel]}] 與 {where}）")
        seen[rel] = i
        out.append({"path": rel, "size": size, "sha256": digest})
    return out


# --------------------------------------------------------------------------- verify
def verify(root: Path, entries: list[dict[str, Any]], scan: Scan, *, quick: bool = False,
           allow_extra: bool = False, chunk_size: int = CHUNK_SIZE,
           disk: dict[str, int] | None = None, manifest_path: str | None = None) -> dict[str, Any]:
    disk_files = dict(scan.files)
    by_fold: dict[str, list[str]] = {}
    for rel in disk_files:
        by_fold.setdefault(rel.casefold(), []).append(rel)

    ok: list[dict] = []
    missing: list[dict] = []
    size_bad: list[dict] = []
    sha_bad: list[dict] = []
    unreadable: list[dict] = []
    case_diff: list[dict] = []
    unhashed: list[str] = []
    hashed_bytes = 0

    matched: list[tuple[dict, DiskFile]] = []
    pending: list[dict] = []
    for e in entries:
        df = disk_files.get(e["path"])
        if df is not None:
            matched.append((e, df))
        else:
            pending.append(e)
    used = {df.rel for _e, df in matched}
    for e in pending:
        cands = [r for r in by_fold.get(e["path"].casefold(), []) if r not in used]
        if len(cands) == 1:
            df = disk_files[cands[0]]
            used.add(df.rel)
            case_diff.append({"manifest_path": e["path"], "disk_path": df.rel})
            matched.append((e, df))
        else:
            missing.append({"path": e["path"], "size": e["size"], "kind": kind_of(e["path"])})

    for e, df in sorted(matched, key=lambda x: x[0]["path"]):
        row = {"path": e["path"], "kind": kind_of(e["path"])}
        if df.size != e["size"]:
            size_bad.append({**row, "expected_size": e["size"], "actual_size": df.size})
            continue
        if quick or e["sha256"] is None:
            if e["sha256"] is None:
                unhashed.append(e["path"])
            ok.append({**row, "size": df.size, "hashed": False})
            continue
        try:
            digest, n = sha256_file(df.path, chunk_size)
        except OSError as exc:
            unreadable.append({**row, "size": e["size"], "error": exc.__class__.__name__})
            continue
        hashed_bytes += n
        if n != e["size"]:  # file changed while we were reading it
            size_bad.append({**row, "expected_size": e["size"], "actual_size": n})
        elif digest != e["sha256"]:
            sha_bad.append({**row, "size": n, "expected_sha256": e["sha256"], "actual_sha256": digest})
        else:
            ok.append({**row, "size": n, "hashed": True})

    extra = [{"path": rel, "size": df.size, "kind": kind_of(rel), "partial_download": _is_partial(rel)}
             for rel, df in sorted(disk_files.items()) if rel not in used]

    by_path = {e["path"]: e for e in entries}
    refetch = sorted([m["path"] for m in missing] + [s["path"] for s in size_bad]
                     + [s["path"] for s in sha_bad] + [u["path"] for u in unreadable])
    refetch_bytes = sum(by_path[p]["size"] for p in refetch)
    extra_bytes = sum(x["size"] for x in extra)
    manifest_bytes = sum(e["size"] for e in entries)

    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    if missing:
        add("fail", "missing", f"缺檔 {len(missing)} 個（共 {fmt_bytes(sum(m['size'] for m in missing))}）。")
    if size_bad:
        add("fail", "size_mismatch", f"大小不符 {len(size_bad)} 個（多半是下載或複製中斷、檔案被截斷）。")
    if sha_bad:
        add("fail", "sha256_mismatch", f"sha256 不符 {len(sha_bad)} 個（大小相同但內容不同：檔案損壞或版本不同）。")
    if unreadable:
        add("fail", "unreadable", f"無法讀取 {len(unreadable)} 個模型檔（權限或磁碟錯誤），無法確認是否完整。")
    if extra and not allow_extra:
        add("warn", "extra", f"多餘檔案 {len(extra)} 個（manifest 沒列，共 {fmt_bytes(extra_bytes)}）。")
    if case_diff:
        add("warn", "case_mismatch",
            f"有 {len(case_diff)} 個檔名只有大小寫和 manifest 不同（Windows 可用，Linux／macOS 區分大小寫時會找不到）。")
    if unhashed and not quick:
        add("warn", "unhashed", f"manifest 有 {len(unhashed)} 個檔沒有 sha256，只比對了大小。")
    _link_issues(scan, add)
    if disk is not None and refetch_bytes and disk["free_bytes"] < refetch_bytes * DISK_HEADROOM:
        add("warn", "low_disk",
            f"磁碟剩餘 {fmt_bytes(disk['free_bytes'])}，不夠重新取得 {fmt_bytes(refetch_bytes)} 的模型檔"
            "（請先清出空間；覆蓋前最好先把壞檔移走）。")

    if any(i["level"] == "fail" for i in issues):
        status, code = "fail", EXIT_FAIL
    elif issues:
        status, code = "warn", EXIT_WARN
    else:
        status, code = "ok", EXIT_OK

    suggestions: list[str] = []
    for m in missing:
        suggestions.append(f"重新取得 {m['path']}（{m['kind']}，應為 {fmt_bytes(m['size'])}）：缺檔。")
    for s in size_bad:
        suggestions.append(f"重新取得 {s['path']}（{s['kind']}）：大小 {fmt_bytes(s['actual_size'])}，"
                           f"應為 {fmt_bytes(s['expected_size'])}，檔案不完整。")
    for s in sha_bad:
        suggestions.append(f"重新取得 {s['path']}（{s['kind']}，{fmt_bytes(s['size'])}）：sha256 不符，檔案損壞或版本不同。")
    for u in unreadable:
        suggestions.append(f"檢查 {u['path']} 的權限／磁碟，必要時重新取得（{fmt_bytes(u['size'])}）。")
    if refetch:
        free = f"；磁碟剩餘 {fmt_bytes(disk['free_bytes'])}" if disk is not None else ""
        suggestions.append(f"共需重新取得 {len(refetch)} 個檔、約 {fmt_bytes(refetch_bytes)}{free}。"
                           "請從原來的來源或 USB／備份手動複製；本工具不會下載任何東西。")
    partial = [x for x in extra if x["partial_download"]]
    if partial:
        suggestions.append(f"{len(partial)} 個多餘檔看起來是中斷的下載（{', '.join(x['path'] for x in partial[:3])}"
                           f"{' 等' if len(partial) > 3 else ''}），確認後可手動刪除，釋放 "
                           f"{fmt_bytes(sum(x['size'] for x in partial))}。")
    if extra and not allow_extra and len(partial) < len(extra):
        suggestions.append("其他多餘檔若是新加入的模型，請重新執行 generate 產生新的 manifest（寫到新檔）；"
                           "若是舊版殘留，可手動移走以釋放空間。")
    if case_diff:
        suggestions.append("把檔名大小寫改成和 manifest 一致（例如 "
                           f"{case_diff[0]['disk_path']} → {case_diff[0]['manifest_path']}）。")
    if scan.escaped_links:
        suggestions.append("根目錄外的連結沒有被檢查：若模型真的放在別處，請改對那個資料夾產生 manifest。")
    if quick and status == "ok":
        suggestions.append("快速模式只比大小；正式上課前建議不加 --quick 再跑一次完整 sha256 比對。")

    report: dict[str, Any] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "mode": "verify",
        "status": status,
        "exit_code": code,
        "quick": quick,
        "allow_extra": allow_extra,
        "root": str(root),
        "manifest": manifest_path,
        "algorithm": ALGORITHM,
        "chunk_size": chunk_size,
        "counts": {
            "manifest_files": len(entries), "disk_files": len(disk_files), "ok": len(ok),
            "missing": len(missing), "size_mismatch": len(size_bad), "sha256_mismatch": len(sha_bad),
            "unreadable": len(unreadable), "extra": len(extra), "case_mismatch": len(case_diff),
            "unhashed": len(unhashed), "excluded": scan.excluded,
        },
        "bytes": {
            "manifest_total": manifest_bytes, "hashed": hashed_bytes,
            "refetch_needed": refetch_bytes, "extra_total": extra_bytes,
        },
        "disk": disk,
        "missing": missing[:MAX_LISTED],
        "size_mismatch": size_bad[:MAX_LISTED],
        "sha256_mismatch": sha_bad[:MAX_LISTED],
        "unreadable": unreadable[:MAX_LISTED],
        "extra": extra[:MAX_LISTED],
        "case_mismatch": case_diff[:MAX_LISTED],
        "refetch": refetch[:MAX_LISTED],
        "links": _scan_report(scan),
        "issues": issues,
        "suggestions": suggestions[:MAX_LISTED],
    }
    report["summary_zh"] = summary_verify_zh(report)
    return report


# --------------------------------------------------------------------------- output
_STATUS_ZH = {"ok": "通過", "warn": "警告", "fail": "不通過"}


def _issue_lines(r: dict[str, Any]) -> list[str]:
    out: list[str] = []
    if r["issues"]:
        out.append("問題：")
        for i in r["issues"]:
            out.append(f"  [{'失敗' if i['level'] == 'fail' else '警告'}] {i['message']}")
    return out


def summary_verify_zh(r: dict[str, Any]) -> str:
    c, b = r["counts"], r["bytes"]
    lines = [
        f"模型檔案比對：{_STATUS_ZH[r['status']]}（exit {r['exit_code']}）",
        f"- 模型根目錄：{r['root']}",
        f"- manifest：{r['manifest'] or '—'}（{c['manifest_files']} 個檔、{fmt_bytes(b['manifest_total'])}）",
        "- 比對方式：" + ("快速模式（只比大小，未驗 sha256）" if r["quick"]
                       else "sha256 全檔串流比對（1 MiB 分塊）＋大小"),
        f"- 結果：相符 {c['ok']}／缺檔 {c['missing']}／大小不符 {c['size_mismatch']}／"
        f"sha256 不符 {c['sha256_mismatch']}／無法讀取 {c['unreadable']}／多餘 {c['extra']}",
    ]
    if r["disk"] is not None:
        lines.append(f"- 磁碟剩餘：{fmt_bytes(r['disk']['free_bytes'])}"
                     + (f"（重新取得需要 {fmt_bytes(b['refetch_needed'])}）" if b["refetch_needed"] else ""))
    lines += _issue_lines(r)
    for m in r["missing"][:10]:
        lines.append(f"  缺檔：{m['path']}（{fmt_bytes(m['size'])}）")
    for s in r["size_mismatch"][:10]:
        lines.append(f"  大小不符：{s['path']}（實際 {s['actual_size']}，應為 {s['expected_size']} 位元組）")
    for s in r["sha256_mismatch"][:10]:
        lines.append(f"  sha256 不符：{s['path']}（實際 {s['actual_sha256'][:12]}…，應為 {s['expected_sha256'][:12]}…）")
    for u in r["unreadable"][:10]:
        lines.append(f"  無法讀取：{u['path']}（{u['error']}）")
    for x in r["extra"][:10]:
        lines.append(f"  多餘：{x['path']}（{fmt_bytes(x['size'])}" + ("，疑似中斷的下載" if x["partial_download"] else "") + "）")
    for lk in r["links"]["escaped"][:10]:
        lines.append(f"  略過根目錄外連結：{lk['path']}" + (f" → {lk['target']}" if lk["target"] else ""))
    if r["suggestions"]:
        lines.append("建議：")
        lines += [f"  - {s}" for s in r["suggestions"][:15]]
    if r["status"] == "ok":
        lines.append("所有模型檔都和 manifest 一致。")
    return "\n".join(lines)


def summary_generate_zh(r: dict[str, Any]) -> str:
    lines = [
        f"模型 manifest 產生：{'完成' if r['status'] == 'ok' else '完成（有警告）'}（exit {r['exit_code']}）",
        f"- 模型根目錄：{r['root']}",
        f"- 已寫入新檔：{r['out']}",
        f"- 檔案數：{r['file_count']}，總大小 {fmt_bytes(r['total_bytes'])}（sha256，1 MiB 分塊串流）",
    ]
    if r["by_kind"]:
        lines.append("- 類型：" + "；".join(f"{k} {v['files']} 個（{fmt_bytes(v['bytes'])}）"
                                          for k, v in sorted(r["by_kind"].items())))
    lines += _issue_lines(r)
    for lk in r["links"]["escaped"][:10]:
        lines.append(f"  略過根目錄外連結：{lk['path']}" + (f" → {lk['target']}" if lk["target"] else ""))
    lines.append("建議：把這份 manifest 跟模型一起備份；之後用 verify 比對（manifest 只寫新檔，不會覆蓋舊的）。")
    return "\n".join(lines)


def generate_report(root: Path, out: Path, manifest: dict[str, Any], scan: Scan) -> dict[str, Any]:
    issues: list[dict[str, str]] = []

    def add(level: str, code: str, msg: str) -> None:
        issues.append({"level": level, "code": code, "message": msg})

    _link_issues(scan, add)
    by_kind: dict[str, dict[str, int]] = {}
    for f in manifest["files"]:
        k = by_kind.setdefault(kind_of(f["path"]), {"files": 0, "bytes": 0})
        k["files"] += 1
        k["bytes"] += f["size"]
    status, code = ("warn", EXIT_WARN) if issues else ("ok", EXIT_OK)
    report = {
        "tool": TOOL, "schema_version": SCHEMA_VERSION, "mode": "generate",
        "status": status, "exit_code": code, "root": str(root), "out": str(out),
        "algorithm": ALGORITHM, "chunk_size": manifest["chunk_size"],
        "file_count": manifest["file_count"], "total_bytes": manifest["total_bytes"],
        "by_kind": by_kind, "links": _scan_report(scan), "issues": issues,
    }
    report["summary_zh"] = summary_generate_zh(report)
    return report


def _write_new(path: Path, obj: dict[str, Any]) -> str | None:
    """Exclusive-create JSON write; returns an error message or None."""
    try:
        with open(path, "x", encoding="utf-8", newline="\n") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except FileExistsError:
        return f"拒絕覆蓋既有檔案：{path}（只寫新檔）"
    except OSError as exc:
        return f"無法寫入：{path}（{exc.__class__.__name__}）"
    return None


def _exists(p: Path | None) -> bool:
    return p is not None and (p.exists() or p.is_symlink())


def build_parser() -> argparse.ArgumentParser:
    ap = _Parser(prog="model_file_manifest.py",
                 description="唯讀產生／比對本機模型資料夾的 sha256＋大小清單（缺檔、損壞、多餘檔）；絕不下載、無網路。")
    sub = ap.add_subparsers(dest="cmd", metavar="{generate,verify}")
    sub.required = True
    g = sub.add_parser("generate", help="掃描模型根目錄，產生 manifest 到新檔（不覆蓋）")
    g.add_argument("--root", type=Path, required=True, help="模型根目錄（例如 models）")
    g.add_argument("--out", type=Path, required=True, help="manifest 輸出路徑（必須是新檔）")
    v = sub.add_parser("verify", help="用既有 manifest 比對模型根目錄")
    v.add_argument("--manifest", type=Path, required=True, help="既有 manifest（JSON）")
    v.add_argument("--root", type=Path, help="模型根目錄（預設：manifest 所在資料夾）")
    v.add_argument("--quick", action="store_true", help="快速模式：只比大小，不算 sha256")
    v.add_argument("--allow-extra", action="store_true", help="多餘檔只列出、不算警告")
    for p in (g, v):
        p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                       help="略過符合的檔名或相對路徑（可重複；預設已略過 .DS_Store、Thumbs.db、desktop.ini）")
        p.add_argument("--json", type=Path, metavar="OUT", help="另寫 JSON 報告到新檔（已存在就拒絕，不覆蓋）")
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    ap = build_parser()
    args = ap.parse_args(argv)
    outs = [p for p in (getattr(args, "out", None), args.json) if p is not None]
    if len(outs) == 2 and os.path.abspath(str(outs[0])) == os.path.abspath(str(outs[1])):
        ap.error("--out 與 --json 不能是同一個檔案")
    if args.cmd == "verify" and args.json is not None \
            and os.path.abspath(str(args.json)) == os.path.abspath(str(args.manifest)):
        ap.error("--json 不能指向 manifest 本身")
    for p in outs:
        if _exists(p):
            sys.stderr.write(f"拒絕覆蓋既有檔案：{p}（只寫新檔）\n")
            return EXIT_OUTPUT
    excludes = DEFAULT_EXCLUDES + tuple(args.exclude)

    if args.cmd == "generate":
        root = args.root
        skip = {_real(str(p)) for p in outs}
        try:
            scan = scan_root(root, excludes, skip)
            if not scan.files:
                raise InputError(f"模型根目錄裡沒有任何檔案可以列進 manifest：{root}")
            manifest = build_manifest(root, scan)
        except InputError as exc:
            sys.stderr.write(f"輸入錯誤：{exc}\n")
            return EXIT_INPUT
        err = _write_new(args.out, manifest)
        if err:
            sys.stderr.write(err + "\n")
            return EXIT_OUTPUT
        report = generate_report(root, args.out, manifest, scan)
    else:
        root = args.root if args.root is not None else args.manifest.parent
        skip = {_real(str(p)) for p in [args.manifest, *outs]}
        try:
            entries = load_manifest(args.manifest)
            scan = scan_root(root, excludes, skip)
        except InputError as exc:
            sys.stderr.write(f"輸入錯誤：{exc}\n")
            return EXIT_INPUT
        report = verify(root, entries, scan, quick=args.quick, allow_extra=args.allow_extra,
                        disk=_disk_usage(root), manifest_path=str(args.manifest))

    if args.json is not None:
        err = _write_new(args.json, report)
        if err:
            sys.stderr.write(err + "\n")
            return EXIT_OUTPUT

    print(report["summary_zh"])
    if args.json is not None:
        print(f"JSON 報告：{args.json}")
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
