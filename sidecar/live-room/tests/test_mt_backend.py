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


def test_session_switch_applies_from_next_segment_only():
    st = mb.SessionTargets("en", clock=lambda: 1.0)
    assert st.start("r", "s") == "en"
    assert st.switch("r", "s", "en", last_seq=3) is None
    ev = st.switch("r", "s", "ja", last_seq=5)
    assert ev == {"kind": "tgt_lang_changed", "room_id": "r", "session_id": "s", "tgt_lang": "ja", "from_seq": 6}
    assert [st.lang_for("r", "s", n) for n in (1, 5, 6, 9)] == ["en", "en", "ja", "ja"]
    st.switch("r", "s", "en", last_seq=9)
    assert st.lang_for("r", "s", 7) == "ja" and st.lang_for("r", "s", 10) == "en"
    assert st.lang_for("other", "s", 1) == "en"          # sessions are independent
    st.end("r", "s")
    assert st.lang_for("r", "s", 10) == "en"


@pytest.mark.parametrize("text,lang,ok", [
    ("Let us begin.", "en", True),
    ("始めましょう。", "ja", True),
    ("因縁が整う。", "ja", True),
    ("Let us begin.", "ja", False),
    ("<think>x</think>始めましょう。", "ja", False),
    ("始めましょう。\n説明：", "ja", False),
    ("始めましょう。", "en", False),
])
def test_caption_gate_per_language(text, lang, ok):
    assert (mb.validate_caption(text, lang, zh="我們開始") is not None) is ok


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
