"""Download a verified Setup.exe from the official GitHub release. No other host."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "aa0968111723-prog/breeze-live-room"
SETUP_NAME = "Breeze-Live-Room-Setup.exe"
SHA_NAME = "Breeze-Live-Room-Setup.exe.sha256"
MAX_BYTES = 2 * 1024 * 1024 * 1024
ALLOWED_HOSTS = {
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
    "codeload.github.com",
}


class GuardedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        assert_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def assert_https(url: str) -> None:
    if not str(url).startswith("https://"):
        raise ValueError("更新位址必須是 HTTPS")
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None or parts.port not in (None, 443):
        raise ValueError("更新位址含有不允許的帳號或連接埠")
    host = parts.hostname
    if host not in ALLOWED_HOSTS:
        raise ValueError("更新位址不在允許的範圍")


def parse_version(value: str) -> tuple[int, int, int]:
    text = str(value).strip()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+", text):
        raise ValueError("版本資訊無效")
    text = text.removeprefix('v')
    return tuple(int(part) for part in text.split("."))


def parse_sha256_sidecar(text: str, filename: str = SETUP_NAME) -> str:
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    parts = line.split()
    if not parts or not re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
        raise ValueError("SHA256 格式無效")
    if len(parts) > 1 and Path(parts[-1].lstrip("*")).name != filename:
        raise ValueError("SHA256 檔名不符")
    return parts[0].lower()


def plan(release: dict, current: str) -> dict:
    if release.get("draft") or release.get("prerelease"):
        return {"status": "unavailable", "message": "最新發布不是正式版，已停止更新。"}
    current_version = parse_version(current)
    tag = str(release.get("tag_name") or "")
    nxt = parse_version(tag)
    if nxt <= current_version:
        return {"status": "current", "version": current.strip()}
    assets = {}
    for item in release.get("assets") or []:
        name = item.get("name")
        if name in {SETUP_NAME, SHA_NAME}:
            assets[name] = item
    if SETUP_NAME not in assets or SHA_NAME not in assets:
        return {"status": "unavailable", "message": "已有較新版本，但正式安裝檔尚未發布。請稍後再試。"}
    setup, digest = assets[SETUP_NAME], assets[SHA_NAME]
    assert_https(setup.get("browser_download_url") or "")
    assert_https(digest.get("browser_download_url") or "")
    prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
    for item in (setup, digest):
        if item['browser_download_url'] != prefix + item['name']:
            raise ValueError('更新檔不屬於此專案的正式版本')
    size = int(setup.get("size") or 0)
    if size <= 0 or size > MAX_BYTES:
        raise ValueError("安裝檔大小異常")
    return {
        "status": "update",
        "version": tag.lstrip("v"),
        "bytes": size,
        "setup_url": setup["browser_download_url"],
        "sha_url": digest["browser_download_url"],
    }


def opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), GuardedRedirect)


def fetch_json(url: str) -> dict:
    assert_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Breeze-Live-Room", "Accept": "application/vnd.github+json"})
    with opener().open(request, timeout=60) as response:
        return json.loads(response.read(1024 * 1024))


def fetch_release() -> dict:
    try:
        return fetch_json(f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("尚未發布正式版本。") from exc
        raise RuntimeError("無法讀取正式版本資訊。") from exc


def stream_verified(url: str, dest: Path, digest: str, limit: int = MAX_BYTES, urlopen=None) -> Path:
    dest = Path(dest)
    if dest.name != SETUP_NAME:
        raise ValueError("安裝檔名稱無效")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Breeze-Live-Room"})
    open_url = urlopen or opener().open
    hasher = hashlib.sha256()
    total = 0
    try:
        with open_url(request, timeout=60) as response, partial.open("wb") as handle:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > limit:
                    raise ValueError("安裝檔超過大小上限")
                hasher.update(block)
                handle.write(block)
        if hasher.hexdigest() != digest:
            raise ValueError("安裝檔 SHA256 不符")
        partial.replace(dest)
        return dest
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def download_update(chosen: dict) -> dict:
    if chosen.get("status") != "update":
        return {key: chosen[key] for key in ("status", "version", "message") if key in chosen}
    sidecar = fetch_text(chosen["sha_url"])
    digest = parse_sha256_sidecar(sidecar)
    destination = Path(tempfile.mkdtemp(prefix='BreezeUpdate-')) / SETUP_NAME
    stream_verified(chosen["setup_url"], destination, digest, limit=int(chosen["bytes"]))
    (destination.parent / 'breeze-update.json').write_text(json.dumps({
        'repository': REPOSITORY, 'filename': SETUP_NAME, 'created_at': time.time()}), encoding='utf-8')
    return {"status": "ready", "version": chosen["version"], "path": str(destination), "sha256": digest}


def cleanup_downloads(minimum_age_s: float = 3600) -> int:
    """Remove old App-created downloads at startup; retain active/unknown files."""
    removed = 0
    for folder in Path(tempfile.gettempdir()).glob('BreezeUpdate-*'):
        if not folder.is_dir() or folder.is_symlink() or (hasattr(folder, 'is_junction') and folder.is_junction()):
            continue
        marker = folder / 'breeze-update.json'
        try:
            if marker.stat().st_size > 4096:
                continue
            record = json.loads(marker.read_text(encoding='utf-8'))
            if record.get('repository') != REPOSITORY or record.get('filename') != SETUP_NAME:
                continue
            if time.time() - float(record['created_at']) < minimum_age_s:
                continue
            installer = folder / SETUP_NAME
            if installer.is_symlink():
                continue
            # Windows keeps executing EXEs locked. Permission errors leave the
            # marker in place for a later startup rather than disrupting Setup.
            installer.unlink(missing_ok=True)
            marker.unlink()
            try:
                folder.rmdir()  # Never recursively delete unexpected user files.
            except OSError:
                pass
            removed += 1
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return removed


def fetch_text(url: str, limit: int = 4096) -> str:
    assert_https(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Breeze-Live-Room"})
    with opener().open(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("SHA256 檔過大")
    return data.decode("ascii")


def run(check: bool, download: bool, root: Path) -> dict:
    os.environ.pop("SSLKEYLOGFILE", None)
    current = (root / "VERSION").read_text(encoding="utf-8").strip()
    chosen = plan(fetch_release(), current)
    if download:
        return download_update(chosen)
    public = {key: chosen[key] for key in ("status", "version", "message", "bytes") if key in chosen}
    return public


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    if args.check == args.download:
        print(json.dumps({"status": "error", "message": "請指定 --check 或 --download"}, ensure_ascii=False))
        return 2
    try:
        print(json.dumps(run(args.check, args.download, args.root), ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
