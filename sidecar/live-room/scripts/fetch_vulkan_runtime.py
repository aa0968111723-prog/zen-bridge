"""Stage one pinned, internally matched Vulkan runtime; no system changes."""
import hashlib
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def stage(spec, destination, archive_bytes):
    if len(archive_bytes) != spec['size'] or hashlib.sha256(archive_bytes).hexdigest() != spec['sha256']:
        raise ValueError('Vulkan archive checksum mismatch')
    verified = {}
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        if len(archive.namelist()) != len(spec['files']) or set(archive.namelist()) != set(spec['files']):
            raise ValueError('Unexpected Vulkan archive entries')
        for name, info in spec['files'].items():
            if '/' in name or '\\' in name or ':' in name or Path(name).name != name or name in ('.','..'):
                raise ValueError('Invalid Vulkan entry name')
            if archive.getinfo(name).file_size != info['size']:
                raise ValueError('Invalid Vulkan entry size')
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != info['sha256']:
                raise ValueError('Vulkan file checksum mismatch')
            verified[name] = data
        build = json.loads(verified['build-manifest.json'].decode('utf-8-sig'))
        if build['source'] != spec['source']:
            raise ValueError('Vulkan source revision mismatch')
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or any((destination/name).is_symlink() for name in verified):
        raise ValueError('Vulkan destination must not be a symlink')
    for name, data in verified.items():
        temporary = destination/(name+'.part')
        if temporary.is_symlink():
            raise ValueError('Vulkan staging path must not be a symlink')
        temporary.write_bytes(data)
        temporary.replace(destination/name)

def main():
    spec = json.loads((ROOT/'desktop/desktop-manifest.json').read_text())['whisper_vulkan']
    destination = ROOT/'tools/whisper-vulkan'
    if all((destination/name).is_file() and hashlib.sha256((destination/name).read_bytes()).hexdigest() == info['sha256']
           for name, info in spec['files'].items()):
        print('Matching Vulkan runtime verified')
        return
    cache = ROOT/'.downloads'/spec['name']
    if cache.is_file() and cache.stat().st_size == spec['size'] and hashlib.sha256(cache.read_bytes()).hexdigest() == spec['sha256']:
        raw = cache.read_bytes()
    else:
        with urllib.request.urlopen(spec['url'], timeout=60) as incoming:
            raw = incoming.read(spec['size']+1)
    stage(spec,destination,raw)
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(raw)
    print('Matching Vulkan runtime staged')

if __name__ == '__main__':
    main()
