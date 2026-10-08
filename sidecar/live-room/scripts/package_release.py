"""Build a small, deterministic source installer, excluding all user data."""
from __future__ import annotations
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('VERSION', 'README.md', '.env.example', 'requirements.txt', 'requirements-lock.txt',
         'runtime-manifest.json', 'bootstrap-manifest.json', 'install.bat', 'install.ps1',
         'install-shortcut.ps1', 'start.bat', 'doctor.bat', 'verify.bat', 'update.bat',
         'update.ps1', 'rollback.bat')
DIRECTORIES = ('app', 'scripts', 'docs', 'desktop')

def source_files(root=ROOT):
    files = [root / name for name in FILES]
    for directory in DIRECTORIES:
        files.extend(p for p in (root / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc'
                     and not (directory == 'desktop' and p.suffix.lower() in {'.exe', '.dll'}))
    return sorted(files, key=lambda p: p.relative_to(root).as_posix())

def build(root=ROOT, output=None):
    version = (root / 'VERSION').read_text().strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('VERSION must be a stable semantic version')
    output = output or root / 'dist'
    output.mkdir(parents=True, exist_ok=True)
    archive = output / 'Breeze-Live-Room-Windows.zip'
    files = source_files(root)
    manifest = {'version': version, 'files': {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as zipped:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            zipped.writestr(info, path.read_bytes())
        zipped.writestr('package-manifest.json', json.dumps(manifest, sort_keys=True))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / (archive.name + '.sha256')).write_text(digest + '  ' + archive.name + '\n', encoding='ascii')
    print(f'{archive.name}: version {version}, SHA256 {digest}')
    return archive

if __name__ == '__main__':
    build(output=Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else None)
