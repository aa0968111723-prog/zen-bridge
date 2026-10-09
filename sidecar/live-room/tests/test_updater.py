import hashlib
import json
import shutil
import zipfile
from pathlib import Path

import pytest
from scripts.package_release import ROOT, build
from scripts.update import unpack, snapshot, restore, replace_source, apply

def copy_source(target):
    archive = build(ROOT, target.parent / ('dist-' + target.name))
    target.mkdir()
    unpack(archive, target)
    return target

def test_release_excludes_configuration_models_and_captions(tmp_path):
    archive = build(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as zipped:
        assert 'install.bat' in zipped.namelist()
        assert 'update.ps1' in zipped.namelist()
        assert 'desktop/Launcher.cs' in zipped.namelist()
        assert 'desktop/setup.iss' in zipped.namelist()
        assert 'app/desktop_update.py' in zipped.namelist()
        assert not any(n == '.env' or n.startswith(('.venv/', '.python/', 'models/', 'data/', '.git/')) for n in zipped.namelist())
    unpack(archive, tmp_path / 'unpacked')

@pytest.mark.parametrize('path', ['../escape.py', 'app/../../escape.py', '.env', 'data/captions.sqlite3', 'app/evil\\name.py', 'app/evil:stream'])
def test_update_rejects_paths_and_user_data(tmp_path, path):
    archive = build(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as original:
        blobs = {n: original.read(n) for n in original.namelist()}
    manifest = json.loads(blobs['package-manifest.json'])
    manifest['files'][path] = hashlib.sha256(b'bad').hexdigest()
    blobs['package-manifest.json'] = json.dumps(manifest).encode()
    blobs[path] = b'bad'
    with zipfile.ZipFile(archive, 'w') as zipped:
        for name, data in blobs.items():
            zipped.writestr(name, data)
    with pytest.raises(ValueError):
        unpack(archive, tmp_path / 'stage')
    assert not (tmp_path / 'escape.py').exists()

def test_corrupt_package_rejected(tmp_path):
    archive = build(ROOT, tmp_path)
    with zipfile.ZipFile(archive) as original:
        blobs = {n: original.read(n) for n in original.namelist()}
    blobs['VERSION'] = b'9.9.9'
    with zipfile.ZipFile(archive, 'w') as zipped:
        for name, data in blobs.items():
            zipped.writestr(name, data)
    with pytest.raises(ValueError, match='checksum'):
        unpack(archive, tmp_path / 'stage')

def test_rollback_preserves_settings_and_captions_and_restores_model(tmp_path):
    root = copy_source(tmp_path / 'installed')
    (root / '.env').write_text('private-settings')
    (root / 'data').mkdir()
    (root / 'data/captions.sqlite3').write_bytes(b'user-captions')
    (root / 'models').mkdir()
    (root / 'models/model.bin').write_bytes(b'original-model')
    backup = root / '.updates/backup'
    snapshot(root, backup)
    (root / 'VERSION').write_text('9.9.9')
    temporary = root / 'models/new.bin'
    temporary.write_bytes(b'new-model')
    temporary.replace(root / 'models/model.bin')
    restore(root, backup)
    assert (root / 'VERSION').read_text().strip() == (ROOT / 'VERSION').read_text().strip()
    assert (root / 'models/model.bin').read_bytes() == b'original-model'
    assert (root / '.env').read_text() == 'private-settings'
    assert (root / 'data/captions.sqlite3').read_bytes() == b'user-captions'

def test_failed_update_restores_source_environment_and_user_data(tmp_path, monkeypatch):
    root = copy_source(tmp_path / 'installed')
    stage = copy_source(tmp_path / 'stage')
    (root / '.updates').mkdir()
    (root / '.venv').mkdir()
    (root / '.venv/old').write_text('old-environment')
    (root / '.env').write_text('keep-settings')
    base_python = root / '.python/3.12.10/tools/python.exe'
    base_python.parent.mkdir(parents=True)
    base_python.touch()
    (stage / 'VERSION').write_text('9.9.9')
    monkeypatch.setattr('scripts.update.require_stopped', lambda root: None)
    def runner(command, **kwargs):
        if command[1:3] == ['-m', 'venv']:
            Path(command[-1]).mkdir()
        elif 'scripts/install_runtime.py' in command:
            raise RuntimeError('injected installation failure')
    with pytest.raises(RuntimeError, match='injected'):
        apply(root, stage, run=runner)
    assert (root / 'VERSION').read_text().strip() == (ROOT / 'VERSION').read_text().strip()
    assert (root / '.venv/old').read_text() == 'old-environment'
    assert (root / '.env').read_text() == 'keep-settings'
