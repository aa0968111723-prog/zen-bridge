import asyncio
import base64
import io
import wave

from app.agent_requests import AgentRequests
from app.asr import AsrResult
from app.asr_report import asr_mode_label
from app.fast_final import FastFinalAsr, SherpaOfflineFinal
from tests.test_fast_final import FakeRecognizer


def _wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x01\x00" * 16000)
    return buf.getvalue()


def test_setup_label_unwraps_fast_final_wrapper():
    class Native:
        pass
    from app.native_asr import NativeResidentAsr
    native = NativeResidentAsr.__new__(NativeResidentAsr)
    wrapped = FastFinalAsr(native, SherpaOfflineFinal("sense_voice", None, recognizer=FakeRecognizer(), convert=lambda text: text))
    assert asr_mode_label(wrapped) == "native"
    assert asr_mode_label(Native()) == "cli"


def test_agent_transcribe_uses_fast_route_and_saves_chinese(tmp_path):
    class Breeze:
        def __init__(self):
            self.calls = 0
        def transcribe(self, wav, prompt, language="zh"):
            self.calls += 1
            return AsrResult(ok=True, text="慢稿")
        def health(self):
            return True
    breeze = Breeze()
    fast = SherpaOfflineFinal("sense_voice", None, recognizer=FakeRecognizer("快稿"), convert=lambda text: text)
    saved = []
    agent = AgentRequests(FastFinalAsr(breeze, fast), tmp_path, on_line=lambda audio, text, request: saved.append((text, request.get("session_id"), audio[:4])))
    (tmp_path / "tmp").mkdir()

    async def exercise():
        return await agent.handle({
            "type": "transcribe", "id": "line-1", "language": "zh",
            "session_id": "11111111-1111-1111-1111-111111111111",
            "audio": base64.b64encode(_wav_bytes()).decode(),
        })

    response = asyncio.run(exercise())
    assert response["ok"] is True and response["source"] == "快稿"
    assert breeze.calls == 0
    assert saved and saved[0][0] == "快稿" and saved[0][1].startswith("11111111")
