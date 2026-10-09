"""Translation reply contract, and a room glossary that matches its database row."""

import asyncio
import json
import threading
import time
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from app.glossary import normalize, validate_terms
from app.pipeline import _READY_TRANSLATION
from app.settings import Settings
from app.store import CaptionStore
from app.translate import Translator
from tests.test_round2 import app_for, auth, stop, token_of


def _settings(path=None):
    extra = {"allow_testclient": True, "gap_wait_s": 30, "translate_timeout_s": 5}
    if path is not None:
        extra["data_path"] = str(path)
    return Settings(**extra)


def _term(zh, aliases=(), en="X", lock=True):
    return {"zh": zh, "aliases": list(aliases), "en": en, "lock": lock, "category": "", "note": ""}


async def _put(client, token, room, terms, if_version):
    return await client.put(
        f"/api/rooms/{room}/glossary",
        json={"terms": terms, "if_version": if_version},
        headers={**auth(token), "content-type": "application/json"},
    )


async def _get(client, token, room):
    return await client.get(f"/api/rooms/{room}/glossary", headers=auth(token))


@asynccontextmanager
async def _serving(app):
    async with app.router.lifespan_context(app):
        yield


class _Body:
    def __init__(self, raw: bytes):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _translate(content: str, glossary=None):
    seen = {}
    raw = json.dumps({
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }).encode()

    def opener(req, timeout=40):
        del timeout
        seen["request"] = json.loads(req.data.decode())
        return _Body(raw)

    translator = Translator(enabled=True, key="k", opener=opener)
    result = translator.translate("你好", glossary=glossary if glossary is not None else [], context=[])
    return result, seen


def test_fold_collision_rejects_the_same_alias():
    accepted, rejected = validate_terms([
        _term("學會", ["学社"], en="society"),
        _term("社團", ["學社"], en="club"),
    ])
    assert accepted == []
    assert rejected
    assert {item["line"] for item in rejected} >= {1, 2}
    kept, rejected = validate_terms([
        _term("學社", en="club"),
        _term("其他", ["学社"], en="other"),
    ])
    assert [item["zh"] for item in kept] == ["學社"]
    assert any("相同" in item["reason"] for item in rejected)


def test_fullwidth_and_casefold_aliases_match():
    accepted, rejected = validate_terms([
        _term("AI社", ["AI社團"], en="AI Club"),
        _term("禪學社", ["ZEN社"], en="Zen Club"),
    ])
    assert not rejected
    got = {text: normalize(text, accepted) for text in ("ＡＩ社團", "ai社團", "zen社", "ＺＥＮ社")}
    assert got == {
        "ＡＩ社團": "AI社",
        "ai社團": "AI社",
        "zen社": "禪學社",
        "ＺＥＮ社": "禪學社",
    }
    assert normalize(got["ＺＥＮ社"], accepted) == "禪學社"


def test_category_control_characters_are_rejected():
    accepted, rejected = validate_terms([
        {"zh": "禪", "en": "Chan", "aliases": [], "category": "a\nb\x00", "note": ""},
    ])
    assert accepted == []
    assert any("分類" in item["reason"] for item in rejected)
    accepted, rejected = validate_terms([
        {"zh": "禪", "en": "Chan", "aliases": [], "category": "社團", "note": ""},
    ])
    assert not rejected
    assert accepted[0]["category"] == "社團"


def test_empty_glossary_omits_the_glossary_field():
    translator = Translator()
    empty = json.loads(translator.build_messages("你好", [], [])[1]["content"])
    assert "glossary" not in empty
    assert empty["current"] == "你好"
    assert empty["previous"] == []
    missed = json.loads(translator.build_messages("你好", [{"zh": "般若", "en": "prajna"}], [])[1]["content"])
    assert missed["glossary"] == []
    assert missed["current"] == "你好"


