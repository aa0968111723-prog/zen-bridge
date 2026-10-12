import asyncio
import base64
import io
import json
import ssl
import wave
from pathlib import Path
from types import SimpleNamespace
from app import zen_agent


def test_agent_verified_tls_native_contract_and_cleanup(monkeypatch):
    from app.preview_asr import PreviewAsr
    monkeypatch.setattr(PreviewAsr,'available',lambda self:False)
    monkeypatch.setenv('ZEN_BRIDGE_AGENT_URL', 'wss://example.com/asr-agent')
    monkeypatch.setenv('ZEN_BRIDGE_AGENT_TOKEN', 'a' * 64)
    audio = io.BytesIO()
    with wave.open(audio, 'wb') as writer:
        writer.setparams((1, 2, 16000, 0, 'NONE', ''))
        writer.writeframes(b'\0' * 3200)
    requests = [
        {'type': 'transcribe', 'id': 'invalid', 'audio': '!'},
        {'type': 'transcribe', 'id': 'valid', 'audio': base64.b64encode(audio.getvalue()).decode(), 'language': 'en'},
    ]
    received, files = [], []
    # This transport unit must not probe any real Hermes/Ollama service.
    monkeypatch.setattr(zen_agent.AgentRequests,'status',lambda self: {'asr_ready':True,'metrics':self.metrics.snapshot()})

    async def exercise():
        closed = asyncio.Event()
        replied = asyncio.Event()

        class Asr:
            ready = True
            def health(self): return self.ready
            def transcribe(self, path, prompt, language):
                assert language == 'en'
                with wave.open(str(path)) as reader:
                    assert (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) == (1, 2, 16000)
                files.append(Path(path))
                self.ready = False
                return SimpleNamespace(ok=True, text='public test')

        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): closed.set()
            async def send(self, message):
                parsed=json.loads(message)
                received.append(parsed)
                if parsed.get('type')=='result' and parsed.get('id')=='valid':
                    replied.set()
            async def close(self, **kwargs): closed.set()
            def __aiter__(self): return self.messages()
            async def messages(self):
                for request in requests: yield json.dumps(request)
                # A real peer stays connected while accepted jobs complete.
                # EOF before completion is a disconnect, and cancels jobs.
                await replied.wait()

        def connect(url, **kwargs):
            context = kwargs['ssl']
            assert isinstance(context, ssl.SSLContext)
            assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
            assert context.get_ca_certs()
            assert kwargs['proxy'] is None
            assert kwargs['additional_headers']['Authorization'].startswith('Bearer ')
            return Socket()

        monkeypatch.setattr(zen_agent, 'connect', connect)
        task = asyncio.create_task(zen_agent.run_agent(Asr()))
        await asyncio.wait_for(closed.wait(), 3)
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass

    asyncio.run(exercise())
    assert received[0]['type'] == 'ready'
    assert received[0]['capabilities']==['asr','translate','metrics']
    results=[message for message in received if message['type']=='result']
    assert results[0]['id']=='invalid' and results[0]['ok'] is False
    assert results[1]['id']=='valid' and results[1]['ok'] is True and results[1]['source'] == 'public test'
    assert files and all(not path.exists() for path in files)
