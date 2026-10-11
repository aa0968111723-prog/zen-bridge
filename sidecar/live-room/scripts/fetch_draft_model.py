"""Stage only byte-verified X-ASR files; safe archive extraction without extractall."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tarfile
import tempfile
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
MANIFEST=ROOT/'app/draft-model-manifest.json'


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            value.update(block)
    return value.hexdigest()


def verified(path,spec):
    return path.is_file() and path.stat().st_size==spec['size'] and digest(path)==spec['sha256']


def extract(archive,output,manifest):
    with tarfile.open(archive,'r:bz2') as tar:
        for name,spec in manifest['files'].items():
            # Match a complete fixed member path; symlinks and directories are refused.
            member=tar.getmember(manifest['name']+'/'+name)
            if not member.isfile() or member.size!=spec['size']:
                raise ValueError('Invalid draft model member')
            with tar.extractfile(member) as source,(output/name).open('xb') as target:
                shutil.copyfileobj(source,target)
            if not verified(output/name,spec):
                raise ValueError('Draft model member checksum mismatch')


def stage(destination,source=None,manifest=None):
    manifest=manifest or json.loads(MANIFEST.read_text())
    if all(verified(destination/name,spec) for name,spec in manifest['files'].items()):
        return destination
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Existing draft model differs; use a new destination')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as work:
        temp=Path(work)/'model';temp.mkdir()
        if source is not None:
            for name,spec in manifest['files'].items():
                if not verified(source/name,spec):
                    raise ValueError('Local draft model checksum mismatch')
                shutil.copy2(source/name,temp/name)
        else:
            archive=Path(work)/'model.tar.bz2'
            with urllib.request.urlopen(manifest['url'],timeout=60) as incoming,archive.open('wb') as target:
                remaining=manifest['size']+1
                while remaining:
                    block=incoming.read(min(1024*1024,remaining))
                    if not block:break
                    target.write(block);remaining-=len(block)
            if not verified(archive,manifest):
                raise ValueError('Draft archive checksum mismatch')
            extract(archive,temp,manifest)
        (temp/'origin.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        if destination.exists():
            destination.rmdir()  # empty directory only
        os.replace(temp,destination)
    return destination


def main():
    manifest=json.loads(MANIFEST.read_text())
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dest',type=Path,default=ROOT/'models/draft'/manifest['name'])
    parser.add_argument('--source',type=Path)
    args=parser.parse_args()
    source=args.source
    if source is None:
        local=Path(os.getenv('LOCALAPPDATA',Path.home()/'AppData'/'Local'))/'ZenBridge/models/draft'/manifest['name']
        if local.is_dir():source=local
    print('Verified draft model:',stage(args.dest,source,manifest))


if __name__=='__main__':
    main()
