import base64,io,wave
from types import SimpleNamespace
import pytest
from app.agent_requests import AgentRequests

def audio(seconds=1,rate=16000):
    data=io.BytesIO()
    with wave.open(data,'wb') as stream:
        stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(rate)
        stream.writeframes(b'\0\0'*int(seconds*rate))
    return base64.b64encode(data.getvalue()).decode()

@pytest.mark.anyio
async def test_preview_does_not_call_primary_asr_or_translate(tmp_path):
    (tmp_path/'tmp').mkdir()
    def forbidden(*args):raise AssertionError('preview reached final ASR/translation')
    agent=AgentRequests(SimpleNamespace(transcribe=forbidden),tmp_path)
    agent.translator=SimpleNamespace(translate=forbidden)
    seen=[]
    def preview(path):seen.append(path);assert path.exists();return '待確認草稿'
    agent.preview=SimpleNamespace(transcribe=preview)
    result=await agent.handle({'type':'preview','id':'draft-1','audio':audio(),'language':'zh'})
    assert result['type']=='control_result' and result['ok']
    assert result['result']=={'source':'待確認草稿','ephemeral':True}
    assert seen and not seen[0].exists()
    assert agent.metrics.snapshot()['history'][-1]['kind']=='preview'

@pytest.mark.anyio
@pytest.mark.parametrize('clip',[audio(7),audio(1,8000),'not base64'],ids=['long','wrong-rate','invalid-base64'])
async def test_invalid_or_long_previews_are_rejected(tmp_path,clip):
    (tmp_path/'tmp').mkdir()
    agent=AgentRequests(None,tmp_path)
    agent.preview=SimpleNamespace(transcribe=lambda *_:pytest.fail('invalid audio decoded'))
    result=await agent.handle({'type':'preview','id':'bad','audio':clip,'language':'zh'})
    assert not result['ok']
