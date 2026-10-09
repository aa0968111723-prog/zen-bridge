import hashlib
import io
import json
import pytest
from scripts import build_setup as builder

@pytest.fixture
def asset_root(tmp_path,monkeypatch):
    root=tmp_path/'source';(root/'desktop').mkdir(parents=True)
    data=b'verified vendor installer'
    asset={'url':'https://vendor.invalid/current.exe','sha256':hashlib.sha256(data).hexdigest(),'size':len(data)}
    (root/'desktop/desktop-manifest.json').write_text(json.dumps({'bootstrapper':asset,'vc_runtime':asset}))
    monkeypatch.setattr(builder,'ROOT',root)
    return root,tmp_path/'payload',data

def test_changed_vendor_bytes_use_exact_pinned_mirror(asset_root,monkeypatch):
    root,payload,data=asset_root;calls=[]
    def open_url(url,timeout):
        calls.append(url);return io.BytesIO(b'changed vendor binary' if url.startswith('https://vendor') else data)
    monkeypatch.setattr(builder.urllib.request,'urlopen',open_url)
    builder.stage_bootstrapper(payload)
    assert len(calls)==2 and calls[-1]==builder.PINNED_INSTALLERS+'WebView2Bootstrapper.exe'
    assert (payload/'desktop/WebView2Bootstrapper.exe').read_bytes()==data

def test_verified_cache_needs_no_network(asset_root,monkeypatch):
    root,payload,data=asset_root;(root/'.downloads').mkdir()
    (root/'.downloads/vc_redist.x64.exe').write_bytes(data)
    monkeypatch.setattr(builder.urllib.request,'urlopen',lambda *a,**kw:pytest.fail('Unexpected network'))
    builder.stage_vc_runtime(payload)
    assert (payload/'desktop/vc_redist.x64.exe').read_bytes()==data

def test_bad_mirror_fails_without_staging_executable(asset_root,monkeypatch):
    root,payload,data=asset_root
    monkeypatch.setattr(builder.urllib.request,'urlopen',lambda *a,**kw:io.BytesIO(b'wrong'))
    with pytest.raises(RuntimeError,match='failed checksum'):builder.stage_bootstrapper(payload)
    assert not (payload/'desktop/WebView2Bootstrapper.exe').exists()

def test_vendor_network_error_can_use_verified_mirror(asset_root,monkeypatch):
    root,payload,data=asset_root
    def open_url(url,timeout):
        if url.startswith('https://vendor'):raise OSError('offline')
        return io.BytesIO(data)
    monkeypatch.setattr(builder.urllib.request,'urlopen',open_url)
    builder.stage_vc_runtime(payload)
    assert (payload/'desktop/vc_redist.x64.exe').read_bytes()==data
