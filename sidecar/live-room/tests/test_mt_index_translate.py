"""round4: Index-Translate-2B opt-in backend (model-card templates verbatim, thinking off) + #10 ja Q8_0."""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app import mt_backend as mb
from tests.local_fakes import FakeOpener, openai_chat

CARD_PLAIN_EN = "请将以下中文文本翻译为英语，直接输出翻译结果，不要进行任何解释。\n\n你好，世界！"


def test_plain_prompt_is_card_text_verbatim():
    be = mb.IndexTranslateLlamaServer(opener=FakeOpener(openai_chat("Hello, world!")))
    assert be.build_messages("你好，世界！", "en") == [{"role": "user", "content": CARD_PLAIN_EN}]
    be.source_name = ""                                  # card's --source auto
    assert be.build_messages("你好，世界！", "en")[0]["content"] == (
        "请将以下文本翻译为英语，直接输出翻译结果，不要进行任何解释。\n\n你好，世界！")   # card python example


def test_glossary_uses_card_insttrans_format():
    be = mb.IndexTranslateLlamaServer(source_name="", opener=FakeOpener(openai_chat("x")))
    msg = be.build_messages("王平仲采用了更加昂贵的碳纤维材料。", "en",
                            [{"zh": "碳纤维", "en": "carbon fiber"}, {"zh": "抗裂缝", "en": "crack resistance"}])
    assert msg[0]["content"] == (
        "请将以下文本翻译成英语，并且严格遵循所有约束要求。\n\n"
        "【源文】\n王平仲采用了更加昂贵的碳纤维材料。\n\n"
        "【约束要求】\n1. 【硬性要求】专名/术语对照: 碳纤维→carbon fiber、抗裂缝→crack resistance\n\n"
        "只输出译文，不要有任何额外说明。")


def test_ja_glossary_and_context_are_numbered_constraints():
    be = mb.IndexTranslateLlamaServer(opener=FakeOpener(openai_chat("x")))
    c = be.build_messages("般若很深", "ja", [{"zh": "般若", "en": "prajna", "ja": "般若"}], ["a", "b", "c"])[0]["content"]
    assert c.startswith("请将以下中文文本翻译成日语，")
    assert "1. 【硬性要求】专名/术语对照: 般若→般若" in c and "prajna" not in c
    assert "2. 【注意】上文" in c and "b / c" in c and "a /" not in c     # last 2 context lines


def test_request_has_thinking_off_and_greedy_and_strips_think():
    op = FakeOpener(openai_chat("<think>\n\n</think>\n\n始めましょう。"))
    be = mb.IndexTranslateLlamaServer(opener=op)
    r = be.translate("我們開始", tgt_lang="ja")
    assert (r.status, r.text) == ("ok", "始めましょう。")
    body = op.requests[0]["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["temperature"] == 0.0 and body["model"] == "index-translate-2b" and body["stream"] is False


def test_index_keeps_ja_copy_gate_and_loopback_guard():
    be = mb.IndexTranslateLlamaServer(opener=FakeOpener(openai_chat("我們開始講般若的意義")))
    assert be.translate("我們開始講般若的意義", tgt_lang="ja").status == "bad_response"
    with pytest.raises(Exception):
        mb.IndexTranslateLlamaServer(base_url="http://127.0.0.1:8645/v1")
    with pytest.raises(Exception):
        mb.IndexTranslateLlamaServer(base_url="http://10.0.0.2:8081/v1")


def test_profile_is_opt_in_never_default():
    assert mb.backend_from_env({}) is None
    assert isinstance(mb.backend_from_env({"BREEZE_MT_BACKEND": "hymt"}), mb.HyMtLlamaServer)
    assert not isinstance(mb.backend_from_env({"BREEZE_MT_BACKEND": "hymt"}), mb.IndexTranslateLlamaServer)
    be = mb.backend_from_env({"BREEZE_MT_BACKEND": "index"})
    assert isinstance(be, mb.IndexTranslateLlamaServer) and be.name == "index"
    with pytest.raises(ValueError):
        mb.backend_from_env({"BREEZE_MT_BACKEND": "index-9b"})


def test_against_fake_llama_server_over_http():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps(openai_chat("Let us begin.")).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        be = mb.IndexTranslateLlamaServer(base_url=f"http://127.0.0.1:{srv.server_port}/v1")
        assert be.translate("我們開始", tgt_lang="en").text == "Let us begin."
        assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False}
    finally:
        srv.shutdown()


def test_ja_default_quant_is_q8_and_en_stays_q4():
    assert mb.DEFAULT_QUANT == {"en": "Q4_K_M", "ja": "Q8_0"}
    assert mb.gguf_for("ja", env={}) == "Hy-MT2-1.8B-Q8_0.gguf"
    assert mb.gguf_for("en", env={}) == "Hy-MT2-1.8B-Q4_K_M.gguf"
    assert mb.gguf_for("ja", "index", env={}) == "Index-Translate-2B-Q8_0.gguf"
    assert mb.gguf_for("ja", env={"BREEZE_MT_GGUF_JA": r"D:\m\x.gguf"}) == r"D:\m\x.gguf"
    assert "Q8_0" in (mb.__doc__ or "") and "IFMTBench" in mb.__doc__
