"""Pick the translation engine from the environment.

Engines
- ``local`` (default): any OpenAI-compatible server on loopback, normally Ollama at
  ``http://127.0.0.1:11434/v1`` with ``qwen3:4b``. No API key is needed or sent.
- ``openai``: the legacy cloud path (``api.openai.com``, ``OPENAI_API_KEY``). Choosing it
  is the explicit opt-in to a cloud call; behaviour is identical to main.

A non-loopback ``BREEZE_TRANSLATE_BASE_URL`` is refused unless
``BREEZE_TRANSLATE_ALLOW_REMOTE=1`` is also set. Nothing here touches the network.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
from urllib.parse import urlsplit

from app.translate import Translator

DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:11434/v1"
DEFAULT_LOCAL_MODEL = "qwen3:4b"
ENGINES = ("local", "openai")
_LOOPBACK_NAMES = {"localhost"}

log = logging.getLogger("breeze.translate")


class TranslateConfigError(ValueError):
    """Bad engine settings. create_app lets it raise so the mistake is visible at start."""


def is_loopback_host(host: str) -> bool:
    host = (host or "").strip().lower().strip("[]")
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_base_url(url: str, *, allow_remote: bool = False) -> str:
    """Return a cleaned base URL or raise. Parsed, not prefix-matched."""
    raw = (url or "").strip()
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError as exc:
        raise TranslateConfigError("翻譯網址無法解析") from exc
    if parts.scheme not in {"http", "https"}:
        raise TranslateConfigError("翻譯網址必須是 http 或 https")
    if not host:
        raise TranslateConfigError("翻譯網址缺少主機")
    if parts.username is not None or parts.password is not None:
        raise TranslateConfigError("翻譯網址不能帶帳號密碼")
    if parts.query or parts.fragment:
        raise TranslateConfigError("翻譯網址不能帶查詢字串或片段")
    if not is_loopback_host(host) and not allow_remote:
        raise TranslateConfigError(
            "本機翻譯只允許 127.0.0.1 / localhost / ::1，拒絕 " + host
            + "（若確定要連外部伺服器，另設 BREEZE_TRANSLATE_ALLOW_REMOTE=1）"
        )
    return raw.rstrip("/")


def _extra_body(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TranslateConfigError("BREEZE_TRANSLATE_EXTRA_BODY 必須是 JSON 物件") from exc
    if not isinstance(value, dict):
        raise TranslateConfigError("BREEZE_TRANSLATE_EXTRA_BODY 必須是 JSON 物件")
    return value


def engine_from_env(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    # Opt-in: without BREEZE_TRANSLATE_ENGINE=local the legacy OpenAI path is unchanged, so
    # applying the patch alone changes nothing until the benchmark has been run.
    engine = (env.get("BREEZE_TRANSLATE_ENGINE") or "openai").strip().lower()
    if engine not in ENGINES:
        raise TranslateConfigError(f"BREEZE_TRANSLATE_ENGINE 只能是 {' / '.join(ENGINES)}")
    return engine


def build_translator(settings, env: dict | None = None, opener=None) -> Translator:
    """Translator for create_app. ``settings`` supplies translate on/off and token_budget."""
    env = os.environ if env is None else env
    engine = engine_from_env(env)
    enabled = bool(getattr(settings, "translate", True))
    if engine == "openai":
        # Byte-for-byte the legacy construction in server.create_app.
        return Translator(
            enabled=enabled,
            key=env.get("OPENAI_API_KEY", ""),
            token_budget=getattr(settings, "token_budget", 0),
            opener=opener,
        )
    allow_remote = (env.get("BREEZE_TRANSLATE_ALLOW_REMOTE") or "").strip() == "1"
    base_url = validate_base_url(env.get("BREEZE_TRANSLATE_BASE_URL") or DEFAULT_LOCAL_BASE_URL, allow_remote=allow_remote)
    model = (env.get("BREEZE_TRANSLATE_MODEL") or DEFAULT_LOCAL_MODEL).strip()
    no_think = (env.get("BREEZE_TRANSLATE_NO_THINK") or "1").strip() != "0"
    suffix = " /no_think" if no_think and model.lower().startswith("qwen3") else ""
    extra = _extra_body(env.get("BREEZE_TRANSLATE_EXTRA_BODY", ""))
    if suffix and protocol_from_env(base_url, env) == "ollama":
        extra.setdefault("think", False)     # native API: disable Qwen3 thinking outright
    return Translator(
        enabled=enabled,
        # A local server needs no key. BREEZE_TRANSLATE_API_KEY exists for a self-hosted
        # server that wants one; OPENAI_API_KEY is never forwarded to a local endpoint.
        key=(env.get("BREEZE_TRANSLATE_API_KEY") or "").strip(),
        model=model,
        base_url=base_url,
        require_key=False,
        extra_body=extra,
        strip_think=True,
        engine="local",
        system_suffix=suffix,
        token_budget=0,
        opener=opener,
        protocol=protocol_from_env(base_url, env),
        ollama_options=ollama_options_from_env(env),
        keep_alive=keep_alive_from_env(env),
    )


def protocol_from_env(base_url: str, env) -> str:
    """auto: Ollama's port (11434) uses the native API so num_thread/num_ctx apply.

    BREEZE_TRANSLATE_PROTOCOL=openai forces /v1/chat/completions (llama-server, LM Studio,
    or an Ollama on another port); =ollama forces /api/chat.
    """
    raw = (env.get("BREEZE_TRANSLATE_PROTOCOL") or "auto").strip().lower()
    if raw in ("openai", "ollama"):
        return raw
    if raw != "auto":
        raise TranslateConfigError(f"BREEZE_TRANSLATE_PROTOCOL must be auto, openai or ollama, not {raw!r}")
    return "ollama" if urlsplit(base_url).port == 11434 else "openai"


def ollama_options_from_env(env) -> dict:
    """hardware.md §7: 4 generation threads (leave cores for ASR), ctx 2048."""
    from app.runtime_tuning import env_int
    options = {"num_ctx": env_int("BREEZE_TRANSLATE_NUM_CTX", 2048, env, minimum=256)}
    threads = env_int("BREEZE_TRANSLATE_NUM_THREAD", 4, env, minimum=0)
    if threads > 0:          # 0 = let Ollama decide
        options["num_thread"] = threads
    return options


def keep_alive_from_env(env):
    raw = (env.get("BREEZE_TRANSLATE_KEEP_ALIVE") or "-1").strip()
    try:
        return int(raw)
    except ValueError:
        return raw           # e.g. "30m"
