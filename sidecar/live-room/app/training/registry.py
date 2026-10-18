"""講者名冊與同意紀錄。撤回同意 = 刪掉這位講者所有音檔、文字配對、含他聲音的匯出與匯入原始檔。"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import sqlite3
import time
from datetime import date
from pathlib import Path

from app.training import db as tdb

CONSENT_VERSION = "zen-train-consent-v1"
CONSENT_TEXT = (
    "我同意淡江禪學社把我的錄音與逐字稿，只存放在社團本機電腦（不上傳雲端），"
    "用來改進本機語音辨識（ASR）與翻譯（MT）模型。我可以隨時撤回；撤回後，"
    "我的錄音會從本機刪除，之後的資料集匯出都不會包含我。")

WITHDRAWN_LABEL = "已撤回"
# 不收學號、電話、Email：暱稱裡連續 6 個以上數字、@、或像電話的格式一律拒絕
_PII = re.compile(r"\d{6,}|@|\+?\d[\d\- ]{7,}\d|[A-Za-z]\d{8,}")


class RegistryError(ValueError):
    def __init__(self, detail: str, code: str = "invalid", status: int = 422):
        super().__init__(detail)
        self.detail, self.code, self.status = detail, code, status


def clean_label(label) -> str:
    if not isinstance(label, str) or not label.strip():
        raise RegistryError("請填顯示名稱（暱稱即可）")
    label = " ".join(label.split())
    if len(label) > 24:
        raise RegistryError("顯示名稱最多 24 字")
    if _PII.search(label):
        raise RegistryError("顯示名稱不可含學號、電話或 Email，請用暱稱")
    return label


def _clean_short(v, name: str, n: int) -> str | None:
    if v is None or v == "":
        return None
    if not isinstance(v, str) or len(v) > n:
        raise RegistryError(f"{name} 最多 {n} 字")
    if _PII.search(v):
        raise RegistryError(f"{name} 不可含學號、電話或 Email")
    return v.strip()


def new_speaker(c: sqlite3.Connection, label, *, accent_note=None, actor: str = "admin") -> dict:
    label = clean_label(label)
    sid = "spk_" + secrets.token_hex(4)
    c.execute("INSERT INTO speakers(id, label, accent_note, created_at) VALUES (?,?,?,?)",
              (sid, label, _clean_short(accent_note, "口音備註", 40), time.time()))
    tdb.audit(c, "speaker_create", speaker_id=sid, actor=actor)
    return get_speaker(c, sid)


def get_speaker(c: sqlite3.Connection, sid: str) -> dict:
    r = c.execute("SELECT * FROM speakers WHERE id=?", (sid,)).fetchone()
    if not r:
        raise RegistryError("找不到這位講者", code="not_found", status=404)
    out = dict(r)
    out["consent"] = active_consent(c, sid)
    secs = c.execute("SELECT origin, COALESCE(SUM(duration_s),0), COUNT(*) FROM clips WHERE speaker_id=? "
                     "AND audio_path IS NOT NULL AND status!='rejected' GROUP BY origin", (sid,)).fetchall()
    out["minutes"] = {o: round(s / 60, 2) for o, s, _ in secs}
    out["clips"] = {o: n for o, _, n in secs}
    out["minutes_total"] = round(sum(s for _, s, _ in secs) / 60, 2)
    return out


def list_speakers(c: sqlite3.Connection, include_withdrawn: bool = False) -> list[dict]:
    q = "SELECT id FROM speakers" + ("" if include_withdrawn else " WHERE withdrawn_at IS NULL") + " ORDER BY created_at"
    return [get_speaker(c, r[0]) for r in c.execute(q).fetchall()]


def active_consent(c: sqlite3.Connection, sid: str) -> dict | None:
    r = c.execute("SELECT * FROM consents WHERE speaker_id=? AND revoked_at IS NULL ORDER BY granted_at DESC LIMIT 1",
                  (sid,)).fetchone()
    return dict(r) if r else None


def has_consent(c: sqlite3.Connection, sid: str | None, scope: str = "asr", *, live: bool = False) -> bool:
    if not sid:
        return False
    sp = c.execute("SELECT withdrawn_at FROM speakers WHERE id=?", (sid,)).fetchone()
    if not sp or sp[0] is not None:
        return False
    cons = active_consent(c, sid)
    if not cons or not cons["local_only"]:
        return False
    if live and not cons["harvest_live"]:
        return False
    return bool(cons["scope_asr"] if scope == "asr" else cons["scope_mt"])


def record_consent(c: sqlite3.Connection, sid: str, *, scope_asr: bool, scope_mt: bool, harvest_live: bool = False,
                   consent_date: str | None = None, recorded_by: str, confirm_text: bool) -> dict:
    sp = get_speaker(c, sid)
    if sp["withdrawn_at"] is not None:
        raise RegistryError("這位講者已撤回；請另建一筆新講者並重新取得同意", code="withdrawn", status=409)
    if confirm_text is not True:
        raise RegistryError("需要講者本人確認同意書內容（confirm_text=true）")
    if not (scope_asr or scope_mt):
        raise RegistryError("至少勾選一個用途：語音辨識或翻譯")
    d = consent_date or date.today().isoformat()
    try:
        date.fromisoformat(d)
    except ValueError:
        raise RegistryError("日期格式需為 YYYY-MM-DD")
    by = _clean_short(recorded_by, "紀錄人", 40)
    if not by:
        raise RegistryError("請填紀錄人（主持人暱稱）")
    now = time.time()
    c.execute("UPDATE consents SET revoked_at=? WHERE speaker_id=? AND revoked_at IS NULL", (now, sid))
    c.execute("INSERT INTO consents(speaker_id, scope_asr, scope_mt, local_only, harvest_live, consent_version, "
              "consent_date, recorded_by, granted_at) VALUES (?,?,?,1,?,?,?,?,?)",
              (sid, int(bool(scope_asr)), int(bool(scope_mt)), int(bool(harvest_live)), CONSENT_VERSION, d, by, now))
    tdb.audit(c, "consent_grant", speaker_id=sid, actor=by, scope_asr=bool(scope_asr), scope_mt=bool(scope_mt),
              harvest_live=bool(harvest_live))
    return get_speaker(c, sid)


def _unlink(root: Path, relpath: str | None) -> int:
    if not relpath:
        return 0
    try:
        p = tdb.absolute(root, relpath)
    except ValueError:
        return 0
    if p.exists():
        p.unlink()
        return 1
    return 0


def _remove_export_dir(root: Path, eid: str, d: str) -> int:
    """Exports may live outside the training dir (CLI --out); delete only a dir whose manifest names eid."""
    p = Path(d)
    if not p.is_absolute():
        try:
            p = tdb.absolute(root, d)
        except ValueError:
            return 0
    man = p / "manifest.json"
    try:
        if json.loads(man.read_text(encoding="utf-8")).get("export_id") != eid:
            return 0
    except (OSError, ValueError):
        return 0
    n = sum(1 for f in p.rglob("*") if f.is_file())
    shutil.rmtree(p, ignore_errors=True)
    return n


def withdraw(c: sqlite3.Connection, root: Path, sid: str, *, actor: str = "admin") -> dict:
    """Revoke consent and delete everything derived from this speaker. Idempotent.

    Deletes: every clip WAV and row of the speaker, MT pairs from their speech, their studio/harvest
    recordings (dirs incl. full transcripts), the source audio of imports that contain them, and every
    local export dir that included them. Keeps only an audit row without the nickname."""
    root = Path(root)
    get_speaker(c, sid)
    now = time.time()
    files = 0
    c.execute("BEGIN IMMEDIATE")
    try:
        c.execute("UPDATE consents SET revoked_at=? WHERE speaker_id=? AND revoked_at IS NULL", (now, sid))
        c.execute("UPDATE class_sessions SET stopped_at=?, stop_reason='withdrawn' WHERE speaker_id=? AND stopped_at IS NULL", (now, sid))
        c.execute("UPDATE room_collect SET collect=0, speaker_id=NULL WHERE speaker_id=?", (sid,))
        c.execute("DELETE FROM speaker_profiles WHERE speaker_id=?", (sid,))
        clip_rows = c.execute("SELECT id, audio_path, recording_id FROM clips WHERE speaker_id=?", (sid,)).fetchall()
        recs = {r["recording_id"] for r in clip_rows}
        recs |= {r[0] for r in c.execute("SELECT id FROM recordings WHERE speaker_id=?", (sid,)).fetchall()}
        for r in clip_rows:
            files += _unlink(root, r["audio_path"])
        c.execute("DELETE FROM mt_pairs WHERE speaker_id=? OR clip_id IN (SELECT id FROM clips WHERE speaker_id=?)",
                  (sid, sid))
        c.execute("DELETE FROM clips WHERE speaker_id=?", (sid,))
        dirs, keep_imports = [], []
        for rid in recs:
            rec = c.execute("SELECT * FROM recordings WHERE id=?", (rid,)).fetchone()
            if not rec:
                continue
            others = c.execute("SELECT COUNT(*) FROM clips WHERE recording_id=? AND speaker_id IS NOT NULL",
                               (rid,)).fetchone()[0]
            if rec["kind"] == "import":
                # the full source audio contains this speaker's voice: delete it even if others remain
                files += _unlink(root, rec["source_audio"])
                c.execute("UPDATE recordings SET source_audio=NULL WHERE id=?", (rid,))
                c.execute("UPDATE clips SET status='needs_review' WHERE recording_id=? AND audio_path IS NULL", (rid,))
            if rec["kind"] != "import" or not others:
                dirs.append(rec["dir"])
                c.execute("DELETE FROM recordings WHERE id=?", (rid,))
            else:
                keep_imports.append(rid)
        exports = [r for r in c.execute("SELECT id, dir, speakers FROM exports WHERE deleted_at IS NULL").fetchall()
                   if sid in json.loads(r["speakers"] or "[]")]
        export_dirs = []
        for e in exports:
            export_dirs.append((e["id"], e["dir"]))
            c.execute("UPDATE exports SET deleted_at=?, deleted_reason='speaker_withdrawn' WHERE id=?", (now, e["id"]))
        c.execute("UPDATE speakers SET withdrawn_at=COALESCE(withdrawn_at, ?), label=?, accent_note=NULL WHERE id=?",
                  (now, WITHDRAWN_LABEL, sid))
        tdb.audit(c, "consent_withdraw", speaker_id=sid, actor=actor, clips=len(clip_rows), exports=len(exports))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    from app.training import recordings as trec
    for rid in keep_imports:
        trec.write_transcript(c, root, rid)      # rewritten without the withdrawn speaker's lines
    for d in dirs:
        try:
            p = tdb.absolute(root, d)
        except ValueError:
            continue
        if p.exists() and p != root.resolve():
            files += sum(1 for f in p.rglob("*") if f.is_file())
            shutil.rmtree(p, ignore_errors=True)
    for kind in ("studio", "harvest", "import"):
        shutil.rmtree(root / "recordings" / kind / sid, ignore_errors=True)
    for eid, d in export_dirs:
        files += _remove_export_dir(root, eid, d)
    return {"speaker_id": sid, "clips_deleted": len(clip_rows), "exports_deleted": len(exports), "files_deleted": files}
