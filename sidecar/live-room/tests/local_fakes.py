"""Shared fakes for the local-backend tests. No network: every HTTP call goes to FakeOpener."""
from __future__ import annotations

import io
import json


class _Resp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests and replies with queued JSON payloads (or raises queued exceptions)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def __call__(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.requests.append({"url": req.full_url, "body": body, "headers": dict(req.header_items()),
                              "timeout": timeout})
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return _Resp(json.dumps(reply).encode())


def ollama_chat(content: str, done_reason: str = "stop") -> dict:
    return {"model": "qwen3:4b", "message": {"role": "assistant", "content": content}, "done": True,
            "done_reason": done_reason, "prompt_eval_count": 42, "eval_count": 7}


def openai_chat(content: str, finish: str = "stop") -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}}


def migrated_db(tmp_path):
    from app.admin import db
    path = tmp_path / "zen.sqlite3"
    db.migrate(path)
    return path


def seed_segment(c, seg="s1-1", room="class", session="s1", seq=1, zh="因緣具足", en=None, started_at=1000.0):
    from app.admin.db import to_uni
    c.execute("INSERT OR IGNORE INTO rooms(id) VALUES (?)", (room,))
    c.execute("INSERT OR IGNORE INTO sessions(id, room_id, started_at) VALUES (?,?,?)", (session, room, started_at))
    c.execute("INSERT INTO segments(id, session_id, room_id, seq, t0_ms, t1_ms, status) VALUES (?,?,?,?,?,?,?)",
              (seg, session, room, seq, seq * 6000, seq * 6000 + 6000, "asr_done"))
    c.execute("INSERT INTO transcripts(segment_id, version, text_raw, text, text_uni) VALUES (?,?,?,?,?)",
              (seg, 1, zh, zh, to_uni(zh)))
    if en:
        c.execute("INSERT INTO translations(segment_id, tgt_lang, version, text, origin, status) VALUES (?,?,?,?,?,?)",
                  (seg, "en", 1, en, "mt", "ok"))
