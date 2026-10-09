"""Explicit stable-release updates with checksum validation and local rollback."""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_release import FILES, DIRECTORIES, source_files
from app.settings import Settings, fill_process_environ

REPOSITORY = 'aa0968111723-prog/breeze-live-room'
ASSET = 'Breeze-Live-Room-Windows.zip'

def fetch(url, limit=20 * 1024 * 1024):
    if not url.startswith('https://'):
        raise ValueError('HTTPS required')
    request = urllib.request.Request(url, headers={'User-Agent': 'Breeze-Updater', 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Download exceeds size limit')
    return data

def allowed(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name and ':' not in name
            and '__pycache__' not in path.parts and path.suffix != '.pyc'
            and (name in FILES or (len(path.parts) > 1 and path.parts[0] in DIRECTORIES)))

def unpack(archive, destination):
    with zipfile.ZipFile(archive) as zipped:
        members = zipped.infolist()
        if len(members) > 2000 or sum(p.file_size for p in members) > 50 * 1024 * 1024:
            raise ValueError('Package too large')
        names = [p.filename for p in members]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError('Duplicate package paths')
        manifest = json.loads(zipped.read('package-manifest.json'))
        expected = manifest['files']
        if set(names) != set(expected) | {'package-manifest.json'}:
            raise ValueError('Unexpected package contents')
        if not set(FILES).issubset(expected) or not re.fullmatch(r'\d+\.\d+\.\d+', manifest['version']):
            raise ValueError('Invalid package manifest')
        for name, digest in expected.items():
            if not allowed(name):
                raise ValueError('Unsafe package path: ' + name)
            data = zipped.read(name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('Package file checksum mismatch')
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        if (destination / 'VERSION').read_text().strip() != manifest['version']:
            raise ValueError('Package version mismatch')
        return manifest['version']

def copy_immutable(source, destination):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() not in {'.bin', '.exe', '.dll'}:
        return shutil.copy2(source, destination)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)

def snapshot(root, backup):
    backup.mkdir(parents=True)
    for path in source_files(root):
        target = backup / 'source' / path.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    for name in ('tools', 'models'):
        if (root / name).exists():
            shutil.copytree(root / name, backup / name, copy_function=copy_immutable)
    (backup / 'complete').write_text('ok')

def replace_source(source, root):
    for directory in DIRECTORIES:
        target = root / directory
        # Only managed source directories are replaced; user data lives elsewhere.
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source / directory, target)
    for name in FILES:
        shutil.copy2(source / name, root / name)

def restore(root, backup):
    if not (backup / 'complete').is_file():
        raise ValueError('Incomplete backup')
    replace_source(backup / 'source', root)
    for name in ('tools', 'models'):
        if (backup / name).exists():
            target = root / name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(backup / name, target, copy_function=copy_immutable)
    if (backup / 'venv').exists():
        if (root / '.venv').exists():
            shutil.rmtree(root / '.venv')
        shutil.move(str(backup / 'venv'), str(root / '.venv'))

def require_stopped(root):
    fill_process_environ(root / '.env')
    settings = Settings.from_env()
    for port in (settings.port, int(__import__('urllib.parse', fromlist=['urlparse']).urlparse(settings.resident_url).port or 8178)):
        with socket.socket() as probe:
            try:
                probe.bind(('0.0.0.0', port))
            except OSError as exc:
                raise RuntimeError('Close Breeze before updating or rolling back.') from exc

@contextlib.contextmanager
def lock(root):
    path = root / '.updates' / 'update.lock'
    path.parent.mkdir(exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError('Another update is running.') from exc
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

def apply(root, stage, run=subprocess.run):
    backup = root / '.updates' / ('backup-' + uuid.uuid4().hex)
    require_stopped(root)
    bootstrap = json.loads((stage / 'bootstrap-manifest.json').read_text())['python']
    base_python = root / '.python' / bootstrap['version'] / 'tools/python.exe'
    if not base_python.is_file():
        raise RuntimeError('New Python runtime required; use the new installer package first.')
    # Validate dependencies in a fresh environment before replacing any source.
    environment = stage / 'venv'
    run([str(base_python), '-m', 'venv', str(environment)], check=True)
    python = environment / 'Scripts' / 'python.exe'
    run([str(python), '-m', 'pip', 'install', '--no-cache-dir', '-r', str(stage / 'requirements-lock.txt')], check=True)
    run([str(python), '-m', 'compileall', '-q', str(stage / 'app'), str(stage / 'scripts')], check=True)
    snapshot(root, backup)
    pointer = root / '.updates' / 'previous.json'
    pointer.write_text(json.dumps({'backup': backup.name}))
    try:
        replace_source(stage, root)
        if (root / '.venv').exists():
            shutil.move(str(root / '.venv'), str(backup / 'venv'))
        shutil.move(str(environment), str(root / '.venv'))
        run([str(root / '.venv' / 'Scripts' / 'python.exe'), 'scripts/install_runtime.py'], cwd=root, check=True)
    except BaseException:
        restore(root, backup)
        pointer.unlink(missing_ok=True)
        raise
    print('Update complete. Settings and captions preserved; rollback.bat restores the previous software.')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--rollback', action='store_true')
    args = parser.parse_args()
    with lock(ROOT):
        if args.rollback:
            require_stopped(ROOT)
            record = json.loads((ROOT / '.updates' / 'previous.json').read_text())
            name = record['backup']
            if not re.fullmatch(r'backup-[a-f0-9]{32}', name):
                raise ValueError('Invalid backup path')
            restore(ROOT, ROOT / '.updates' / name)
            (ROOT / '.updates' / 'previous.json').unlink()
            print('Previous software restored. Captions and settings preserved.')
            return 0
        try:
            release = json.loads(fetch(f'https://api.github.com/repos/{REPOSITORY}/releases/latest', 1024 * 1024))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                print('No stable GitHub release has been published yet.')
                return 0
            raise
        tag = release['tag_name']
        if release.get('prerelease') or release.get('draft') or not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
            raise ValueError('Not a stable release')
        current = (ROOT / 'VERSION').read_text().strip()
        print(f'Installed: {current}; latest stable release: {tag[1:]}')
        if tuple(map(int, tag[1:].split('.'))) <= tuple(map(int, current.split('.'))) or args.check:
            return 0
        assets = {p['name']: p['browser_download_url'] for p in release['assets']}
        for name in (ASSET, ASSET + '.sha256'):
            expected_prefix = f'https://github.com/{REPOSITORY}/releases/download/{tag}/'
            if not assets.get(name, '').startswith(expected_prefix):
                raise ValueError('Missing official release asset')
        digest = fetch(assets[ASSET + '.sha256'], 1024).decode('ascii').split()[0]
        if not re.fullmatch(r'[a-fA-F0-9]{64}', digest):
            raise ValueError('Invalid release checksum')
        data = fetch(assets[ASSET])
        if hashlib.sha256(data).hexdigest().lower() != digest.lower():
            raise ValueError('Release checksum mismatch; nothing changed')
        stage = ROOT / '.updates' / ('stage-' + uuid.uuid4().hex)
        stage.mkdir()
        archive = stage / ASSET
        archive.write_bytes(data)
        if unpack(archive, stage) != tag[1:]:
            raise ValueError('Release tag and package version differ')
        apply(ROOT, stage)
        return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('Update failed: ' + str(exc), file=sys.stderr)
        raise SystemExit(1)
