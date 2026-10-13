import asyncio
import threading
from app.agent_requests import AgentRequests

def test_cancelled_translation_keeps_lane_until_inference_thread_exits(tmp_path):
    async def exercise():
        requests=AgentRequests(None,tmp_path)
        started=threading.Event()
        release=threading.Event()
        calls=[]
        def translate(*args):
            calls.append(args)
            started.set()
            assert release.wait(3)
            return {'route':'local','text':'Hello'}
        requests.translator.translate=translate
        request={'type':'translate','instructions':'Translate','input':'你好','mode':'local'}
        first=asyncio.create_task(requests.handle({**request,'id':'first'}))
        second=None
        try:
            assert await asyncio.to_thread(started.wait,2)
            first.cancel()
            await asyncio.sleep(0)
            second=asyncio.create_task(requests.handle({**request,'id':'second'}))
            await asyncio.sleep(0)
            assert requests.translation_lane.locked()
            assert len(calls)==1
            assert not first.done()
        finally:
            release.set()
            outcomes=await asyncio.gather(first,*([second] if second else []),return_exceptions=True)
        assert isinstance(outcomes[0],asyncio.CancelledError)
        assert outcomes[1]['ok'] is True
        assert len(calls)==2 and not requests.translation_lane.locked()
    asyncio.run(exercise())

def test_queued_translation_expires_without_starting_model(tmp_path, monkeypatch):
    monkeypatch.setattr('app.agent_requests.TRANSLATION_BUDGET_S', .05)
    async def exercise():
        requests=AgentRequests(None,tmp_path)
        await requests.translation_lane.acquire()
        calls=[]
        requests.translator.translate=lambda *args: calls.append(args)
        try:
            result=await requests.handle({'type':'translate','id':'waiting','mode':'local','instructions':'Translate','input':'hello'})
            assert result['ok'] is False
            assert not calls
        finally:
            requests.translation_lane.release()
    asyncio.run(exercise())
