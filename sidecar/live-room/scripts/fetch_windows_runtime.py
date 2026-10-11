"""Fetch the byte-pinned app-local Microsoft runtime; changes no system DLLs."""
from __future__ import annotations
import hashlib
import io
import json
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    if os.name != 'nt':
        return 0
    meta = json.loads((ROOT / 'app/windows-runtime-manifest.json').read_text(encoding='utf8'))
    folder = ROOT / 'app/vendor/msvc'
    folder.mkdir(parents=True, exist_ok=True)
    if all((folder/name).is_file() and hashlib.sha256((folder/name).read_bytes()).hexdigest() == info['sha256']
           for name, info in meta['files'].items()):
        print('App-local Microsoft runtime verified')
        return 0
    import ssl
    try:
        import certifi
        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()
    with urllib.request.urlopen(meta['asset_url'], timeout=60, context=context) as response:
        raw = response.read(meta['size'] + 1)
    if len(raw) != meta['size'] or hashlib.sha256(raw).hexdigest() != meta['sha256']:
        raise RuntimeError('Microsoft runtime archive checksum mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.namelist()) != len(meta['files']) or set(archive.namelist()) != set(meta['files']):
            raise RuntimeError('Unexpected Microsoft runtime archive entries')
        verified = {}
        for name, info in meta['files'].items():
            if Path(name).name != name or not name.endswith('.dll') or archive.getinfo(name).file_size != info['size']:
                raise RuntimeError('Invalid Microsoft runtime entry')
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise RuntimeError('Microsoft runtime file checksum mismatch')
            verified[name] = data
        for name, data in verified.items():
            target = folder / name
            temporary = folder / (name + '.part')
            temporary.write_bytes(data)
            temporary.replace(target)
    print('App-local Microsoft runtime installed and verified')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
