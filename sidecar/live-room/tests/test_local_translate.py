"""Local (Ollama) translation config + native /api/chat protocol. Fakes only, no network."""
from types import SimpleNamespace

import pytest

from app.translate import Translator
from app.translate_config import TranslateConfigError, build_translator, validate_base_url
from tests.local_fakes import FakeOpener, ollama_chat, openai_chat

SETTINGS = SimpleNamespace(translate=True, token_budget=0)


def local_env(**extra):
    env = {"BREEZE_TRANSLATE_ENGINE": "local"}
    env.update(extra)
    return env


def test_default_engine_is_legacy_openai_until_opted_in():
    assert build_translator(SETTINGS, env={}).engine == "openai"
    assert build_translator(SETTINGS, env={"OPENAI_API_KEY": "k"}).key == "k"


def test_local_defaults_qwen3_on_loopback_without_key():
    tr = build_translator(SETTINGS, env=local_env())
    assert tr.engine == "local"
    assert tr.model == "qwen3:4b"
    assert tr.require_key is False and tr.key == ""
    assert tr.base_url == "http://127.0.0.1:11434/v1"


@pytest.mark.parametrize("url", ["http://localhost:11434/v1", "http://127.0.0.1:8080/v1", "http://[::1]:11434"])
def test_loopback_urls_accepted(url):
    assert validate_base_url(url)


@pytest.mark.parametrize("url", ["http://192.168.1.5:11434/v1", "https://api.example.com/v1", "http://10.0.0.1"])
def test_remote_urls_rejected_without_opt_in(url):
    with pytest.raises(TranslateConfigError):
        build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_BASE_URL=url))


def test_remote_url_allowed_only_with_explicit_opt_in():
    tr = build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_BASE_URL="http://192.168.1.5:11434/v1",
                                                  BREEZE_TRANSLATE_ALLOW_REMOTE="1"))
    assert tr.base_url.startswith("http://192.168.1.5")


def test_openai_key_is_never_forwarded_to_local_endpoint():
    opener = FakeOpener(ollama_chat("Hello."))
    tr = build_translator(SETTINGS, env=local_env(OPENAI_API_KEY="sk-secret-should-not-leak"), opener=opener)
    assert tr.translate("你好").status == "ok"
    assert "Authorization" not in opener.requests[0]["headers"]


def test_openai_engine_keeps_legacy_construction():
    tr = build_translator(SETTINGS, env={"BREEZE_TRANSLATE_ENGINE": "openai", "OPENAI_API_KEY": "k"})
    assert tr.engine == "openai" and tr.require_key is True and tr.key == "k"
    assert tr.endpoint() == "https://api.openai.com/v1/chat/completions"


def test_ollama_port_uses_native_api_with_thread_and_ctx_defaults():
    opener = FakeOpener(ollama_chat("Causes and conditions are complete."))
    tr = build_translator(SETTINGS, env=local_env(), opener=opener)
    assert tr.protocol == "ollama"
    res = tr.translate("因緣具足")
    assert res.status == "ok" and res.text == "Causes and conditions are complete."
    req = opener.requests[0]
    assert req["url"] == "http://127.0.0.1:11434/api/chat"
    body = req["body"]
    assert body["stream"] is False
    assert body["options"]["num_thread"] == 4
    assert body["options"]["num_ctx"] == 2048
    assert body["options"]["num_predict"] > 0
    assert body["keep_alive"] == -1
    assert body["think"] is False
    assert res.prompt_tokens == 42 and res.completion_tokens == 7


def test_thread_ctx_keepalive_are_env_configurable():
    opener = FakeOpener(ollama_chat("Hi."))
    tr = build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_NUM_THREAD="2", BREEZE_TRANSLATE_NUM_CTX="4096",
                                                  BREEZE_TRANSLATE_KEEP_ALIVE="30m",
                                                  BREEZE_TRANSLATE_MODEL="qwen3:4b-q8_0"), opener=opener)
    tr.translate("嗨")
    body = opener.requests[0]["body"]
    assert body["model"] == "qwen3:4b-q8_0"
    assert body["options"]["num_thread"] == 2 and body["options"]["num_ctx"] == 4096
    assert body["keep_alive"] == "30m"


def test_num_thread_zero_lets_ollama_decide():
    tr = build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_NUM_THREAD="0"))
    assert "num_thread" not in tr.ollama_options


def test_openai_protocol_forced_for_other_servers():
    opener = FakeOpener(openai_chat("Hello."))
    tr = build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_BASE_URL="http://127.0.0.1:8080/v1"), opener=opener)
    assert tr.protocol == "openai"
    tr.translate("你好")
    assert opener.requests[0]["url"] == "http://127.0.0.1:8080/v1/chat/completions"
    tr2 = build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_PROTOCOL="openai"))
    assert tr2.endpoint() == "http://127.0.0.1:11434/v1/chat/completions"


def test_bad_protocol_rejected():
    with pytest.raises(TranslateConfigError):
        build_translator(SETTINGS, env=local_env(BREEZE_TRANSLATE_PROTOCOL="grpc"))


def test_native_length_cutoff_is_not_published():
    tr = build_translator(SETTINGS, env=local_env(), opener=FakeOpener(ollama_chat("Half a sen", "length")))
    res = tr.translate("這是一句很長的話")
    assert res.status == "bad_response" and res.text == ""


def test_think_block_is_stripped():
    tr = build_translator(SETTINGS, env=local_env(), opener=FakeOpener(ollama_chat("<think>hmm</think>\nHello.")))
    assert tr.translate("你好").text == "Hello."


def test_native_malformed_reply_is_bad_response():
    tr = build_translator(SETTINGS, env=local_env(), opener=FakeOpener({"error": "model not found"}))
    assert tr.translate("你好").status == "bad_response"


def test_local_translator_retries_network_errors_without_key():
    import urllib.error
    tr = build_translator(SETTINGS, env=local_env(), opener=FakeOpener(urllib.error.URLError("refused"),
                                                                        ollama_chat("Hi.")))
    tr.sleeper = lambda s: None
    assert tr.translate("嗨").status in ("ok", "network")


def test_direct_translator_default_unchanged():
    tr = Translator(enabled=True, key="")
    assert tr.protocol == "openai" and tr.require_key is True
    assert tr.translate("你好").status == "no_key"
