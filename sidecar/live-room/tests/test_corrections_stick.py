"""QA 全端工程師 B1/B2: human corrections are pushed as-is and never overwritten by MT/ASR."""
import asyncio
import warnings

import pytest
from httpx import ASGITransport, AsyncClient

warnings.filterwarnings("ignore", category=DeprecationWarning)
from starlette.testclient import TestClient  # noqa: E402

from app.admin import db, security  # noqa: E402
from app.admin.server import API, create_admin_app  # noqa: E402
from app.ledger import Ledger  # noqa: E402
from tests.local_fakes import seed_segment  # noqa: E402
from tests.test_pipeline_repair import app_for, auth, push, settings_with, stop, token_of  # noqa: E402

TOKEN = "test-admin-token-0123456789"
BEARER = {"authorization": f"Bearer {TOKEN}"}


def cap(seq=1, zh="因緣具足", en="", status="zh_ready", **kw):
    ev = {"type": "caption", "id": f"class:s1:{seq}", "room_id": "class", "session_id": "s1", "seq": seq,
          "zh": zh, "en": en, "status": status, "version": 1, "t0_ms": seq * 6000, "t1_ms": seq * 6000 + 6000}
    ev.update(kw)
    return ev


# ---------------------------------------------------------------- ledger
def _current(c, table, seg="class:s1:1"):
    extra = " AND tgt_lang='en'" if table == "translations" else ""
    return tuple(c.execute(f"SELECT text, origin FROM {table} WHERE segment_id=? AND is_current=1{extra}", (seg,)).fetchone())


def test_ledger_keeps_human_translation_over_mt(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(cap(status="ready", en="machine one", translate_status="ok"))
    led.submit(cap(status="ready", en="Human fix.", en_origin="human", translate_status="ok"))
    led.submit(cap(status="ready", en="machine two", translate_status="ok"))
    assert led.wait_idle(5)
    led.close()
    c = db.connect(path)
    assert _current(c, "translations") == ("Human fix.", "human")
    c.close()


def test_ledger_keeps_human_transcript_over_asr(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(cap(zh="因緣具足的時候"))
    assert led.wait_idle(5)
    c = db.connect(path)
    c.execute("BEGIN IMMEDIATE")
    c.execute("UPDATE transcripts SET is_current=0 WHERE segment_id='class:s1:1'")
    c.execute("INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni, origin) VALUES (?,?,?,?,?,?)",
              ("class:s1:1", 2, "因緣具足時", "因緣具足時", db.to_uni("因緣具足時"), "human"))
    c.execute("COMMIT")
    led.submit(cap(zh="因緣具足的時候"))          # ASR replay
    assert led.wait_idle(5)
    led.close()
    assert _current(c, "transcripts") == ("因緣具足時", "human")
    c.close()


# ---------------------------------------------------------------- live room override
class SlowTranslator:
    def __init__(self):
        self.calls = 0

    def translate(self, zh, **kw):
        import time
        from app.translate import TranslateResult
        self.calls += 1
        time.sleep(0.3)
        return TranslateResult(text="machine line", status="ok")

    def __getattr__(self, name):
        from app.translate import Translator
        return getattr(Translator(enabled=True, key="x"), name)


@pytest.mark.anyio
async def test_live_override_publishes_human_en_and_beats_inflight_mt():
    tr = SlowTranslator()
    app = app_for(translator=tr, settings=settings_with(allow_testclient=True, translate_workers=1, translate_timeout_s=5))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1:8780") as client:
            token = await token_of(app, client)
            first = asyncio.create_task(push(client, token, "class", "s", 1, "因緣".encode(), wait_translation="0",
                                             async_header=True))
            await asyncio.sleep(0.1)                 # MT attempt in flight
            r = await client.post("/api/segment/retranslate", headers=auth(token),
                                  json={"room_id": "class", "session_id": "s", "seq": 1, "en": "Human fix.",
                                        "zh": "因緣具足"})
            assert r.status_code == 200, r.text
            assert r.json()["en"] == "Human fix."
            await asyncio.wait_for(first, 3)
            await asyncio.sleep(0.5)                 # let the stale MT attempt finish
            rows = {i["seq"]: i for i in app.state.bus.caption_state("class")}
            assert rows[1]["en"] == "Human fix." and rows[1]["zh"] == "因緣具足"
            bad = await client.post("/api/segment/retranslate", headers=auth(token),
                                    json={"room_id": "class", "session_id": "s", "seq": 1, "en": "  "})
            assert bad.status_code == 400
    finally:
        await stop(app)


# ---------------------------------------------------------------- admin routing
class FakeLive:
    def __init__(self):
        self.calls = []

    def metrics(self):
        return {}

    def retranslate(self, room_id, session_id, seq, zh=None):
        self.calls.append(("retranslate", seq, zh))
        return 200, {"en": "mt", "seq": seq}

    def push_correction(self, room_id, session_id, seq, en, zh=None):
        self.calls.append(("push", seq, en, zh))
        return 200, {"en": en, "seq": seq}


@pytest.fixture
def admin(tmp_path):
    path = tmp_path / "zen.sqlite3"
    live = FakeLive()
    app = create_admin_app(path, token_hash_hex=security.token_hash(TOKEN), port=8791,
                           identity_path=tmp_path / "zen-identity.sqlite3", probes={"live": lambda: "up"},
                           live_client=live, backup_dir=lambda: tmp_path / "b", start_worker=False)
    client = TestClient(app, base_url="http://127.0.0.1:8791", client=("127.0.0.1", 50000))
    c = db.connect(path)
    seed_segment(c, seg="s1-1", seq=1, zh="今天講因緣具足", en="Today, conditions")
    c.close()
    with client:
        yield client, live


def test_admin_retranslate_pushes_human_english(admin):
    client, live = admin
    r = client.post(f"{API}/corrections", headers=BEARER,
                    json={"segment_id": "s1-1", "target_type": "translation", "text": "Today: causes and conditions.",
                          "promote_tm": False})
    assert r.status_code == 201, r.text
    r = client.post(f"{API}/segments/s1-1/retranslate", headers=BEARER)
    assert r.status_code == 200
    assert live.calls[-1][0] == "push" and live.calls[-1][2] == "Today: causes and conditions."
    r = client.post(f"{API}/segments/s1-1/retranslate", headers=BEARER, json={"mode": "model"})
    assert r.status_code == 200 and live.calls[-1][0] == "retranslate"
    assert client.post(f"{API}/segments/s1-1/retranslate", headers=BEARER, json={"mode": "x"}).status_code == 400


def test_admin_retranslate_sends_human_transcript(admin):
    client, live = admin
    r = client.post(f"{API}/corrections", headers=BEARER,
                    json={"segment_id": "s1-1", "target_type": "transcript", "text": "今天講因緣具足時"})
    assert r.status_code == 201, r.text
    client.post(f"{API}/segments/s1-1/retranslate", headers=BEARER)
    assert live.calls[-1] == ("retranslate", 1, "今天講因緣具足時")
