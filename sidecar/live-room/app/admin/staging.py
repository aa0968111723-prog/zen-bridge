"""Admin staging approval flow (round3 §5-1, VocBench-style validation).

Human corrections never write translation memory (``tm_units``) or the glossary
(``glossary_terms``) directly any more. They become ``staging_items`` (state ``pending``);
an admin approves or rejects each one. Approval writes the target and the item's new state
in ONE SQLite transaction and leaves a before/after row in ``staging_audit``.

State machine (anything else is 409 ``invalid_transition``)::

    pending --approve--> approved      (admin; writes TM / glossary)
    pending --reject---> rejected      (admin; reason required)
    pending --withdraw-> withdrawn     (author or admin)
    pending --(new proposal for the same target)--> superseded
    pending --edit-----> pending       (author or admin; rev+1)

Conflict detection: when an item is staged, a snapshot of its target (the TM units for the same
source sentence / the glossary row for the same zh) is stored with its sha256. Approval recomputes
it; a different snapshot is 409 ``target_changed`` unless the admin passes ``override_conflict``
(audited) or first rebases the item (PATCH ``{"rebase": true}``).

Glossary approval bumps ``glossaries.version`` and the term ``rev`` like every other glossary
write. It does NOT push to the live room: the existing push flow (``POST /rooms/{id}/glossary/push``
with ``dry_run`` diff + ``if_room_version``) stays the only way to change a running room.

Library functions take a connection; the caller owns the transaction (same as app.feedback).
``register(app, ctx)`` mounts the APIRouter under ``{ctx.api}/staging``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pathlib import Path

from app import feedback
from app.admin.security import role_allows
from app.tm import add_unit, src_hash

KINDS = ("tm", "term")
STATES = ("pending", "approved", "rejected", "superseded", "withdrawn")
TGT_LANGS = ("en", "ja")
TRANSITIONS = {"pending": {"approved", "rejected", "withdrawn", "superseded", "pending"}}
BULK_MAX = 100
STATIC = Path(__file__).with_name("static")
PAGE_FILES = {"staging.js": "text/javascript", "staging.css": "text/css"}
TM_APPROVED = 5          # served first by TranslationMemory.exact (ORDER BY quality DESC)
JA_GLOSSARY = "日文詞表"  # same name app.admin.info uses for the lazily created ja glossary

_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]|</?think\b|<\|", re.IGNORECASE)
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


class StagingError(feedback.FeedbackError):
    """problem+json via the admin app's FeedbackError handler; the router adds ``extra``."""

    def __init__(self, detail: str, code: str = "invalid", status: int = 422, **extra):
        super().__init__(detail, code=code, status=status)
        self.extra = extra


# ---------------------------------------------------------------- helpers
def item_etag(item_id: int, rev: int) -> str:
    return f'"s{int(item_id)}-r{int(rev)}"'


def _sig(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True)


def _clean(v, name: str, lo: int, hi: int, *, multiline_ok: bool = False) -> str:
    if v is not None and not isinstance(v, str):
        raise StagingError(f"{name} 必須是字串")
    s = (v or "").strip()
    if not (lo <= len(s) <= hi):
        raise StagingError(f"{name} 需 {lo}–{hi} 字")
    if _CTRL.search(s) or (not multiline_ok and "\n" in s) or "\n\n" in s:
        raise StagingError(f"{name} 不能含控制字元、模型標記或多段")
    return s


def _traditional(text: str) -> str:
    """Simplified -> traditional with the matcher's fold (glossary.py:91) plus the TM's extra fold."""
    from app.glossary import _traditional as trad
    from app.tm import _EXTRA_FOLD
    return trad(text).translate(_EXTRA_FOLD)


def glossary_id_for(c: sqlite3.Connection, lang: str) -> int:
    """Same target glossary as app.admin.info.glossary_for: en = global, ja = admin_glossary_lang."""
    if lang == "en":
        return feedback._global_glossary(c)
    row = c.execute("SELECT glossary_id FROM admin_glossary_lang WHERE tgt_lang='ja' LIMIT 1").fetchone()
    if row:
        return int(row[0])
    c.execute("INSERT INTO glossaries(name, scope) VALUES (?, 'global')", (JA_GLOSSARY,))
    gid = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    c.execute("INSERT INTO admin_glossary_lang(glossary_id, tgt_lang) VALUES (?, 'ja')", (gid,))
    return gid


