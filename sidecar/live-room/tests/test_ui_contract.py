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
    assert 'lang="en"' in text
    assert "min-height: 42vh" in text
    assert 'id="retry"' in text and 'id="live"' in text
    assert 'id="state"' in text and 'aria-live="polite"' in text
    view = (ROOT / "app/static/room_view.js").read_text(encoding="utf-8")
    assert "stageNote" in text and "gapCopy" in text
    assert "目前沒有新字幕" in view
    assert "等待主持人開始說話" in view
    assert "漏了一小段，已接回最新內容" in view
    header = text.split("header {", 1)[1].split("}", 1)[0]
    assert "safe-area-inset-top" in header
    assert "safe-area-inset-left" in header
    assert "calc(10px + env(safe-area-inset-top))" not in text
    assert "body.zh-only .zh" in text
    assert 'value="project"' in text and "投影" in text
    assert "特大" in text and "只看譯文" in text and "保持螢幕亮著" in text
    assert "clamp(" in text and "body.project" in text
    assert "leaveProjection" in text and "refreshWakeLock" in text
    assert "fullscreenAvailable" in text
    prefs = (ROOT / "app/static/room_prefs.js").read_text(encoding="utf-8")
    assert "breeze.audience.prefs" in prefs
    assert "自動鎖定改長一點" in text
