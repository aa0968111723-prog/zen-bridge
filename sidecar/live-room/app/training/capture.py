"""社課連續收錄（主要路徑）：整堂課音檔＋即時逐字稿，完全不在字幕路徑上做事。

字幕程式（pipeline）只呼叫這裡的幾個函式，每個都只做「查一個 dict」或「放進佇列」：

* ``tap_wav``   每段音訊解碼成 16k WAV 之後（含靜音段）→ 讀出 bytes 丟進佇列
* ``final``     Breeze 定稿 → 丟進佇列
* ``draft``     草稿字幕（PR #49 合併後由草稿 hub 呼叫）
* ``tap_pcm``   草稿串流的原始 PCM（PR #49 快速定稿模式不再上傳 6 秒片段時用）
* ``end``       場次結束

房間沒有開啟收錄時，``room_id in _armed`` 為 False，函式立刻 return；沒有 training.sqlite3 時
背景執行緒只每 10 秒 stat 一次檔案。真正的寫檔（WAV 分段、JSONL、fsync、SQLite）全部在單一背景
執行緒 ``zen-train-capture``；佇列滿了就丟掉音訊並計數，絕不讓字幕等待。
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from app.training import classaudio, recordings, registry
from app.training import db as tdb

log = logging.getLogger("zen.training")

QUEUE_MAX = 400                 # ≈ 40 分鐘的 6 秒片段；正常情況佇列幾乎是空的
REFRESH_S = 3.0
FSYNC_S = 10.0
WARN_NO_FINAL_S = 240           # 4 分鐘沒有辨識結果 → 主持頁警告（靜坐中？）
PAUSE_NO_FINAL_S = 300          # 5 分鐘 → 寫出目前的逐字稿、暫停寫原始音檔（保留最近 60 秒，說話後補寫）
END_NO_AUDIO_S = 300            # 5 分鐘完全沒收到音訊 → 這堂課結束
RING_S = 60
DRAIN_S = 0.5                   # writer wakes at most twice a second (batch)
DERIVE_IDLE_S = 60              # 所有收錄都停了 60 秒後才做課後整理（不和上課搶 CPU）

_armed: dict[str, dict] = {}    # room_id -> {"speaker_id", "label"}；只由背景執行緒整個替換
_status: dict[str, dict] = {}   # room_id -> 主持頁顯示用
_q: "queue.Queue[tuple]" = queue.Queue(maxsize=QUEUE_MAX)
_stats = {"taps": 0, "tap_ns": 0, "tap_max_ns": 0, "dropped": 0, "written_bytes": 0, "errors": 0}
_thread: threading.Thread | None = None
_tlock = threading.Lock()
_force = threading.Event()
_root_override: Path | None = None
_gen = 0                        # bumps on reset so an old writer thread exits


def _enabled() -> bool:
    return (os.environ.get("ZEN_TRAINING_CAPTURE") or "1").strip() != "0"


def _ensure() -> None:
    global _thread
    if _thread is not None or not _enabled():
        return
    with _tlock:
        if _thread is None:
            _thread = threading.Thread(target=_Writer(_gen).run, daemon=True, name="zen-train-capture")
            _thread.start()


def _put(item: tuple) -> None:
    try:
        _q.put_nowait(item)
    except queue.Full:
        _stats["dropped"] += 1


# ---------------------------------------------------------------- hot path (event loop)
def armed(room_id: str) -> bool:
    _ensure()
    return room_id in _armed


def tap_wav(room_id: str, session_id: str, seq: int, t0_ms, t1_ms, t1_wall_ms, wav_path) -> None:
    if room_id not in _armed:
        _ensure()
        return
    t = time.perf_counter_ns()
    try:
        data = Path(wav_path).read_bytes()
    except OSError:
        return
    _put(("audio", room_id, session_id, seq, t0_ms, t1_ms, t1_wall_ms, data, time.time()))
    dt = time.perf_counter_ns() - t
    _stats["taps"] += 1
    _stats["tap_ns"] += dt
    _stats["tap_max_ns"] = max(_stats["tap_max_ns"], dt)


def tap_pcm(room_id: str, session_id: str, t0_ms: int, pcm: bytes, wall_ms: int | None = None) -> None:
    if room_id in _armed:
        _put(("pcm", room_id, session_id, t0_ms, bytes(pcm), wall_ms, time.time()))


def final(room_id: str, session_id: str, seq: int, t0_ms, t1_ms, zh_raw: str, zh: str, tgt_lang=None) -> None:
    if room_id in _armed:
        _put(("final", room_id, session_id, seq, t0_ms, t1_ms, zh_raw, zh, tgt_lang, time.time()))


def draft(room_id: str, session_id: str, t0_ms, t1_ms, text: str) -> None:
    if room_id in _armed:
        _put(("draft", room_id, session_id, t0_ms, t1_ms, text, time.time()))


def mark(room_id: str, kind: str, on: bool, wall_ms: int | None = None) -> None:
    """Host hotkey: 'question' = 學員提問中（這段時間的話預設排除）."""
    _put(("mark", room_id, kind, bool(on), wall_ms or int(time.time() * 1000), time.time()))


def end(room_id: str, session_id: str | None = None, reason: str = "session_end") -> None:
    if room_id in _armed or any(s.get("room_id") == room_id for s in _status.values()):
        _put(("end", room_id, session_id, reason, time.time()))


def refresh() -> None:
    """Settings changed (host toggle, consent): re-read within a fraction of a second."""
    _ensure()
    _force.set()
    _put(("noop",))


def status(room_id: str) -> dict | None:
    s = _status.get(room_id)
    return dict(s) if s else None


def stats() -> dict:
    n = _stats["taps"] or 1
    return {"armed_rooms": len(_armed), "queue": _q.qsize(), "dropped": _stats["dropped"],
            "tap_avg_us": round(_stats["tap_ns"] / n / 1000, 1), "tap_max_us": round(_stats["tap_max_ns"] / 1000, 1),
            "taps": _stats["taps"], "written_bytes": _stats["written_bytes"], "errors": _stats["errors"]}


def flush(timeout: float = 5.0) -> bool:
    """Tests / shutdown: wait until the writer drained the queue."""
    t = time.monotonic() + timeout
    while time.monotonic() < t:
        if _q.unfinished_tasks == 0:
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- background writer
class _Session:
    def __init__(self, row: dict, rec: dict, root: Path):
        self.id = row["id"]
        self.room_id = row["room_id"]
        self.session_id = row["session_id"]
        self.speaker_id = row["speaker_id"]
        self.rec = rec
        self.dir = Path(root) / rec["dir"]
        self.audio = classaudio.ChunkWriter(self.dir / "audio")
        self.live = open(self.dir / "live.jsonl", "a", encoding="utf-8")
        self.audio_s = float(row.get("audio_s") or 0)
        self.bytes = int(row.get("bytes") or 0)
        self.finals = int(row.get("finals") or 0)
        self.started = row["started_at"]
        self.last_audio = time.time()
        self.last_final = time.time()
        self.origin_wall: int | None = None
        self.question = False
        self.paused = False
        self.ring: collections.deque = collections.deque()
        self.ring_bytes = 0
        self.dirty = False

    def event(self, ev: dict) -> None:
        self.live.write(json.dumps({"at": round(time.time(), 3), **ev}, ensure_ascii=False) + "\n")
        self.live.flush()
        self.dirty = True

    def write_audio(self, t0_ms, pcm: bytes) -> int:
        if t0_ms is None:
            t0_ms = self.audio.end_byte // (classaudio.RATE * classaudio.BPS // 1000)
        if self.paused:
            self.ring.append((t0_ms, pcm))
            self.ring_bytes += len(pcm)
            while self.ring and self.ring_bytes > RING_S * classaudio.RATE * classaudio.BPS:
                self.ring_bytes -= len(self.ring.popleft()[1])
            return 0
        n = self.audio.write(t0_ms, pcm)
        self.audio_s += n / (classaudio.RATE * classaudio.BPS)
        self.bytes += n
        self.dirty = True
        return n

    def resume(self) -> None:
        self.paused = False
        while self.ring:
            t0, pcm = self.ring.popleft()
            self.write_audio(t0, pcm)
        self.ring_bytes = 0
        self.event({"type": "resume"})

    def sync(self) -> None:
        self.audio.flush(fsync=True)
        self.live.flush()
        os.fsync(self.live.fileno())

    def close(self) -> None:
        try:
            self.audio.close()
        finally:
            self.live.flush()
            os.fsync(self.live.fileno())
            self.live.close()


class _Writer:
    def __init__(self, gen: int = 0):
        self.gen = gen
        self.q = _q
        self.root = Path(_root_override) if _root_override else tdb.training_dir()
        self.c = None
        self.sessions: dict[str, _Session] = {}        # room_id -> open session
        self.last_refresh = 0.0
        self.last_sync = time.monotonic()
        self.last_audio_any = 0.0
        self.pending_derive: list[str] = []
        self.recovered = False

    # -- db
    def _db(self):
        if self.c is None:
            if not tdb.db_path(self.root).exists():
                return None
            self.c = tdb.connect(self.root)
        return self.c

    def run(self) -> None:
        batch: collections.deque = collections.deque()
        while self.gen == _gen:
            # Batch drain instead of waking on every item: each wake-up competes with the caption
            # event loop for the GIL, so the writer runs at most every DRAIN_S and then does it all.
            if not batch:
                _force.wait(DRAIN_S)
                while True:
                    try:
                        batch.append(self.q.get_nowait())
                    except queue.Empty:
                        break
            item = batch.popleft() if batch else None
            if self.gen != _gen:                  # reset (tests): never touch the shared state again
                break
            try:
                now = time.monotonic()
                period = REFRESH_S if self.c is not None else 10.0
                if _force.is_set() or now - self.last_refresh >= period:
                    _force.clear()
                    self.last_refresh = now
                    self._refresh()
                if item is not None:
                    self._handle(item)
                self._tick()
            except Exception:
                _stats["errors"] += 1
                log.warning("training capture writer error", exc_info=True)
            finally:
                if item is not None:
                    self.q.task_done()
        for room in list(self.sessions):
            self._drop(room, wiped=False)

    def _refresh(self) -> None:
        global _armed
        c = self._db()
        if c is None:
            _armed = {}
            return
        rows = c.execute("SELECT rc.room_id, rc.speaker_id, s.label FROM room_collect rc JOIN speakers s "
                         "ON s.id = rc.speaker_id WHERE rc.collect = 1 AND s.withdrawn_at IS NULL").fetchall()
        new = {r["room_id"]: {"speaker_id": r["speaker_id"], "label": r["label"]} for r in rows
               if registry.has_consent(c, r["speaker_id"], "asr", live=True)}
        _armed = new
        for room, s in list(self.sessions.items()):
            row = c.execute("SELECT stopped_at, stop_reason FROM class_sessions WHERE id=?", (s.id,)).fetchone()
            if row is None or row["stopped_at"] is not None:          # stopped elsewhere (admin / withdrawal)
                self._drop(room, wiped=(row is None or row["stop_reason"] == "withdrawn"))
            elif room not in new or new[room]["speaker_id"] != s.speaker_id:
                self._finish(room, "toggle_off")
        for room in list(_status):
            if room not in self.sessions:
                _status.pop(room, None)
        for room, a in new.items():
            if room not in self.sessions:
                _status[room] = {"room_id": room, "collecting": False, "armed": True, "speaker_id": a["speaker_id"],
                                 "label": a["label"]}
        if not self.recovered:
            self.recovered = True
            self._recover()

    def _recover(self) -> None:
        """Sessions left open by a crash: close them from what is on disk and write the transcript."""
        c = self.c
        for r in c.execute("SELECT * FROM class_sessions WHERE stopped_at IS NULL").fetchall():
            if r["room_id"] in self.sessions:
                continue
            if r["room_id"] in _armed and time.time() - (r["last_audio_at"] or r["started_at"]) < END_NO_AUDIO_S:
                continue                      # may continue right away; _open() picks it up again
            self._close_row(dict(r), "recovered")

    def _close_row(self, r: dict, reason: str) -> None:
        c = self.c
        rec = recordings.get(c, r["recording_id"]) if r["recording_id"] else None
        audio_s = classaudio.total_seconds(Path(self.root) / rec["dir"] / "audio") if rec else 0
        c.execute("UPDATE class_sessions SET stopped_at=?, stop_reason=?, audio_s=MAX(audio_s, ?) WHERE id=?",
                  (time.time(), reason, audio_s, r["id"]))
        if rec:
            recordings.finish(c, self.root, rec["id"])
            self.pending_derive.append(rec["id"])

    def _open(self, room: str, session_id: str) -> _Session | None:
        a = _armed.get(room)
        c = self._db()
        if not a or c is None:
            return None
        cur = self.sessions.get(room)
        if cur and cur.session_id == session_id:
            return cur
        if cur:
            self._finish(room, "new_session")
        row = c.execute("SELECT * FROM class_sessions WHERE room_id=? AND session_id=?", (room, session_id)).fetchone()
        if row and row["stopped_at"] is not None:
            if row["stop_reason"] == "withdrawn":
                return None
            # the host switched it back on (or the class resumed after a pause / crash): continue the same files
            c.execute("UPDATE class_sessions SET stopped_at=NULL, stop_reason=NULL WHERE id=?", (row["id"],))
            row = c.execute("SELECT * FROM class_sessions WHERE id=?", (row["id"],)).fetchone()
        rec = recordings.get(c, row["recording_id"]) if row and row["recording_id"] else None
        if rec is None:
            rec = recordings.start(c, self.root, kind="harvest", speaker_id=a["speaker_id"], room_id=room,
                                   session_id=session_id, title=f"{room} 社課 {time.strftime('%Y-%m-%d %H:%M')}")
            if row:
                c.execute("UPDATE class_sessions SET recording_id=? WHERE id=?", (rec["id"], row["id"]))
            else:
                c.execute("INSERT INTO class_sessions(room_id, session_id, speaker_id, recording_id, started_at) "
                          "VALUES (?,?,?,?,?)", (room, session_id, a["speaker_id"], rec["id"], time.time()))
                tdb.audit(c, "class_collect_start", speaker_id=a["speaker_id"], room_id=room)
            row = c.execute("SELECT * FROM class_sessions WHERE room_id=? AND session_id=?", (room, session_id)).fetchone()
        elif rec["status"] == "ended":
            c.execute("UPDATE recordings SET status='recording', ended_at=NULL WHERE id=?", (rec["id"],))
            rec = recordings.get(c, rec["id"])
        s = _Session(dict(row), rec, self.root)
        s.event({"type": "start", "speaker_id": a["speaker_id"], "room_id": room, "session_id": session_id})
        self.sessions[room] = s
        self._publish(s)
        return s

    def _publish(self, s: _Session) -> None:
        a = _armed.get(s.room_id, {})
        now = time.time()
        idle = now - s.last_final
        _status[s.room_id] = {"room_id": s.room_id, "collecting": True, "armed": True, "session_id": s.session_id,
                              "speaker_id": s.speaker_id, "label": a.get("label"), "recording_id": s.rec["id"],
                              "started_at": s.started, "elapsed_s": round(now - s.started), "audio_s": round(s.audio_s, 1),
                              "bytes": s.bytes, "finals": s.finals, "no_final_s": round(idle),
                              "warn": idle >= WARN_NO_FINAL_S, "paused": s.paused, "question": s.question,
                              "dropped": _stats["dropped"]}

    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "noop":
            return
        if kind == "audio":
            _, room, sid, seq, t0, t1, t1_wall, data, at = item
            s = self._open(room, sid)
            if not s:
                return
            pcm = classaudio.pcm_from_wav_bytes(data)
            if pcm is None:
                return
            if t1_wall and t1 is not None and s.origin_wall is None:
                s.origin_wall = int(t1_wall) - int(t1)
            _stats["written_bytes"] += s.write_audio(t0, pcm)
            s.last_audio = self.last_audio_any = time.time()
            self._publish(s)
        elif kind == "pcm":
            _, room, sid, t0, pcm, wall, at = item
            s = self._open(room, sid)
            if s:
                if wall and s.origin_wall is None:
                    s.origin_wall = int(wall) - int(t0)
                _stats["written_bytes"] += s.write_audio(t0, pcm)
                s.last_audio = self.last_audio_any = time.time()
        elif kind == "final":
            _, room, sid, seq, t0, t1, raw, zh, lang, at = item
            s = self._open(room, sid)
            if not s:
                return
            s.event({"type": "final", "seq": seq, "t0_ms": t0, "t1_ms": t1, "asr_text": raw, "text": zh,
                     "tgt_lang": lang})
            s.finals += 1
            s.last_final = time.time()
            if s.paused:
                s.resume()
            self._publish(s)
        elif kind == "draft":
            _, room, sid, t0, t1, text, at = item
            s = self.sessions.get(room)
            if s and s.session_id == sid:
                s.event({"type": "draft", "t0_ms": t0, "t1_ms": t1, "text": text})
        elif kind == "mark":
            _, room, mkind, on, wall, at = item
            s = self.sessions.get(room)
            if s:
                t_ms = (wall - s.origin_wall) if s.origin_wall is not None else s.audio.end_byte // 32
                s.event({"type": mkind, "on": on, "t_ms": int(t_ms)})
                if mkind == "question":
                    s.question = on
                self._publish(s)
        elif kind == "end":
            _, room, sid, reason, at = item
            s = self.sessions.get(room)
            if s and (sid is None or s.session_id == sid):
                self._finish(room, reason)

    def _tick(self) -> None:
        now = time.monotonic()
        wall = time.time()
        for room, s in list(self.sessions.items()):
            if wall - s.last_audio >= END_NO_AUDIO_S:
                self._finish(room, "no_audio")
                continue
            if not s.paused and wall - s.last_final >= PAUSE_NO_FINAL_S:
                s.paused = True
                s.event({"type": "pause", "reason": "no_recognition_5min"})
                s.sync()
                self._save(s)
                recordings.write_transcript(self.c, self.root, s.rec["id"])      # 先存一份目前的完整逐字稿
            if wall - s.last_final >= WARN_NO_FINAL_S:
                self._publish(s)
        if now - self.last_sync >= FSYNC_S:
            self.last_sync = now
            for s in self.sessions.values():
                if s.dirty:
                    s.sync()
                    self._save(s)
                    s.dirty = False
                self._publish(s)
        if self.pending_derive and not self.sessions and time.time() - self.last_audio_any >= DERIVE_IDLE_S:
            rids, self.pending_derive = self.pending_derive, []
            threading.Thread(target=_derive_bg, args=(self.root, rids), daemon=True, name="zen-train-derive").start()

    def _save(self, s: _Session) -> None:
        self.c.execute("UPDATE class_sessions SET audio_s=?, bytes=?, finals=?, last_audio_at=?, last_final_at=?, "
                       "dropped=? WHERE id=?", (s.audio_s, s.bytes, s.finals, s.last_audio, s.last_final,
                                                _stats["dropped"], s.id))

    def _finish(self, room: str, reason: str) -> None:
        s = self.sessions.pop(room, None)
        if not s:
            return
        s.event({"type": "end", "reason": reason})
        s.close()
        self._save(s)
        self.c.execute("UPDATE class_sessions SET stopped_at=?, stop_reason=? WHERE id=?", (time.time(), reason, s.id))
        recordings.finish(self.c, self.root, s.rec["id"])                       # 完整逐字稿（JSON／SRT／VTT／TXT）
        self.pending_derive.append(s.rec["id"])
        a = _armed.get(room)
        if a:
            _status[room] = {"room_id": room, "collecting": False, "armed": True, "speaker_id": a["speaker_id"],
                             "label": a["label"], "last_recording_id": s.rec["id"]}
        else:
            _status.pop(room, None)

    def _drop(self, room: str, wiped: bool) -> None:
        s = self.sessions.pop(room, None)
        if not s:
            return
        try:
            s.close()
        except Exception:
            pass
        if wiped:
            shutil.rmtree(s.dir, ignore_errors=True)
        _status.pop(room, None)


def _derive_bg(root: Path, rids: list[str]) -> None:
    try:
        from app.training import derive
        derive.lower_priority()
        c = tdb.connect(root)
        try:
            for rid in rids:
                derive.derive_recording(c, root, rid)
            derive.cleanup(c, root)
        finally:
            c.close()
    except Exception:
        log.warning("class derive failed", exc_info=True)


def _reset_for_tests(root: Path | None = None) -> None:
    """Tests only: drop the writer thread state."""
    global _thread, _armed, _root_override, _gen, _q
    _gen += 1
    _q = queue.Queue(maxsize=QUEUE_MAX)
    _root_override = Path(root) if root else None
    _armed = {}
    _status.clear()
    for k in _stats:
        _stats[k] = 0
    _thread = None
