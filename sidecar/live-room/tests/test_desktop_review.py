"""Regression checks for desktop review findings; no model or network needed."""
import io
from types import SimpleNamespace
import pytest
from app.settings import Settings
from app.native_asr import NativeResidentAsr

@pytest.mark.parametrize('value',[float('inf'),float('-inf'),float('nan'),0,-1])
def test_upload_timeout_rejects_nonfinite_and_nonpositive(value):
    with pytest.raises(ValueError,match='BREEZE_UPLOAD_READ_TIMEOUT'):
        Settings(upload_read_timeout_s=value)

@pytest.mark.parametrize('ok,text,blank',[(True,'',True),(True,'  ',True),(True,'hello',False),(False,'',False)])
def test_native_blank_does_not_enter_rtf_sample(tmp_path,monkeypatch,ok,text,blank):
    engine=NativeResidentAsr(tmp_path/'model.bin')
    engine.proc=SimpleNamespace(stdin=io.StringIO())
    monkeypatch.setattr(engine,'health',lambda:True)
    monkeypatch.setattr(engine,'_receive',lambda timeout:{'ok':ok,'text':text})
    result=engine.transcribe(tmp_path/'audio.wav','')
    assert result.blank is blank
    assert result.ok is ok
    assert result.text==text

def test_desktop_service_ignores_forged_proxy_headers(monkeypatch):
    from app import desktop_service as service
    configs=[]
    monkeypatch.setattr(service.sys,'stdin',io.StringIO('start\n'))
    monkeypatch.setattr(service,'cleanup_downloads',lambda:None)
    monkeypatch.setattr(service,'fill_process_environ',lambda path:None)
    monkeypatch.setattr(service.os,'chdir',lambda path:None)
    monkeypatch.setattr(service.Settings,'from_env',lambda:Settings())
    class Server:
        def __init__(self,config):configs.append(config)
        def run(self):pass
    monkeypatch.setattr(service.uvicorn,'Server',Server)
    monkeypatch.setattr(service.threading,'Thread',lambda **kwargs:SimpleNamespace(start=lambda:None))
    service.main()
    assert len(configs)==1
    configs[0].load()
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
    assert not isinstance(configs[0].loaded_app,ProxyHeadersMiddleware)
    assert configs[0].proxy_headers is False
