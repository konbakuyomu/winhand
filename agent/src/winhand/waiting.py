"""How long one tool call may wait, and keeping a long wait alive.

Claude Code times a call to a remote MCP server only up to the first byte of the answer (at least
60 s). After that a call may go on for as long as something arrives at least every five minutes,
and every progress notification counts. A call in the main conversation that is still going after
two minutes moves to the background and reports when it ends (code.claude.com/docs/en/mcp,
checked 2026-11-01). So a client that asked for progress (its request carries a progress token)
can be kept waiting far longer than 40 s, as long as a notification goes out at once and then
every HEARTBEAT_S. A client that did not ask keeps the 40 s that fits inside a plain 60 s limit.

The beat is per caller: identical waits share one task (coalesce), but each caller needs its own
notifications, or the ones sharing a task would get no first byte and time out.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from typing import Any

SHORT_WAIT_S = 40.0
LONG_WAIT_S = 1800.0
HEARTBEAT_S = 15.0


def takes_progress(ctx: Any) -> bool:
    """Did this request ask for progress notifications (carry a progress token)?"""
    if ctx is None:
        return False
    with contextlib.suppress(Exception):
        rc = ctx.request_context
        meta = getattr(getattr(rc, "_srctx", None), "meta", None)
        if isinstance(meta, dict):
            return meta.get("progress_token") is not None
        meta = getattr(rc, "meta", None)
        return getattr(meta, "progressToken", None) is not None
    return False


def limit(ctx: Any, wanted: float) -> float:
    """The wait this caller gets: up to LONG_WAIT_S with progress, SHORT_WAIT_S without."""
    ceiling = LONG_WAIT_S if takes_progress(ctx) else SHORT_WAIT_S
    return max(0.0, min(float(wanted), ceiling))


def capped_note(ctx: Any, wanted: float) -> str | None:
    """Say so when a long wait was asked for and this client cannot have it."""
    if wanted > SHORT_WAIT_S and not takes_progress(ctx):
        return (
            f"waits stop at {int(SHORT_WAIT_S)} s for this client (it does not take progress "
            "notifications): call again to keep waiting"
        )
    return None


async def heartbeat[T](ctx: Any, work: Awaitable[T], total: float, label: str) -> T:
    """Await `work`, telling this caller it is still waiting: one notification at once (the first
    byte of the answer, before the client's 60 s first-byte limit) and one every HEARTBEAT_S (its
    idle timer starts over). Without a progress token this is a plain await."""
    if not takes_progress(ctx):
        return await work
    task = asyncio.ensure_future(work)
    began = time.monotonic()
    try:
        while True:
            elapsed = time.monotonic() - began
            with contextlib.suppress(Exception):
                await ctx.report_progress(round(elapsed, 1), total, f"{label}: waited {int(elapsed)} s")
            done, _ = await asyncio.wait({task}, timeout=HEARTBEAT_S)
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()  # this caller gave up; a shared wait behind it is shielded (coalesce)