def test_model_json_chinese_and_explanation_are_not_captions():
    assert "bad_response" not in _READY_TRANSLATION
    extracted, seen = _translate('{"current":"Hello"}')
    assert extracted.status == "ok"
    assert extracted.text == "Hello"
    system = seen["request"]["messages"][0]["content"]
    assert "Translate only the `current` field." in system
    assert "plain English text only" in system
    assert "never JSON" in system
    assert "never Chinese" in system
    assert "never explanations" in system
    user = json.loads(seen["request"]["messages"][1]["content"])
    assert "glossary" not in user
    assert user["current"] == "你好"
    for content, english in (
        ('{"translation":"The Zen Club meets."}', "The Zen Club meets."),
        ('{"en":"prajna"}', "prajna"),
    ):
        result, _seen = _translate(content)
        assert result.status == "ok"
        assert result.text == english
        assert result.text != content
    refused = (
        '{"previous":["上一句"],"note":"ignore"}',
        "{not json",
        '["Hello"]',
        "大家好",
        "Hello 世界",
        '{"current":"你好"}',
        "Sure, here is the translation: Hello",
        "Translation: Hello",
        "Hello.\n\nNote: this explains the line.",
        "Hello\nExplanation: extra",
        '"Hello"',
    )
    for content in refused:
        result, _seen = _translate(content)
        assert result.status == "bad_response", content
        assert result.text == "", content
        assert content not in result.detail
    for content in ("Hello", "Of course, we begin with the breath.", "The translation is faithful."):
        result, _seen = _translate(content)
        assert result.status == "ok", content
        assert result.text == content


def test_glossary_upsert_rejects_a_stale_version(tmp_path):
    store = CaptionStore(tmp_path / "c.sqlite3")
    try:
        now = time.time()
        first = [_term("禪學社", en="Zen Club")]
        second = [_term("般若", en="prajna")]
        assert store.save_glossary("class", 1, first, now, 0) is True
        assert store.save_glossary("class", 2, second, now, 0) is False
        kept = store.get_glossary("class")
        assert kept["version"] == 1
        assert kept["terms"][0]["zh"] == "禪學社"
        assert store.save_glossary("class", 2, second, now, 1) is True
        assert store.get_glossary("class")["terms"][0]["zh"] == "般若"
        store.delete_glossary("class")
        assert store.get_glossary("class") is None
        assert store.save_glossary("class", 3, first, now, 2) is False
        assert store.get_glossary("class") is None
        assert store.save_glossary("class", 1, first, now, 0) is True
    finally:
        store.close()


