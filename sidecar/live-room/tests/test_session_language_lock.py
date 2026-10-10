import pytest

from app import mt_backend
from tests.local_fakes import FakeOpener, openai_chat


def _backend(*captions):
    return mt_backend.HyMtLlamaServer(opener=FakeOpener(*(openai_chat(text) for text in captions)))


@pytest.mark.parametrize(
    ("language", "caption"),
    [("en", "Let us begin."), ("ja", "始めましょう。")],
)
def test_session_captions_use_only_the_language_chosen_at_start(language, caption):
    targets = mt_backend.SessionTargets(default="en")
    backend = _backend(caption)
    assert targets.start("room", "session", language) == language

    result = backend.translate("我們開始", tgt_lang=targets.lang_for("room", "session", 1))

    assert result.status == "ok"
    assert result.text == caption
    assert mt_backend.LANG_NAMES[language] in backend.opener.requests[0]["body"]["messages"][0]["content"]


@pytest.mark.xfail(
    strict=True,
    reason="SessionTargets.switch currently changes a running session's target from the next segment.",
)
def test_changing_target_during_a_session_keeps_captions_in_the_original_language():
    targets = mt_backend.SessionTargets(default="en")
    backend = _backend("Let us begin.", "Let us continue.")
    targets.start("room", "session", "en")
    first = backend.translate("我們開始", tgt_lang=targets.lang_for("room", "session", 1))

    targets.switch("room", "session", "ja", last_seq=1)
    second = backend.translate("我們繼續", tgt_lang=targets.lang_for("room", "session", 2))

    assert first.text == "Let us begin."
    assert targets.lang_for("room", "session", 2) == "en"
    assert second.text == "Let us continue."


def test_a_new_session_can_choose_the_other_language():
    targets = mt_backend.SessionTargets(default="en")
    backend = _backend("Let us begin.", "始めましょう。")
    targets.start("room", "first", "en")
    targets.start("room", "second", "ja")

    first = backend.translate("我們開始", tgt_lang=targets.lang_for("room", "first", 1))
    second = backend.translate("我們開始", tgt_lang=targets.lang_for("room", "second", 1))

    assert first.text == "Let us begin."
    assert second.text == "始めましょう。"


def test_invalid_language_is_rejected_or_uses_the_documented_default():
    targets = mt_backend.SessionTargets(default="en")
    assert targets.start("room", "empty", "") == "en"
    assert targets.start("room", "missing", None) == "en"
    with pytest.raises(mt_backend.TargetLangError):
        targets.start("room", "unsupported", "fr")
