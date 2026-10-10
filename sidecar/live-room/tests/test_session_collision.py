"""全站 D8: two rooms using the same session_id both reach the ledger."""
from app.admin import db
from app.admin.retention import purge_session
from app.ledger import Ledger


def ev(room, seq, zh):
    return {"type": "caption", "id": f"{room}:s1:{seq}", "room_id": room, "session_id": "s1", "seq": seq,
            "zh": zh, "status": "ready", "t0_ms": seq * 6000, "t1_ms": seq * 6000 + 6000}


def test_same_session_id_in_two_rooms(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(ev("roomA", 1, "甲房第一句"))
    led.submit(ev("roomB", 1, "乙房第一句"))
    led.submit(ev("roomB", 2, "乙房第二句"))
    assert led.wait_idle(5)
    led.close()
    c = db.connect(path)
    rows = dict(c.execute("SELECT room_id, COUNT(*) FROM segments GROUP BY room_id").fetchall())
    c.close()
    assert rows == {"roomA": 1, "roomB": 2} and led.stats()["ledger_errors"] == 0


def test_purged_namespaced_session_not_resurrected(tmp_path):
    path = tmp_path / "zen.sqlite3"
    led = Ledger(path)
    led.submit(ev("roomA", 1, "甲"))
    led.submit(ev("roomB", 1, "乙"))
    assert led.wait_idle(5)
    c = db.connect(path)
    c.execute("UPDATE sessions SET status='ended', ended_at=started_at+1")
    c.commit()
    c.close()
    purge_session(path, None, "roomB:s1")
    led.submit(ev("roomB", 2, "乙遲到"))
    led.submit(ev("roomA", 2, "甲第二句"))
    assert led.wait_idle(5)
    led.close()
    c = db.connect(path)
    rows = dict(c.execute("SELECT room_id, COUNT(*) FROM segments GROUP BY room_id").fetchall())
    c.close()
    assert rows == {"roomA": 2}
