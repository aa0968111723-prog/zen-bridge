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


def test_model_changes_force_full_update_and_same_size_damage_is_detected(tmp_path):
    runtime(tmp_path)
    legacy = fingerprint(tmp_path)
    (tmp_path/'app').mkdir()
    model = tmp_path/'models/fast-final/fixed/model.int8.onnx'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'verified model')
    manifest = tmp_path/'app/fast-final-model-manifest.json'
    value = {'name':'fixed','files':{'model.int8.onnx':{
        'size':model.stat().st_size,'sha256':hashlib.sha256(model.read_bytes()).hexdigest()}}}
    manifest.write_text(json.dumps(value))
    expected = fingerprint(tmp_path)
    assert expected != legacy and verify_files(tmp_path, expected)
    model.write_bytes(b'x'*model.stat().st_size)
    assert not verify_files(tmp_path, expected)
    model.unlink()
    assert not verify_files(tmp_path, expected)
    value['name']='../outside'
    manifest.write_text(json.dumps(value))
    assert not verify_files(tmp_path, fingerprint(tmp_path))


def test_compact_requires_matching_gpu_files_not_just_cpu_tools(tmp_path):
    runtime(tmp_path)
    native = tmp_path/'tools/whisper-vulkan'
    native.mkdir()
    files = {}
    for name in ('whisper-server.exe','ggml-vulkan.dll'):
        data = b'native fixture'
        (native/name).write_bytes(data)
        files[name] = {'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    manifest = tmp_path/'desktop/desktop-manifest.json'
    manifest.write_text(json.dumps({'whisper_vulkan':{'files':files}}))
    expected = descriptor(tmp_path)
    assert verify_files(tmp_path,expected['fingerprint'],expected['tools'])
    (native/'ggml-vulkan.dll').write_bytes(b'x'*len(data))
    assert not verify_files(tmp_path,expected['fingerprint'],expected['tools'])
    (native/'ggml-vulkan.dll').unlink()
    assert not verify_files(tmp_path,expected['fingerprint'],expected['tools'])


def test_isolated_runtime_does_not_reuse_the_legacy_python(tmp_path):
    from scripts.update_runtime import probably_compatible
    runtime(tmp_path)
    asset={'python':{'version':'3.12.10','directory':'3.12.10-zen050'}}
    (tmp_path/'bootstrap-manifest.json').write_text(json.dumps(asset))
    legacy=tmp_path/'.python/3.12.10/tools/python.exe'
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b'legacy still used by a bot')
    expected=descriptor(tmp_path)
    assert not probably_compatible(tmp_path,expected)
    active=tmp_path/'.python/3.12.10-zen050/tools/python.exe'
    active.parent.mkdir(parents=True)
    active.write_bytes(b'new private runtime')
    assert probably_compatible(tmp_path,expected)
    assert legacy.read_bytes()==b'legacy still used by a bot'
