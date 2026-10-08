from app.native_paths import NativePaths

def test_model_copy_fallback_when_hardlinks_not_supported(tmp_path, monkeypatch):
    model = tmp_path / '模型.bin'
    model.write_bytes(b'model')
    def unsupported(*args):
        raise OSError('filesystem does not support hard links')
    monkeypatch.setattr('app.native_paths.os.link', unsupported)
    paths = NativePaths(model)
    paths.cwd = tmp_path
    try:
        argument = paths.argument(model)
        assert argument.isascii()
        assert (tmp_path / argument).read_bytes() == b'model'
    finally:
        paths.close()
    assert model.read_bytes() == b'model'
