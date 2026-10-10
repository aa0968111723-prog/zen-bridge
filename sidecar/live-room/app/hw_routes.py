"""Read-only hardware status endpoint (app/hw_tune.py) for the host page / admin.

Mount from server.py (integrator):  ``app.include_router(make_router(lambda r: require_host(r, token, settings)))``
GET /api/hw/status -> topology, power (AC/battery, plan, Win11 power mode), recommended
thread plan and what apply_profile changed. It never changes priorities or power plans.
"""
from __future__ import annotations

import asyncio
from typing import Callable

from fastapi import APIRouter, Request

from app import hw_tune


def make_router(guard: Callable[[Request], object], *, status_fn: Callable[..., dict] = hw_tune.status) -> APIRouter:
    router = APIRouter()

    @router.get("/api/hw/status")
    async def hw_status(request: Request, live: bool = True, draft_threads: int = 0) -> dict:
        guard(request)                                   # host auth, supplied by the caller
        return await asyncio.to_thread(status_fn, live=live, draft_threads=max(0, min(int(draft_threads), 4)))

    return router
