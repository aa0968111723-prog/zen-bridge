"""round4 #5: live *draft* captions (X-ASR streaming) - grey text that the final replaces in place.

Flow: host page -> ``/ws/draft`` (16 kHz mono PCM16, 100 ms packets) -> one ``DraftSession`` per
(room, session) -> partial text -> ``{"type": "draft", "id", "seq", "zh"}`` to the room's listeners.

Rules (round4 §7 #5):
* off by default: only when ``BREEZE_DRAFT_ASR=xasr|sherpa`` and the model folder
  (``BREEZE_DRAFT_MODEL_DIR``) is complete; nothing is ever downloaded here;
* ``num_threads=1`` unless ``BREEZE_DRAFT_THREADS`` says otherwise (D1);
* drafts are never translated, never written to the TM, the ledger, the caption store or
  the export - they only exist on listeners' screens until the final line arrives;
* the draft's ``seq`` is the next slice after the last one the host uploaded, so the final
  ``/api/push`` for that seq replaces it in place (same ``id``);
* a private pause drops the stream and sends nothing; listeners in project mode hide drafts.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.draft_asr import DraftAsr, NullDraft, draft_from_env
from app.endpointer import Endpointer

MIN_EMIT_S = 0.2              # at most 5 draft updates per second per session
FINALIZE_MIN_EMIT_S = 0.3     # grok/fast-draft-asr: endpoint-final sessions update every 0.3-0.5 s
MAX_PACKET_BYTES = 32000      # 1 s of PCM16 @ 16 kHz; larger packets are refused


@dataclass
class DraftSession:
    room_id: str
    session_id: str
    engine: DraftAsr
    seq: int = 1
    committed: list = field(default_factory=list)
    partial: str = ""
    last_emit: float = 0.0
    last_text: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)
    # grok/fast-draft-asr: when set, the server cuts utterances itself and Breeze finalises each one
    endpointer: Endpointer | None = None
    t0_offset_ms: int = 0


@dataclass(frozen=True)
class FinalUtterance:
    """One endpoint-cut utterance that the server hands to Breeze under ``seq`` (replaces that draft)."""
    room_id: str
    session_id: str
    seq: int
    wav: bytes
    t0_ms: int
    t1_ms: int
    reason: str


class DraftHub:
    def __init__(self, factory: Callable[[], DraftAsr] | None = None, *, publish: Callable[[dict], None],
                 is_paused: Callable[[str], bool] = lambda room: False, clock=time.monotonic, latency=None):
        self.factory = factory or draft_from_env
        self.latency = latency                     # M-03 B1: first packet of a seq -> first draft write
        self.publish = publish
        self.is_paused = is_paused
        self.clock = clock
        self._sessions: dict[tuple[str, str], DraftSession] = {}
        self._last_pushed: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()
        self.emitted = 0
        self.dropped_paused = 0
        self.finalized = 0

    # ---------------------------------------------------------------- availability
    def available(self) -> bool:
        """Cheap: never loads a model just to answer (the recognizer is built per host stream)."""
        if self.factory is draft_from_env:
            return (os.environ.get("BREEZE_DRAFT_ASR") or "off").strip().lower() in ("xasr", "sherpa")
        try:
            return not isinstance(self.factory(), NullDraft)
        except Exception:
            return False

    # ---------------------------------------------------------------- seq bookkeeping
    def note_pushed(self, room_id: str, session_id: str, seq: int) -> None:
        """Called on every /api/push: drafts from now on belong to ``seq + 1``."""
        key = (room_id, session_id)
        with self._lock:
            if seq <= self._last_pushed.get(key, 0):
                return
            self._last_pushed[key] = seq
            sess = self._sessions.get(key)
        if sess is not None:
            with sess.lock:
                if seq + 1 > sess.seq:
                    sess.seq = seq + 1
                    sess.committed, sess.partial, sess.last_text = [], "", ""

    # ---------------------------------------------------------------- sessions
    def open(self, room_id: str, session_id: str, *, endpointer: Endpointer | None = None,
             t0_offset_ms: int = 0) -> DraftSession | None:
        engine = self.factory()
        if isinstance(engine, NullDraft):
            return None
        key = (room_id, session_id)
        with self._lock:
            old = self._sessions.get(key)
            sess = DraftSession(room_id, session_id, engine, seq=self._last_pushed.get(key, 0) + 1,
                                endpointer=endpointer, t0_offset_ms=max(0, int(t0_offset_ms)))
            self._sessions[key] = sess
        if old is not None:
            old.committed.clear()
        return sess

    def close(self, sess: DraftSession | None) -> None:
        if sess is None:
            return
        with self._lock:
            if self._sessions.get((sess.room_id, sess.session_id)) is sess:
                del self._sessions[(sess.room_id, sess.session_id)]

    def drop_room(self, room_id: str) -> None:
        with self._lock:
            for key in [k for k in self._sessions if k[0] == room_id]:
                self._sessions[key].engine.reset()
                del self._sessions[key]

    def feed(self, sess: DraftSession, pcm16: bytes) -> dict | None:
        """Blocking (run in a thread). Returns the draft event to ``emit`` on the loop, if any."""
        return self.feed_full(sess, pcm16)[0]

    def feed_full(self, sess: DraftSession, pcm16: bytes) -> tuple[dict | None, list[FinalUtterance]]:
        """Like ``feed`` but also returns the utterances an endpoint-final session cut in this packet.

        Each cut closes the current seq (its Breeze final will replace the draft in place), moves
        the session to ``seq + 1`` and restarts the draft engine on the audio after the cut.
        """
        if len(pcm16) > MAX_PACKET_BYTES:
            raise ValueError("draft packet too large")
        if self.is_paused(sess.room_id):
            self.dropped_paused += 1
            with sess.lock:
                sess.engine.reset()
                sess.committed, sess.partial = [], ""
                if sess.endpointer is not None:
                    sess.endpointer = Endpointer(sess.endpointer.cfg)   # private audio is never finalised
            return None, []
        finals: list[FinalUtterance] = []
        with sess.lock:
            if self.latency is not None:
                self.latency.mark("B1", f"{sess.room_id}:{sess.session_id}:{sess.seq}")
            self._absorb(sess, sess.engine.feed(pcm16))
            if sess.endpointer is not None:
                for utt in sess.endpointer.feed(pcm16):
                    finals.append(self._close_seq(sess, utt))
            text = "".join(sess.committed) + sess.partial
            now = self.clock()
            gap = FINALIZE_MIN_EMIT_S if sess.endpointer is not None else MIN_EMIT_S
            if not text or text == sess.last_text or now - sess.last_emit < gap:
                return None, finals
            sess.last_text, sess.last_emit = text, now
            seq = sess.seq
        event = {"type": "draft", "room_id": sess.room_id, "session_id": sess.session_id, "seq": seq,
                 "id": f"{sess.room_id}:{sess.session_id}:{seq}", "zh": text, "status": "draft"}
        return event, finals

    @staticmethod
    def _absorb(sess: DraftSession, partials) -> None:
        for p in partials:
            if p.is_endpoint:
                if p.text:
                    sess.committed.append(p.text)
                sess.partial = ""
            else:
                sess.partial = p.text

    def _close_seq(self, sess: DraftSession, utt) -> FinalUtterance:
        """Caller holds ``sess.lock``. Hands ``sess.seq`` to Breeze and opens the next seq."""
        done = FinalUtterance(sess.room_id, sess.session_id, sess.seq, utt.wav(),
                              sess.t0_offset_ms + utt.t0_ms, sess.t0_offset_ms + utt.t1_ms, utt.reason)
        key = (sess.room_id, sess.session_id)
        with self._lock:
            if done.seq > self._last_pushed.get(key, 0):
                self._last_pushed[key] = done.seq
        sess.seq += 1
        sess.committed, sess.partial, sess.last_text = [], "", ""
        sess.last_emit = float("-inf")      # the first draft word of the next line is never throttled
        sess.engine.reset()
        rest = sess.endpointer.remainder_pcm() if sess.endpointer is not None else b""
        for i in range(0, len(rest), MAX_PACKET_BYTES):
            self._absorb(sess, sess.engine.feed(rest[i:i + MAX_PACKET_BYTES]))
        self.finalized += 1
        return done

    def flush(self, sess: DraftSession | None) -> list[FinalUtterance]:
        """End of the host stream: the speech still open becomes the last utterance."""
        if sess is None or sess.endpointer is None or self.is_paused(sess.room_id):
            return []
        with sess.lock:
            return [self._close_seq(sess, utt) for utt in sess.endpointer.flush()]

    def emit(self, event: dict | None) -> bool:
        """Publish on the event loop thread (listener queues are asyncio objects)."""
        if not event or self.is_paused(event["room_id"]):
            return False
        self.publish(event)
        self.emitted += 1
        return True

    def feed_and_emit(self, sess: DraftSession, pcm16: bytes) -> dict | None:
        """Single-threaded convenience (tests, tools)."""
        event = self.feed(sess, pcm16)
        return event if self.emit(event) else None

    def stats(self) -> dict:
        with self._lock:
            n = len(self._sessions)
        return {"draft_sessions": n, "draft_emitted": self.emitted, "draft_dropped_paused": self.dropped_paused,
                "draft_finalized": self.finalized}