def _glossary_lookup(c: sqlite3.Connection, lang: str) -> int | None:
    if lang == "en":
        row = c.execute("SELECT id FROM glossaries WHERE scope='global' AND name=?", (feedback.GLOBAL_GLOSSARY,)).fetchone()
    else:
        row = c.execute("SELECT glossary_id FROM admin_glossary_lang WHERE tgt_lang='ja' LIMIT 1").fetchone()
    return int(row[0]) if row else None


def validate(kind: str, tgt_lang: str, src_text, tgt_text, aliases=None, note=None) -> dict:
    if kind not in KINDS:
        raise StagingError("kind 只能是 tm 或 term")
    if tgt_lang not in TGT_LANGS:
        raise StagingError("目標語言只能是 en 或 ja（一次一種）")
    if note is not None:
        note = _clean(note, "note", 0, 500) or None
    if kind == "tm":
        if aliases:
            raise StagingError("翻譯記憶沒有 aliases")
        zh = _clean(src_text, "中文原句", 1, 2000)
        tgt = _clean(tgt_text, "譯文", 1, 2000)
        if tgt_lang == "en":
            from app.translate import validate_caption_en
            if validate_caption_en(tgt, zh=zh) is None:
                raise StagingError("英譯必須是單行純英文（不能含中文、多段、控制字元或模型標記）")
        return {"src_text": zh, "tgt_text": tgt, "aliases": [], "note": note}
    zh = _clean(src_text, "zh", 1, 20)
    if _traditional(zh) != zh:
        # glossary.py: a simplified canonical never matches, so it would be a dead term.
        raise StagingError("詞條中文需用繁體字（簡體詞不會被比對到）", code="not_traditional")
    tgt = _clean(tgt_text, "譯詞", 1, 80)
    if tgt_lang == "en" and _CJK.search(tgt):
        raise StagingError("英文譯詞不能含中日文字")
    if aliases is None:
        aliases = []
    if not isinstance(aliases, list) or len(aliases) > 8:
        raise StagingError("aliases 最多 8 個")
    al = []
    for a in aliases:
        a = _clean(a, "別名", 1, 20)
        if a != zh and a not in al:
            al.append(a)
    return {"src_text": zh, "tgt_text": tgt, "aliases": al, "note": note}


def target_key(c: sqlite3.Connection, kind: str, tgt_lang: str, src_text: str) -> str:
    if kind == "tm":
        return src_hash(src_text, tgt_lang)
    return f"g{glossary_id_for(c, tgt_lang)}:{src_text}"


def snapshot(c: sqlite3.Connection, kind: str, tgt_lang: str, src_text: str):
    """What the target looks like now. Counters (use_count, hit_count) are left out on purpose."""
    if kind == "tm":
        rows = c.execute("SELECT id, tgt_text, quality FROM tm_units WHERE src_hash=? AND tgt_lang=? ORDER BY id",
                         (src_hash(src_text, tgt_lang), tgt_lang)).fetchall()
        return [{"id": r[0], "tgt_text": r[1], "quality": r[2]} for r in rows]
    gid = _glossary_lookup(c, tgt_lang)       # read-only: a GET must never create the glossary
    if gid is None:
        return None
    r = c.execute("SELECT id, en, aliases, locked, status, rev FROM glossary_terms WHERE glossary_id=? AND zh=?",
                  (gid, src_text)).fetchone()
    if not r:
        return None
    return {"id": r[0], "en": r[1], "aliases": json.loads(r[2] or "[]"), "locked": r[3], "status": r[4], "rev": r[5]}


def item_out(r) -> dict:
    d = dict(r)
    d["aliases"] = json.loads(d.get("aliases") or "[]")
    d["base"] = json.loads(d.pop("base_json") or "null")
    d.pop("base_sig", None)
    d.pop("target_key", None)
    d["etag"] = item_etag(d["id"], d["rev"])
    return d


def get_row(c: sqlite3.Connection, item_id: int):
    r = c.execute("SELECT * FROM staging_items WHERE id=?", (int(item_id),)).fetchone()
    if not r:
        raise StagingError("找不到這筆待審項目", code="not_found", status=404)
    return r