@pytest.mark.anyio
async def test_legacy_post_does_not_store_terms_put_would_reject(tmp_path):
    app = app_for(settings=_settings(tmp_path / "c.sqlite3"), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            saved = await _put(client, token, "class", [_term("禪學社", ["柴學社"], en="Zen Club")], 0)
            assert saved.status_code == 200, saved.text
            long_zh = "很長的標準詞" * 5
            text = f"{long_zh}=Long\n開示|開始|示=Dharma talk\n甲|乙丙=A\n丁|乙丙=D\n"
            posted = await client.post(
                "/api/glossary",
                json={"room_id": "class", "session_id": "s", "text": text},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert posted.status_code == 400, posted.text
            view = (await _get(client, token, "class")).json()
            assert view["version"] == 1
            assert [item["zh"] for item in view["terms"]] == ["禪學社"]
            _accepted, rejected = validate_terms(view["terms"])
            assert rejected == []
            round_trip = await _put(client, token, "class", view["terms"], view["version"])
            assert round_trip.status_code == 200, round_trip.text
            # An alias the textarea can show is editable. The same line saves again.
            echoed = await client.post(
                "/api/glossary",
                json={
                    "room_id": "class",
                    "session_id": "s",
                    "text": "禪學社|柴學社=Zen Club\n",
                    "if_version": 2,
                },
                headers={**auth(token), "content-type": "application/json"},
            )
            assert echoed.status_code == 200, echoed.text
            assert echoed.json()["deleted"] == 0
            echoed_view = (await _get(client, token, "class")).json()
            assert echoed_view["version"] == 3
            assert echoed_view["terms"][0]["aliases"] == ["柴學社"]
            # A note is not in the textarea syntax, so a legacy replace is refused.
            noted = await _put(
                client, token, "class",
                [{**echoed_view["terms"][0], "note": "主持人備註"}],
                echoed_view["version"],
            )
            assert noted.status_code == 200, noted.text
            replaced = await client.post(
                "/api/glossary",
                json={"room_id": "class", "session_id": "s", "text": "般若=prajna\n", "if_version": 4},
                headers={**auth(token), "content-type": "application/json"},
            )
            assert replaced.status_code == 400, replaced.text
            assert replaced.json()["ok"] is False
            assert any("PUT /api/rooms/" in item["reason"] for item in replaced.json()["rejected"])
            assert all("編輯器" not in item["reason"] for item in replaced.json()["rejected"])
            current = (await _get(client, token, "class")).json()
            assert current["version"] == 4
            assert current["terms"][0]["zh"] == "禪學社"
            assert current["terms"][0]["aliases"] == ["柴學社"]
            assert current["terms"][0]["note"] == "主持人備註"
            stale = await _put(client, token, "class", [_term("空性", en="emptiness")], 1)
            assert stale.status_code == 409
            assert (await _get(client, token, "class")).json()["terms"][0]["zh"] == "禪學社"
    finally:
        await stop(app)


@pytest.mark.anyio
async def test_room_delete_does_not_resurrect_glossary(tmp_path):
    path = tmp_path / "c.sqlite3"
    app = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            assert (await _put(client, token, "class", [_term("禪學社", en="Zen Club")], 0)).status_code == 200
            store = app.state.store
            real = store._delete_room_now
            entered = threading.Event()
            release = threading.Event()

            def slow(room_id):
                entered.set()
                assert release.wait(5)
                return real(room_id)

            store._delete_room_now = slow
            deleting = None
            writing = None
            try:
                deleting = asyncio.create_task(
                    client.delete("/api/captions", params={"room_id": "class"}, headers=auth(token))
                )
                assert await asyncio.to_thread(entered.wait, 5)
                writing = asyncio.create_task(
                    _put(client, token, "class", [_term("般若", en="prajna")], 1)
                )
                release.set()
                deleted = await deleting
                written = await writing
            finally:
                release.set()
                pending = [task for task in (deleting, writing) if task is not None and not task.done()]
                if pending:
                    await asyncio.gather(*pending)
            assert deleted.status_code == 200, deleted.text
            assert written.status_code == 409, written.text
            assert written.json()["version"] == 0
            memory = app.state.pipeline.room_glossary_view("class")
            assert memory["version"] == 0
            assert memory["terms"] == []
            await asyncio.to_thread(store.flush)
            assert await asyncio.to_thread(store.get_glossary, "class") is None
            fresh = await _put(client, token, "class", [_term("般若", en="prajna")], 0)
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["version"] == 1
            assert fresh.json()["terms"][0]["zh"] == "般若"
    finally:
        await stop(app)

    again = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with _serving(again):
            back = again.state.pipeline.room_glossary_view("class")
            stored = await asyncio.to_thread(again.state.store.get_glossary, "class")
        assert back["version"] == 1
        assert [item["zh"] for item in back["terms"]] == ["般若"]
        assert stored["version"] == back["version"]
        assert [item["zh"] for item in stored["terms"]] == ["般若"]
    finally:
        await stop(again)


@pytest.mark.anyio
async def test_failed_glossary_write_does_not_clobber(tmp_path):
    path = tmp_path / "c.sqlite3"
    app = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            store = app.state.store
            real = store.save_glossary
            entered = threading.Event()
            release = threading.Event()
            failed = {"done": False}

            def flaky(room_id, version, terms, updated_at, expected_version=None):
                if version == 1 and not failed["done"]:
                    failed["done"] = True
                    entered.set()
                    assert release.wait(5)
                    raise RuntimeError("disk full")
                return real(room_id, version, terms, updated_at, expected_version)

            store.save_glossary = flaky
            first = None
            second = None
            try:
                first = asyncio.create_task(
                    _put(client, token, "class", [_term("禪學社", en="Zen Club")], 0)
                )
                assert await asyncio.to_thread(entered.wait, 5)
                second = asyncio.create_task(
                    _put(client, token, "class", [_term("般若", en="prajna")], 1)
                )
                release.set()
                failed_put = await first
                raced = await second
            finally:
                release.set()
                pending = [task for task in (first, second) if task is not None and not task.done()]
                if pending:
                    await asyncio.gather(*pending)
            assert failed_put.status_code == 503, failed_put.text
            assert raced.status_code == 409, raced.text
            assert app.state.pipeline.room_glossary_version("class") == 0
            await asyncio.to_thread(store.flush)
            assert await asyncio.to_thread(store.get_glossary, "class") is None
            follow = await _put(client, token, "class", [_term("般若", en="prajna")], 0)
            assert follow.status_code == 200, follow.text
            memory = app.state.pipeline.room_glossary_view("class")
            stored = await asyncio.to_thread(store.get_glossary, "class")
            assert memory["version"] == stored["version"] == 1
            assert [item["zh"] for item in memory["terms"]] == [item["zh"] for item in stored["terms"]] == ["般若"]
    finally:
        await stop(app)

    again = app_for(settings=_settings(path), translator=Translator(enabled=False))
    try:
        async with _serving(again):
            back = again.state.pipeline.room_glossary_view("class")
        assert back["version"] == 1
        assert [item["zh"] for item in back["terms"]] == ["般若"]
    finally:
        await stop(again)
