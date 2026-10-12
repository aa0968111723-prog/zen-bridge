"""Admin routes for training data: /admin/api/v1/training + /admin/training page."""
import json
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
from app.admin.server import API  # noqa: E402
from app.training import capture  # noqa: E402
from tests.test_admin_api import BEARER, browser_login, env, writes  # noqa: E402,F401
from tests.training_fakes import class_clip, speechy, wav_bytes  # noqa: E402

T = f"{API}/training"


@pytest.fixture
def troot(env, tmp_path, monkeypatch):  # noqa: F811
    root = tmp_path / "training"
    monkeypatch.setenv("ZEN_TRAINING_DIR", str(root))
    capture._reset_for_tests(root)
    yield root
    capture._reset_for_tests(None)


def _speaker(cl, label="師兄", live=False):
    r = cl.post(f"{T}/speakers", json={"label": label}, headers=BEARER)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = cl.post(f"{T}/speakers/{sid}/consent", headers=BEARER, json={
        "scope_asr": True, "scope_mt": True, "harvest_live": live, "recorded_by": "社長", "confirm_text": True,
        "consent_date": "2026-10-12"})
    assert r.status_code == 200, r.text
    return sid


def test_page_and_assets_served_with_csp(env, troot):  # noqa: F811
    cl = env["client"]
    r = cl.get("/admin/training")
    assert r.status_code == 200 and "訓練資料" in r.text and "script-src 'self'" in r.headers["content-security-policy"]
    assert "lotus" not in r.text.lower() and "brand/" not in r.text
    for name in ("training.js", "training_logic.js", "rec-worklet.js", "training.css"):
        assert cl.get(f"/admin/training/{name}").status_code == 200
    assert cl.get("/admin/training/../server.py").status_code == 404
    assert cl.get(T).status_code == 401
    assert "/admin/training" in cl.get("/admin").text


def test_studio_flow_over_http(env, troot):  # noqa: F811
    cl = env["client"]
    sid = _speaker(cl)
    p = cl.get(f"{T}/prompts", params={"speaker_id": sid, "n": 2}, headers=BEARER).json()["prompts"]
    assert len(p) == 2
    rid = cl.post(f"{T}/studio", json={"speaker_id": sid}, headers=BEARER).json()["id"]
    bad = cl.post(f"{T}/studio/{rid}/takes", params={"prompt_id": p[0]["id"]}, content=b"x",
                  headers={**BEARER, "content-type": "text/plain"})
    assert bad.status_code == 415
    r = cl.post(f"{T}/studio/{rid}/takes", params={"prompt_id": p[0]["id"]}, content=wav_bytes(speechy(3)),
                headers={**BEARER, "content-type": "audio/wav"})
    assert r.status_code == 200 and r.json()["saved"], r.text
    cid = r.json()["clip"]["id"]
    a = cl.get(f"{T}/clips/{cid}/audio", headers=BEARER)
    assert a.status_code == 200 and a.content[:4] == b"RIFF"
    assert cl.post(f"{T}/studio/{rid}/finish", headers=BEARER).status_code == 200
    for name in ("transcript.srt", "transcript.vtt", "transcript.txt", "transcript.json", "recording.wav", "live.jsonl"):
        assert cl.get(f"{T}/recordings/{rid}/files/{name}", headers=BEARER).status_code == 200, name
    assert cl.get(f"{T}/recordings/{rid}/files/..%2Ftraining.sqlite3", headers=BEARER).status_code == 404
    z = cl.get(f"{T}/recordings/{rid}/bundle.zip", headers=BEARER)
    assert z.status_code == 200 and z.content[:2] == b"PK"
    sp = cl.get(f"{T}/speakers/{sid}", headers=BEARER).json()
    assert sp["minutes_total"] > 0


def test_roles_and_csrf(env, troot):  # noqa: F811
    cl = env["client"]
    csrf = browser_login(cl, env["app"])
    r = cl.post(f"{T}/speakers", json={"label": "無 CSRF"}, headers={"origin": "http://127.0.0.1:8791", "sec-fetch-site": "same-origin"})
    assert r.status_code == 403
    r = cl.post(f"{T}/speakers", json={"label": "有 CSRF"}, headers=writes(csrf))
    assert r.status_code == 201
    r = cl.post(f"{T}/speakers/{r.json()['id']}/withdraw", json={}, headers=writes(csrf))
    assert r.status_code == 422          # confirm=true required