def _audit(c, item_id: int, action: str, actor: dict, before=None, after=None, note: str | None = None) -> None:
    c.execute("INSERT INTO staging_audit(item_id, action, actor_id, actor_via, before_json, after_json, note) "
              "VALUES (?,?,?,?,?,?,?)",
              (item_id, action, actor.get("user_id"), actor.get("via"),
               None if before is None else _dumps(before), None if after is None else _dumps(after), note))
    # events stays text-free (schema: payload 不得含字幕全文或個資): ids only.
    c.execute("INSERT INTO events(kind, actor_id, payload) VALUES (?,?,?)",
              (f"audit.staging.{action}", actor.get("user_id"),
               json.dumps({"item_id": item_id, "via": actor.get("via"), "role": actor.get("role")})))


def _public(r) -> dict:
    return {"state": r["state"], "rev": r["rev"], "tgt_text": r["tgt_text"],
            "aliases": json.loads(r["aliases"] or "[]"), "note": r["note"]}


def check_etag(row, if_match: str | None) -> None:
    """Strong comparison only. Approving is never blind: '*' and weak tags do not count."""
    cur = item_etag(row["id"], row["rev"])
    if not if_match:
        raise StagingError("需要 If-Match（請先 GET 取得 ETag）", code="precondition_required", status=428, etag=cur)
    tags = [t.strip() for t in if_match.split(",")]
    if tags == ["*"]:
        raise StagingError("If-Match 需要具體的 ETag，不能用 *", code="precondition_required", status=428, etag=cur)
    if cur not in tags:
        raise StagingError("這筆待審項目已被修改，請重新載入", code="precondition_failed", status=412, etag=cur)


def _transition(row, new: str) -> None:
    if new not in TRANSITIONS.get(row["state"], set()):
        raise StagingError(f"這筆項目目前是 {row['state']}，不能改成 {new}", code="invalid_transition", status=409,
                           state=row["state"], etag=item_etag(row["id"], row["rev"]))


def _is_author(row, actor: dict) -> bool:
    return row["author_id"] == actor.get("user_id") and (row["author_via"] or "") == (actor.get("via") or "")


def _may_touch(row, actor: dict) -> None:
    if not (role_allows(actor.get("role", ""), "admin") or _is_author(row, actor)):
        raise StagingError("只有提案者本人或管理員可以修改／撤回", code="forbidden", status=403)


