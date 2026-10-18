"""The last tool calls this server handled, for the model to check after a lost reply.

A reply can get lost between here and the client (a relay hiccup, the PC dropping off the
network for a moment). The call itself may still have run. `history` answers "did my last
command run, and what was its run id / exit code?" so nothing is blindly sent twice. Several
clients can share this machine: each call names its client, and the caller's own are marked.
After winhand restarted, the calls the desktop app logged before that are listed too.
"""

from __future__ import annotations

import collections
import contextlib
import contextvars
import itertools
import json
import threading
import time
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

from .activity import _reports_error, preview, summarize
from .redact import mask

_SKIP = {"history"}
EARLIER_BYTES = 512 * 1024  # how much of the end of each day's activity log is read


def caller(ctx: Any) -> tuple[str | None, str | None]:
    """(MCP session id, client name) of the request being served. The session id only where the
    client keeps a session (the Mcp-Session-Id header): clients of the newer, sessionless protocol
    get None rather than an id made up for each request."""
    session = client = None
    with contextlib.suppress(Exception):
        session = ctx.request_context.request.headers.get("mcp-session-id") or None
    with contextlib.suppress(Exception):
        params = ctx.session.client_params
        info = getattr(params, "client_info", None) or getattr(params, "clientInfo", None)
        client = f"{info.name} {info.version}" if info.version else info.name
    return session, client


def stamp(t: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def _tail_lines(path: Path, size: int = EARLIER_BYTES) -> list[str]:
    with path.open("rb") as fh:
        fh.seek(0, 2)
        end = fh.tell()
        fh.seek(max(0, end - size))
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[1:] if end > size else lines  # the first line was cut


def earlier(folder: Path, before: float, limit: int, tool: str | None = None) -> list[dict]:
    """Tool calls the desktop app logged (activity/YYYY-MM-DD.jsonl) before `before`, newest first."""
    found: list[dict] = []
    with contextlib.suppress(OSError):
        days = sorted(folder.glob("*.jsonl"))[-2:]
        for day in reversed(days):
            for line in reversed(_tail_lines(day)):
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict) or entry.get("kind") != "tool":
                    continue
                t = entry.get("t")
                if not isinstance(t, (int, float)) or t >= before or (tool and entry.get("title") != tool):
                    continue
                item = {"tool": entry.get("title"), "args": entry.get("args"), "started": stamp(t)}
                for key in ("status", "summary", "duration_ms"):
                    if key in entry:
                        item[key] = entry[key]
                item["before_restart"] = True
                found.append(item)
                if len(found) >= limit:
                    return found
    return found


def _structured(result: Any) -> dict | None:
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], dict):
        data = data["result"]
    return data if isinstance(data, dict) else None


class RecentCalls(Middleware):
    def __init__(self, keep: int = 100) -> None:
        self.entries: collections.deque[dict] = collections.deque(maxlen=keep)
        self.started = time.time()  # calls logged before this ran before winhand (re)started
        self._numbers = itertools.count(1)
        self._lock = threading.Lock()
        # the entry of a call(tool, arguments) in progress: the tool it runs comes through this
        # middleware again and fills that entry in, so one call is one entry, under the tool's name
        self._wrapper: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
            f"winhand-history-{id(self)}", default=None
        )

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        wrapper = self._wrapper.get()
        if message.name in _SKIP:
            if wrapper is not None:  # history() reached through call() is not recorded either
                with self._lock, contextlib.suppress(ValueError):
                    self.entries.remove(wrapper)
            return await call_next(context)
        args = mask(preview(message.arguments or {}, 160))
        if wrapper is not None:
            wrapper.update(tool=message.name, args=args, via="call")
            return await call_next(context)
        started = time.time()
        session, client = caller(context.fastmcp_context)
        entry: dict[str, Any] = {
            "n": next(self._numbers),
            "tool": message.name,
            "args": args,
            "started": stamp(started),
            "status": "running",
        }
        if client:
            entry["client"] = client
        if session:
            entry["_session"] = session
        with self._lock:
            self.entries.append(entry)
        token = self._wrapper.set(entry) if message.name == "call" else None
        try:
            result = await call_next(context)
        except BaseException as exc:
            entry.update(
                status="error",
                summary=f"{type(exc).__name__}: {exc}"[:240],
                duration_ms=round((time.time() - started) * 1000),
            )
            raise
        finally:
            if token is not None:
                self._wrapper.reset(token)
        data = _structured(result) or {}
        entry.update(
            status="error" if bool(getattr(result, "is_error", False)) or _reports_error(result) else "ok",
            summary=summarize(result),
            duration_ms=round((time.time() - started) * 1000),
        )
        if data.get("still_running"):
            entry["status"] = "still_running"
            entry["run_id"] = data.get("id") or data.get("call_id")  # mcp_call names it call_id
        for key in ("exit_code", "id", "state"):
            if key in data and key not in entry:
                entry[key] = data[key]
        return result

    def list(self, limit: int = 20, tool: str | None = None, session: str | None = None) -> list[dict]:
        """Newest first; with `session`, each entry says whether that session made the call."""
        with self._lock:
            items = [dict(e) for e in self.entries if tool is None or e["tool"] == tool]
        out = []
        for item in items[::-1][: max(1, limit)]:
            made_by = item.pop("_session", None)
            if session and made_by:
                item["this_session"] = made_by == session
            out.append(item)
        return out
