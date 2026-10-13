import asyncio
import base64
import tempfile
import time
import wave
from pathlib import Path
from app.agent_metrics import AgentMetrics
from app.desktop_llm import DesktopTranslator, TRANSLATION_BUDGET_S, TRANSLATION_DEADLINE

class AgentRequests:
    def __init__(self,asr,root,on_line=None):
        self.asr=asr;self.root=root;self.on_line=on_line;self.metrics=AgentMetrics(path=Path(root)/'tmp'/'agent-metrics.jsonl');self.translator=DesktopTranslator()
        import os
        if os.getenv('ZEN_AGENT_SUITE_GRAPH','0')=='1':
            from app.agent_suite import AgentSuiteTranslator
            self.translator=AgentSuiteTranslator(self.translator)
        self.asr_lane=asyncio.Semaphore(1);self.translation_lane=asyncio.Semaphore(1)
        self.preview_lane=asyncio.Semaphore(1)
        from app.preview_asr import PreviewAsr
        self.preview=PreviewAsr(root)
    def status(self):
        from app.agent_suite import suite_status
        translation=self.translator.status()
        translation.setdefault('agent_suite',{'profiles':suite_status(),'enabled':False})
        return {'metrics':self.metrics.snapshot(),'translation':translation,'asr_ready':self.asr.health(),
                'preview_available':self.preview.available()}
    async def _run_blocking(self, function, *args, kind='transcribe'):
        from app.runtime_activity import work
        with work(kind):
            return await self._await_blocking(function,*args)

    async def _await_blocking(self, function, *args):
        operation=asyncio.create_task(asyncio.to_thread(function,*args))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # A cancelled await cannot stop an inference thread. Keep its lane
            # until it exits, so reconnecting does not start a second inference.
            await operation
            raise
    async def handle(self,request):
        kind=request.get('type');identifier=request.get('id')
        if kind not in ('transcribe','translate','status','preview') or not isinstance(identifier,str) or len(identifier)>80:return None
        response={'type':'result' if kind=='transcribe' else 'control_result','id':identifier,'ok':False}
        if kind=='status':return {**response,'ok':True,'result':await asyncio.to_thread(self.status)}
        started=time.monotonic();duration=None;route=None;result=None
        try:
            if kind=='translate':
                deadline=time.monotonic()+TRANSLATION_BUDGET_S
                async with asyncio.timeout(TRANSLATION_BUDGET_S):
                    async with self.translation_lane:
                        token=TRANSLATION_DEADLINE.set(deadline)
                        try:
                            result=await self._run_blocking(self.translator.translate,request.get('instructions'),request.get('input'),request.get('mode'),kind='translate')
                        finally:
                            TRANSLATION_DEADLINE.reset(token)
                route=result['route'];response.update(ok=True,result=result)
            else:
                async with (self.preview_lane if kind=='preview' else self.asr_lane):
                    if not isinstance(request.get('audio'),str) or len(request['audio'])>(294000 if kind=='preview' else 6_700_000):raise ValueError()
                    audio=base64.b64decode(request['audio'],validate=True)
                    if not 44<=len(audio)<=(220000 if kind=='preview' else 5_000_000):raise ValueError()
                    with tempfile.TemporaryDirectory(dir=self.root/'tmp',prefix='zen-audio-') as work:
                        wav=Path(work)/'audio.wav';wav.write_bytes(audio)
                        with wave.open(str(wav),'rb') as reader:
                            duration=reader.getnframes()/16000
                            if (reader.getnchannels(),reader.getsampwidth(),reader.getframerate())!=(1,2,16000) or not 0<duration<=(6.5 if kind=='preview' else 30):raise ValueError()
                        language=request.get('language','zh')
                        if language not in ('zh','en'):raise ValueError()
                        if kind=='preview':
                            text=await self._run_blocking(self.preview.transcribe,wav)
                            response.update(ok=True,result={'source':text,'ephemeral':True})
                        else:
                            from app.fast_final import ROUTE, FastFinalAsr
                            token=ROUTE.set('fast') if isinstance(self.asr, FastFinalAsr) and language=='zh' else None
                            try:
                                result=await self._run_blocking(self.asr.transcribe,wav,str(request.get('prompt',''))[:2000],language)
                            finally:
                                if token is not None:ROUTE.reset(token)
                            response.update(ok=result.ok,source=result.text if result.ok else '')
                            if result.ok and self.on_line is not None:
                                try:self.on_line(audio,result.text,{**request,'duration_ms':int(duration*1000)})
                                except Exception:pass
        except Exception:response['error']='Selected desktop operation failed'
        self.metrics.record(kind,(time.monotonic()-started)*1000,response['ok'],duration,route,result.get('usage_known') if isinstance(result,dict) else None)
        return response
