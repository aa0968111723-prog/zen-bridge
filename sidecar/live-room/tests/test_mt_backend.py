"""optimization-round2 #11/#13: translation backend stub (Hy-MT2 via llama-server), en|ja per session."""
import json
import threading

import pytest

from app import mt_backend as mb
from app.translate_config import TranslateConfigError
from tests.local_fakes import FakeOpener, openai_chat


def test_target_langs_are_en_and_ja_only():
    assert mb.validate_tgt_lang("EN") == "en" and mb.validate_tgt_lang("ja") == "ja"
    for bad in ("", "zh", "fr", None):
        with pytest.raises(mb.TargetLangError):
            mb.validate_tgt_lang(bad)


def test_session_language_is_locked_once_started():
    """PR #29 product rule (replaces the old "switch applies from the next segment")."""
    st = mb.SessionTargets("en", clock=lambda: 1.0)
    assert st.start("r", "s") == "en"
    assert st.switch("r", "s", "en", last_seq=3) is None
    ev = st.switch("r", "s", "ja", last_seq=5)
    assert ev["kind"] == "tgt_lang_refused" and ev["tgt_lang"] == "en" and ev["requested"] == "ja"
    assert "新場次" in ev["message"]
    assert [st.lang_for("r", "s", n) for n in (1, 5, 6, 9)] == ["en"] * 4
    assert st.switch("new", "s", "ja", last_seq=0) is None       # before a session starts: just the choice
    assert st.lang_for("new", "s", 1) == "ja"
    assert st.lang_for("other", "s", 1) == "en"          # sessions are independent
    st.end("r", "s")
    assert st.lang_for("r", "s", 10) == "en"


def test_hymt_request_shape_and_switch_is_prompt_only():
    op = FakeOpener(openai_chat("Let us begin."), openai_chat("始めましょう。"))
    be = mb.HyMtLlamaServer(opener=op)
    assert be.translate("我們開始", tgt_lang="en").text == "Let us begin."
    assert be.translate("我們開始", tgt_lang="ja").text == "始めましょう。"
    a, b = op.requests
    assert a["url"] == "http://127.0.0.1:8081/v1/chat/completions"
    assert a["body"]["model"] == b["body"]["model"] == "hy-mt2-1.8b"      # same model for en and ja
    assert "英语" in a["body"]["messages"][0]["content"] and "日语" in b["body"]["messages"][0]["content"]
    assert "我們開始" in b["body"]["messages"][0]["content"]


def test_hymt_glossary_uses_target_language_column():
    be = mb.HyMtLlamaServer(opener=FakeOpener(openai_chat("x")))
    msg = be.build_messages("禪修", "ja", [{"zh": "禪修", "en": "Chan practice", "ja": "禅修行"}])[0]["content"]
    assert "禪修 翻译成 禅修行" in msg and "Chan practice" not in msg


@pytest.mark.parametrize("url", ["http://127.0.0.1:8645/v1", "http://192.168.1.2:8081/v1"])
def test_hymt_refuses_hermes_and_remote(url):
    with pytest.raises(TranslateConfigError):
        mb.HyMtLlamaServer(base_url=url)


def test_hymt_errors_never_publish():
    import urllib.error
    be = mb.HyMtLlamaServer(opener=FakeOpener(urllib.error.URLError("refused")))
    assert be.translate("我們開始", tgt_lang="en").status == "network"
    be = mb.HyMtLlamaServer(opener=FakeOpener(openai_chat("Let us", finish="length")))
    assert be.translate("我們開始", tgt_lang="en").status == "bad_response"
    be = mb.HyMtLlamaServer(opener=FakeOpener(openai_chat("Let us begin.")))
    assert be.translate("我們開始", tgt_lang="fr").status == "error"
    ev = threading.Event()
    ev.set()
    assert be.translate("我們開始", tgt_lang="en", cancel=ev).status == "timeout"


def test_backend_from_env():
    assert mb.backend_from_env({}) is None
    be = mb.backend_from_env({"BREEZE_MT_BACKEND": "hymt", "BREEZE_MT_BASE_URL": "http://127.0.0.1:9000/v1",
                              "BREEZE_MT_MAX_TOKENS": "x"})
    assert isinstance(be, mb.HyMtLlamaServer) and be.base_url.endswith(":9000/v1") and be.max_tokens == 256
    with pytest.raises(ValueError):
        mb.backend_from_env({"BREEZE_MT_BACKEND": "deepl"})
