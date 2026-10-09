from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def parse_env_file(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Blank lines and # comments are ignored."""
    if not path.exists() or not path.is_file():
        return {}
    found: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        found[key] = value
    return found


def fill_process_environ(env_file: Path | None = None) -> None:
    """Copy .env into the process only for keys that are not already set. Values are not printed."""
    path = Path(".env") if env_file is None else env_file
    for key, value in parse_env_file(path).items():
        os.environ.setdefault(key, value)


def merge_env(process: dict[str, str] | None = None, env_file: Path | None = None) -> dict[str, str]:
    """Process environment wins over the env file. File values never replace existing keys."""
    merged = dict(os.environ if process is None else process)
    file_path = env_file
    if file_path is None and process is None:
        file_path = Path(".env")
    if file_path is not None:
        for key, value in parse_env_file(file_path).items():
            merged.setdefault(key, value)
    return merged


def _raw_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def _raw_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw.strip())


def _csv(env: dict[str, str], name: str) -> tuple[str, ...]:
    raw = env.get(name, "")
    items = []
    for part in raw.split(","):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return tuple(items)


@dataclass(frozen=True)
class Settings:
    port: int = 8780
    max_audio_bytes: int = 8 * 1024 * 1024
    max_audio_seconds: float = 30.0
    max_rooms: int = 8
    max_listeners: int = 40
    max_queue: int = 8
    max_inflight_bytes: int = 32 * 1024 * 1024
    asr_workers: int = 1
    asr_threads: int = 6
    asr_audio_context: int = 0
    asr_beam_size: int = 0
    asr_best_of: int = 0
    history_limit: int = 200
    max_results: int = 500
    max_held: int = 32
    allow_testclient: bool = False
    share_host: str = ""
    share_scheme: str = "http"
    translate: bool = True
    data_path: str = ""
    model_path: str = ""
    whisper_path: str = ""
    server_path: str = ""
    resident_url: str = "http://127.0.0.1:8178"
    resident_startup_s: float = 180.0
    asr_mode: str = "cli"
    gap_wait_s: float = 3.0
    decode_timeout_s: float = 40.0
    asr_timeout_s: float = 120.0
    translate_timeout_s: float = 40.0
    translate_queue: int = 4
    translate_workers: int = 2
    heartbeat_s: float = 15.0
    idle_timeout_s: float = 45.0
    room_idle_s: float = 1800.0
    listener_queue: int = 32
    silence_rms: float = 0.0
    caption_ttl_s: float = 86400.0
    room_caption_cap: int = 5000
    # Full replay/backfill responses per client IP per minute. Live captions are not counted.
    replay_per_minute: int = 8
    stop_flush_s: float = 8.0
    shutdown_flush_s: float = 2.0
    token_budget: int = 0
    allowed_hosts: tuple[str, ...] = ()
    allowed_schemes: tuple[str, ...] = ("http",)
    segment_ms: int = 6000

    def __post_init__(self) -> None:
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("BREEZE_PORT 必須在 1 到 65535")
        if self.share_scheme not in {"http", "https"}:
            raise ValueError("BREEZE_SHARE_SCHEME 只接受 http 或 https")
        if self.share_scheme not in self.allowed_schemes:
            object.__setattr__(self, "allowed_schemes", self.allowed_schemes + (self.share_scheme,))
        if self.max_queue < 1:
            raise ValueError("BREEZE_MAX_QUEUE 至少為 1")
        if self.max_audio_bytes < 1:
            raise ValueError("BREEZE_MAX_AUDIO_BYTES 至少為 1")
        if self.max_audio_seconds <= 0:
            raise ValueError("BREEZE_MAX_AUDIO_SECONDS 必須大於 0")
        if self.max_rooms < 1 or self.max_listeners < 1:
            raise ValueError("房間數與聽眾數至少為 1")
        if self.asr_workers < 1:
            raise ValueError("BREEZE_ASR_WORKERS 至少為 1")
        if self.history_limit < 1 or self.translate_queue < 1 or self.listener_queue < 1:
            raise ValueError("歷史、翻譯佇列與聽眾佇列至少為 1")
        if self.room_caption_cap < 1:
            raise ValueError("BREEZE_ROOM_CAPTION_CAP 至少為 1")
        if self.replay_per_minute < 1:
            raise ValueError("BREEZE_REPLAY_PER_MINUTE 至少為 1")
        if self.translate_workers < 1:
            raise ValueError("BREEZE_TRANSLATE_WORKERS 至少為 1")
        if self.gap_wait_s < 0 or self.room_idle_s < 0 or self.stop_flush_s < 0 or self.shutdown_flush_s < 0:
            raise ValueError("等待時間不能是負數")
        if self.asr_mode not in {"cli", "resident", "native"}:
            raise ValueError("BREEZE_ASR 只接受 cli、resident 或 native")
        if self.resident_startup_s <= 0 or self.asr_timeout_s <= 0:
            raise ValueError("辨識啟動及推論逾時必須大於零")
        if self.asr_audio_context != 0 and not 128 <= self.asr_audio_context <= 1500:
            raise ValueError("BREEZE_ASR_AUDIO_CONTEXT 必須是 0 或 128 到 1500")
        if not 0 <= self.asr_beam_size <= 8 or not 0 <= self.asr_best_of <= 8:
            raise ValueError("BREEZE_ASR_BEAM_SIZE 與 BREEZE_ASR_BEST_OF 必須在 0 到 8")
        for scheme in self.allowed_schemes:
            if scheme not in {"http", "https"}:
                raise ValueError("允許的 scheme 只接受 http 或 https")

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None, env_file: str | Path | None = None) -> "Settings":
        file_path = Path(env_file) if env_file else None
        env = merge_env(environ, file_path)
        schemes = _csv(env, "BREEZE_ALLOWED_SCHEMES") or ("http",)
        return cls(
            port=_raw_int(env, "BREEZE_PORT", 8780),
            max_audio_bytes=_raw_int(env, "BREEZE_MAX_AUDIO_BYTES", 8 * 1024 * 1024),
            max_audio_seconds=_raw_float(env, "BREEZE_MAX_AUDIO_SECONDS", 30.0),
            max_rooms=_raw_int(env, "BREEZE_MAX_ROOMS", 8),
            max_listeners=_raw_int(env, "BREEZE_MAX_LISTENERS", 40),
            max_queue=_raw_int(env, "BREEZE_MAX_QUEUE", 8),
            max_inflight_bytes=_raw_int(env, "BREEZE_MAX_INFLIGHT_BYTES", 32 * 1024 * 1024),
            asr_workers=_raw_int(env, "BREEZE_ASR_WORKERS", 1),
            asr_threads=_raw_int(env, "BREEZE_ASR_THREADS", 6),
            asr_audio_context=_raw_int(env, "BREEZE_ASR_AUDIO_CONTEXT", 0),
            asr_beam_size=_raw_int(env, "BREEZE_ASR_BEAM_SIZE", 0),
            asr_best_of=_raw_int(env, "BREEZE_ASR_BEST_OF", 0),
            history_limit=_raw_int(env, "BREEZE_HISTORY_LIMIT", 200),
            max_results=_raw_int(env, "BREEZE_MAX_RESULTS", 500),
            max_held=_raw_int(env, "BREEZE_MAX_HELD", 32),
            allow_testclient=env.get("BREEZE_ALLOW_TESTCLIENT") == "1",
            share_host=env.get("BREEZE_SHARE_HOST", "").strip(),
            share_scheme=env.get("BREEZE_SHARE_SCHEME", "http").strip() or "http",
            translate=env.get("BREEZE_TRANSLATE", "1") != "0",
            data_path=env.get("BREEZE_DATA_PATH", "").strip(),
            model_path=env.get("BREEZE_MODEL", "").strip(),
            whisper_path=env.get("BREEZE_WHISPER", "").strip(),
            server_path=env.get("BREEZE_WHISPER_SERVER", "").strip(),
            resident_url=env.get("BREEZE_RESIDENT_URL", "http://127.0.0.1:8178").strip() or "http://127.0.0.1:8178",
            resident_startup_s=_raw_float(env, "BREEZE_RESIDENT_STARTUP_TIMEOUT", 180.0),
            asr_mode=env.get("BREEZE_ASR", "cli").strip() or "cli",
            gap_wait_s=_raw_float(env, "BREEZE_GAP_WAIT", 3.0),
            decode_timeout_s=_raw_float(env, "BREEZE_DECODE_TIMEOUT", 40.0),
            asr_timeout_s=_raw_float(env, "BREEZE_ASR_TIMEOUT", 120.0),
            translate_timeout_s=_raw_float(env, "BREEZE_TRANSLATE_TIMEOUT", 40.0),
            translate_queue=_raw_int(env, "BREEZE_TRANSLATE_QUEUE", 4),
            translate_workers=_raw_int(env, "BREEZE_TRANSLATE_WORKERS", 2),
            heartbeat_s=_raw_float(env, "BREEZE_HEARTBEAT", 15.0),
            idle_timeout_s=_raw_float(env, "BREEZE_IDLE_TIMEOUT", 45.0),
            room_idle_s=_raw_float(env, "BREEZE_ROOM_IDLE", 1800.0),
            listener_queue=_raw_int(env, "BREEZE_LISTENER_QUEUE", 32),
            silence_rms=_raw_float(env, "BREEZE_SILENCE_RMS", 0.0),
            caption_ttl_s=_raw_float(env, "BREEZE_CAPTION_TTL", 86400.0),
            room_caption_cap=_raw_int(env, "BREEZE_ROOM_CAPTION_CAP", 5000),
            replay_per_minute=_raw_int(env, "BREEZE_REPLAY_PER_MINUTE", 8),
            stop_flush_s=_raw_float(env, "BREEZE_STOP_FLUSH", 8.0),
            shutdown_flush_s=_raw_float(env, "BREEZE_SHUTDOWN_FLUSH", 2.0),
            token_budget=_raw_int(env, "BREEZE_TOKEN_BUDGET", 0),
            allowed_hosts=_csv(env, "BREEZE_ALLOWED_HOSTS"),
            allowed_schemes=schemes,
            segment_ms=_raw_int(env, "BREEZE_SEGMENT_MS", 6000),
        )
