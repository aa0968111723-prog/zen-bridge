"""OBS browser-source overlay page (fullstack).

Wiring (server.py, inside create_app(), after the /static mount):

    from app.overlay_routes import router as overlay_router
    app.include_router(overlay_router)

The page itself is a static file; captions come from the existing audience
socket /ws/listen, so this router adds no new data path. overlay.js is served
by the existing /static mount.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

from app.rooms import RoomIdError, validate_room_id

STATIC = Path(__file__).resolve().parent / "static"
OVERLAY_HTML = STATIC / "overlay.html"

# Same policy as RevalidatingStaticFiles: revalidate so a cached overlay.js
# never pairs with a newer overlay.html. The page is embedded in OBS, so it
# also must not be framed by another site.
_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' ws: wss:; img-src 'self' data:; font-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
}

router = APIRouter(tags=["overlay"])


def _overlay_page() -> Response:
    if not OVERLAY_HTML.is_file():
        raise HTTPException(status_code=404, detail="overlay.html 不存在")
    return FileResponse(OVERLAY_HTML, media_type="text/html; charset=utf-8", headers=dict(_HEADERS))


@router.get("/overlay", include_in_schema=False)
async def overlay_default() -> Response:
    """Room comes from ?room= (default "class", same default as /ws/listen)."""
    return _overlay_page()


@router.get("/overlay/{room_id}", include_in_schema=False)
async def overlay_room(room_id: str) -> Response:
    try:
        validate_room_id(room_id)
    except RoomIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _overlay_page()
