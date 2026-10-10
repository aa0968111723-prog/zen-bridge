"""Read-only compatibility gate for small desktop updates, also embedded in Setup."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path

FILES = ('requirements-lock.txt', 'bootstrap-manifest.json', 'runtime-manifest.json',
         'desktop/desktop-manifest.json')
GATE_VERSION = 1


def fingerprint(root: Path) -> str:
    contents = []
    for name in FILES:
        text = (root / name).read_text(encoding='utf-8-sig')
        value = json.loads(text) if name.endswith('.json') else '\n'.join(text.splitlines())
        contents.append((name, value))
    return hashlib.sha256(json.dumps([GATE_VERSION, contents], sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as reader:
        for block in iter(lambda: reader.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def descriptor(root: Path) -> dict:
    tools = {path.name: {'size': path.stat().st_size, 'sha256': file_digest(path)}
             for path in sorted((root / 'tools').iterdir()) if path.suffix.lower() in ('.exe', '.dll')}
    return {'schema': 1, 'gate_version': GATE_VERSION, 'fingerprint': fingerprint(root), 'tools': tools}


def valid_descriptor(value: dict) -> bool:
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('gate_version') != GATE_VERSION:
        return False
    tools = value.get('tools')
    if not isinstance(tools, dict) or not 2 <= len(tools) <= 128 or not {'ffmpeg.exe', 'whisper-cli.exe'} <= tools.keys():
        return False
    if not isinstance(value.get('fingerprint'), str) or not re.fullmatch('[a-f0-9]{64}', value['fingerprint']):
        return False
    for name, spec in tools.items():
        if Path(name).name != name or '/' in name or '\\' in name or ':' in name or not name.lower().endswith(('.exe', '.dll')):
            return False
        if not isinstance(spec, dict) or not isinstance(spec.get('size'), int) or not 0 < spec['size'] < 500 * 1024**2:
            return False
        if not isinstance(spec.get('sha256'), str) or not re.fullmatch('[a-f0-9]{64}', spec['sha256']):
            return False
    return True


def probably_compatible(root: Path, value: dict) -> bool:
    try:
        if not valid_descriptor(value) or fingerprint(root) != value['fingerprint']:
            return False
        if not native_matches(root, full=False):
            return False
        bootstrap = json.loads((root / 'bootstrap-manifest.json').read_text(encoding='utf-8-sig'))
        python = root / '.python' / bootstrap['python']['version'] / 'tools/python.exe'
        spec = json.loads((root / 'runtime-manifest.json').read_text(encoding='utf-8-sig'))['assets']['model']
        model = root / 'models' / spec['name']
        return (python.is_file() and model.is_file() and model.stat().st_size == spec['size']
                and all((root / 'tools' / name).is_file() and (root / 'tools' / name).stat().st_size == tool['size']
                        for name, tool in value['tools'].items()))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def native_matches(root: Path, *, full: bool) -> bool:
    spec = json.loads((root/'desktop/desktop-manifest.json').read_text(encoding='utf-8-sig')).get('whisper_vulkan')
    if spec is None:
        return True  # Previously installed versions did not include this runtime.
    files = spec.get('files') if isinstance(spec, dict) else None
    if not isinstance(files, dict) or not 2 <= len(files) <= 128:
        return False
    for name, info in files.items():
        if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+',name) or name in ('.','..'):
            return False
        path = root/'tools/whisper-vulkan'/name
        if not isinstance(info,dict) or not isinstance(info.get('size'),int) or not 0<info['size']<100*1024**2:
            return False
        if not isinstance(info.get('sha256'),str) or not re.fullmatch(r'[a-f0-9]{64}',info['sha256']):
            return False
        if not path.is_file() or path.stat().st_size != info['size']:
            return False
        if full and file_digest(path) != info['sha256']:
            return False
    return True


def verify_files(root: Path, expected: str, tools: dict | None = None) -> bool:
    if not re.fullmatch('[a-f0-9]{64}', expected) or fingerprint(root) != expected:
        return False
    if not native_matches(root, full=True):
        return False
    spec = json.loads((root / 'runtime-manifest.json').read_text(encoding='utf-8-sig'))['assets']['model']
    name = spec['name']
    if Path(name).name != name or '/' in name or '\\' in name:
        return False
    model = root / 'models' / name
    if not model.is_file() or model.stat().st_size != spec['size']:
        return False
    if file_digest(model) != spec['sha256']:
        return False
    if tools is None:
        return all((root / 'tools' / name).is_file() for name in ('ffmpeg.exe', 'whisper-cli.exe'))
    for name, spec in tools.items():
        path = root / 'tools' / name
        if not path.is_file() or path.stat().st_size != spec['size'] or file_digest(path) != spec['sha256']:
            return False
    return True


def verify_packages(root: Path) -> bool:
    for line in (root / 'requirements-lock.txt').read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s;]+)', line.strip())
        if match and importlib.metadata.version(match[1]) != match[2]:
            return False
    # Importing the native binding catches missing Microsoft runtime DLLs.
    for name in ('fastapi', 'uvicorn', 'pywhispercpp', 'numpy', 'websockets', 'certifi'):
        __import__(name)
    return True


def compatible(root: Path, descriptor: dict) -> bool:
    try:
        if not valid_descriptor(descriptor):
            return False
        version = json.loads((root / 'bootstrap-manifest.json').read_text(encoding='utf-8-sig'))['python']['version']
        python = root / '.python' / version / 'tools/python.exe'
        if not python.is_file():
            return False
        result = subprocess.run([str(python), '-I', str(Path(__file__).resolve()),
            '--root', str(root), '--expected', descriptor['fingerprint'], '--descriptor', '-'],
            input=json.dumps(descriptor).encode(), capture_output=True, timeout=45,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        return result.returncode == 0
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--expected', required=True)
    parser.add_argument('--descriptor', required=True)
    args = parser.parse_args()
    try:
        value = json.load(sys.stdin) if args.descriptor == '-' else json.loads(Path(args.descriptor).read_text())
        ready = (valid_descriptor(value) and value['fingerprint'] == args.expected
                 and verify_files(args.root, args.expected, value['tools']) and verify_packages(args.root))
    except Exception:
        ready = False
    # Never emit local settings, paths, audio or credentials from this probe.
    print(json.dumps({'compatible': ready}))
    return 0 if ready else 2


if __name__ == '__main__':
    raise SystemExit(main())
