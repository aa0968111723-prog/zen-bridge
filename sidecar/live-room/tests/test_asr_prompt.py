"""round3 C8: the Whisper initial prompt carries no instruction meant for humans."""
from app import server


def test_prompt_has_no_human_note():
    assert "提示偏置" not in server.PROMPT and "不保證" not in server.PROMPT
    assert server.PROMPT.startswith("以下是台灣國語的句子") and "菩提心" in server.PROMPT
