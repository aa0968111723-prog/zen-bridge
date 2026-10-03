import io
import os
import wave

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODEL_ID = os.getenv("BREEZE_MODEL_PATH", "")
if not MODEL_ID:
    raise RuntimeError("BREEZE_MODEL_PATH must point to a local CTranslate2 conversion of MediaTek-Research/Breeze-ASR-25")
model = WhisperModel(
    MODEL_ID,
    device=os.getenv("BREEZE_DEVICE", "auto"),
    compute_type=os.getenv("BREEZE_COMPUTE_TYPE", "default"),
)
app = FastAPI(title="Zen Bridge Breeze ASR sidecar")


@app.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form("zh"),
    task: str = Form("transcribe"),
    initial_prompt: str = Form(""),
    hotwords: str = Form(""),
):
    content = await audio.read(5_000_001)
    if len(content) > 5_000_000:
        raise HTTPException(413, "audio too large")
    try:
        with wave.open(io.BytesIO(content)) as wav:
            if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                raise ValueError
    except (wave.Error, ValueError):
        raise HTTPException(400, "16-bit mono PCM WAV required")
    segments, _ = model.transcribe(
        io.BytesIO(content),
        language=language,
        task=task,
        initial_prompt=initial_prompt or None,
        hotwords=hotwords or None,
        vad_filter=True,
        beam_size=5,
    )
    return {"source": "".join(segment.text for segment in segments).strip()}
