"""Load only the pinned app-local Microsoft runtime before ONNX on Windows."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path

_handle = None

def ensure_windows_runtime() -> None:
    global _handle
    if os.name != 'nt' or _handle is not None:
        return
    root = Path(__file__).parent
    manifest = json.loads((root / 'windows-runtime-manifest.json').read_text(encoding='utf8'))
    folder = root / 'vendor' / 'msvc'
    for name, expected in manifest['files'].items():
        if Path(name).name != name or not name.endswith('.dll'):
            raise RuntimeError('Invalid Windows runtime manifest')
        path = folder / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size != expected['size']:
            raise RuntimeError('App-local Microsoft runtime is missing; run setup_local.ps1')
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            raise RuntimeError('App-local Microsoft runtime checksum mismatch')
    # Keep the handle alive: closing it removes the directory from DLL resolution.
    _handle = os.add_dll_directory(str(folder.resolve()))
