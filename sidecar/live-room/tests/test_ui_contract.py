from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_host_page_is_labeled_for_the_host():
    text = (ROOT / "app/static/host.html").read_text(encoding="utf-8")
    assert "<title>禪譯主持</title>" in text
    assert "<h1>禪譯主持</h1>" in text
    assert "聽眾掃這裡" in text
    assert 'id="go"' in text and "開始聽" in text
    for element_id in ("room", "mic", "stop", "try", "qr", "url", "glossary", "export", "log"):
        assert f'id="{element_id}"' in text


def test_room_page_keeps_the_caption_in_front():
    text = (ROOT / "app/static/room.html").read_text(encoding="utf-8")
    assert "<title>禪譯聽眾</title>" in text
    assert 'id="en"' in text and 'id="zh"' in text
    assert "min-height: 42vh" in text
    assert 'id="retry"' in text and 'id="live"' in text