def test_harvest_review_withdraw_export_over_http(env, troot, tmp_path):  # noqa: F811
    cl = env["client"]
    sid = _speaker(cl, live=True)
    no_live = _speaker(cl, "只錄音室")
    room = lambda s: cl.post(f"{T}/classes/room", json={"room_id": "room1", "speaker_id": s, "collect": True},  # noqa: E731
                             headers=BEARER)
    assert room(no_live).status_code == 403
    assert room(sid).status_code == 200
    ov = cl.get(T, headers=BEARER).json()
    assert ov["rooms"][0]["collect"] == 1 and [x["id"] for x in ov["lecturers"]] == [sid]
    assert ov["disk"]["estimate"]["raw_mb_per_class_hour"] == 115.2
    w = tmp_path / "seg.wav"
    w.write_bytes(wav_bytes(speechy(3)))
    from app.training import db as tdb
    c = tdb.connect(troot)
    cid = class_clip(c, troot, room_id="room1", session_id="s1", seq=1, wav_path=w, zh_raw="今天講因緣", zh="今天講因緣",
                     tgt_lang="en", speaker_id=sid)
    rid = c.execute("SELECT recording_id FROM class_sessions").fetchone()[0]
    c.close()
    b = cl.get(f"{T}/classes/{rid}/batch", headers=BEARER).json()
    assert [x["id"] for x in b["items"]] == [cid] and b["counts"]["above_threshold"] == 1 and b["min_conf"] == 0.85
    q = cl.get(f"{T}/review", headers=BEARER).json()["items"]
    assert [x["id"] for x in q] == [cid]
    r = cl.post(f"{T}/review/{cid}", headers=BEARER, json={"action": "approve", "text": "今天講因緣",
                                                            "mt_text": "Today we talk about causes and conditions.",
                                                            "tgt_lang": "en"})
    assert r.status_code == 200 and r.json()["mt_pair"]["tgt_lang"] == "en"
    assert "staging_id" in r.json()["staging"]           # corrected translation went to the TM staging queue
    lst = cl.get(f"{API}/staging", headers=BEARER).json()
    assert any(i["src_text"] == "今天講因緣" for i in lst["items"])
    h = cl.get(f"{T}/classes", headers=BEARER).json()
    assert h["classes"][0]["recording_id"] == rid and h["classes"][0]["clips"]["approved"] == 1
    hrs = cl.get(f"{T}/stats/hours", headers=BEARER).json()["speakers"]
    assert hrs[sid]["approved_total_h"] > 0
    assert cl.post(f"{T}/settings", json={"raw_retention_days": 7}, headers=BEARER).json()["raw_retention_days"] == 7
    assert cl.post(f"{T}/settings", json={"raw_retention_days": -3}, headers=BEARER).status_code == 422
    assert cl.get(f"{T}/recordings/{rid}/bundle.zip", headers=BEARER).headers["content-type"] == "application/zip"
    q = cl.post(f"{T}/speakers/quick", headers=BEARER, json={"label": "快速", "recorded_by": "社長", "confirm_text": True})
    assert q.status_code == 201 and q.json()["consent"]["harvest_live"] == 1
    e = cl.post(f"{T}/exports", headers=BEARER, json={"options": {}})
    assert e.status_code == 201, e.text
    out = e.json()["dir"]
    w = cl.post(f"{T}/speakers/{sid}/withdraw", json={"confirm": True}, headers=BEARER)
    assert w.status_code == 200 and w.json()["clips_deleted"] == 1 and w.json()["exports_deleted"] == 1
    import os
    assert not os.path.exists(out)
    assert cl.get(f"{T}/exports", headers=BEARER).json()["exports"][0]["deleted_reason"] == "speaker_withdrawn"


def test_import_upload_and_map(env, troot, tmp_path):  # noqa: F811
    from app.training import audioq
    if not audioq.ffmpeg_bin():
        pytest.skip("ffmpeg not available")
    cl = env["client"]
    sid = _speaker(cl)
    assert cl.post(f"{T}/imports/upload", params={"name": "x.exe"}, content=b"MZ", headers=BEARER).status_code == 415
    assert cl.post(f"{T}/imports/upload", params={"name": "a.mp3"}, content=wav_bytes(speechy(6)), headers=BEARER).status_code == 201
    srt = "1\n00:00:00,300 --> 00:00:03,000\nSpeaker 0: 大家好\n\n2\n00:00:03,200 --> 00:00:05,800\nSpeaker 1: 請問\n"
    assert cl.post(f"{T}/imports/upload", params={"name": "a.srt"}, content=srt.encode(), headers=BEARER).status_code == 201
    r = cl.post(f"{T}/imports", json={"audio": "a.mp3", "transcript": "a.srt"}, headers=BEARER)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert all(s["cut"] == 0 for s in r.json()["speakers"])          # everyone excluded by default
    m = cl.post(f"{T}/imports/{rid}/map", json={"tag": "Speaker 0", "speaker_id": sid}, headers=BEARER).json()
    assert {s["tag"]: s["cut"] for s in m["speakers"]} == {"Speaker 0": 1, "Speaker 1": 0}
    assert not (troot / "import" / "uploads" / "a.mp3").exists()
