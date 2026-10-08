"""Real local inference/throughput check without transcript or paid API calls."""
from __future__ import annotations

import argparse
import json
import platform
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.asr import CliAsr, ResidentAsr
from app.native_asr import NativeResidentAsr
from app.audio import ffmpeg_bin
from app.settings import Settings, fill_process_environ
from scripts.install_runtime import download_verified, sha256

FIXTURE = {"url": "https://raw.githubusercontent.com/ggml-org/whisper.cpp/v1.9.2/samples/jfk.wav", "size": 352078, "sha256": "59dfb9a4acb36fe2a2affc14bacbee2920ff435cb13cc314a08c13f66ba7860e"}


def main() -> int:
    parser = argparse.ArgumentParser(description="本機辨識自檢，請先關閉字幕服務")
    parser.add_argument("--audio", type=Path, help="可使用自己的中文音訊，不上傳雲端")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "runtime-check.json")
    parser.add_argument("--seconds", type=float, default=6)
    args = parser.parse_args()
    if not 0.5 <= args.seconds <= 30:
        parser.error("音訊長度須在 0.5 到 30 秒")
    fill_process_environ(ROOT / ".env")
    settings = Settings.from_env()
    model = Path(settings.model_path) if settings.model_path else ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
    whisper = Path(settings.whisper_path) if settings.whisper_path else ROOT / "tools" / "whisper-cli.exe"
    server = Path(settings.server_path) if settings.server_path else ROOT / "tools" / "whisper-server.exe"
    ffmpeg = ffmpeg_bin(ROOT)
    if not model.is_file() or not ffmpeg:
        print("缺少模型或 ffmpeg，請先執行 install.bat。")
        return 1
    report = {"service": "breeze-live-room", "platform": platform.system(), "python": platform.python_version(), "engine": settings.asr_mode, "audio_context": settings.asr_audio_context, "beam_size": settings.asr_beam_size, "best_of": settings.asr_best_of, "fixture": "provided audio" if args.audio else "public-domain English JFK sample; not Chinese microphone validation", "microphone_tested": False, "translation_tested": False, "calls": [], "inference_ok": False}
    asr = ResidentAsr(settings.resident_url, server_bin=server, model=model, threads=settings.asr_threads, startup_timeout_s=settings.resident_startup_s, inference_timeout_s=settings.asr_timeout_s, audio_context=settings.asr_audio_context, beam_size=settings.asr_beam_size, best_of=settings.asr_best_of) if settings.asr_mode == "resident" else CliAsr(whisper, model, threads=settings.asr_threads, timeout_s=settings.asr_timeout_s, audio_context=settings.asr_audio_context, beam_size=settings.asr_beam_size, best_of=settings.asr_best_of)
    if settings.asr_mode == 'native':
        asr = NativeResidentAsr(model, threads=settings.asr_threads, startup_timeout_s=settings.resident_startup_s,
            inference_timeout_s=settings.asr_timeout_s, audio_context=settings.asr_audio_context,
            beam_size=settings.asr_beam_size, best_of=settings.asr_best_of)
    try:
        report["model_sha256"] = sha256(model)
        source = args.audio or download_verified(FIXTURE, ROOT / ".downloads" / "jfk.wav")
        with tempfile.TemporaryDirectory(prefix="breeze-check-") as temporary:
            sample = Path(temporary) / "sample.wav"
            proc = subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(source), "-t", str(args.seconds), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(sample)], capture_output=True, timeout=60)
            if proc.returncode:
                raise RuntimeError("音訊無法轉換，請使用有效的語音檔案")
            with wave.open(str(sample), "rb") as audio:
                seconds = audio.getnframes() / audio.getframerate()
            if seconds < 0.5:
                raise ValueError("語音太短")
            if isinstance(asr, ResidentAsr):
                began = time.monotonic()
                started = asr.start()
                report["startup_s"] = round(time.monotonic() - began, 3)
                if not started.ok:
                    raise RuntimeError(started.error)
            for number in range(2):
                began = time.monotonic()
                result = asr.transcribe(sample, "請辨識繁體中文，英文原句可保留英文。")
                elapsed = time.monotonic() - began
                report["calls"].append({"iteration": number + 1, "ok": result.ok, "characters": len(result.text), "seconds": round(elapsed, 3), "rtf": round(elapsed / seconds, 3)})
                if not result.ok or not result.text:
                    raise RuntimeError(result.error or "沒有文字，請提供清楚語音")
            report["inference_ok"] = True
            report["ready_loads"] = asr.loads if isinstance(asr, ResidentAsr) else None
            report["live_capacity_in_this_test"] = all(row["rtf"] < 1 for row in report["calls"])
            print("真實模型已完成兩次辨識。", flush=True)
            for row in report["calls"]:
                print(f"第 {row['iteration']} 次：{row['seconds']} 秒，RTF {row['rtf']}", flush=True)
            if not report["live_capacity_in_this_test"]:
                print("辨識速度跟不上這段音訊，先不要當作連續即時字幕正式使用。")
            print("英文樣本不能代替中文準確度、真實麥克風與長時間測試。")
            return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report["error"] = str(exc)[:300]
        print("辨識自檢未完成：" + report["error"], file=sys.stderr)
        return 1
    finally:
        if hasattr(asr, "close"):
            asr.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print("結果存到 " + str(args.output) + "，未保存逐字稿或金鑰。")


if __name__ == "__main__":
    raise SystemExit(main())
