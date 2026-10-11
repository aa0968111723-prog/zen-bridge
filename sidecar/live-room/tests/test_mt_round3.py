"""round3 C5 (ja gate rejects copied Chinese / long kana-less text) and C6 (Hy-MT2 model-card
templates verbatim, context wired)."""
import json

import pytest

from app.mt_backend import HyMtLlamaServer, validate_caption

CARD_DEFAULT = "将以下文本翻译为日语，注意只需要输出翻译后的结果，不要额外解释：\n\n今天講空性"
CARD_TERMS = ("参考下面的翻译：\n空性 翻译成 空性（くうしょう）\n公案 翻译成 公案（こうあん）\n"
              "将以下文本翻译为日语，注意只需要输出翻译后的结果，不要额外解释：\n\n今天講空性")
CARD_CONTEXT = ("【背景信息】\n上一句。\n再上一句。\n\n请结合背景信息将以下文本翻译为日语。\n\n【待翻译文本】\n今天講空性")


@pytest.mark.parametrize("text,zh,ok", [
    ("今日は空性について話します。", "今天講空性", True),
    ("今天講空性", "今天講空性", False),                          # verbatim copy
    ("今天講空性。", "今天講空性", False),                        # copy + punctuation
    ("今天我們講空性的道理", "今天我們講空性的道理啦", False),       # near copy, no kana
    ("仏教哲学概論第一講義録", "佛教哲學", False),                  # >8 chars without kana
    ("仏教哲学概論", "佛教哲學概論", True),                        # short kanji-only title is allowed
    ("空性", "空性", False),                                      # even a 2-char copy is a copy
])
def test_ja_gate(text, zh, ok):
    assert (validate_caption(text, "ja", zh=zh) is not None) is ok


def test_templates_match_model_card_verbatim():
    mt = HyMtLlamaServer()
    assert mt.build_messages("今天講空性", "ja") == [{"role": "user", "content": CARD_DEFAULT}]
    gl = [{"zh": "空性", "ja": "空性（くうしょう）"}, {"zh": "公案", "ja": "公案（こうあん）"}]
    assert mt.build_messages("今天講空性", "ja", gl)[0]["content"] == CARD_TERMS
    ctx = ["很久以前。", "上一句。", "再上一句。"]                    # only the last 2 are used
    assert mt.build_messages("今天講空性", "ja", None, ctx)[0]["content"] == CARD_CONTEXT
    assert "英语" in mt.build_messages("今天講空性", "en")[0]["content"]


class Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


def test_translate_sends_context():
    seen = {}

    def opener(req, timeout=0):
        seen["body"] = json.loads(req.data)
        return Resp(json.dumps({"choices": [{"message": {"content": "今日は空性について話します。"}}]}).encode())
    mt = HyMtLlamaServer(opener=opener)
    out = mt.translate("今天講空性", tgt_lang="ja", context=["上一句。"])
    assert out.status == "ok"
    assert seen["body"]["messages"][0]["content"].startswith("【背景信息】\n上一句。")


def test_copied_reply_is_bad_response():
    mt = HyMtLlamaServer(opener=lambda req, timeout=0: Resp(json.dumps(
        {"choices": [{"message": {"content": "今天講空性"}}]}).encode()))
    assert mt.translate("今天講空性", tgt_lang="ja").status == "bad_response"
