"""The last tool calls this server handled, for the model to check after a lost reply.

A reply can get lost between here and the client (a relay hiccup, the PC dropping off the
network for a moment). The call itself may still have run. `history` answers "did my last
command run, and what was its run id / exit code?" so nothing is blindly sent twice.
"""

from __future__ import annotations

import collections
import itertools
import threading
import time
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .activity import _reports_error, preview, summarize

_SKIP = {"history"}


def _structured(result: Any) -> dict | None:
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    return data if isinstance(data, dict) else None


class RecentCalls(Middleware):
    def __init__(self, keep: int = 100) -> None:
        self.entries: collections.deque[dict] = collections.deque(maxlen=keep)
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        if message.name in _SKIP:
            return await call_next(context)
        started = time.time()
        entry: dict[str, Any] = {
            "n": next(self._numbers),
            "tool": message.name,
            "args": preview(message.arguments or {}, 160),
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
            "status": "running",
        }
        with self._lock:
            self.entries.append(entry)
        try:
            result = await call_next(context)
        except BaseException as exc:
            entry.update(
                status="error",
                summary=f"{type(exc).__name__}: {exc}"[:240],
                duration_ms=round((time.time() - started) * 1000),
            )
            raise
        data = _structured(result) or {}
        entry.update(
            status="error" if bool(getattr(result, "is_error", False)) or _reports_error(result) else "ok",
            summary=summarize(result),
            duration_ms=round((time.time() - started) * 1000),
        )
        if data.get("still_running"):
            entry["status"] = "still_running"
            entry["run_id"] = data.get("id")
        for key in ("exit_code", "id", "state"):
            if key in data and key not in entry:
                entry[key] = data[key]
        return result

    def list(self, limit: int = 20, tool: str | None = None) -> list[dict]:
        with self._lock:
            items = [dict(e) for e in self.entries if tool is None or e["tool"] == tool]
        return items[::-1][: max(1, limit)]
