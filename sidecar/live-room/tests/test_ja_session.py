"""round4 #7 + testlead P0: a ja session's slices reach the MT backend with tgt_lang='ja'."""
import pytest
from httpx import ASGITransport, AsyncClient

from app.pipeline import Segment, ja_ruby, ja_segments
from app.translate import TranslateResult, Translator
from tests.test_round2 import Socket, app_for, auth, push, stop, token_of


class EnOnly(Translator):
    def __init__(self):
        super().__init__(enabled=True, key="k")
        self.seen = []

    def translate(self, zh, glossary=None, context=None):
        self.seen.append(zh)
        return TranslateResult("EN " + zh, "ok")


class JaBackend:
    def __init__(self):
        self.calls = []

    def translate(self, zh, *, tgt_lang, glossary=None, context=None, deadline=None, cancel=None):
        self.calls.append((zh, tgt_lang, glossary))
        return TranslateResult("今日は、般若の話です。", "ok")


def test_segments_rebuild_text_and_ruby_needs_reading():
    t = "今日は、般若の意味について話しましょう。それでは始めます"
    segs = ja_segments(t)
    assert "".join(segs) == t and len(segs) >= 3 and segs[0] == "今日は、"
    assert ja_ruby(t, [{"zh": "般若", "ja": "般若", "reading": "はんにゃ"}, {"zh": "x", "ja": "意味"}]) == [
        {"text": "般若", "reading": "はんにゃ"}]
    seg = Segment(room_id="r", session_id="s", seq=1, en="今日は。", tgt_lang="ja")
    pub = seg.public()
    assert pub["tgt_lang"] == "ja" and pub["segments"] == ["今日は。"]
    assert "tgt_lang" not in Segment(room_id="r", session_id="s", seq=1, en="Hi").public()


@pytest.mark.anyio
async def test_ja_room_goes_to_backend_with_tgt_lang_and_en_room_unchanged():
    en = EnOnly()
    app = app_for(translator=en)
    ja = JaBackend()
    app.state.pipeline._target_backend, app.state.pipeline._target_backend_loaded = ja, True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            r = await client.post("/api/rooms/open", json={"room_id": "jp", "tgt_lang": "ja"},
                                  headers={**auth(token), "content-type": "application/json"})
            assert r.status_code == 200 and r.json()["tgt_lang"] == "ja"
            assert (await client.get("/api/setup?room_id=jp")).json()["tgt_lang"] == "ja"
            bad = await client.post("/api/rooms/open", json={"room_id": "x", "tgt_lang": "fr"},
                                    headers={**auth(token), "content-type": "application/json"})
            assert bad.status_code == 400
            resp = await push(client, token, "jp", "s1", 1, "今天講般若".encode())
            body = resp.json()
            assert ja.calls and ja.calls[0][1] == "ja", "testlead P0: tgt_lang reaches the backend"
            assert en.seen == [], "the en prompt translator is never asked for Japanese"
            assert body["en"] == "今日は、般若の話です。" and body["tgt_lang"] == "ja"
            assert "".join(body["segments"]) == body["en"]
            # the session keeps ja even if the room is switched mid-session
            await client.post("/api/rooms/open", json={"room_id": "jp", "tgt_lang": "en"},
                              headers={**auth(token), "content-type": "application/json"})
            await push(client, token, "jp", "s1", 2, "第二段".encode())
            assert ja.calls[-1][0] == "第二段" and en.seen == []
            # an en room is untouched
            body = (await push(client, token, "class", "s2", 1, "英文房".encode())).json()
            assert body["en"] == "EN 英文房" and "tgt_lang" not in body
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_ja_without_backend_fails_soft_and_keeps_zh():
    app = app_for(translator=EnOnly())
    app.state.pipeline._target_backend, app.state.pipeline._target_backend_loaded = None, True
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            await client.post("/api/rooms/open", json={"room_id": "jp", "tgt_lang": "ja"},
                              headers={**auth(token), "content-type": "application/json"})
            body = (await push(client, token, "jp", "s1", 1, "中文保留".encode())).json()
            assert body["zh"] == "中文保留" and body["en"] == ""
            assert "BREEZE_MT_BACKEND" in (body.get("error") or "")
    finally:
        await stop(app)


def test_ledger_archives_ja_session_as_ja(tmp_path):
    import sqlite3
    from app.ledger import ledger_from_env
    path = tmp_path / "zen.sqlite3"
    led = ledger_from_env({"ZEN_LEDGER": "1", "ZEN_DB_PATH": str(path)})
    led.submit({"type": "caption", "room_id": "jp", "session_id": "s", "seq": 1, "id": "jp:s:1", "t0_ms": 0,
                "t1_ms": 1, "zh": "般若", "en": "般若です。", "tgt_lang": "ja", "status": "ready"})
    assert led.wait_idle(5)
    led.close()
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT tgt_lang, text FROM translations").fetchall() == [("ja", "般若です。")]
