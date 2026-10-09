import hashlib
import json
from scripts.update_runtime import descriptor, fingerprint, verify_files


def runtime(root):
    (root / 'desktop').mkdir()
    (root / 'models').mkdir()
    (root / 'tools').mkdir()
    model = b'bounded runtime fixture'
    (root / 'models/model.bin').write_bytes(model)
    (root / 'tools/ffmpeg.exe').write_bytes(b'tool')
    (root / 'tools/whisper-cli.exe').write_bytes(b'tool')
    (root / 'requirements-lock.txt').write_text('package==1.0\n')
    (root / 'bootstrap-manifest.json').write_text('{"python":{"version":"3.12.10"}}')
    (root / 'desktop/desktop-manifest.json').write_text('{"sdk":"fixed"}')
    (root / 'runtime-manifest.json').write_text(json.dumps({'assets': {'model': {
        'name': 'model.bin', 'size': len(model), 'sha256': hashlib.sha256(model).hexdigest()}}}))


def test_reuse_requires_actual_model_integrity_and_tools(tmp_path):
    runtime(tmp_path)
    expected = fingerprint(tmp_path)
    assert verify_files(tmp_path, expected)
    model = tmp_path / 'models/model.bin'
    original = model.read_bytes()
    model.write_bytes(b'x' * len(original))
    assert not verify_files(tmp_path, expected)
    model.write_bytes(original)
    (tmp_path / 'tools/ffmpeg.exe').unlink()
    assert not verify_files(tmp_path, expected)


def test_runtime_fingerprint_normalizes_platform_line_endings(tmp_path):
    runtime(tmp_path)
    expected = fingerprint(tmp_path)
    (tmp_path / 'requirements-lock.txt').write_bytes(b'package==1.0\r\n')
    assert fingerprint(tmp_path) == expected
    (tmp_path / 'requirements-lock.txt').write_text('package==2.0\n')
    assert not verify_files(tmp_path, expected)


def test_changed_runtime_manifest_and_path_escape_are_rejected(tmp_path):
    runtime(tmp_path)
    manifest = tmp_path / 'runtime-manifest.json'
    value = json.loads(manifest.read_text())
    value['assets']['model']['name'] = '../model.bin'
    manifest.write_text(json.dumps(value))
    assert not verify_files(tmp_path, fingerprint(tmp_path))


def test_compact_detects_modified_tools_even_with_unchanged_size(tmp_path):
    runtime(tmp_path)
    expected = descriptor(tmp_path)
    assert verify_files(tmp_path, expected['fingerprint'], expected['tools'])
    (tmp_path / 'tools/ffmpeg.exe').write_bytes(b'evil')
    assert not verify_files(tmp_path, expected['fingerprint'], expected['tools'])
