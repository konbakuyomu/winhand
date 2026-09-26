"""Local MCP servers, each offered to remote clients as its own MCP endpoint.

A server configured in ~/.winhand/config.toml under [mcp_servers.<name>] is reachable at
https://<relay>/mcp/<name> (the relay forwards every /mcp/... request down the tunnel).
Nothing is renamed or merged: the remote client talks to that server as if it ran next to
it. For a stdio server winhand starts the process on first use and passes JSON-RPC
messages through unchanged, except that request ids are renumbered so several client
sessions can share the one process. A server that already speaks HTTP locally (`url`) is
proxied as-is.

Transport towards the client is MCP streamable HTTP: POST answers requests as a short
SSE stream (progress and server->client requests travel on it, then the response);
GET streams are not offered (405), which the protocol allows.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import copy
import itertools
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from . import __version__, winenv
from .activity import PREVIEW_CHARS, ActivityHub, preview
from .config import ServerEntry, home

log = logging.getLogger("winhand.mcp")

IS_WINDOWS = sys.platform == "win32"
KEEPALIVE_S = 15.0
SESSION_TTL_S = 24 * 3600
CRASH_WINDOW_S = 60.0
DEFAULT_INIT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {},
    "clientInfo": {"name": "winhand", "version": __version__},
}


class ServiceError(RuntimeError):
    pass


def _error(rid: Any, message: str, code: int = -32000) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _is_request(m: dict) -> bool:
    return "method" in m and "id" in m


def _is_response(m: dict) -> bool:
    return "method" not in m and "id" in m and ("result" in m or "error" in m)


# ---------------------------------------------------------------- process


class StdioProcess:
    """One running stdio MCP server: newline-delimited JSON-RPC on stdin/stdout. Threads do
    the pipe work (asyncio pipes differ between event loops on Windows)."""

    def __init__(
        self,
        entry: ServerEntry,
        loop: asyncio.AbstractEventLoop,
        on_message: Callable[[dict], None],
        on_exit: Callable[[int | None], None],
    ) -> None:
        env = winenv.build_env(entry.env)
        exe = winenv.resolve_executable(entry.command, env)
        cwd = os.path.expanduser(entry.cwd) if entry.cwd else None
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                [exe, *entry.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=env,
                **kwargs,
            )
        except OSError as exc:
            raise ServiceError(f"cannot start {entry.command!r}: {exc}") from exc
        self.name = entry.name
        self.loop = loop
        self.on_message = on_message
        self.on_exit = on_exit
        self.stderr: collections.deque[str] = collections.deque(maxlen=200)
        self._write_lock = threading.Lock()
        log_dir = home() / "logs" / "mcp"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = log_dir / f"{entry.name}.log"
        self._log = open(self.log_path, "a", encoding="utf-8")  # noqa: SIM115 - closed by the stderr thread
        self._log.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} started pid {self.proc.pid}: {exe}\n")
        self._log.flush()
        threading.Thread(target=self._read_stdout, name=f"mcp-{entry.name}-out", daemon=True).start()
        threading.Thread(target=self._read_stderr, name=f"mcp-{entry.name}-err", daemon=True).start()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                # a server that prints to stdout breaks the protocol; keep the evidence
                self.stderr.append("stdout (not JSON): " + line.decode("utf-8", "replace")[:500])
                continue
            for item in message if isinstance(message, list) else [message]:
                if isinstance(item, dict):
                    self.loop.call_soon_threadsafe(self.on_message, item)
        code = self.proc.wait()
        self.loop.call_soon_threadsafe(self.on_exit, code)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for raw in self.proc.stderr:
                text = raw.decode("utf-8", "replace").rstrip()
                self.stderr.append(text)
                with contextlib.suppress(OSError, ValueError):
                    self._log.write(text + "\n")
                    self._log.flush()
        finally:
            with contextlib.suppress(OSError):
                self._log.close()

    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        assert self.proc.stdin is not None
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            if self.proc.stdin:
                self.proc.stdin.close()  # a well-behaved server exits on EOF
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            from .proc import _kill_tree

            _kill_tree(self.proc.pid)


# ---------------------------------------------------------------- routing


@dataclass
class _Stream:
    """The SSE answer to one POST: what goes to that client, and how many answers are due."""

    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    due: set = field(default_factory=set)  # upstream ids not answered yet


@dataclass
class _Session:
    id: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    streams: list[_Stream] = field(default_factory=list)


@dataclass
class _Pending:
    session: _Session | None
    client_id: Any
    stream: _Stream | None
    future: asyncio.Future | None = None
    activity: dict | None = None
    progress_token: Any = None


class Service:
    def __init__(self, entry: ServerEntry, hub: ActivityHub | None = None) -> None:
        self.entry = entry
        self.hub = hub
        self.process: StdioProcess | None = None
        self.state = "stopped"  # stopped | starting | running | error
        self.error: str | None = None
        self.started_at: float | None = None
        self.last_used: float | None = None
        self.init_params: dict | None = None
        self.init_result: dict | None = None
        self.sessions: dict[str, _Session] = {}
        self.pending: dict[int, _Pending] = {}
        self.server_requests: dict[str, Any] = {}  # id sent to the client -> upstream id
        self.exits: collections.deque[float] = collections.deque(maxlen=3)
        self.calls = 0
        self._ids = itertools.count(1)
        self._start_lock = asyncio.Lock()
        self._http: httpx.AsyncClient | None = None
        self.listeners: list[Callable[[Service], None]] = []

    # ------------------------------------------------------------ status

    def status(self) -> dict:
        info = {
            "name": self.entry.name,
            "kind": self.entry.kind,
            "enabled": self.entry.enabled,
            "state": self.state
            if self.entry.kind == "stdio"
            else ("running" if self.entry.enabled else "stopped"),
            "error": self.error,
            "pid": self.process.pid if self.process and self.process.alive() else None,
            "started_at": self.started_at,
            "last_used": self.last_used,
            "calls": self.calls,
            "sessions": len(self.sessions),
        }
        if self.init_result:
            server = self.init_result.get("serverInfo") or {}
            info["server"] = {"name": server.get("name"), "version": server.get("version")}
            info["protocol"] = self.init_result.get("protocolVersion")
        if self.process:
            info["stderr_tail"] = list(self.process.stderr)[-20:]
            info["log_file"] = str(self.process.log_path)
        return info

    def _changed(self) -> None:
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                listener(self)

    def _set_state(self, state: str, error: str | None = None) -> None:
        self.state, self.error = state, error
        self._changed()

    # ------------------------------------------------------------ lifecycle

    async def ensure_started(self, params: dict | None = None) -> None:
        async with self._start_lock:
            if self.process and self.process.alive() and self.init_result is not None:
                return
            if len(self.exits) == self.exits.maxlen and time.time() - self.exits[0] < CRASH_WINDOW_S:
                if time.time() - self.exits[-1] < CRASH_WINDOW_S / 2:
                    raise ServiceError(
                        f"{self.entry.name} keeps exiting right after start ({self.error or 'no details'}); "
                        "check it in the winhand app"
                    )
            if params is not None:
                self.init_params = params
            self._set_state("starting")
            loop = asyncio.get_running_loop()
            try:
                self.process = StdioProcess(self.entry, loop, self._on_message, self._on_exit)
            except ServiceError as exc:
                self._set_state("error", str(exc))
                raise
            self.started_at = time.time()
            process = self.process
            try:
                answer = await self._internal(
                    "initialize", self.init_params or DEFAULT_INIT, self.entry.startup_timeout_s
                )
            except (TimeoutError, ServiceError, OSError, ValueError) as exc:
                tail = " | ".join(line for line in list(process.stderr)[-5:] if line)
                reason = str(exc) or "timed out"
                message = f"{self.entry.name} did not initialize: {reason}" + (
                    f" ({tail})" if tail and tail not in reason else ""
                )
                await asyncio.to_thread(process.stop)
                self.process = None
                self._set_state("error", message[:600])
                raise ServiceError(message[:600]) from None
            if "error" in answer:
                await asyncio.to_thread(process.stop)
                self.process = None
                message = f"{self.entry.name} refused to initialize: {answer['error'].get('message')}"
                self._set_state("error", message)
                raise ServiceError(message)
            self.init_result = answer["result"]
            self.process.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._set_state("running")
            log.info("mcp server %s started (pid %s)", self.entry.name, self.process.pid)

    async def _internal(self, method: str, params: dict, timeout: float) -> dict:
        uid = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self.pending[uid] = _Pending(None, None, None, future=future)
        assert self.process is not None
        try:
            await asyncio.to_thread(
                self.process.send, {"jsonrpc": "2.0", "id": uid, "method": method, "params": params}
            )
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(uid, None)

    async def stop(self, reason: str = "stopped") -> None:
        process, self.process = self.process, None
        self.init_result = None
        if process is not None:
            await asyncio.to_thread(process.stop)
        self._fail_pending(f"{self.entry.name} was {reason}")
        self._set_state("stopped")

    def _on_exit(self, code: int | None) -> None:
        if self.process is None or self.process.alive():
            return  # an older process (already replaced or stopped on purpose)
        tail = " | ".join(line for line in list(self.process.stderr)[-5:] if line)
        self.process = None
        self.init_result = None
        self.exits.append(time.time())
        message = f"{self.entry.name} exited (code {code})" + (f": {tail}" if tail else "")
        log.warning("%s", message)
        self._fail_pending(message)
        self._set_state("error" if code else "stopped", message[:600] if code else None)

    def _fail_pending(self, message: str) -> None:
        for uid, p in list(self.pending.items()):
            self.pending.pop(uid, None)
            if p.future is not None:
                if not p.future.done():
                    p.future.set_exception(ServiceError(message))
            elif p.stream is not None:
                p.stream.queue.put_nowait(("answer", uid, _error(p.client_id, message)))
                self._finish_activity(p, error=message)

    # ------------------------------------------------------------ messages from the server

    def _on_message(self, message: dict) -> None:
        if _is_response(message):
            p = self.pending.pop(message["id"], None) if isinstance(message["id"], int) else None
            if p is None:
                return
            if p.future is not None:
                if not p.future.done():
                    p.future.set_result(message)
                return
            answer = {**message, "id": p.client_id}
            if p.stream is not None:
                p.stream.queue.put_nowait(("answer", message["id"], answer))
            self._finish_activity(p, answer=answer)
            return
        if _is_request(message):
            if message["method"] == "ping":
                self._reply(message["id"], {})
                return
            stream = self._any_stream()
            if stream is None:
                self._reply_error(message["id"], "no client is connected to answer this request", -32603)
                return
            key = f"winhand-{next(self._ids)}"
            self.server_requests[key] = message["id"]
            stream.queue.put_nowait(("message", None, {**message, "id": key}))
            return
        # a notification
        if message.get("method") == "notifications/progress":
            token = (message.get("params") or {}).get("progressToken")
            for p in self.pending.values():
                if p.stream is not None and p.progress_token == token:
                    p.stream.queue.put_nowait(("message", None, message))
                    return
            return
        for session in self.sessions.values():
            if session.streams:
                session.streams[-1].queue.put_nowait(("message", None, message))

    def _any_stream(self) -> _Stream | None:
        sessions = sorted(self.sessions.values(), key=lambda s: s.last_seen, reverse=True)
        for session in sessions:
            if session.streams:
                return session.streams[-1]
        return None

    def _reply(self, rid: Any, result: dict) -> None:
        if self.process:
            with contextlib.suppress(OSError, ValueError):
                self.process.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def _reply_error(self, rid: Any, message: str, code: int) -> None:
        if self.process:
            with contextlib.suppress(OSError, ValueError):
                self.process.send(_error(rid, message, code))

    # ------------------------------------------------------------ activity

    def _start_activity(self, message: dict) -> dict | None:
        if self.hub is None or message.get("method") != "tools/call":
            return None
        params = message.get("params") or {}
        entry = {
            "id": self.hub.new_id(),
            "t": time.time(),
            "kind": "tool",
            "title": f"{self.entry.name} · {params.get('name')}",
            "server": self.entry.name,
            "args": preview(params.get("arguments") or {}),
            "status": "running",
        }
        self.hub.publish(entry, persist=False)
        return entry

    def _finish_activity(self, p: _Pending, answer: dict | None = None, error: str | None = None) -> None:
        if self.hub is None or p.activity is None:
            return
        result = (answer or {}).get("result") or {}
        failed = bool(error) or "error" in (answer or {}) or bool(result.get("isError"))
        summary = error or ((answer or {}).get("error") or {}).get("message") or ""
        if not summary:
            for block in result.get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    summary = block["text"].strip().splitlines()[0][:PREVIEW_CHARS]
                    break
                if block.get("type") in ("image", "audio", "resource"):
                    summary = f"[{block['type']}]"
                    break
        self.hub.counts["error" if failed else "ok"] += 1
        self.hub.publish(
            {
                **p.activity,
                "status": "error" if failed else "ok",
                "duration_ms": round((time.time() - p.activity["t"]) * 1000),
                "summary": summary,
            }
        )

    # ------------------------------------------------------------ HTTP (client side)

    async def handle(self, request: Request) -> Response:
        self.last_used = time.time()
        if self.entry.kind == "http":
            return await self._proxy(request)
        if request.method == "POST":
            return await self._post(request)
        if request.method == "DELETE":
            self.sessions.pop(request.headers.get("mcp-session-id", ""), None)
            return Response(status_code=200)
        if request.method == "GET":
            return Response(status_code=405, headers={"allow": "POST, DELETE"})
        return Response(status_code=405)

    async def _post(self, request: Request) -> Response:
        try:
            body = json.loads(await request.body())
        except ValueError:
            return JSONResponse(_error(None, "Parse error", -32700), status_code=400)
        messages = [m for m in (body if isinstance(body, list) else [body]) if isinstance(m, dict)]
        if not messages:
            return JSONResponse(_error(None, "Invalid Request", -32600), status_code=400)

        initialize = next((m for m in messages if m.get("method") == "initialize"), None)
        if initialize is not None:
            try:
                await self.ensure_started(initialize.get("params") or DEFAULT_INIT)
            except ServiceError as exc:
                return JSONResponse(_error(initialize.get("id"), str(exc)))
            session = _Session(uuid.uuid4().hex)
            self.sessions[session.id] = session
            self._expire_sessions()
            self._changed()
            answer = {"jsonrpc": "2.0", "id": initialize.get("id"), "result": self.init_result}
            return JSONResponse(answer, headers={"mcp-session-id": session.id})

        sid = request.headers.get("mcp-session-id")
        if not sid:
            return JSONResponse(
                _error(None, "Bad Request: Mcp-Session-Id header is required", -32600), status_code=400
            )
        session = self.sessions.get(sid)
        if session is None:  # e.g. winhand restarted: the client starts a new session
            return JSONResponse(_error(None, "Session not found", -32001), status_code=404)
        session.last_seen = time.time()

        requests = [m for m in messages if _is_request(m)]
        for m in messages:
            if _is_request(m):
                continue
            await self._forward_other(session, m)
        if not requests:
            return Response(status_code=202)

        try:
            await self.ensure_started()
        except ServiceError as exc:
            return JSONResponse(
                [_error(m["id"], str(exc)) for m in requests]
                if len(requests) > 1
                else _error(requests[0]["id"], str(exc))
            )
        stream = _Stream()
        session.streams.append(stream)
        for m in requests:
            uid = next(self._ids)
            token = ((m.get("params") or {}).get("_meta") or {}).get("progressToken")
            p = _Pending(session, m["id"], stream, activity=self._start_activity(m), progress_token=token)
            self.pending[uid] = p
            stream.due.add(uid)
            if m.get("method") == "tools/call":
                self.calls += 1
            try:
                assert self.process is not None
                await asyncio.to_thread(self.process.send, {**m, "id": uid})
            except (OSError, ValueError, AssertionError) as exc:
                self.pending.pop(uid, None)
                stream.queue.put_nowait(
                    ("answer", uid, _error(m["id"], f"{self.entry.name} is not running: {exc}"))
                )
        return StreamingResponse(
            self._sse(session, stream),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    async def _forward_other(self, session: _Session, m: dict) -> None:
        if not self.process or not self.process.alive():
            return
        if _is_response(m):
            upstream_id = self.server_requests.pop(str(m["id"]), None)
            if upstream_id is None:
                return
            m = {**m, "id": upstream_id}
        elif m.get("method") == "notifications/initialized":
            return  # winhand already told the server when it started it
        elif m.get("method") == "notifications/cancelled":
            wanted = (m.get("params") or {}).get("requestId")
            uid = next(
                (u for u, p in self.pending.items() if p.session is session and p.client_id == wanted), None
            )
            if uid is None:
                return
            m = {**m, "params": {**(m.get("params") or {}), "requestId": uid}}
        with contextlib.suppress(OSError, ValueError):
            await asyncio.to_thread(self.process.send, m)

    async def _sse(self, session: _Session, stream: _Stream) -> AsyncIterator[bytes]:
        try:
            while stream.due:
                try:
                    kind, uid, message = await asyncio.wait_for(stream.queue.get(), KEEPALIVE_S)
                except TimeoutError:
                    yield b": keepalive\n\n"
                    continue
                if kind == "answer":
                    stream.due.discard(uid)
                yield b"event: message\ndata: " + json.dumps(message, ensure_ascii=False).encode() + b"\n\n"
        finally:
            with contextlib.suppress(ValueError):
                session.streams.remove(stream)
            # the client went away before all answers came: tell the server to stop working on them
            for uid in list(stream.due):
                p = self.pending.pop(uid, None)
                if p is not None and self.process and self.process.alive():
                    self._finish_activity(p, error="the client disconnected")
                    with contextlib.suppress(OSError, ValueError):
                        self.process.send(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/cancelled",
                                "params": {"requestId": uid, "reason": "client disconnected"},
                            }
                        )

    def _expire_sessions(self) -> None:
        cutoff = time.time() - SESSION_TTL_S
        for sid, session in list(self.sessions.items()):
            if session.last_seen < cutoff and not session.streams:
                self.sessions.pop(sid, None)

    # ------------------------------------------------------------ HTTP servers on this machine

    async def _proxy(self, request: Request) -> Response:
        assert self.entry.url
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))
        keep = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
        headers = {k: v for k, v in request.headers.items() if k.lower() in keep}
        headers.update(self.entry.headers)
        upstream = self._http.build_request(
            request.method,
            self.entry.url,
            headers=headers,
            content=await request.body(),
            params=request.query_params,
        )
        try:
            response = await self._http.send(upstream, stream=True)
        except httpx.HTTPError as exc:
            self._set_state("error", f"{self.entry.url}: {exc}")
            return JSONResponse(
                _error(None, f"{self.entry.name} is not reachable at {self.entry.url}: {exc}"),
                status_code=502,
            )
        if self.error:
            self._set_state("running")
        drop = {"content-length", "transfer-encoding", "connection", "keep-alive", "content-encoding"}
        out_headers = {k: v for k, v in response.headers.items() if k.lower() not in drop}

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in response.aiter_raw():
                    yield chunk
            finally:
                await response.aclose()

        if request.method == "POST":
            self.calls += 1
        return StreamingResponse(body(), status_code=response.status_code, headers=out_headers)

    async def close(self) -> None:
        await self.stop("shut down")
        if self._http is not None:
            await self._http.aclose()


# ---------------------------------------------------------------- all services


class Services:
    """The configured local MCP servers, by name."""

    def __init__(self, entries: list[ServerEntry], hub: ActivityHub | None = None) -> None:
        self.hub = hub
        self.services: dict[str, Service] = {}
        self.listeners: list[Callable[[dict], None]] = []
        self._idle_task: asyncio.Task | None = None
        self.configure(entries)

    def configure(self, entries: list[ServerEntry]) -> list[Service]:
        """Apply a new configuration; returns services whose process must be stopped because
        they were removed, disabled or changed (the caller awaits their stop)."""
        stale = []
        wanted = {e.name: copy.deepcopy(e) for e in entries}  # the caller may edit its own list later
        for name, service in list(self.services.items()):
            entry = wanted.get(name)
            if entry is None or entry != service.entry:
                stale.append(self.services.pop(name))
        for name, entry in wanted.items():
            if name not in self.services:
                service = Service(entry, self.hub)
                service.listeners.append(lambda s: self._emit())
                self.services[name] = service
        return stale

    def _emit(self) -> None:
        snapshot = self.snapshot()
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                listener(snapshot)

    def snapshot(self) -> list[dict]:
        return [s.status() for s in self.services.values()]

    def get(self, name: str) -> Service | None:
        service = self.services.get(name)
        return service if service is not None and service.entry.enabled else None

    async def endpoint(self, request: Request) -> Response:
        name = request.path_params["name"]
        service = self.get(name)
        if service is None:
            known = ", ".join(n for n, s in self.services.items() if s.entry.enabled) or "none"
            return JSONResponse(
                _error(None, f"no local MCP server named {name!r} is enabled in winhand (enabled: {known})"),
                status_code=404,
            )
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_loop())
        return await service.handle(request)

    async def _idle_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for service in list(self.services.values()):
                minutes = service.entry.idle_stop_minutes
                busy = service.pending or any(s.streams for s in service.sessions.values())
                if (
                    minutes
                    and service.process
                    and not busy
                    and now - (service.last_used or now) > minutes * 60
                ):
                    await service.stop("stopped after being idle")

    async def close(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
        await asyncio.gather(*(s.close() for s in self.services.values()), return_exceptions=True)


def compose_app(mcp_app, services: Services | None):
    """winhand's own MCP app at /mcp plus one endpoint per local server at /mcp/<name>."""
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    if services is None:
        return mcp_app
    return Starlette(
        routes=[
            Route("/mcp/{name}", services.endpoint, methods=["GET", "POST", "DELETE"]),
            Mount("/", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )


# ---------------------------------------------------------------- testing a configuration


async def probe(entry: ServerEntry, timeout: float | None = None) -> dict:
    """Start a throwaway copy of the server, list what it offers, stop it. For the app's
    "test" button: nothing is shared with the running service."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport, StreamableHttpTransport

    started = time.time()
    timeout = timeout or max(entry.startup_timeout_s, 15)
    result: dict[str, Any] = {"name": entry.name, "ok": False}
    errlog_path = home() / "logs" / "mcp" / f"{entry.name}.test.log"
    errlog_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if entry.url:
            transport: Any = StreamableHttpTransport(entry.url, headers=entry.headers or None)
        else:
            env = winenv.build_env(entry.env)
            transport = StdioTransport(
                command=winenv.resolve_executable(entry.command, env),
                args=entry.args,
                env=env,
                cwd=os.path.expanduser(entry.cwd) if entry.cwd else None,
                keep_alive=False,
                log_file=errlog_path,
            )
        async with asyncio.timeout(timeout):
            async with Client(transport, timeout=timeout, mode="legacy") as client:
                init = client.initialize_result
                result["server"] = {"name": init.server_info.name, "version": init.server_info.version}
                result["protocol"] = init.protocol_version
                result["tools"] = [
                    {"name": t.name, "description": (t.description or "").strip().split("\n")[0][:200]}
                    for t in await client.list_tools()
                ]
                if init.capabilities.prompts:
                    with contextlib.suppress(Exception):
                        result["prompts"] = [p.name for p in await client.list_prompts()]
                if init.capabilities.resources:
                    with contextlib.suppress(Exception):
                        result["resources"] = len(await client.list_resources())
                result["ok"] = True
    except BaseException as exc:  # noqa: BLE001 - report whatever went wrong
        while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
            exc = exc.exceptions[0]
        if isinstance(exc, asyncio.CancelledError):
            raise
        if isinstance(exc, TimeoutError):
            result["error"] = f"timed out after {timeout:.0f}s"
        else:
            result["error"] = f"{type(exc).__name__}: {exc}"[:600]
    result["duration_ms"] = round((time.time() - started) * 1000)
    if not entry.url and errlog_path.exists():
        with contextlib.suppress(OSError):
            result["stderr_tail"] = errlog_path.read_text(encoding="utf-8", errors="replace").splitlines()[
                -15:
            ]
    return result
