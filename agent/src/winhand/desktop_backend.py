"""`winhand desktop-backend`: the tray app's engine, spoken to over stdio in JSON lines.

The app starts this process (the packaged winhand.exe) and owns its lifetime: when the
app's end of stdin closes, the backend stops the relay tunnel and all sessions and exits.
It runs the same MCP server and relay tunnel as `winhand connect`, and reports live state:
connection status, every tool call and the open sessions. See desktop/PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from . import __version__, config
from .activity import ActivityHub, ToolActivity
from .relay_client import AlreadyRunning, Replaced, run_tunnel, single_instance
from .server import build_server
from .session import SessionManager

PROTOCOL_VERSION = 1
log = logging.getLogger("winhand.desktop")


class RpcError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Backend:
    def __init__(self, write_line) -> None:
        self.write_line = write_line
        self.generation = str(uuid.uuid4())
        self.home = config.home()
        self.hub = ActivityHub(self.home / "activity")
        self.hub.load_today()
        self.hub.listeners.append(lambda entry: self.emit("activity", entry))
        self.sessions = SessionManager()
        self.cfg = config.load()
        self.mcp = build_server(self.cfg, self.sessions)
        self.mcp.add_middleware(ToolActivity(self.hub))
        self.status: dict[str, Any] = {}
        self.tunnel: asyncio.Task | None = None
        self.tunnel_stop: asyncio.Event | None = None
        self.lock: contextlib.ExitStack | None = None
        self.last_sessions: list | None = None
        self.done = asyncio.Event()

    # ----------------------------------------------------------------- output

    def emit(self, event: str, data: dict) -> None:
        self.write_line({"event": event, "data": {**data, "generation": self.generation}})

    def set_status(self, state: str, **detail: Any) -> None:
        previous = self.status.get("state")
        relay = urlparse(self.cfg.relay_url).hostname if self.cfg.relay_url else None
        since = self.status.get("since") if previous == state else time.time()
        self.status = {"state": state, "since": since, "relay": relay, **detail}
        self.emit("status", self.status)
        if state == previous:
            return
        titles = {
            "online": "已连接到中转",
            "offline": "连接中断，稍后重试",
            "refused": "中转拒绝连接（检查设备令牌）",
            "replaced": "另一个 winhand 接管了中转",
            "conflict": "终端里的 winhand connect 仍在运行",
            "unconfigured": "尚未配置中转地址和设备令牌",
            "paused": "已手动断开",
        }
        if state in titles:
            self.hub.record("connection", titles[state], status=state, summary=detail.get("reason") or "")

    # ----------------------------------------------------------------- tunnel

    async def start_tunnel(self) -> None:
        await self.stop_tunnel(quiet=True)
        if not self.cfg.relay_url or not self.cfg.device_token:
            self.set_status("unconfigured")
            return
        lock = contextlib.ExitStack()
        try:
            lock.enter_context(single_instance(self.home / "connect.lock"))
        except AlreadyRunning as exc:
            self.set_status("conflict", reason=str(exc))
            return
        self.lock = lock
        self.tunnel_stop = asyncio.Event()
        self.tunnel = asyncio.create_task(
            run_tunnel(
                self.cfg.relay_url,
                self.cfg.device_token,
                mcp=self.mcp,
                stop=self.tunnel_stop,
                on_status=self._on_tunnel_status,
            )
        )
        self.tunnel.add_done_callback(self._on_tunnel_done)

    def _on_tunnel_status(self, state: str, **detail: Any) -> None:
        if state == "stopped" or state == "replaced":
            return  # reported by _on_tunnel_done / stop_tunnel with the final reason
        current = self.status.get("state")
        if state == "connecting" and current in ("offline", "refused"):
            # keep showing why we are offline while the retry runs
            kept = {k: v for k, v in self.status.items() if k not in ("state", "since", "relay")}
            self.set_status(current, **{**kept, **detail, "connecting": True})
            return
        self.set_status(state, **detail)

    def _on_tunnel_done(self, task: asyncio.Task) -> None:
        if self.tunnel is task:
            self.tunnel = None
            self._release_lock()
        if task.cancelled():
            return
        exc = task.exception()
        if isinstance(exc, Replaced):
            self.set_status("replaced", reason=str(exc))
        elif exc is not None:
            log.error("relay tunnel crashed", exc_info=exc)
            self.set_status("offline", reason=f"tunnel crashed: {exc}", retry_in_s=None)

    async def stop_tunnel(self, *, quiet: bool = False) -> None:
        task, self.tunnel = self.tunnel, None
        if task is not None:
            if self.tunnel_stop is not None:
                self.tunnel_stop.set()
            try:
                await asyncio.wait_for(task, 10)
            except (TimeoutError, asyncio.CancelledError, Exception):
                task.cancel()
        self._release_lock()
        if not quiet:
            self.set_status("paused")

    def _release_lock(self) -> None:
        lock, self.lock = self.lock, None
        if lock is not None:
            lock.close()

    # --------------------------------------------------------------- sessions

    def session_list(self) -> list[dict]:
        return [s.summary() for s in self.sessions.list()]

    async def watch_sessions(self) -> None:
        while not self.done.is_set():
            with contextlib.suppress(Exception):
                current = self.session_list()
                key = [(s["id"], s["state"], s["reason"], s["unread_chars"] > 0) for s in current]
                if key != self.last_sessions:
                    self.last_sessions = key
                    self.emit("sessions", {"sessions": current})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.done.wait(), 1.0)

    # ---------------------------------------------------------------- methods

    def snapshot(self) -> dict:
        return {
            "version": __version__,
            "generation": self.generation,
            "paths": {
                "home": str(self.home),
                "config": str(config.config_path()),
                "activity": str(self.home / "activity"),
                "logs": str(self.home / "logs"),
            },
            "relay": {"url": self.cfg.relay_url, "has_token": bool(self.cfg.device_token)},
            "gateway": [s.name for s in self.cfg.servers if s.enabled],
            "status": self.status,
            "counts": dict(self.hub.counts),
            "activity": self.hub.snapshot(),
            "sessions": self.session_list(),
        }

    async def m_initialize(self, params: dict) -> dict:
        if params.get("protocol_version") != PROTOCOL_VERSION:
            raise RpcError("protocol_mismatch", f"backend speaks protocol {PROTOCOL_VERSION}")
        if not self.status:
            if params.get("connect", True):
                await self.start_tunnel()
            else:
                self.set_status("paused")
        return {"protocol_version": PROTOCOL_VERSION, **self.snapshot()}

    async def m_get_state(self, params: dict) -> dict:
        return self.snapshot()

    async def m_set_relay(self, params: dict) -> dict:
        url = (params.get("url") or "").strip()
        token = (params.get("token") or "").strip() or self.cfg.device_token
        if urlparse(url).scheme not in ("ws", "wss") or not urlparse(url).hostname:
            raise RpcError("invalid_url", "中转地址应形如 wss://your-domain/agent")
        if not token:
            raise RpcError("missing_token", "需要设备令牌")
        self.cfg.relay_url, self.cfg.device_token = url, token
        config.save(self.cfg)
        await self.start_tunnel()
        return self.snapshot()

    async def m_reconnect(self, params: dict) -> dict:
        await self.start_tunnel()
        return {"status": self.status}

    async def m_disconnect(self, params: dict) -> dict:
        await self.stop_tunnel()
        return {"status": self.status}

    async def m_stop_session(self, params: dict) -> dict:
        try:
            session = self.sessions.get(str(params.get("id")))
        except KeyError as exc:
            raise RpcError("not_found", str(exc)) from None
        result = await asyncio.to_thread(session.stop, bool(params.get("force")))
        self.sessions.remove(session.id)
        self.hub.record("session", f"在应用中停止了会话 {session.id}", status="ok", summary="")
        return result

    async def m_shutdown(self, params: dict) -> dict:
        self.done.set()
        return {"ok": True}

    async def handle(self, raw: bytes) -> None:
        try:
            message = json.loads(raw)
            rid, method = message.get("id"), message.get("method")
        except (ValueError, AttributeError):
            return
        handler = getattr(self, f"m_{method}", None) if isinstance(method, str) else None
        try:
            if handler is None:
                raise RpcError("unknown_method", f"unknown method {method!r}")
            result = await handler(message.get("params") or {})
            self.write_line({"id": rid, "result": result})
        except RpcError as exc:
            self.write_line({"id": rid, "error": {"code": exc.code, "message": str(exc)}})
        except Exception as exc:
            log.exception("method %s failed", method)
            self.write_line({"id": rid, "error": {"code": "internal", "message": str(exc)}})

    async def close(self) -> None:
        await self.stop_tunnel(quiet=True)
        await asyncio.to_thread(self.sessions.stop_all)


def _setup_logging() -> None:
    logs = config.home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        logs / "desktop-backend.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler(sys.stderr))


async def serve(stdin, stdout) -> int:
    """Run until shutdown or until the app closes stdin."""
    write_lock = threading.Lock()

    def write_line(obj: dict) -> None:
        data = (json.dumps(obj, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with write_lock:
            stdout.write(data)
            stdout.flush()

    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[bytes | None] = asyncio.Queue()

    def read_stdin() -> None:  # a thread: pipe reads are not awaitable everywhere on Windows
        try:
            for line in stdin:
                loop.call_soon_threadsafe(lines.put_nowait, line)
        finally:
            loop.call_soon_threadsafe(lines.put_nowait, None)

    backend = Backend(write_line)
    threading.Thread(target=read_stdin, name="desktop-stdin", daemon=True).start()
    watcher = asyncio.create_task(backend.watch_sessions())
    pending: set[asyncio.Task] = set()
    try:
        while not backend.done.is_set():
            getter = asyncio.create_task(lines.get())
            finished, _ = await asyncio.wait(
                {getter, asyncio.create_task(backend.done.wait())}, return_when=asyncio.FIRST_COMPLETED
            )
            if getter not in finished:
                getter.cancel()
                break
            line = getter.result()
            if line is None:
                log.info("app closed the pipe; shutting down")
                break
            if line.strip():
                task = asyncio.create_task(backend.handle(line))
                pending.add(task)
                task.add_done_callback(pending.discard)
        # let a shutdown reply go out before tearing down
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        backend.done.set()
        watcher.cancel()
        await backend.close()
    return 0


def main() -> int:
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # stdout belongs to the protocol; a stray print must not corrupt it
    _setup_logging()
    log.info("desktop backend %s starting", __version__)
    code = asyncio.run(serve(stdin, stdout))
    # The stdin reader thread may still be blocked in read(); a normal interpreter shutdown
    # would abort on its buffer lock. Everything is stopped and flushed by now, so leave directly.
    logging.shutdown()
    with contextlib.suppress(Exception):
        stdout.flush()
        sys.stderr.flush()
    os._exit(code)
