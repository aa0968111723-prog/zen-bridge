import pytest
from app.zen_agent import validate_pairing
from app.desktop_llm import DesktopTranslator

def test_plain_websocket_requires_loopback_and_explicit_local_mode():
    token='test-only-pairing-token-'+'x'*32
    assert validate_pairing('ws://127.0.0.1:8785/asr-agent',token,True)
    assert not validate_pairing('wss://example.com/asr-agent',token)
    for url,enabled in [('ws://127.0.0.1:8785/asr-agent',False),('ws://example.com/asr-agent',True),('ws://192.168.1.5/asr-agent',True),('ws://user:pass@localhost/asr-agent',True),('ws://localhost/asr-agent?token=secret',True)]:
        with pytest.raises(ValueError):validate_pairing(url,token,enabled)

def test_local_desktop_cannot_route_to_cloud_or_probe_hermes(monkeypatch):
    monkeypatch.setenv('ZEN_BRIDGE_LOCAL','1')
    translator=DesktopTranslator()
    calls=[]
    def request(base,path,payload,**kwargs):
        calls.append((base,path))
        return {'message':{'content':'Please return to your breathing.'},'done_reason':'stop','prompt_eval_count':1,'eval_count':1}
    translator._request=request
    for mode in ('local','cloud','hybrid'):
        assert translator.translate('Translate to English.','請回到呼吸。',mode)
    assert calls==[(translator.local,'/api/chat')]*3
    def reject_network(*args,**kwargs):raise AssertionError('Unexpected Hermes health request')
    translator.opener.open=reject_network
    assert translator.status()['local_only'] is True
    assert translator.status()['cloud_model']==''
