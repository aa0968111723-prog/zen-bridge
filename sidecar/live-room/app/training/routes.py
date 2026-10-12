"""後台 API：/admin/api/v1/training/*，頁面：/admin/training。由 app/admin/server.py 掛上。

權限：講者名冊／同意／撤回／匯出 = admin；錄音、審核、匯入 = editor；開關現場收錄 = host 以上。
寫入都走後台既有的 Cookie + CSRF（或 Bearer）檢查。資料只存在本機 training 資料夾。
"""
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.training import audioq, capture, dataset, derive, harvest, importer, prompts, recordings, registry, review, studio
from app.training import db as tdb

STATIC = Path(__file__).with_name("static")
PAGE_FILES = {"training.js": "text/javascript", "training.css": "text/css", "rec-worklet.js": "text/javascript",
              "training_logic.js": "text/javascript"}
REC_FILES = set(recordings.TRANSCRIPT_FILES) | {"recording.wav", "live.jsonl"}
_UPLOAD_NAME = re.compile(r"^[\w\-. ()\u4e00-\u9fff]{1,120}$")
MAX_IMPORT_BYTES = 2 * 1024 ** 3


def register(app, ctx) -> None:
    api, need, Problem = ctx.api, ctx.need, ctx.Problem
    root_fn = getattr(ctx, "training_root", None) or tdb.training_dir
    zen_db = getattr(ctx, "db_path", None)
    r = APIRouter(prefix=f"{api}/training")

    def actor(user) -> str:
        return f"{user.get('via')}:{user.get('user_id') or 'owner'}"

    def run(fn, write=True):
        root = Path(root_fn())
        c = tdb.connect(root)
        try:
            if write:
                c.execute("BEGIN IMMEDIATE")
            try:
                out = fn(c, root)
                if write:
                    c.execute("COMMIT")
                return out
            except BaseException:
                if write:
                    c.execute("ROLLBACK")
                raise
        except registry.RegistryError as exc:
            raise Problem(exc.status, exc.code, exc.detail)
        except audioq.AudioFormatError as exc:
            raise Problem(422, "bad_audio", str(exc))
        except ValueError as exc:
            raise Problem(422, "invalid", str(exc))
        finally:
            c.close()

    async def th(fn, write=True):
        return await asyncio.to_thread(run, fn, write)

    async def body(request):
        b, _ = await ctx.read_json(request)
        return b

    # ------------------------------------------------------------ overview / speakers / consent
    @r.get("")
    async def overview(user=Depends(need("viewer"))):
        def f(c, root):
            return {"speakers": registry.list_speakers(c), "consent_text": registry.CONSENT_TEXT,
                    "consent_version": registry.CONSENT_VERSION, "root": str(root),
                    "queue": dict(c.execute("SELECT status, COUNT(*) FROM clips GROUP BY status").fetchall()),
                    "classes": harvest.sessions(c, limit=10), "lecturers": harvest.lecturers(c),
                    "rooms": [dict(x) for x in c.execute("SELECT * FROM room_collect ORDER BY updated_at DESC")],
                    "settings": derive.get_settings(c), "disk": derive.disk(c, root),
                    "mt_pairs": c.execute("SELECT COUNT(*) FROM mt_pairs").fetchone()[0]}
        return await th(f, write=False)

    @r.post("/speakers", status_code=201)
    async def add_speaker(request: Request, user=Depends(need("admin"))):
        b = await body(request)
        return await th(lambda c, root: registry.new_speaker(c, b.get("label"), accent_note=b.get("accent_note"),
                                                              actor=actor(user)))

    @r.post("/speakers/quick", status_code=201)
    async def quick_speaker(request: Request, user=Depends(need("admin"))):
        """One form, one click: nickname + consent (defaults: ASR + MT + live class recording)."""
        b = await body(request)

        def f(c, root):
            sp = registry.new_speaker(c, b.get("label"), accent_note=b.get("accent_note"), actor=actor(user))
            registry.record_consent(c, sp["id"], scope_asr=b.get("scope_asr", True) is not False,
                                    scope_mt=b.get("scope_mt", True) is not False,
                                    harvest_live=b.get("harvest_live", True) is not False,
                                    consent_date=b.get("consent_date"), recorded_by=b.get("recorded_by") or "",
                                    confirm_text=b.get("confirm_text") is True)
            return registry.get_speaker(c, sp["id"])
        return await th(f)

    @r.get("/speakers/{sid}")
    async def get_speaker(sid: str, user=Depends(need("viewer"))):
        def f(c, root):
            sp = registry.get_speaker(c, sid)
            sp["prompts"] = prompts.progress(c, sid)
            return sp
        return await th(f, write=False)

    @r.post("/speakers/{sid}/consent")
    async def consent(sid: str, request: Request, user=Depends(need("admin"))):
        b = await body(request)
        return await th(lambda c, root: registry.record_consent(
            c, sid, scope_asr=bool(b.get("scope_asr")), scope_mt=bool(b.get("scope_mt")),
            harvest_live=bool(b.get("harvest_live")), consent_date=b.get("consent_date"),
            recorded_by=b.get("recorded_by") or "", confirm_text=b.get("confirm_text") is True))

    @r.post("/speakers/{sid}/withdraw")
    async def withdraw(sid: str, request: Request, user=Depends(need("admin"))):
        b = await body(request)
        if b.get("confirm") is not True:
            raise Problem(422, "confirm_required", "撤回會永久刪除這位講者的錄音，請帶 confirm=true")
        out = await th(lambda c, root: registry.withdraw(c, root, sid, actor=actor(user)), write=False)
        capture.refresh()          # same process (tests / single-process runs); the live server re-reads within 3 s
        return out

    # ------------------------------------------------------------ prompts
    @r.get("/prompts")
    async def list_prompts(speaker_id: str, source: str | None = None, n: int = Query(10, ge=1, le=50),
                           user=Depends(need("viewer"))):
        if source and source not in prompts.SOURCES:
            raise Problem(422, "invalid", "source 不正確")

        def f(c, root):
            if not c.execute("SELECT 1 FROM prompts LIMIT 1").fetchone():
                prompts.sync(c, zen_db)
            return {"prompts": prompts.next_prompts(c, speaker_id, source=source, n=n),
                    "progress": prompts.progress(c, speaker_id)}
        return await th(f)

    @r.post("/prompts/sync")
    async def sync_prompts(user=Depends(need("editor"))):
        return await th(lambda c, root: {"added": prompts.sync(c, zen_db)})

    @r.post("/prompts", status_code=201)
    async def add_prompt(request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: prompts.add_custom(c, b.get("text") or ""))

    # ------------------------------------------------------------ studio
    @r.post("/studio", status_code=201)
    async def studio_start(request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: studio.start(c, root, b.get("speaker_id") or "", title=b.get("title")))

    @r.post("/studio/{rid}/takes")
    async def studio_take(rid: str, request: Request, prompt_id: int | None = None, replace: str | None = None,
                          user=Depends(need("editor"))):
        if (request.headers.get("content-type") or "").split(";")[0].strip() not in ("audio/wav", "audio/x-wav"):
            raise Problem(415, "unsupported_media", "需要 audio/wav（16 kHz 單聲道）")
        raw = await request.body()
        if len(raw) > studio.MAX_TAKE_BYTES:
            raise Problem(413, "too_large", "一句最多 30 秒")
        return await th(lambda c, root: studio.take(c, root, rid, raw, prompt_id=prompt_id, replace=replace))

    @r.post("/studio/{rid}/finish")
    async def studio_finish(rid: str, user=Depends(need("editor"))):
        return await th(lambda c, root: recordings.finish(c, root, rid) or _404())

    @r.delete("/clips/{cid}")
    async def delete_clip(cid: str, user=Depends(need("editor"))):
        return await th(lambda c, root: {"deleted": studio.delete_clip(c, root, cid)})

    def _404():
        raise Problem(404, "not_found", "找不到")

    # ------------------------------------------------------------ recordings / files
    @r.get("/recordings")
    async def list_recordings(speaker_id: str | None = None, user=Depends(need("viewer"))):
        def f(c, root):
            q, a = "SELECT r.*, COUNT(k.id) AS clips, COALESCE(SUM(k.duration_s),0) AS seconds FROM recordings r " \
                   "LEFT JOIN clips k ON k.recording_id=r.id", []
            if speaker_id:
                q += " WHERE r.speaker_id=?"
                a.append(speaker_id)
            return {"recordings": [dict(x) for x in c.execute(q + " GROUP BY r.id ORDER BY r.started_at DESC LIMIT 100", a)]}
        return await th(f, write=False)

    @r.get("/recordings/{rid}/files/{name}")
    async def rec_file(rid: str, name: str, user=Depends(need("viewer"))):
        if name not in REC_FILES and name != "source.wav":
            raise Problem(404, "not_found", "找不到")

        def f(c, root):
            rec = recordings.get(c, rid) or _404()
            if rec["kind"] == "harvest" and name in recordings.TRANSCRIPT_FILES:
                recordings.write_transcript(c, root, rid)          # reflects the latest review decisions
            if name == "source.wav":
                if not rec["source_audio"]:
                    _404()
                return tdb.absolute(root, rec["source_audio"])
            return tdb.absolute(root, f"{rec['dir']}/{name}")
        p = await th(f)
        if not p.exists():
            raise Problem(404, "not_found", "還沒有這個檔案")
        mt = {"wav": "audio/wav", "json": "application/json", "srt": "text/plain", "vtt": "text/vtt",
              "txt": "text/plain", "jsonl": "application/json"}[name.rsplit(".", 1)[1]]
        return FileResponse(p, media_type=mt + ("; charset=utf-8" if not mt.startswith("audio") else ""),
                            headers={"Cache-Control": "no-store"})

    @r.get("/recordings/{rid}/bundle.zip")
    async def rec_bundle(rid: str, user=Depends(need("viewer"))):
        from starlette.background import BackgroundTask

        def f(c, root):
            if not recordings.get(c, rid):
                _404()
            tmp = Path(root) / "tmp"
            tmp.mkdir(exist_ok=True)
            return recordings.bundle(c, root, rid, out_path=tmp / f"{rid}-{time.time_ns()}.zip")
        p = await th(f)
        return FileResponse(p, media_type="application/zip", filename=f"{rid}.zip",
                            headers={"Cache-Control": "no-store"},
                            background=BackgroundTask(lambda: Path(p).unlink(missing_ok=True)))

    @r.post("/recordings/{rid}/speakers")
    async def rec_speakers(rid: str, request: Request, user=Depends(need("editor"))):
        """{tag, rename_to} renames a label everywhere; {tag, speaker_id|null} maps it to the registry."""
        b = await body(request)
        tag = b.get("tag") or ""

        def f(c, root):
            if b.get("rename_to"):
                return recordings.rename_tag(c, root, rid, tag, b["rename_to"])
            if "speaker_id" in b:
                return recordings.map_tag(c, root, rid, tag, b.get("speaker_id") or None)
            raise Problem(422, "invalid", "需要 rename_to 或 speaker_id")
        return await th(f)

    @r.post("/clips/{cid}/speaker")
    async def clip_speaker(cid: str, request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: recordings.set_clip_tag(c, root, cid, b.get("tag") or "") or _404())

    @r.get("/clips/{cid}/audio")
    async def clip_audio(cid: str, user=Depends(need("viewer"))):
        def f(c, root):
            k = recordings.clip(c, cid) or _404()
            if not k["audio_path"]:
                _404()
            return tdb.absolute(root, k["audio_path"])
        p = await th(f, write=False)
        if not p.exists():
            raise Problem(404, "not_found", "音檔不存在")
        return FileResponse(p, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------ class collection (primary path)
    @r.get("/classes")
    async def classes(speaker_id: str | None = None, user=Depends(need("viewer"))):
        return await th(lambda c, root: {"classes": harvest.sessions(c, speaker_id=speaker_id),
                                         "lecturers": harvest.lecturers(c), "pending_derive": derive.pending(c)},
                        write=False)

    @r.post("/classes/room")
    async def class_room(request: Request, user=Depends(need("host"))):
        b = await body(request)
        room = ctx.check_room_id(b.get("room_id") or "")
        return await th(lambda c, root: harvest.set_room(c, room_id=room, speaker_id=b.get("speaker_id") or None,
                                                        collect=b.get("collect") is True, by=actor(user)))

    @r.post("/classes/{cs_id}/stop")
    async def class_stop(cs_id: int, user=Depends(need("host"))):
        return await th(lambda c, root: harvest.stop_session(c, root, cs_id, by=actor(user)) or _404())

    @r.get("/classes/{rid}/batch")
    async def class_batch(rid: str, sort: str = "confidence", show: str = "todo",
                          min_conf: float | None = Query(None, ge=0, le=1), user=Depends(need("viewer"))):
        def f(c, root):
            mc = derive.get_settings(c)["auto_approve_min"] if min_conf is None else min_conf
            out = review.session_batch(c, rid, sort=sort, show=show, min_conf=mc)
            out["min_conf"] = mc
            out["session"] = next((dict(x) for x in c.execute("SELECT * FROM class_sessions WHERE recording_id=?", (rid,))),
                                  None)
            if out["session"] and out["session"]["derive_note"]:
                import json
                out["session"]["derive_note"] = json.loads(out["session"]["derive_note"])
            return out
        return await th(f, write=False)

    @r.post("/classes/{rid}/approve-above")
    async def class_approve_above(rid: str, request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: review.approve_above(c, root, rid, min_conf=float(b.get("min_conf") or 0.85),
                                                            actor=actor(user)))

    @r.post("/classes/{rid}/derive")
    async def class_derive(rid: str, user=Depends(need("editor"))):
        return await th(lambda c, root: derive.derive_recording(c, root, rid), write=False)

    @r.post("/classes/{rid}/delete-raw")
    async def class_delete_raw(rid: str, user=Depends(need("editor"))):
        def f(c, root):
            cs = c.execute("SELECT id FROM class_sessions WHERE recording_id=?", (rid,)).fetchone() or _404()
            return {"freed_bytes": derive.delete_raw(c, root, cs["id"], reason="reviewer")}
        return await th(f)

    @r.post("/clips/{cid}/exclude")
    async def clip_exclude(cid: str, request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: review.set_excluded(c, root, cid, excluded=b.get("excluded") is not False,
                                                           actor=actor(user)))

    @r.get("/stats/hours")
    async def stats_hours(bucket: str = "week", user=Depends(need("viewer"))):
        return await th(lambda c, root: {"bucket": bucket, "speakers": harvest.hours_over_time(c, bucket=bucket)},
                        write=False)

    @r.get("/settings")
    async def get_settings(user=Depends(need("viewer"))):
        return await th(lambda c, root: {**derive.get_settings(c), "disk": derive.disk(c, root)}, write=False)

    @r.post("/settings")
    async def put_settings(request: Request, user=Depends(need("admin"))):
        b = await body(request)
        return await th(lambda c, root: derive.set_settings(c, **{k: b.get(k) for k in derive.DEFAULTS}))

    @r.post("/cleanup")
    async def run_cleanup(user=Depends(need("admin"))):
        return await th(lambda c, root: derive.cleanup(c, root))

    # ------------------------------------------------------------ review queue
    @r.get("/review")
    async def review_list(status: str = "open", speaker_id: str | None = None, origin: str | None = None,
                          limit: int = Query(50, ge=1, le=200), user=Depends(need("viewer"))):
        def f(c, root):
            items = review.queue(c, status=status, speaker_id=speaker_id, origin=origin, limit=limit)
            for it in items:
                if it["segment_id"]:
                    it["ledger"] = review.ledger_lookup(zen_db, it["segment_id"])
            return {"items": items}
        return await th(f, write=False)

    @r.post("/review/{cid}")
    async def review_decide(cid: str, request: Request, user=Depends(need("editor"))):
        b = await body(request)
        out = await th(lambda c, root: review.decide(c, root, cid, action=b.get("action") or "", actor=actor(user),
                                                     text=b.get("text"), mt_text=b.get("mt_text"),
                                                     tgt_lang=b.get("tgt_lang")))
        pair = out.get("mt_pair")
        if pair and b.get("stage_tm", True) and not pair.get("staging_id"):
            out["staging"] = await asyncio.to_thread(_stage_tm, pair, user)
        return out

    def _stage_tm(pair: dict, user) -> dict:
        """Corrected translation -> existing staging queue (admin approves into translation memory)."""
        try:
            from app.admin import staging as zstaging
            with ctx.conn(write=True) as zc:
                seg = pair.get("segment_id")
                if seg and not zc.execute("SELECT 1 FROM segments WHERE id=?", (seg,)).fetchone():
                    seg = None
                item, _ = zstaging.stage(zc, kind="tm", tgt_lang=pair["tgt_lang"], src_text=pair["src_text"],
                                         tgt_text=pair["tgt_text"], actor=user, segment_id=seg,
                                         note="訓練資料審核")
            run(lambda c, root: review.link_staging(c, pair["id"], item["id"]))
            return {"staging_id": item["id"]}
        except Exception as exc:          # staging is a bonus; the training pair is already saved
            return {"error": str(getattr(exc, "detail", exc))[:200]}

    # ------------------------------------------------------------ import existing recording + transcript
    @r.post("/imports/upload", status_code=201)
    async def import_upload(request: Request, name: str, user=Depends(need("editor"))):
        if not _UPLOAD_NAME.match(name) or name.startswith("."):
            raise Problem(422, "invalid", "檔名不正確")
        ext = Path(name).suffix.lower()
        if ext not in importer.AUDIO_EXT | importer.TEXT_EXT:
            raise Problem(415, "unsupported_media", "只接受音檔與 TXT／SRT／VTT／JSON")
        inbox = Path(root_fn()) / "import" / "uploads"
        inbox.mkdir(parents=True, exist_ok=True)
        dst = inbox / name
        part = dst.with_suffix(dst.suffix + ".part")
        size = 0
        try:
            with open(part, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_IMPORT_BYTES:
                        raise Problem(413, "too_large", "檔案太大（上限 2 GB）")
                    f.write(chunk)
            part.replace(dst)
        finally:
            part.unlink(missing_ok=True)
        return {"name": name, "bytes": size}

    @r.post("/imports", status_code=201)
    async def import_create(request: Request, user=Depends(need("editor"))):
        b = await body(request)
        up = Path(root_fn()) / "import" / "uploads"

        def pick(k):
            n = b.get(k) or ""
            if not _UPLOAD_NAME.match(n) or not (up / n).is_file():
                raise Problem(422, "invalid", f"{k} 找不到（請先上傳）")
            return up / n
        a, t = pick("audio"), pick("transcript")
        smap = b.get("speaker_map") or {}
        if not isinstance(smap, dict):
            raise Problem(422, "invalid", "speaker_map 需為物件")

        def f(c, root):
            out = importer.import_recording(c, root, a, t, title=b.get("title"), speaker_map=smap, actor=actor(user))
            a.unlink(missing_ok=True)
            t.unlink(missing_ok=True)
            return out
        return await th(f)

    @r.get("/imports/{rid}")
    async def import_get(rid: str, user=Depends(need("viewer"))):
        return await th(lambda c, root: importer.summary(c, rid), write=False)

    @r.post("/imports/{rid}/map")
    async def import_map(rid: str, request: Request, user=Depends(need("editor"))):
        b = await body(request)
        return await th(lambda c, root: importer.map_speaker(c, root, rid, b.get("tag") or "", b.get("speaker_id") or "",
                                                            actor=actor(user)))

    @r.post("/imports/{rid}/purge-source")
    async def import_purge(rid: str, user=Depends(need("editor"))):
        return await th(lambda c, root: importer.purge_source(c, root, rid))

    # ------------------------------------------------------------ dataset exports
    @r.get("/exports")
    async def exports(user=Depends(need("viewer"))):
        return await th(lambda c, root: {"exports": [dict(x) for x in c.execute(
            "SELECT * FROM exports ORDER BY created_at DESC LIMIT 50")]}, write=False)

    @r.post("/exports", status_code=201)
    async def export_create(request: Request, user=Depends(need("admin"))):
        b = await body(request)
        opts = {k: v for k, v in (b.get("options") or {}).items() if k != "zen_db"}
        opts["zen_db"] = str(zen_db) if zen_db else None
        return await th(lambda c, root: dataset.build(c, root, options=opts, actor=actor(user)))

    @r.delete("/exports/{eid}")
    async def export_delete(eid: str, user=Depends(need("admin"))):
        return await th(lambda c, root: {"deleted": dataset.delete_export(c, root, eid)})

    app.include_router(r)

    # ------------------------------------------------------------ page
    pr = APIRouter()

    @pr.get("/admin/training")
    def page():
        return FileResponse(STATIC / "training.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    @pr.get("/admin/training/{name}")
    def asset(name: str):
        if name not in PAGE_FILES:
            raise Problem(404, "not_found", "找不到")
        return FileResponse(STATIC / name, media_type=PAGE_FILES[name], headers={"Cache-Control": "no-store"})

    app.include_router(pr)
