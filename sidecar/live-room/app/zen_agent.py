"""Outbound paired transport. Uses the same local model as the desktop service."""
import asyncio
import base64
import json
import os
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlsplit
from websockets.asyncio.client import connect

async def run_agent(asr):
    url = os.getenv('ZEN_BRIDGE_AGENT_URL', '')
    token = os.getenv('ZEN_BRIDGE_AGENT_TOKEN', '')
    if not url or not token:
        return
    parsed = urlsplit(url)
    if parsed.scheme != 'wss' or parsed.username or parsed.password or parsed.query or parsed.fragment or len(token) < 32 or '\n' in token or '\r' in token:
        raise ValueError('Invalid Zen Bridge pairing configuration')
    root = Path(__file__).resolve().parents[1]
    (root / 'tmp').mkdir(exist_ok=True)
    while True:
        try:
            if not asr.health():
                await asyncio.sleep(2)
                continue
            async with connect(url, additional_headers={'Authorization': 'Bearer ' + token}, proxy=None,
                    max_size=12_000_000, ping_interval=20, ping_timeout=30, open_timeout=20) as socket:
                await socket.send(json.dumps({'type': 'ready', 'protocol': 1, 'engine': 'breeze-native'}))
                async for message in socket:
                    request = json.loads(message)
                    if request.get('type') != 'transcribe' or not isinstance(request.get('id'), str):
                        continue
                    response = {'type': 'result', 'id': request['id'], 'ok': False}
                    try:
                        if len(request.get('audio', '')) > 6_700_000:
                            raise ValueError('Audio too large')
                        audio = base64.b64decode(request['audio'], validate=True)
                        if not 44 <= len(audio) <= 5_000_000:
                            raise ValueError('Invalid audio size')
                        with tempfile.TemporaryDirectory(dir=root / 'tmp', prefix='zen-audio-') as work:
                            wav = Path(work) / 'audio.wav'
                            wav.write_bytes(audio)
                            with wave.open(str(wav), 'rb') as reader:
                                if (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()) != (1,2,16000) or reader.getnframes()/16000 > 30:
                                    raise ValueError('Expected bounded PCM16/16kHz/mono audio')
                            language = request.get('language', 'zh')
                            if language not in ('zh', 'en'):
                                raise ValueError('Unsupported language')
                            result = await asyncio.to_thread(asr.transcribe, wav, str(request.get('prompt',''))[:2000], language)
                            response.update(ok=result.ok, source=result.text if result.ok else '')
                    except (ValueError, OSError, wave.Error, KeyError):
                        response['error'] = 'Invalid local audio request'
                    await socket.send(json.dumps(response, ensure_ascii=True))
                    if not asr.health():
                        await socket.close(code=1011, reason='Local model needs restart')
                        break
        except asyncio.CancelledError:
            raise
        except Exception:
            # Never log request audio, the pairing token or raw TLS/URL errors.
            await asyncio.sleep(3)
