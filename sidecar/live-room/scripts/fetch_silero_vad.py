"""Download the pinned Silero VAD ONNX model and verify its sha256.

    python scripts/fetch_silero_vad.py [--dest app/vendor/silero_vad.onnx] [--force]

Source: snakers4/silero-vad v6.2.3 (MIT). Never run by tests. Exit 0 = file present and
verified; 1 = download failed; 2 = hash mismatch (the bad file is deleted).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.vad import MODEL, MODEL_BYTES, MODEL_SHA256, MODEL_URL  # noqa: E402


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, default=MODEL)
    ap.add_argument("--url", default=MODEL_URL)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    dest: Path = args.dest
    if dest.is_file() and not args.force:
        got = sha256(dest)
        if got == MODEL_SHA256:
            print(f"OK 已存在且 sha256 相符：{dest}\n{got}")
            return 0
        print(f"既有檔案 sha256 不符（{got}），重新下載", file=sys.stderr)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="silero_", suffix=".part", dir=str(dest.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        print(f"下載 {args.url}")
        with urllib.request.urlopen(args.url, timeout=60) as resp, open(tmp, "wb") as out:
            while True:
                block = resp.read(1 << 16)
                if not block:
                    break
                out.write(block)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        print(f"下載失敗：{exc}", file=sys.stderr)
        return 1
    got = sha256(tmp)
    if got != MODEL_SHA256:
        tmp.unlink(missing_ok=True)
        print(f"sha256 不符：得到 {got}，預期 {MODEL_SHA256}", file=sys.stderr)
        return 2
    if tmp.stat().st_size != MODEL_BYTES:
        print(f"警告：大小 {tmp.stat().st_size} 與預期 {MODEL_BYTES} 不同（sha256 已相符）", file=sys.stderr)
    os.replace(tmp, dest)
    print(f"OK {dest}\nsha256 {got}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
