import asyncio
import base64
import tempfile
import time
import wave
from pathlib import Path
from app.agent_metrics import AgentMetrics
from app.desktop_llm import DesktopTranslator

class AgentRequests:
    def __init__(self,asr,root):
        self.asr=asr;self.root=root;self.metrics=AgentMetrics();self.translator=DesktopTranslator()
        self.asr_lane=asyncio.Semaphore(1);self.translation_lane=asyncio.Semaphore(1)
    def status(self):
        return {'metrics':self.metrics.snapshot(),'translation':self.translator.status(),'asr_ready':self.asr.health()}
    async def handle(self,request):
        kind=request.get('type');identifier=request.get('id')
        if kind not in ('transcribe','translate','status') or not isinstance(identifier,str) or len(identifier)>80:return None
        response={'type':'result' if kind=='transcribe' else 'control_result','id':identifier,'ok':False}
        if kind=='status':return {**response,'ok':True,'result':await asyncio.to_thread(self.status)}
        started=time.monotonic();duration=None;route=None
        try:
            if kind=='translate':
                async with self.translation_lane:
                    result=await asyncio.to_thread(self.translator.translate,request.get('instructions'),request.get('input'),request.get('mode'))
                route=result['route'];response.update(ok=True,result=result)
            else:
                async with self.asr_lane:
                    if not isinstance(request.get('audio'),str) or len(request['audio'])>6_700_000:raise ValueError()
                    audio=base64.b64decode(request['audio'],validate=True)
                    if not 44<=len(audio)<=5_000_000:raise ValueError()
                    with tempfile.TemporaryDirectory(dir=self.root/'tmp',prefix='zen-audio-') as work:
                        wav=Path(work)/'audio.wav';wav.write_bytes(audio)
                        with wave.open(str(wav),'rb') as reader:
                            duration=reader.getnframes()/16000
                            if (reader.getnchannels(),reader.getsampwidth(),reader.getframerate())!=(1,2,16000) or not 0<duration<=30:raise ValueError()
                        language=request.get('language','zh')
                        if language not in ('zh','en'):raise ValueError()
                        operation=asyncio.create_task(asyncio.to_thread(self.asr.transcribe,wav,str(request.get('prompt',''))[:2000],language))
                        try:
                            result=await asyncio.shield(operation)
                        except asyncio.CancelledError:
                            # Keep the WAV and ASR lane alive until the native thread exits.
                            await operation
                            raise
                        response.update(ok=result.ok,source=result.text if result.ok else '')
        except Exception:response['error']='Selected desktop operation failed'
        self.metrics.record(kind,(time.monotonic()-started)*1000,response['ok'],duration,route)
        return response
