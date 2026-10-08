"""Bounded waits that still notice cancellation on Python 3.11.

asyncio.wait_for before 3.12 returns the inner result when that awaitable
finishes in the same cycle as an outer cancellation. A loop then keeps
running after it was cancelled. asyncio.timeout (3.11+) does not do that.
TimeoutError is asyncio.TimeoutError on 3.10 and later.
"""
from __future__ import annotations

import asyncio


def cancellation_pending() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def wait_bounded(awaitable, timeout: float):
    task = asyncio.current_task()
    try:
        async with asyncio.timeout(timeout):
            result = await awaitable
    except TimeoutError:
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()
        raise
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()
    return result
