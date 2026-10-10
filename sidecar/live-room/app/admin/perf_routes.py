"""後台「即時效能」頁（uiux-a）：頁面、靜態檔白名單、metrics 形狀轉接端點。

掛載方式（由整合者在 app/admin/server.py 的 create_admin_app() 裡、LoopbackOnly 之前）::

    from app.admin import perf_routes
    perf_routes.register(app, ctx)     # ctx 就是傳給 zinfo.register(app, ctx) 的同一個 SimpleNamespace

只用到 ctx.api、ctx.need、ctx.live、ctx.Problem，所以這些路由和既有端點一樣在
LoopbackOnly／need(role)／CSP 之後；不放寬任何檢查。也可以用 build_router(...) 拿到 APIRouter
自行 app.include_router()。

路由：
  GET /admin/perf                         → static/perf.html（和 /admin 一樣不需登入；資料端點才需要）
  GET /admin/perf/static/{name}           → 只允許 PERF_STATIC 白名單
  GET {api}/perf/metrics                  → viewer；呼叫 live.metrics()（8780 /api/metrics），只留
                                            latency（A2–A6，app/latency.py）、RTF、積壓欄位
（既有的 /admin/api/v1/live/stream SSE 用 keep 清單過濾掉 latency，所以本頁改用這個端點每 2 秒輪詢。）
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

STATIC = Path(__file__).with_name("static")
PAGE = "perf.html"
PERF_STATIC = {
    "perf.js": "text/javascript; charset=utf-8",
    "perf_logic.js": "text/javascript; charset=utf-8",
    "perf.css": "text/css; charset=utf-8",
    "perf_fixture.json": "application/json",     # ?demo=1 示範模式用（真實形狀、假數字）
}

# 扁平欄位：app/rtf.py RtfMeter.snapshot() 與 app/pipeline.py stats()（grok/integrate bea2e5c）。
FLAT_KEYS = (
    "asr_rtf_p50", "asr_rtf_p95", "asr_rtf_last", "asr_samples",
    "backlog_s", "backlog_estimated", "pending", "inflight", "oldest_wait_ms", "translate_queued",
    "tgt_lang",
)
STAGE_IDS = ("A2", "A3", "A4", "A5", "A6")          # app/latency.py STAGES
STAGE_KEYS = ("name", "n", "p50_ms", "p95_ms")      # app/latency.py StageLatency.snapshot()


def trim_metrics(data: dict) -> dict:
    """只留效能頁要的欄位：latency（A2–A6）＋RTF＋積壓。不回傳 room_id／session_id、token 用量、價格等。"""
    if not isinstance(data, dict):
        return {}
    out = {k: data[k] for k in FLAT_KEYS if k in data}
    lat = data.get("latency")
    if isinstance(lat, dict):
        out["latency"] = {sid: {k: lat[sid].get(k) for k in STAGE_KEYS if k in lat[sid]}
                          for sid in STAGE_IDS if isinstance(lat.get(sid), dict)}
    rtf = data.get("rtf")
    if isinstance(rtf, (int, float)) and not isinstance(rtf, bool):
        out["rtf"] = rtf                                  # 舊格式：單一數字
    elif isinstance(rtf, dict) and isinstance(rtf.get("session"), dict):
        sess = rtf["session"]
        block = sess.get("rtf") if isinstance(sess.get("rtf"), dict) else {}
        out["rtf"] = {"session": {"count": sess.get("count"),
                                  "rtf": {k: block.get(k) for k in ("p50", "p95") if k in block}}}
    return out


def _file(name: str, media_type: str | None = None) -> FileResponse:
    return FileResponse(STATIC / name, media_type=media_type, headers={"Cache-Control": "no-store"})


def build_router(*, api: str, need, live, Problem, clock=time.time) -> APIRouter:
    router = APIRouter()

    @router.get("/admin/perf")
    def perf_page():
        return _file(PAGE, "text/html; charset=utf-8")

    @router.get("/admin/perf/static/{name}")
    def perf_static(name: str):
        media = PERF_STATIC.get(name)
        if media is None:
            raise Problem(404, "not_found", "找不到")
        return _file(name, media)

    @router.get(f"{api}/perf/metrics")
    async def perf_metrics(user=Depends(need("viewer"))):
        try:
            data = await asyncio.to_thread(live.metrics)
        except Exception as exc:  # LiveDown/LiveError 交給 app 既有的 handler（503／502 problem+json）
            if type(exc).__name__ in {"LiveDown", "LiveError"}:
                raise
            raise Problem(502, "live_error", "直播服務回傳無法辨識的資料")
        body = trim_metrics(data)
        body.setdefault("updated_at", clock())
        return body

    return router


def register(app, ctx) -> None:
    app.include_router(build_router(api=ctx.api, need=ctx.need, live=ctx.live, Problem=ctx.Problem,
                                    clock=getattr(ctx, "clock", time.time)))
