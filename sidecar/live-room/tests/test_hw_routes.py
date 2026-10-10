"""app/hw_routes.py: read-only status route, guarded by the caller's host check."""
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.hw_routes import make_router


def _client(allow: bool, seen: list):
    def guard(request):
        if not allow:
            raise HTTPException(status_code=401, detail="host only")

    def fake_status(**kw):
        seen.append(kw)
        return {"supported": False, "plan": {"asr": 4, "mt": 2}}
    app = FastAPI()
    app.include_router(make_router(guard, status_fn=fake_status))
    return TestClient(app)


def test_status_route_returns_plan_and_clamps_args():
    seen = []
    r = _client(True, seen).get("/api/hw/status?live=false&draft_threads=99")
    assert r.status_code == 200 and r.json()["plan"] == {"asr": 4, "mt": 2}
    assert seen == [{"live": False, "draft_threads": 4}]


def test_status_route_requires_guard():
    seen = []
    assert _client(False, seen).get("/api/hw/status").status_code == 401 and seen == []


def test_status_route_is_get_only():
    assert _client(True, []).post("/api/hw/status").status_code == 405