# ---------------------------------------------------------------- stage
def stage(c: sqlite3.Connection, *, kind: str, tgt_lang: str = "en", src_text, tgt_text, aliases=None, note=None,
          actor: dict, correction_id: int | None = None, segment_id: str | None = None,
          room_id: str | None = None) -> tuple[dict, bool]:
    """Create a pending item. Returns (item, created). An identical pending proposal for the same
    target is returned as is (created=False); a different one supersedes the older pending item."""
    v = validate(kind, tgt_lang, src_text, tgt_text, aliases, note)
    if segment_id is not None:
        seg = c.execute("SELECT room_id FROM segments WHERE id=?", (segment_id,)).fetchone()
        if not seg:
            raise StagingError("找不到這個段落", code="not_found", status=404)
        room_id = room_id or seg[0]
    if room_id is not None and not c.execute("SELECT 1 FROM rooms WHERE id=?", (room_id,)).fetchone():
        room_id = None
    key = target_key(c, kind, tgt_lang, v["src_text"])
    base = snapshot(c, kind, tgt_lang, v["src_text"])
    old = c.execute("SELECT * FROM staging_items WHERE kind=? AND tgt_lang=? AND target_key=? AND state='pending'",
                    (kind, tgt_lang, key)).fetchone()
    if old is not None and old["tgt_text"] == v["tgt_text"] and json.loads(old["aliases"]) == v["aliases"]:
        return item_out(old), False
    if old is not None:
        c.execute("UPDATE staging_items SET state='superseded', rev=rev+1, updated_at=unixepoch('subsec') WHERE id=?",
                  (old["id"],))
    c.execute(
        """INSERT INTO staging_items(kind, tgt_lang, src_text, tgt_text, aliases, note, target_key, base_json, base_sig,
                                     correction_id, segment_id, room_id, author_id, author_via)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (kind, tgt_lang, v["src_text"], v["tgt_text"], json.dumps(v["aliases"], ensure_ascii=False), v["note"], key,
         _dumps(base), _sig(base), correction_id, segment_id, room_id, actor.get("user_id"), actor.get("via")))
    new_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    row = get_row(c, new_id)
    _audit(c, new_id, "create", actor, before=base, after=_public(row))
    if old is not None:
        c.execute("UPDATE staging_items SET superseded_by=? WHERE id=?", (new_id, old["id"]))
        _audit(c, old["id"], "supersede", actor, before=_public(old), after={"superseded_by": new_id})
    return item_out(row), True


def stage_from_correction(c: sqlite3.Connection, corr_id: int, *, actor: dict, to_tm: bool = True,
                          propose: dict | None = None) -> dict:
    """Replacement for feedback.promote_correction on the correction endpoints: the caption fix
    itself is already a new human version (record_correction); TM / glossary only get a pending item."""
    row = c.execute("SELECT target_type, target_id, segment_id, after_text, status FROM corrections WHERE id=?",
                    (corr_id,)).fetchone()
    if not row:
        raise StagingError("找不到這筆修正", code="not_found", status=404)
    ttype, target_id, seg, after, status = row
    if status == "rejected":
        raise StagingError("這筆修正已被駁回", code="conflict", status=409)
    out: dict = {"tm_staging_id": None, "term_staging_id": None}
    if ttype == "translation" and to_tm and seg:
        lang_row = c.execute("SELECT tgt_lang FROM translations WHERE id=?", (target_id,)).fetchone()
        lang = lang_row[0] if lang_row and lang_row[0] in TGT_LANGS else "en"
        zh = c.execute("SELECT text FROM transcripts WHERE segment_id=? AND is_current=1", (seg,)).fetchone()
        if zh and zh[0]:
            merged = c.execute("SELECT 1 FROM translations WHERE segment_id=? AND status='merged' LIMIT 1",
                               (seg,)).fetchone()
            item, _ = stage(c, kind="tm", tgt_lang=lang, src_text=zh[0], tgt_text=after, actor=actor,
                            correction_id=corr_id, segment_id=seg,
                            # 全站 D4: a fix on a merged MT line may also cover the earlier zh.
                            note="合併句：核准前請確認中文涵蓋範圍" if merged else None)
            out["tm_staging_id"] = item["id"]
    if propose:
        lang = str(propose.get("tgt_lang") or "en")
        item, _ = stage(c, kind="term", tgt_lang=lang, src_text=propose.get("zh"),
                        tgt_text=propose.get("en") if lang == "en" else (propose.get("target") or propose.get("en")),
                        aliases=propose.get("aliases"), actor=actor, correction_id=corr_id, segment_id=seg,
                        note=f"from correction {corr_id}")
        out["term_staging_id"] = item["id"]
    return out


# ---------------------------------------------------------------- decisions
def _write_tm(c, row) -> tuple[int, list]:
    lang, zh, tgt = row["tgt_lang"], row["src_text"], row["tgt_text"]
    if lang == "en":
        from app.translate import validate_caption_en
        if validate_caption_en(tgt, zh=zh) is None:
            raise StagingError("這筆譯文不是單行純英文，不能核准")
    tm_id = add_unit(c, zh, tgt, tgt=lang, origin="approved", quality=TM_APPROVED, room_id=row["room_id"],
                     segment_id=row["segment_id"])
    # add_unit keeps a disabled (1) or pending (2) unit's quality on conflict; an explicit approval wins.
    c.execute("UPDATE tm_units SET quality=?, updated_at=unixepoch('subsec') WHERE id=?", (TM_APPROVED, tm_id))
    # The approved line must be the one served: other lines for the same source drop below it.
    c.execute("UPDATE tm_units SET quality=4, updated_at=unixepoch('subsec') "
              "WHERE src_hash=? AND tgt_lang=? AND id<>? AND quality>=5", (src_hash(zh, lang), lang, tm_id))
    if row["correction_id"] is not None:
        c.execute("UPDATE corrections SET promoted_tm_id=? WHERE id=?", (tm_id, row["correction_id"]))
    return tm_id, snapshot(c, "tm", lang, zh)


def _write_term(c, row, actor: dict) -> tuple[int, dict, int]:
    lang, zh, tgt = row["tgt_lang"], row["src_text"], row["tgt_text"]
    aliases = json.loads(row["aliases"] or "[]")
    gid = glossary_id_for(c, lang)
    cur = c.execute("SELECT id, en, aliases, locked FROM glossary_terms WHERE glossary_id=? AND zh=?", (gid, zh)).fetchone()
    if cur and cur[3] and cur[1] != tgt:
        raise StagingError("這個詞已鎖定；請先在詞表解除鎖定再核准", code="term_locked", status=409, term_id=cur[0])
    if cur is None:
        c.execute("INSERT INTO glossary_terms(glossary_id, zh, en, aliases, locked, source, status, note) "
                  "VALUES (?,?,?,?,0,?, 'active', ?)",
                  (gid, zh, tgt, json.dumps(aliases, ensure_ascii=False),
                   "correction" if row["correction_id"] is not None else "manual", row["note"]))
        term_id = int(c.execute("SELECT last_insert_rowid()").fetchone()[0])
    else:
        term_id = int(cur[0])
        merged = json.loads(cur[2] or "[]")
        for a in aliases:
            if a not in merged and len(merged) < 8:
                merged.append(a)
        c.execute("UPDATE glossary_terms SET en=?, aliases=?, status='active', rev=rev+1, updated_at=unixepoch('subsec') "
                  "WHERE id=?", (tgt, json.dumps(merged, ensure_ascii=False), term_id))
    c.execute("UPDATE glossaries SET version=version+1, updated_at=unixepoch('subsec') WHERE id=?", (gid,))
    version = int(c.execute("SELECT version FROM glossaries WHERE id=?", (gid,)).fetchone()[0])
    c.execute("INSERT INTO events(kind, actor_id, payload) VALUES ('glossary.active', ?, ?)",
              (actor.get("user_id"), json.dumps({"term_id": term_id, "glossary_version": version, "via": "staging"})))
    if row["correction_id"] is not None:
        c.execute("UPDATE corrections SET promoted_term_id=? WHERE id=?", (term_id, row["correction_id"]))
    return term_id, snapshot(c, "term", lang, zh), version


def approve(c: sqlite3.Connection, item_id: int, *, actor: dict, if_match: str | None,
            override_conflict: bool = False, note: str | None = None) -> dict:
    row = get_row(c, item_id)
    check_etag(row, if_match)
    _transition(row, "approved")
    note = _clean(note, "note", 0, 500) or None if note is not None else None
    current = snapshot(c, row["kind"], row["tgt_lang"], row["src_text"])
    changed = _sig(current) != row["base_sig"]
    if changed and not override_conflict:
        raise StagingError("送審後目標條目已被別人修改；請檢視後重新送審（rebase）或帶 override_conflict",
                           code="target_changed", status=409, base=json.loads(row["base_json"]), current=current,
                           etag=item_etag(row["id"], row["rev"]))
    result: dict = {"kind": row["kind"]}
    if row["kind"] == "tm":
        target_id, after = _write_tm(c, row)
    else:
        target_id, after, version = _write_term(c, row, actor)
        result["glossary_version"] = version
        # No automatic live push: the room changes only through the diff + if_room_version flow.
        result["live_push"] = {"pushed": False, "how": "POST /admin/api/v1/rooms/{room_id}/glossary/push "
                                                        "先 dry_run 看差異，再帶 if_room_version 推送"}
    c.execute("UPDATE staging_items SET state='approved', rev=rev+1, decided_by=?, decided_via=?, "
              "decided_at=unixepoch('subsec'), decision_note=?, target_id=?, updated_at=unixepoch('subsec') WHERE id=?",
              (actor.get("user_id"), actor.get("via"), note, target_id, row["id"]))
    audit_note = "override_conflict" + (f": {note}" if note else "") if changed else note
    _audit(c, row["id"], "approve", actor, before=current, after=after, note=audit_note)
    result.update({"item": item_out(get_row(c, row["id"])), "target_id": target_id, "target": after,
                   "override_used": bool(changed)})
    return result


def reject(c: sqlite3.Connection, item_id: int, *, actor: dict, if_match: str | None, reason) -> dict:
    row = get_row(c, item_id)
    check_etag(row, if_match)
    _transition(row, "rejected")
    reason = _clean(reason, "退回原因", 1, 500)
    c.execute("UPDATE staging_items SET state='rejected', rev=rev+1, decided_by=?, decided_via=?, "
              "decided_at=unixepoch('subsec'), decision_note=?, updated_at=unixepoch('subsec') WHERE id=?",
              (actor.get("user_id"), actor.get("via"), reason, row["id"]))
    _audit(c, row["id"], "reject", actor, before=_public(row), after={"state": "rejected"}, note=reason)
    return {"item": item_out(get_row(c, row["id"]))}


def withdraw(c: sqlite3.Connection, item_id: int, *, actor: dict, if_match: str | None) -> dict:
    row = get_row(c, item_id)
    _may_touch(row, actor)
    check_etag(row, if_match)
    _transition(row, "withdrawn")
    c.execute("UPDATE staging_items SET state='withdrawn', rev=rev+1, updated_at=unixepoch('subsec') WHERE id=?",
              (row["id"],))
    _audit(c, row["id"], "withdraw", actor, before=_public(row), after={"state": "withdrawn"})
    return {"item": item_out(get_row(c, row["id"]))}


def edit(c: sqlite3.Connection, item_id: int, *, actor: dict, if_match: str | None, body: dict) -> dict:
    row = get_row(c, item_id)
    _may_touch(row, actor)
    check_etag(row, if_match)
    _transition(row, "pending")
    allowed = {"tgt_text", "aliases", "note", "rebase"}
    extra = set(body) - allowed
    if extra or not (set(body) & allowed):
        raise StagingError(f"只能修改 {sorted(allowed)}")
    if "rebase" in body and not isinstance(body["rebase"], bool):
        raise StagingError("rebase 必須是 true/false")
    v = validate(row["kind"], row["tgt_lang"], row["src_text"], body.get("tgt_text", row["tgt_text"]),
                 body.get("aliases", json.loads(row["aliases"] or "[]")) if row["kind"] == "term" else None,
                 body.get("note", row["note"]))
    base_json, base_sig = row["base_json"], row["base_sig"]
    if body.get("rebase") is True:
        base = snapshot(c, row["kind"], row["tgt_lang"], row["src_text"])
        base_json, base_sig = _dumps(base), _sig(base)
    c.execute("UPDATE staging_items SET tgt_text=?, aliases=?, note=?, base_json=?, base_sig=?, rev=rev+1, "
              "updated_at=unixepoch('subsec') WHERE id=?",
              (v["tgt_text"], json.dumps(v["aliases"], ensure_ascii=False), v["note"], base_json, base_sig, row["id"]))
    new = get_row(c, row["id"])
    _audit(c, row["id"], "edit", actor, before=_public(row), after={**_public(new), "rebased": body.get("rebase") is True})
    return item_out(new)


def detail(c: sqlite3.Connection, item_id: int) -> dict:
    row = get_row(c, item_id)
    out = item_out(row)
    current = snapshot(c, row["kind"], row["tgt_lang"], row["src_text"])
    out["current"] = current
    out["conflict"] = row["state"] == "pending" and _sig(current) != row["base_sig"]
    out["diff"] = {"before": current, "after": {"tgt_text": row["tgt_text"], "aliases": json.loads(row["aliases"])}}
    out["audit"] = [{**dict(a), "before": json.loads(a["before_json"]) if a["before_json"] else None,
                     "after": json.loads(a["after_json"]) if a["after_json"] else None}
                    for a in c.execute("SELECT id, action, actor_id, actor_via, at, before_json, after_json, note "
                                       "FROM staging_audit WHERE item_id=? ORDER BY id", (row["id"],))]
    for a in out["audit"]:
        a.pop("before_json", None)
        a.pop("after_json", None)
    return out


def bulk_approve(c: sqlite3.Connection, items: list, *, actor: dict, override_conflict: bool = False) -> list[dict]:
    """Each item is approved inside its own SAVEPOINT: a failing item rolls back only itself."""
    results = []
    for n, it in enumerate(items):
        sp = f"staging_bulk_{n}"
        c.execute(f"SAVEPOINT {sp}")
        try:
            out = approve(c, it["id"], actor=actor, if_match=it["etag"], override_conflict=override_conflict)
            c.execute(f"RELEASE {sp}")
            results.append({"id": it["id"], "ok": True, "status": 200, "etag": out["item"]["etag"],
                            "target_id": out["target_id"], **({"glossary_version": out["glossary_version"]}
                                                              if "glossary_version" in out else {})})
        except StagingError as exc:
            c.execute(f"ROLLBACK TO {sp}")
            c.execute(f"RELEASE {sp}")
            results.append({"id": it["id"], "ok": False, "status": exc.status, "code": exc.code, "detail": exc.detail,
                            **{k: v for k, v in exc.extra.items() if k in ("etag", "state", "term_id")}})
    return results


# ---------------------------------------------------------------- HTTP
def build_router(ctx) -> APIRouter:
    """ctx: api, need, conn, read_json, Problem, enc_cursor, dec_cursor, check_room_id, idem_key,
    idem_replay, idem_store (all from app.admin.server.create_admin_app)."""
    need, conn, read_json, Problem = ctx.need, ctx.conn, ctx.read_json, ctx.Problem
    router = APIRouter(prefix=f"{ctx.api}/staging", tags=["staging"])

    def run(fn):
        try:
            return fn()
        except StagingError as exc:
            raise Problem(exc.status, exc.code, exc.detail, **exc.extra)

    async def in_thread(fn):
        return await asyncio.to_thread(run, fn)

    def caller(user) -> str:
        return f"{user.get('via')}:{user.get('user_id')}"

    @router.get("")
    def list_items(state: str = "pending", kind: str | None = None, tgt_lang: str | None = None,
                   room_id: str | None = None, segment_id: str | None = None, cursor: str | None = None,
                   limit: int = Query(50, ge=1, le=200), user=Depends(need("editor"))):
        if state not in STATES + ("all",):
            raise Problem(422, "invalid", f"state 只能是 {', '.join(STATES)} 或 all")
        if kind is not None and kind not in KINDS:
            raise Problem(422, "invalid", "kind 只能是 tm 或 term")
        if tgt_lang is not None and tgt_lang not in TGT_LANGS:
            raise Problem(422, "invalid", "tgt_lang 只能是 en 或 ja")
        if segment_id is not None and not (1 <= len(segment_id) <= 200):
            raise Problem(422, "invalid", "segment_id 長度不正確")
        after = ctx.dec_cursor(cursor, 2)
        if after and (isinstance(after[0], str) or not isinstance(after[1], int)):
            raise Problem(400, "bad_cursor", "cursor 欄位型別不符")
        sql, args = "SELECT * FROM staging_items WHERE 1=1", []
        if state != "all":
            sql += " AND state=?"; args.append(state)
        for col, val in (("kind", kind), ("tgt_lang", tgt_lang), ("segment_id", segment_id)):
            if val is not None:
                sql += f" AND {col}=?"; args.append(val)
        if room_id is not None:
            sql += " AND room_id=?"; args.append(ctx.check_room_id(room_id))
        if after:
            sql += " AND (created_at < ? OR (created_at = ? AND id < ?))"
            args += [after[0], after[0], after[1]]
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(limit + 1)
        with conn() as c:
            rows = c.execute(sql, args).fetchall()
            items = [item_out(r) for r in rows[:limit]]
            for it, r in zip(items, rows):
                if r["state"] == "pending":
                    it["conflict"] = _sig(snapshot(c, r["kind"], r["tgt_lang"], r["src_text"])) != r["base_sig"]
        nxt = ctx.enc_cursor([rows[limit - 1]["created_at"], rows[limit - 1]["id"]]) if len(rows) > limit else None
        return {"items": items, "next": nxt}

    @router.get("/{item_id}")
    def get_item(item_id: int, user=Depends(need("editor"))):
        with conn() as c:
            out = run(lambda: detail(c, item_id))
        return JSONResponse(out, headers={"ETag": out["etag"]})

    @router.post("", status_code=201)
    async def create_item(request: Request, user=Depends(need("editor"))):
        key = ctx.idem_key(request)
        body, raw = await read_json(request)
        route = f"POST /staging|{caller(user)}"
        unknown = set(body) - {"kind", "tgt_lang", "src_text", "tgt_text", "aliases", "note", "segment_id", "room_id"}
        if unknown:
            raise Problem(422, "invalid", f"不認得的欄位：{sorted(unknown)}")
        seg = body.get("segment_id")
        if seg is not None and (not isinstance(seg, str) or not (1 <= len(seg) <= 200)):
            raise Problem(422, "invalid", "segment_id 格式不正確")
        room = body.get("room_id")
        if room is not None:
            room = ctx.check_room_id(str(room))

        def work():
            with conn(write=True) as c:
                replay = ctx.idem_replay(c, key, route, raw)
                if replay is not None:
                    return replay
                item, created = stage(c, kind=str(body.get("kind") or ""), tgt_lang=str(body.get("tgt_lang") or "en"),
                                      src_text=body.get("src_text"), tgt_text=body.get("tgt_text"),
                                      aliases=body.get("aliases"), note=body.get("note"), actor=user,
                                      segment_id=seg, room_id=room)
                status = 201 if created else 200
                headers = {"Location": f"{ctx.api}/staging/{item['id']}", "ETag": item["etag"]}
                ctx.idem_store(c, key, route, raw, status, item, headers)
                return JSONResponse(item, status_code=status, headers=headers)
        return await in_thread(work)

    @router.patch("/{item_id}")
    async def patch_item(item_id: int, request: Request, user=Depends(need("editor"))):
        body, _ = await read_json(request)

        def work():
            with conn(write=True) as c:
                out = edit(c, item_id, actor=user, if_match=request.headers.get("if-match"), body=body)
            return JSONResponse(out, headers={"ETag": out["etag"]})
        return await in_thread(work)

    def _flag(body: dict, name: str) -> bool:
        v = body.get(name, False)
        if not isinstance(v, bool):
            raise Problem(422, "invalid", f"{name} 必須是 true/false")
        return v

    @router.post("/{item_id}/approve")
    async def approve_item(item_id: int, request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)
        override = _flag(body, "override_conflict")

        def work():
            with conn(write=True) as c:
                out = approve(c, item_id, actor=user, if_match=request.headers.get("if-match"),
                              override_conflict=override, note=body.get("note"))
            return JSONResponse(out, headers={"ETag": out["item"]["etag"]})
        return await in_thread(work)

    @router.post("/{item_id}/reject")
    async def reject_item(item_id: int, request: Request, user=Depends(need("admin"))):
        body, _ = await read_json(request)

        def work():
            with conn(write=True) as c:
                out = reject(c, item_id, actor=user, if_match=request.headers.get("if-match"), reason=body.get("reason"))
            return JSONResponse(out, headers={"ETag": out["item"]["etag"]})
        return await in_thread(work)

    @router.post("/{item_id}/withdraw")
    async def withdraw_item(item_id: int, request: Request, user=Depends(need("editor"))):
        def work():
            with conn(write=True) as c:
                out = withdraw(c, item_id, actor=user, if_match=request.headers.get("if-match"))
            return JSONResponse(out, headers={"ETag": out["item"]["etag"]})
        return await in_thread(work)

    @router.post("/bulk-approve")
    async def bulk(request: Request, user=Depends(need("admin"))):
        key = ctx.idem_key(request)
        body, raw = await read_json(request)
        route = f"POST /staging/bulk-approve|{caller(user)}"
        items = body.get("items")
        if not isinstance(items, list) or not (1 <= len(items) <= BULK_MAX):
            raise Problem(422, "invalid", f"items 需 1–{BULK_MAX} 筆 {{id, etag}}")
        clean = []
        for it in items:
            if (not isinstance(it, dict) or isinstance(it.get("id"), bool) or not isinstance(it.get("id"), int)
                    or not isinstance(it.get("etag"), str) or not it["etag"]):
                raise Problem(422, "invalid", "每筆需要 {id: 整數, etag: 字串}")
            clean.append({"id": it["id"], "etag": it["etag"]})
        if len({it["id"] for it in clean}) != len(clean):
            raise Problem(422, "invalid", "items 裡有重複的 id")
        override = _flag(body, "override_conflict")

        def work():
            with conn(write=True) as c:
                replay = ctx.idem_replay(c, key, route, raw)
                if replay is not None:
                    return replay
                results = bulk_approve(c, clean, actor=user, override_conflict=override)
                out = {"results": results, "approved": sum(r["ok"] for r in results),
                       "failed": sum(not r["ok"] for r in results)}
                ctx.idem_store(c, key, route, raw, 200, out, {})
            return JSONResponse(out)
        return await in_thread(work)

    return router


def build_page_router(ctx) -> APIRouter:
    """The review page: /admin/staging (+ its own JS/CSS). Static files only; data comes from the
    API with the session cookie + X-Zen-CSRF, like /admin. Same CSP (script-src 'self', no inline)."""
    router = APIRouter()

    def page(name: str, media_type: str | None = None):
        return FileResponse(STATIC / name, media_type=media_type, headers={"Cache-Control": "no-store"})

    @router.get("/admin/staging")
    def staging_page():
        return page("staging.html", "text/html")

    @router.get("/admin/staging/{name}")
    def staging_asset(name: str):
        if name not in PAGE_FILES:
            raise ctx.Problem(404, "not_found", "找不到")
        return page(name, PAGE_FILES[name])

    return router


def register(app, ctx) -> APIRouter:
    router = build_router(ctx)
    app.include_router(router)
    app.include_router(build_page_router(ctx))
    return router
