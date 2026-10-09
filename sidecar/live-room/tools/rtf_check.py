#!/usr/bin/env python3
"""Measure recognition real-time factor on this machine.

  python tools/rtf_check.py metrics --base http://127.0.0.1:8780
  python tools/rtf_check.py run --n 4 --audio sample.wav

PASS means this run's session RTF p95 is below 0.9. Device acceptance stays
尚未驗證 until someone runs this on the real host. The host token and the
transcript are never printed.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.asr import CliAsr, ResidentAsr  # noqa: E402
from app.audio import ffmpeg_bin, riff_duration_seconds  # noqa: E402
from app.rtf import RtfMeter  # noqa: E402
from app.settings import Settings, fill_process_environ  # noqa: E402

P95_LIMIT = 0.9
SEGMENT_S = 6.0
MATRIX_THREADS = (4, 6, 8)
MATRIX_WORKERS = (1, 2)
UNVERIFIED = "實機結果尚未驗證。請在真正的主持機上執行後，才把數字當成那一台的測量。"
PROMPT = "以下是台灣國語的句子，請用繁體中文輸出。常見專有名詞：般若、菩提心、空性、因緣。這是提示偏置，不保證鎖詞。"


def _open(url: str, token: str | None, timeout: float) -> tuple[int, bytes]:
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _redact(text: str, token: str | None) -> str:
    if not token or not text:
        return text
    return text.replace(token, "[redacted]")


def fetch_token(base: str) -> str:
    status, raw = _open(base + "/api/host-token", None, 5)
    if status == 403:
        raise SystemExit("拿不到主持權杖（403）。請在主持機本機對 127.0.0.1 執行，不要從別的裝置跑。")
    if status != 200:
        raise SystemExit(f"拿不到主持權杖（HTTP {status}）。請確認服務已在 {base} 啟動。")
    try:
        token = json.loads(raw.decode("utf-8")).get("token")
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        token = None
    if not isinstance(token, str) or not token:
        raise SystemExit("主持權杖回應無法解讀。沒有印出內容。")
    return token


def fetch_metrics(base: str, token: str) -> dict:
    status, raw = _open(base + "/api/metrics", token, 10)
    text = raw.decode("utf-8", errors="replace")
    if status != 200:
        raise SystemExit(_redact(f"GET /api/metrics 失敗（HTTP {status}）", token))
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(_redact(f"GET /api/metrics 不是 JSON：{exc}", token)) from exc
    if not isinstance(payload, dict):
        raise SystemExit("GET /api/metrics 不是物件。")
    return payload


def build_asr(threads: int | None = None):
    """The configured local recognizer. Missing pieces do not fall through to a cloud API.

    ``threads`` overrides only this process's recognizer. It does not change saved settings.
    """
    fill_process_environ(ROOT / ".env")
    settings = Settings.from_env()
    thread_count = settings.asr_threads if threads is None else int(threads)
    model = Path(settings.model_path) if settings.model_path else ROOT / "models" / "ggml-breeze-asr-25-q5_0.bin"
    whisper = Path(settings.whisper_path) if settings.whisper_path else ROOT / "tools" / "whisper-cli.exe"
    server = Path(settings.server_path) if settings.server_path else ROOT / "tools" / "whisper-server.exe"
    if not model.is_file():
        raise SystemExit("缺少 Breeze 模型，請先執行 install.bat。不會改走雲端辨識。")
    if settings.asr_mode == "resident":
        asr = ResidentAsr(
            settings.resident_url,
            server_bin=server,
            model=model,
            threads=thread_count,
            startup_timeout_s=settings.resident_startup_s,
            inference_timeout_s=settings.asr_timeout_s,
            audio_context=settings.asr_audio_context,
            beam_size=settings.asr_beam_size,
            best_of=settings.asr_best_of,
        )
        started = asr.start()
        if not started.ok:
            asr.close()
            raise SystemExit((started.error or "常駐辨識沒有就緒") + "。不會改走雲端辨識。")
        return asr
    if not whisper.is_file():
        raise SystemExit("缺少 whisper-cli。請先執行 install.bat。不會改走雲端辨識。")
    return CliAsr(
        whisper,
        model,
        threads=thread_count,
        timeout_s=settings.asr_timeout_s,
        audio_context=settings.asr_audio_context,
        beam_size=settings.asr_beam_size,
        best_of=settings.asr_best_of,
    )


def _decode_wav(path: Path) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    duration = riff_duration_seconds(path)
    if duration and duration > 0:
        return path, None
    ffmpeg = ffmpeg_bin(ROOT)
    if not ffmpeg:
        raise SystemExit("這不是 WAV，也找不到 ffmpeg，所以沒有可靠的音訊長度。不會用檔案大小去猜。")
    temporary = tempfile.TemporaryDirectory(prefix="breeze-rtf-")
    dest = Path(temporary.name) / "slice.wav"
    try:
        proc = subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(path), "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
            capture_output=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        temporary.cleanup()
        raise SystemExit("ffmpeg 轉檔逾時，沒有量到音訊長度。")
    if proc.returncode != 0 or not (riff_duration_seconds(dest) or 0) > 0:
        temporary.cleanup()
        raise SystemExit("無法解出這段音訊的長度。不會用檔案大小去猜。")
    return dest, temporary


def measure_slices(transcribe, wav: Path, n: int) -> list[tuple[float, float]]:
    """Time n recognition calls. Each row is (asr_seconds, audio_seconds) from the WAVE duration."""
    audio_s = riff_duration_seconds(wav)
    if not audio_s or audio_s <= 0:
        raise SystemExit("讀不到這段 WAV 的長度。")
    samples: list[tuple[float, float]] = []
    for index in range(n):
        started = time.monotonic()
        result = transcribe(wav, PROMPT)
        elapsed = time.monotonic() - started
        if not getattr(result, "ok", False):
            detail = getattr(result, "error", "") or "辨識失敗"
            raise SystemExit(f"第 {index + 1} 段辨識失敗：{str(detail)[:180]}")
        samples.append((elapsed, float(audio_s)))
    return samples


def snapshot_from_pairs(pairs: list[tuple[float, float]]) -> dict:
    meter = RtfMeter()
    for asr_s, audio_s in pairs:
        meter.record(asr_s, audio_s, session=("rtf-check", "run"))
    return meter.snapshot()


def _fmt_ms(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    if number == int(number):
        return str(int(number))
    return f"{number:.1f}"


def _fmt_rtf(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.3f}"


def _scope_lines(title: str, scope: dict) -> list[str]:
    count = int(scope.get("count") or 0)
    limit = int(scope.get("limit") or 0)
    lines = [f"{title} {count}／{limit} 段"]
    for label, key in (("辨識毫秒", "asr_ms"), ("音訊毫秒", "audio_ms")):
        block = scope.get(key) or {}
        lines.append(
            f"  {label} p50 {_fmt_ms(block.get('p50'))}  p95 {_fmt_ms(block.get('p95'))}  最大 {_fmt_ms(block.get('max'))}"
        )
    rtf = scope.get("rtf") or {}
    lines.append(
        f"  RTF p50 {_fmt_rtf(rtf.get('p50'))}  p95 {_fmt_rtf(rtf.get('p95'))}  最大 {_fmt_rtf(rtf.get('max'))}"
    )
    return lines


def _session_rows(snapshot: dict, room: str | None = None) -> list[dict]:
    rtf = snapshot.get("rtf") if isinstance(snapshot, dict) else None
    if not isinstance(rtf, dict):
        return []
    rows = [row for row in rtf.get("sessions") or [] if isinstance(row, dict)]
    if room:
        rows = [row for row in rows if str(row.get("room_id") or "") == room]
    return rows


def verdict_p95(snapshot: dict, room: str | None = None) -> float | None:
    """Slowest session p95. ``room`` keeps one room; otherwise every room counts.

    A quiet room updated last must not hide a room that is already behind.
    """
    rows = _session_rows(snapshot, room)
    values: list[float] = []
    for row in rows:
        block = row.get("rtf") if isinstance(row.get("rtf"), dict) else {}
        p95 = block.get("p95")
        if p95 is not None:
            values.append(float(p95))
    if values:
        return max(values)
    if room:
        return None
    rtf = snapshot.get("rtf") if isinstance(snapshot, dict) else None
    session = rtf.get("session") if isinstance(rtf, dict) else None
    if isinstance(session, dict):
        p95 = (session.get("rtf") or {}).get("p95") if isinstance(session.get("rtf"), dict) else None
        if p95 is not None:
            return float(p95)
    return None


def _sample_count(snapshot: dict, room: str | None = None) -> int:
    """Samples the verdict may use. A room that only failed recognition counts as zero."""
    rows = _session_rows(snapshot, room)
    if rows:
        return sum(int(row.get("count") or 0) for row in rows)
    if room:
        return 0
    rtf = snapshot.get("rtf") if isinstance(snapshot, dict) else None
    session = rtf.get("session") if isinstance(rtf, dict) else None
    if isinstance(session, dict):
        return int(session.get("count") or 0)
    return 0


def render(snapshot: dict, source: str, room: str | None = None) -> tuple[str, int]:
    """Text report and process exit code. Max session p95 < 0.9 passes; no sample fails."""
    rtf = snapshot.get("rtf") if isinstance(snapshot, dict) else None
    if not isinstance(rtf, dict) or not isinstance(rtf.get("session"), dict):
        text = "這份結果沒有 rtf。請更新到會回報辨識即時率的版本。\n" + UNVERIFIED
        return text, 1
    session = rtf["session"]
    window = rtf.get("window") if isinstance(rtf.get("window"), dict) else {"count": 0, "limit": 0}
    p95 = verdict_p95(snapshot, room)
    lines = [f"來源：{source}"]
    if room:
        lines.append(f"判定房間：{room}")
    else:
        lines.append("判定：所有房間本場 p95 的最大值")
    backlog = snapshot.get("backlog_audio_s")
    if backlog is not None:
        lines.append(f"等待辨識的音訊：{float(backlog):.3f} 秒")
    queued = snapshot.get("backlog_s")
    if queued is not None:
        lines.append(f"尚未開始辨識的音訊：{float(queued):.3f} 秒")
    active = snapshot.get("asr_active_s")
    if active is not None:
        lines.append(f"正在辨識：{float(active):.3f} 秒")
    lines.extend(_scope_lines("近期", window))
    # --room must not print another room's latest session next to this room's verdict.
    detail = session
    if room:
        matched = _session_rows(snapshot, room)
        detail = matched[0] if matched else {"count": 0, "limit": int(session.get("limit") or 0)}
    lines.extend(_scope_lines("本場以來", detail))
    timeouts = snapshot.get("asr_timeouts")
    if timeouts is not None:
        lines.append(f"辨識逾時（不計入 RTF）：{int(timeouts)}")
    errors = snapshot.get("asr_errors")
    if errors is not None:
        lines.append(f"辨識錯誤（不計入 RTF）：{int(errors)}")
    rows = rtf.get("sessions") if isinstance(rtf.get("sessions"), list) else []
    if len(rows) > 1:
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_p95 = (row.get("rtf") or {}).get("p95")
            lines.append(
                f"房間 {row.get('room_id')}/{row.get('session_id')}："
                f"{int(row.get('count') or 0)} 段，RTF p95 {_fmt_rtf(row_p95)}"
            )
    sample_count = _sample_count(snapshot, room)
    if room and not _session_rows(snapshot, room):
        lines.append(f"結果：FAIL（沒有房間 {room} 的 RTF 樣本，門檻 p95 < {P95_LIMIT}）")
        code = 1
    elif p95 is None or sample_count <= 0:
        lines.append(f"結果：FAIL（沒有 RTF 樣本，門檻 p95 < {P95_LIMIT}）")
        code = 1
    elif float(p95) < P95_LIMIT:
        lines.append(f"結果：PASS（RTF p95 最大值 {_fmt_rtf(p95)} < {P95_LIMIT}）")
        code = 0
    else:
        lines.append(f"結果：FAIL（RTF p95 最大值 {_fmt_rtf(p95)} >= {P95_LIMIT}）")
        code = 1
    lines.append(UNVERIFIED)
    return "\n".join(lines), code


_PCM_SUBTYPE = bytes.fromhex("0100000000001000800000aa00389b71")


def _is_pcm_wave(fmt: bytes) -> bool:
    """PCM, including WAVE_FORMAT_EXTENSIBLE whose subtype is PCM. Float and ADPCM are not."""
    if len(fmt) < 16:
        return False
    tag = int.from_bytes(fmt[0:2], "little")
    if tag == 1:
        return True
    if tag != 0xFFFE or len(fmt) < 40:
        return False
    return fmt[24:40] == _PCM_SUBTYPE


def _wave_spec(path: Path) -> tuple[int, int, int, int, int] | None:
    """PCM span as (offset, size, rate, channels, width). None when this is not PCM WAVE."""
    try:
        handle = path.open("rb")
    except OSError:
        return None
    with handle:
        header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            return None
        rate = channels = width = 0
        while True:
            chunk = handle.read(8)
            if len(chunk) < 8:
                return None
            chunk_id = chunk[:4]
            chunk_size = int.from_bytes(chunk[4:8], "little")
            pos = handle.tell()
            if chunk_id == b"fmt " and chunk_size >= 16:
                fmt = handle.read(min(chunk_size, 128))
                if not _is_pcm_wave(fmt):
                    return None
                channels = int.from_bytes(fmt[2:4], "little")
                rate = int.from_bytes(fmt[4:8], "little")
                width = int.from_bytes(fmt[14:16], "little") // 8
                if width not in (1, 2, 3, 4) or channels <= 0 or rate <= 0:
                    return None
            elif chunk_id == b"data" and rate > 0 and channels > 0 and width > 0:
                return pos, chunk_size, rate, channels, width
            step = chunk_size + (chunk_size & 1)
            nxt = pos + step
            if nxt <= pos:
                return None
            try:
                handle.seek(nxt)
            except OSError:
                return None


def _write_pcm_wav(path: Path, pcm: bytes, rate: int, channels: int, width: int) -> None:
    import wave

    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(pcm)


def slice_limit(segment_s: float, minutes: float | None) -> int | None:
    """How many slices ``minutes`` allows. None means the whole file."""
    if minutes is None or minutes <= 0 or segment_s <= 0:
        return None
    return max(1, int(float(minutes) * 60 / float(segment_s)))


def cut_wav_segments(path: Path, segment_s: float, dest: Path, max_slices: int | None = None) -> list[tuple[Path, float]]:
    """Split a PCM WAVE into ``segment_s`` pieces. Each row is (path, duration_seconds).

    ``max_slices`` stops reading and writing once that many pieces exist, so a
    multi-hour file with ``--minutes 10`` is not loaded or sliced in full.
    """
    if segment_s <= 0:
        raise SystemExit("段長必須大於 0。")
    spec = _wave_spec(path)
    if spec is None:
        raise SystemExit("無法把這段音訊切成固定長度。不是可讀的 PCM WAV。")
    offset, size, rate, channels, width = spec
    frame = channels * width
    frames_per = int(round(float(segment_s) * rate))
    if frames_per < 1 or frame < 1:
        raise SystemExit("段長太短，切不出樣本。")
    chunk_bytes = frames_per * frame
    cap = None if max_slices is None else max(0, int(max_slices))
    dest.mkdir(parents=True, exist_ok=True)
    slices: list[tuple[Path, float]] = []
    remaining = size - (size % frame)
    index = 0
    with path.open("rb") as handle:
        handle.seek(offset)
        while remaining >= frame:
            if cap is not None and index >= cap:
                break
            take = min(chunk_bytes, remaining)
            take -= take % frame
            piece = handle.read(take)
            if len(piece) < frame:
                break
            if len(piece) < take:
                piece = piece[: len(piece) - (len(piece) % frame)]
            if len(piece) < frame:
                break
            out = dest / f"slice-{index:04d}.wav"
            _write_pcm_wav(out, piece, rate, channels, width)
            duration = (len(piece) // frame) / float(rate)
            slices.append((out, duration))
            remaining -= len(piece)
            index += 1
    if not slices:
        raise SystemExit("音訊太短，沒有切出段落。")
    return slices


def limit_slices(slices: list[tuple[Path, float]], segment_s: float, minutes: float | None) -> list[tuple[Path, float]]:
    if minutes is None or minutes <= 0:
        return list(slices)
    cap = max(1, int(float(minutes) * 60 / float(segment_s)))
    return list(slices[:cap])


def _accept_result(result, index: int) -> str:
    if not getattr(result, "ok", False):
        detail = getattr(result, "error", "") or "辨識失敗"
        raise SystemExit(f"第 {index + 1} 段辨識失敗：{str(detail)[:180]}")
    return str(getattr(result, "text", "") or "")


def _pace_serial(transcribe, slices, pace_s: float, sleep, now, prompt: str) -> dict:
    """One worker. Releases a slice every pace_s even when recognition runs long.

    A slice that starts immediately is not backlog. Slices that come due while
    the worker is busy are backlog until recognition starts.
    """
    pending: list[tuple[Path, float, float]] = []
    next_index = 0
    t0 = now()
    backlog = 0.0
    max_backlog = 0.0
    pairs: list[tuple[float, float, float]] = []
    text_lens: list[int] = []
    last_done = None
    last_release = None

    def take_overdue() -> None:
        nonlocal next_index, backlog, max_backlog, last_release
        while next_index < len(slices) and now() + 1e-9 >= t0 + next_index * pace_s:
            path, duration = slices[next_index]
            due = t0 + next_index * pace_s
            next_index += 1
            backlog += float(duration)
            max_backlog = max(max_backlog, backlog)
            pending.append((path, float(duration), due))
            last_release = due

    def run_one(path: Path, duration: float, release_at: float, index: int) -> None:
        nonlocal last_done
        started = now()
        text = _accept_result(transcribe(path, prompt), index)
        elapsed = now() - started
        text_lens.append(len(text))
        pairs.append((max(0.0, elapsed), duration, max(0.0, started - release_at)))
        last_done = now()

    while next_index < len(slices) or pending:
        if not pending:
            due = t0 + next_index * pace_s
            delay = due - now()
            if delay > 0:
                sleep(delay)
            path, duration = slices[next_index]
            release_at = due
            next_index += 1
            last_release = release_at
            run_one(path, float(duration), release_at, len(pairs))
            take_overdue()
            continue
        path, duration, release_at = pending.pop(0)
        backlog = max(0.0, backlog - duration)
        run_one(path, duration, release_at, len(pairs))
        take_overdue()
    final_lag = 0.0 if last_done is None or last_release is None else last_done - last_release
    return {
        "pairs": pairs,
        "max_backlog_s": max_backlog,
        "final_lag_s": final_lag,
        "text_lens": text_lens,
    }


class _PaceBook:
    """Busy workers are counted when one of them takes a slice, not when it is queued.

    Decrementing an idle count at enqueue lets a different worker steal that
    slice. The reserved worker then stays asleep and uncounted, and the final
    ``idle < workers`` wait never ends.
    """

    def __init__(self, workers: int) -> None:
        self.workers = max(1, int(workers))
        self.busy = 0
        self.pending: list[tuple[Path, float, float, bool]] = []
        self.backlog = 0.0
        self.max_backlog = 0.0

    def enqueue(self, path: Path, duration: float, release_at: float) -> None:
        free = self.workers - self.busy - len(self.pending)
        charged = free <= 0
        if charged:
            self.backlog += float(duration)
            self.max_backlog = max(self.max_backlog, self.backlog)
        self.pending.append((path, float(duration), float(release_at), charged))

    def dequeue(self) -> tuple[Path, float, float] | None:
        if not self.pending:
            return None
        path, duration, release_at, charged = self.pending.pop(0)
        self.busy += 1
        if charged:
            self.backlog = max(0.0, self.backlog - float(duration))
        return path, float(duration), float(release_at)

    def finish(self) -> None:
        self.busy = max(0, self.busy - 1)

    def settled(self) -> bool:
        return not self.pending and self.busy == 0


def _pace_parallel(transcribe, slices, workers: int, pace_s: float, sleep, now, prompt: str) -> dict:
    """Several workers. The producer releases on ``sleep``/``now``; workers run concurrently.

    A slice taken by a free worker is not backlog. A slice released while every
    worker is busy counts until recognition starts. Workers time themselves with
    the real clock, so a stand-in clock is only exact for one worker.
    """
    import threading

    book = _PaceBook(workers)
    stop = False
    pairs: list[tuple[float, float, float]] = []
    text_lens: list[int] = []
    errors: list[BaseException] = []
    last_done = None
    last_release = None
    wakeup = threading.Condition()

    def worker() -> None:
        nonlocal last_done, stop
        while True:
            with wakeup:
                while not book.pending and not stop:
                    wakeup.wait()
                if not book.pending:
                    return
                taken = book.dequeue()
            if taken is None:
                continue
            path, duration, release_at = taken
            started = time.monotonic()
            try:
                text = _accept_result(transcribe(path, prompt), len(pairs))
            except BaseException as exc:
                with wakeup:
                    errors.append(exc if isinstance(exc, SystemExit) else SystemExit(str(exc)[:180]))
                    stop = True
                    book.finish()
                    wakeup.notify_all()
                return
            finished = time.monotonic()
            with wakeup:
                pairs.append((max(0.0, finished - started), duration, max(0.0, started - release_at)))
                text_lens.append(len(text))
                last_done = finished if last_done is None else max(last_done, finished)
                book.finish()
                wakeup.notify_all()

    threads = [threading.Thread(target=worker, name=f"breeze-rtf-{index}", daemon=True) for index in range(workers)]
    for thread in threads:
        thread.start()
    t0 = now()
    for index, (path, duration) in enumerate(slices):
        due = t0 + index * pace_s
        delay = due - now()
        if delay > 0:
            sleep(delay)
        with wakeup:
            if errors:
                break
            last_release = due
            book.enqueue(path, float(duration), due)
            wakeup.notify()
    with wakeup:
        while not errors and not book.settled():
            wakeup.wait()
        stop = True
        wakeup.notify_all()
    for thread in threads:
        thread.join()
    if errors:
        raise errors[0]
    final_lag = 0.0 if last_done is None or last_release is None else float(last_done) - float(last_release)
    return {
        "pairs": pairs,
        "max_backlog_s": book.max_backlog,
        "final_lag_s": final_lag,
        "text_lens": text_lens,
    }


def _resource_note() -> dict:
    rss = None
    source = "UNKNOWN"
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss = int(getattr(usage, "ru_maxrss", 0) or 0)
        source = "resource"
    except (ImportError, OSError, AttributeError, ValueError):
        rss = None
        source = "UNKNOWN"
    return {"cpu_p95": None, "cpu_source": "UNKNOWN", "rss_peak": rss, "rss_source": source}


def pace_transcriptions(transcribe, slices, *, workers: int = 1, pace_s: float = SEGMENT_S, sleep=time.sleep, now=time.monotonic, prompt: str = PROMPT) -> dict:
    """Feed slices at real pace. ``workers`` 1 stays on one thread so a stand-in clock is exact."""
    if not slices:
        raise SystemExit("沒有可辨識的段落。")
    worker_count = max(1, int(workers))
    if worker_count == 1:
        report = _pace_serial(transcribe, slices, float(pace_s), sleep, now, prompt)
    else:
        report = _pace_parallel(transcribe, slices, worker_count, float(pace_s), sleep, now, prompt)
    report.update(_resource_note())
    report["workers"] = worker_count
    report["pace_s"] = float(pace_s)
    return report


def _pace_metrics(report: dict, threads: int, workers: int) -> dict:
    pairs = [(row[0], row[1]) for row in report["pairs"]]
    meter = RtfMeter()
    for asr_s, audio_s, wait_s in report["pairs"]:
        meter.record(asr_s, audio_s, session=("rtf-check", f"t{threads}w{workers}"), wait_s=wait_s)
    snap = meter.snapshot()
    session = snap["rtf"]["session"]
    total_asr = sum(row[0] for row in pairs)
    total_audio = sum(row[1] for row in pairs)
    throughput = None
    if total_audio > 0 and workers > 0:
        throughput = total_asr / (workers * total_audio)
    rtf = session.get("rtf") or {}
    return {
        "threads": int(threads),
        "workers": int(workers),
        "segments": len(pairs),
        "rtf_p50": rtf.get("p50"),
        "rtf_p95": rtf.get("p95"),
        "rtf_max": rtf.get("max"),
        "throughput_rtf": None if throughput is None else round(throughput, 6),
        "max_backlog_s": round(float(report["max_backlog_s"]), 6),
        "final_lag_s": round(float(report["final_lag_s"]), 6),
        "cpu_p95": report.get("cpu_p95"),
        "cpu_source": report.get("cpu_source") or "UNKNOWN",
        "rss_peak": report.get("rss_peak"),
        "rss_source": report.get("rss_source") or "UNKNOWN",
        "text_len_max": max(report.get("text_lens") or [0]),
        "keeps_pace": bool(rtf.get("p95") is not None and float(rtf["p95"]) < P95_LIMIT and float(report["max_backlog_s"]) < 12),
    }


def recommend_matrix(rows: list[dict]) -> dict:
    """Smallest threads, then workers, that stays under the RTF and backlog gates.

    CPU is UNKNOWN without psutil. An unknown CPU does not count as under 85%.
    """
    eligible = []
    for row in rows:
        p95 = row.get("rtf_p95")
        if p95 is None or float(p95) >= P95_LIMIT:
            continue
        if float(row.get("max_backlog_s") or 0) >= 12:
            continue
        cpu = row.get("cpu_p95")
        if cpu is not None and float(cpu) >= 85:
            continue
        eligible.append(row)
    if not eligible:
        return {
            "threads": None,
            "workers": None,
            "cpu_gate": "UNKNOWN",
            "reason": "沒有同時滿足 RTF p95 < 0.9 且積壓 < 12 秒的組合",
        }
    best = min(eligible, key=lambda row: (int(row["threads"]), int(row["workers"])))
    cpu_gate = "UNKNOWN" if best.get("cpu_p95") is None else "measured"
    reason = (
        f"建議 BREEZE_ASR_THREADS={best['threads']}、BREEZE_ASR_WORKERS={best['workers']}。"
        "CPU p95 未量到，85% 那一條仍是尚未驗證。"
        if cpu_gate == "UNKNOWN"
        else f"建議 BREEZE_ASR_THREADS={best['threads']}、BREEZE_ASR_WORKERS={best['workers']}。"
    )
    return {"threads": best["threads"], "workers": best["workers"], "cpu_gate": cpu_gate, "reason": reason}


def run_matrix(audio: Path, *, threads: list[int], workers: list[int], segment_s: float, minutes: float, build, sleep=time.sleep, now=time.monotonic) -> dict:
    """Cut ``segment_s`` slices and run each threads × workers pair. ``build`` must not call a cloud API."""
    wav, temporary = _decode_wav(audio)
    try:
        dest = Path(temporary.name) if temporary is not None else Path(tempfile.mkdtemp(prefix="breeze-rtf-matrix-"))
        owned = temporary is None
        try:
            slices = limit_slices(
                cut_wav_segments(wav, segment_s, dest / "slices", max_slices=slice_limit(segment_s, minutes)),
                segment_s,
                minutes,
            )
            rows = []
            for thread_count in threads:
                for worker_count in workers:
                    asr = build(int(thread_count), int(worker_count))
                    try:
                        paced = pace_transcriptions(
                            asr.transcribe,
                            slices,
                            workers=int(worker_count),
                            pace_s=float(segment_s),
                            sleep=sleep,
                            now=now,
                        )
                    finally:
                        close = getattr(asr, "close", None)
                        if close is not None:
                            close()
                    rows.append(_pace_metrics(paced, int(thread_count), int(worker_count)))
        finally:
            if owned:
                import shutil
                shutil.rmtree(dest, ignore_errors=True)
    finally:
        if temporary is not None:
            temporary.cleanup()
    return {
        "segment_s": float(segment_s),
        "minutes": float(minutes),
        "audio": audio.name,
        "rows": rows,
        "recommendation": recommend_matrix(rows),
        "unverified": UNVERIFIED,
    }


def _parse_ints(text: str, label: str) -> list[int]:
    values = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            number = int(part)
        except ValueError as exc:
            raise SystemExit(f"{label} 必須是逗號分隔的整數。") from exc
        if number < 1:
            raise SystemExit(f"{label} 必須大於 0。")
        values.append(number)
    if not values:
        raise SystemExit(f"{label} 至少要一個數字。")
    return values


def _print_pace(report: dict, source: str) -> tuple[str, int]:
    pairs = [(row[0], row[1]) for row in report["pairs"]]
    text, code = render(snapshot_from_pairs(pairs), source)
    extra = [
        f"真實節奏：每 {report.get('pace_s', SEGMENT_S):g} 秒放一段，workers {report.get('workers', 1)}",
        f"最長積壓：{float(report['max_backlog_s']):.3f} 秒",
        f"尾段落後：{float(report['final_lag_s']):.3f} 秒",
        f"CPU p95：{report.get('cpu_source') or 'UNKNOWN'}",
    ]
    lines = text.splitlines()
    if lines and lines[-1] == UNVERIFIED:
        lines = lines[:-1] + extra + [UNVERIFIED]
    else:
        lines.extend(extra)
        lines.append(UNVERIFIED)
    return "\n".join(lines), code


def _print_matrix(report: dict) -> str:
    lines = [
        f"矩陣：段長 {report['segment_s']:g} 秒，音訊 {report['audio']}",
        "threads  workers  段數  RTF p50  RTF p95  throughput  積壓最大  尾段",
    ]
    for row in report["rows"]:
        lines.append(
            f"{row['threads']:>7}  {row['workers']:>7}  {row['segments']:>4}  "
            f"{_fmt_rtf(row['rtf_p50']):>7}  {_fmt_rtf(row['rtf_p95']):>7}  "
            f"{_fmt_rtf(row['throughput_rtf']):>10}  {float(row['max_backlog_s']):.3f}  {float(row['final_lag_s']):.3f}"
        )
    advice = report.get("recommendation") or {}
    lines.append(str(advice.get("reason") or ""))
    lines.append(UNVERIFIED)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="量這台電腦的辨識即時率（RTF）。不會印出主持權杖或逐字稿。")
    sub = parser.add_subparsers(dest="cmd", required=True)
    metrics = sub.add_parser("metrics", help="讀正在跑的服務的 /api/metrics")
    metrics.add_argument("--base", default="http://127.0.0.1:8780")
    metrics.add_argument("--room", default="", help="只看這個房間的本場 p95；未指定時取所有房間的最大值")
    run = sub.add_parser("run", help="冒煙：整檔重複辨識 N 次，不切段、不按真實節奏")
    run.add_argument("--n", type=int, default=4)
    run.add_argument("--audio", type=Path, required=True)
    pace = sub.add_parser("pace", help="切成 6 秒段，依真實節奏送辨識")
    pace.add_argument("--audio", type=Path, required=True)
    pace.add_argument("--segment-s", type=float, default=SEGMENT_S)
    pace.add_argument("--minutes", type=float, default=10.0)
    pace.add_argument("--workers", type=int, default=0, help="0 表示沿用目前的 BREEZE_ASR_WORKERS")
    matrix = sub.add_parser("matrix", help="threads × workers。需要本機模型，不會改走雲端")
    matrix.add_argument("--audio", type=Path, required=True)
    matrix.add_argument("--threads", default=",".join(str(item) for item in MATRIX_THREADS))
    matrix.add_argument("--workers", default=",".join(str(item) for item in MATRIX_WORKERS))
    matrix.add_argument("--segment-s", type=float, default=SEGMENT_S)
    matrix.add_argument("--minutes", type=float, default=10.0)
    matrix.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "metrics":
        base = str(args.base).rstrip("/")
        token = fetch_token(base)
        payload = fetch_metrics(base, token)
        room = str(args.room).strip() or None
        text, code = render(payload, source=base + "/api/metrics", room=room)
        print(text)
        return code
    if args.cmd == "run":
        if not 1 <= args.n <= 30:
            parser.error("段數須在 1 到 30")
        audio = Path(args.audio)
        if not audio.is_file():
            raise SystemExit("找不到音訊檔。")
        wav, temporary = _decode_wav(audio)
        asr = None
        try:
            duration = riff_duration_seconds(wav) or 0
            if duration > 30:
                print("這次是整檔冒煙，不是 6 秒段的真實節奏。長檔請改用 pace 或 matrix。", file=sys.stderr)
            asr = build_asr()
            pairs = measure_slices(asr.transcribe, wav, args.n)
        finally:
            if temporary is not None:
                temporary.cleanup()
            close = getattr(asr, "close", None)
            if close is not None:
                close()
        text, code = render(snapshot_from_pairs(pairs), source=f"本機辨識 {args.n} 段 {audio.name}")
        print(text)
        return code
    if args.cmd == "pace":
        audio = Path(args.audio)
        if not audio.is_file():
            raise SystemExit("找不到音訊檔。")
        if args.segment_s <= 0:
            parser.error("段長必須大於 0")
        fill_process_environ(ROOT / ".env")
        settings = Settings.from_env()
        worker_count = int(args.workers) if int(args.workers) > 0 else max(1, int(settings.asr_workers))
        wav, temporary = _decode_wav(audio)
        asr = None
        try:
            dest_parent = Path(temporary.name) if temporary is not None else Path(tempfile.mkdtemp(prefix="breeze-rtf-pace-"))
            owned = temporary is None
            try:
                slices = limit_slices(
                    cut_wav_segments(
                        wav,
                        float(args.segment_s),
                        dest_parent / "slices",
                        max_slices=slice_limit(float(args.segment_s), float(args.minutes)),
                    ),
                    float(args.segment_s),
                    float(args.minutes),
                )
                asr = build_asr()
                report = pace_transcriptions(asr.transcribe, slices, workers=worker_count, pace_s=float(args.segment_s))
            finally:
                if owned:
                    import shutil
                    shutil.rmtree(dest_parent, ignore_errors=True)
        finally:
            if temporary is not None:
                temporary.cleanup()
            close = getattr(asr, "close", None)
            if close is not None:
                close()
        text, code = _print_pace(report, source=f"真實節奏 {audio.name}")
        print(text)
        return code
    audio = Path(args.audio)
    if not audio.is_file():
        raise SystemExit("找不到音訊檔。")
    if args.segment_s <= 0:
        parser.error("段長必須大於 0")
    thread_list = _parse_ints(args.threads, "threads")
    worker_list = _parse_ints(args.workers, "workers")

    def build(threads: int, workers: int):
        del workers  # The pool size is applied by pace_transcriptions, not by changing ASR defaults.
        return build_asr(threads)

    report = run_matrix(
        audio,
        threads=thread_list,
        workers=worker_list,
        segment_s=float(args.segment_s),
        minutes=float(args.minutes),
        build=build,
    )
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(_print_matrix(report))
    advice = report["recommendation"]
    return 0 if advice.get("threads") else 1


if __name__ == "__main__":
    sys.exit(main())
