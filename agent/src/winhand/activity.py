"""What the desktop app shows: every tool call and connection change, live and on disk.

The hub keeps recent entries in memory for the timeline, appends finished ones to a
daily audit log (~/.winhand/activity/YYYY-MM-DD.jsonl) and pushes each change to
listeners (the desktop backend forwards them to the app).
"""

from __future__ import annotations

import collections
import contextlib
import itertools
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

PREVIEW_CHARS = 240
_SECRET_KEYS = {"env", "token", "password", "secret"}


def preview(value: Any, limit: int = PREVIEW_CHARS) -> Any:
    """Short, JSON-safe rendering of tool arguments for the timeline (never full file contents)."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + f"… ({len(value)} chars)"
    if isinstance(value, dict):
        return {
            k: ("•••" if str(k).lower() in _SECRET_KEYS and v else preview(v, limit))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [preview(v, limit) for v in value[:20]]
        return items + [f"… ({len(value)} items)"] if len(value) > 20 else items
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return preview(str(value), limit)


def summarize(result: Any) -> str:
    """One line describing a tool result: state for sessions, exit code for commands, else text."""
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict):
        if "result" in data and len(data) == 1:
            data = data["result"]
    if isinstance(data, dict):
        if data.get("error"):
            return str(data["error"])[:PREVIEW_CHARS]
        parts = []
        for key in ("state", "exit_code", "condition", "path"):
            if key in data and data[key] is not None:
                parts.append(f"{key}={data[key]}")
        hit = data.get("hit")
        if isinstance(hit, dict) and hit.get("condition"):
            parts.append(f"hit={hit['condition']}")
        if parts:
            return " ".join(parts)
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            line = text.strip().splitlines()[0] if text.strip() else ""
            return line[:PREVIEW_CHARS]
    return ""


class ActivityHub:
    def __init__(self, directory: Path | None = None, keep: int = 500) -> None:
        self.directory = directory
        self.recent: collections.deque[dict] = collections.deque(maxlen=keep)
        self.listeners: list[Callable[[dict], None]] = []
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self.counts = collections.Counter()

    def new_id(self) -> str:
        return f"{int(time.time() * 1000):x}-{next(self._ids)}"

    def publish(self, entry: dict, *, persist: bool = True) -> dict:
        with self._lock:
            for i, old in enumerate(self.recent):
                if old["id"] == entry["id"]:
                    self.recent[i] = entry
                    break
            else:
                self.recent.append(entry)
        if persist:
            self._append(entry)
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                listener(entry)
        return entry

    def record(self, kind: str, title: str, **fields: Any) -> dict:
        return self.publish({"id": self.new_id(), "t": time.time(), "kind": kind, "title": title, **fields})

    def load_today(self, limit: int = 200) -> None:
        """Seed the timeline with today's audit log so a restart does not blank it."""
        path = self._path()
        if path is None or not path.exists():
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
        with self._lock:
            for line in lines:
                with contextlib.suppress(ValueError):
                    entry = json.loads(line)
                    if isinstance(entry, dict) and "id" in entry:
                        self.recent.append(entry)

    def snapshot(self, limit: int = 200) -> list[dict]:
        with self._lock:
            return list(self.recent)[-limit:]

    def _path(self) -> Path | None:
        if self.directory is None:
            return None
        return self.directory / f"{datetime.now():%Y-%m-%d}.jsonl"

    def _append(self, entry: dict) -> None:
        path = self._path()
        if path is None or entry.get("status") == "running":
            return
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class ToolActivity(Middleware):
    """Records each MCP tool call (start, then result or error) into the hub."""

    def __init__(self, hub: ActivityHub) -> None:
        self.hub = hub

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        started = time.time()
        entry = {
            "id": self.hub.new_id(),
            "t": started,
            "kind": "tool",
            "title": message.name,
            "args": preview(message.arguments or {}),
            "status": "running",
        }
        self.hub.publish(entry, persist=False)
        try:
            result = await call_next(context)
        except BaseException as exc:
            self.hub.counts["error"] += 1
            self.hub.publish(
                {
                    **entry,
                    "status": "error",
                    "duration_ms": round((time.time() - started) * 1000),
                    "summary": f"{type(exc).__name__}: {exc}"[:PREVIEW_CHARS],
                }
            )
            raise
        failed = bool(getattr(result, "is_error", False))
        self.hub.counts["error" if failed else "ok"] += 1
        self.hub.publish(
            {
                **entry,
                "status": "error" if failed else "ok",
                "duration_ms": round((time.time() - started) * 1000),
                "summary": summarize(result),
            }
        )
        return result
