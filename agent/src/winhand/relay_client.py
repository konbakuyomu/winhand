"""Outbound tunnel to the winhand relay.

The machine opens one WebSocket to the relay (no inbound ports). The relay
pushes each authenticated MCP HTTP request down the socket; we replay it
against winhand's own streamable-HTTP app on 127.0.0.1 and stream the response
(JSON or SSE) back in chunks. Protocol: see relay/src/protocol.ts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import platform
import random
import secrets
import socket
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import uvicorn
from uvicorn.lifespan.on import LifespanOn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from . import __version__

log = logging.getLogger("winhand.relay")

CHUNK_BYTES = 256 * 1024
REPLACED_CODE = 4000  # relay closes the old socket with this when a newer agent connects
PING_EVERY_S = 20
DEAD_AFTER_S = 70
STABLE_AFTER_S = 60  # a successful handshake alone doesn't mean a flapping connection recovered
_DROP_RESPONSE = {"connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"}
# Resuming calls across a dropped connection (relay frames `welcome`, `resume`, `ack`): the
# connection to the relay drops every so often and is back within seconds. A call in flight then
# keeps running here, its answer is kept, and the relay asks for the frames it missed.
RESUME_HOLD_S = 60.0  # a call whose connection dropped waits this long for the relay to ask for it
RESUME_MAX_BYTES = 8 * 1024 * 1024  # answers bigger than this are not kept: they end with the connection
RESUME_TOTAL_BYTES = 48 * 1024 * 1024
ACK_WAIT_S = 60.0  # a finished answer is kept this long when the relay does not confirm it


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Calls:
    """The calls this agent serves, across connections to the relay.

    Over a connection whose relay can resume (it said `welcome`), every answer frame is kept until
    the relay confirms the answer (`ack`): when the connection drops, the call keeps running, its
    frames pile up here, and on the next connection the relay asks for the ones it missed
    (`resume` from frame n). Over any other connection, and for answers too big to keep, a drop
    ends the call as before."""

    def __init__(self) -> None:
        self.tasks: dict[str, asyncio.Task] = {}
        self.frames: dict[str, list[str]] = {}  # kept answer frames, in order
        self.sizes: dict[str, int] = {}
        self.unkept: set[str] = set()
        self.ended: dict[str, float] = {}  # answer complete (end/error sent) at
        self.parked: dict[str, float] = {}  # connection dropped at; waiting for `resume`
        self.route: dict[str, Tunnel] = {}  # the connection a call's frames go out on now

    def total(self) -> int:
        return sum(self.sizes.values())

    def start(self, rid: str, tunnel: Tunnel, coro) -> None:
        self.route[rid] = tunnel
        if tunnel.resumable:
            self.frames[rid], self.sizes[rid] = [], 0
        else:
            self.unkept.add(rid)
        task = asyncio.create_task(coro)
        self.tasks[rid] = task

        def finished(done, r=rid):
            if self.tasks.get(r) is done:
                self.tasks.pop(r, None)
                if r not in self.frames:  # nothing kept: nothing to resume
                    self._forget(r)

        task.add_done_callback(finished)

    def attached(self, tunnel: Tunnel) -> dict[str, asyncio.Task]:
        return {r: t for r, t in self.tasks.items() if self.route.get(r) is tunnel}

    async def emit(self, rid: str, frame: dict, via: Tunnel | None = None) -> None:
        """Send one answer frame of a call (keeping it, when it may have to go again). `via` is
        the connection the call came on, for a call served without start() (its route)."""
        text = json.dumps(frame)
        if rid in self.frames:
            self.frames[rid].append(text)
            self.sizes[rid] += len(text)
            if self.sizes[rid] > RESUME_MAX_BYTES or self.total() > RESUME_TOTAL_BYTES:
                self._unkeep(rid)
        if frame["type"] in ("end", "error"):
            self.ended[rid] = time.monotonic()
        tunnel = self.route.get(rid)
        if tunnel is None and via is not None and rid not in self.parked and rid not in self.frames:
            tunnel = via  # never registered: answer on the connection it came on
        if tunnel is None:
            if rid in self.unkept:  # parked but not kept: nobody will ever ask for it
                raise asyncio.CancelledError
            return
        try:
            await tunnel.send_text(text)
        except ConnectionClosed:
            if rid in self.unkept:
                raise
            # kept: the frame goes out again when the relay resumes this call

    def _unkeep(self, rid: str) -> None:
        self.frames.pop(rid, None)
        self.sizes.pop(rid, None)
        self.unkept.add(rid)

    def _forget(self, rid: str) -> None:
        for table in (self.frames, self.sizes, self.ended, self.parked, self.route):
            table.pop(rid, None)
        self.unkept.discard(rid)

    def cancel(self, rid: str) -> None:
        task = self.tasks.get(rid)
        if task is not None:
            task.cancel()
        self._forget(rid)

    def disconnected(self, tunnel: Tunnel) -> list[asyncio.Task]:
        """The connection is gone: kept calls wait for the relay, the others end. Returns the
        tasks ended here, for the caller to await."""
        ended = []
        now = time.monotonic()
        for rid in [r for r, t in self.route.items() if t is tunnel]:
            del self.route[rid]
            if rid in self.frames:
                self.parked[rid] = now
            else:
                task = self.tasks.get(rid)
                if task is not None:
                    task.cancel()
                    ended.append(task)
                self._forget(rid)
        return ended

    def pending(self) -> list[str]:
        """Calls a relay may still be waiting for: parked, or kept and not yet confirmed."""
        return [r for r in self.frames if r not in self.route]

    async def resume(self, rid: str, tunnel: Tunnel, start: int) -> None:
        frames = self.frames.get(rid)
        if frames is None or (rid not in self.tasks and rid not in self.ended):
            await tunnel.send(
                {
                    "type": "error",
                    "id": rid,
                    "message": "winhand lost this call while the connection was down; check history() before resending",
                }
            )
            self._forget(rid)
            return
        self.parked.pop(rid, None)
        index = max(0, int(start))
        while index < len(frames):  # frames added meanwhile are sent too, in order
            await tunnel.send_text(frames[index])
            index += 1
            frames = self.frames.get(rid) or []
        if rid in self.frames:
            self.route[rid] = tunnel

    def ack(self, rid: str) -> None:
        if rid not in self.tasks:
            self._forget(rid)
        else:  # confirmed while still running cannot happen; keep it consistent anyway
            self.frames.pop(rid, None)
            self.sizes.pop(rid, None)

    def sweep(self) -> list[asyncio.Task]:
        """End parked calls nobody resumed in time and drop confirmations that never came."""
        now = time.monotonic()
        ended = []
        for rid, since in list(self.parked.items()):
            if now - since >= RESUME_HOLD_S:
                task = self.tasks.get(rid)
                if task is not None:
                    task.cancel()
                    ended.append(task)
                self._forget(rid)
        for rid, at in list(self.ended.items()):
            if now - at >= ACK_WAIT_S and rid not in self.parked and rid not in self.tasks:
                self._forget(rid)
        return ended

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for rid in list(self.frames) + list(self.route):
            self._forget(rid)


class Tunnel:
    """Serves relay frames arriving on one WebSocket connection."""

    def __init__(
        self,
        ws: ClientConnection,
        http: httpx.AsyncClient,
        calls: Calls | None = None,
        secret: str | None = None,
    ) -> None:
        self.ws = ws
        self.http = http
        self.secret = secret  # proves to the local port that a request comes through the tunnel
        self.calls = calls if calls is not None else Calls()
        self.requests: dict[str, dict] = {}
        self.send_lock = asyncio.Lock()
        self.last_seen = time.monotonic()
        self.resumable = False  # the relay said `welcome`: calls survive a drop of this connection
        self._jobs: set[asyncio.Task] = set()

    @property
    def tasks(self) -> dict[str, asyncio.Task]:
        return self.calls.attached(self)

    async def send(self, frame: dict) -> None:
        await self.send_text(json.dumps(frame))

    async def send_text(self, text: str) -> None:
        async with self.send_lock:
            await self.ws.send(text)

    async def run(self) -> None:
        receiver = asyncio.create_task(self._receive())
        keepalive = asyncio.create_task(self._keepalive())
        try:
            done, _ = await asyncio.wait({receiver, keepalive}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            tasks = [receiver, keepalive, *self._jobs]
            for task in tasks:
                task.cancel()
            ended = self.calls.disconnected(self)
            await asyncio.gather(*tasks, *ended, return_exceptions=True)
            self.requests.clear()

    def _spawn(self, coro) -> None:
        job = asyncio.create_task(coro)
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _receive(self) -> None:
        async for message in self.ws:
            self.last_seen = time.monotonic()
            if message == "pong" or not isinstance(message, str):
                continue
            try:
                frame = json.loads(message)
            except ValueError:
                continue
            self._on_frame(frame)

    def _on_frame(self, frame: dict) -> None:
        kind, rid = frame.get("type"), frame.get("id")
        if kind == "req":
            self.requests[rid] = {**frame, "body": bytearray()}
        elif kind == "req_body" and rid in self.requests:
            self.requests[rid]["body"] += base64.b64decode(frame["data"])
        elif kind == "req_end" and rid in self.requests:
            request = self.requests.pop(rid)
            self.calls.start(rid, self, self._serve(rid, request))
        elif kind == "cancel":
            self.requests.pop(rid, None)
            self.calls.cancel(rid)
        elif kind == "welcome":
            self.resumable = bool(frame.get("resume"))
        elif kind == "resume" and rid:
            self._spawn(self.calls.resume(rid, self, frame.get("from") or 0))
        elif kind == "ack" and rid:
            self.calls.ack(rid)

    async def _serve(self, rid: str, request: dict) -> None:
        from .mcp_bridge import LocalSecretOnly

        drop = ("host", LocalSecretOnly.HEADER)
        headers = [(k, v) for k, v in request["headers"] if k.lower() not in drop]
        if self.secret:
            headers.append((LocalSecretOnly.HEADER, self.secret))
        try:
            async with self.http.stream(
                request["method"], request["path"], headers=headers, content=bytes(request["body"])
            ) as response:
                head = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _DROP_RESPONSE]
                frame = {"type": "head", "id": rid, "status": response.status_code, "headers": head}
                await self.calls.emit(rid, frame, self)
                async for chunk in response.aiter_raw():
                    for i in range(0, len(chunk), CHUNK_BYTES):
                        part = chunk[i : i + CHUNK_BYTES]
                        frame = {"type": "body", "id": rid, "data": base64.b64encode(part).decode()}
                        await self.calls.emit(rid, frame, self)
                await self.calls.emit(rid, {"type": "end", "id": rid}, self)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # report instead of leaving the caller hanging
            log.warning("request %s failed: %s", rid, exc)
            with contextlib.suppress(Exception):
                frame = {"type": "error", "id": rid, "message": f"winhand agent error: {exc}"}
                await self.calls.emit(rid, frame, self)

    async def _keepalive(self) -> None:
        recovering = False
        while True:
            expected = time.monotonic() + PING_EVERY_S
            await asyncio.sleep(PING_EVERY_S)
            now = time.monotonic()
            if now - self.last_seen > DEAD_AFTER_S:
                if not recovering and now - expected > PING_EVERY_S:
                    # A local pause delayed both this watchdog and receiving queued pongs.
                    # Probe once after resuming, then use the next normal tick as the deadline.
                    recovering = True
                else:
                    log.warning("relay stopped answering; reconnecting")
                    await self.ws.close(code=4001, reason="keepalive timeout")
                    return
            else:
                recovering = False
            async with self.send_lock:
                await self.ws.send("ping")


class _OwnedLifespan(LifespanOn):
    task: asyncio.Task | None = None

    async def main(self) -> None:
        self.task = asyncio.current_task()
        await super().main()


class _LocalServer(uvicorn.Server):
    async def _startup_owned(self, sockets) -> None:
        try:
            await super().startup(sockets)
        except SystemExit as exc:
            # Uvicorn's CLI exit must not terminate the desktop event loop.
            raise RuntimeError(f"local MCP server startup failed (exit {exc.code})") from exc

    async def startup(self, sockets=None) -> None:
        # create_server() can yield after binding, before Uvicorn records its
        # listener. Let that step finish so cancellation cannot orphan a port.
        startup = asyncio.create_task(self._startup_owned(sockets))
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            while self.lifespan.task is None and not startup.done():
                await asyncio.sleep(0)
            if self.lifespan.task is not None:
                self.lifespan.task.cancel()
            await asyncio.gather(startup, return_exceptions=True)
            raise


async def _finish_local_server(server: uvicorn.Server) -> None:
    # Cancelling Server.serve() skips Uvicorn's normal shutdown, including when
    # startup has just bound a port but our caller has not received the server.
    listeners = getattr(server, "servers", ())
    for listener in listeners:
        listener.close()
    state = getattr(server, "server_state", None)
    for connection in list(getattr(state, "connections", ())):
        connection.transport.close()
    requests = list(getattr(state, "tasks", ()))
    for request in requests:
        request.cancel()
    await asyncio.gather(*requests, return_exceptions=True)
    await asyncio.gather(*(listener.wait_closed() for listener in listeners), return_exceptions=True)
    task = getattr(getattr(server, "lifespan", None), "task", None)
    if task is not None:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def serve_local(app, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="on", timeout_graceful_shutdown=5
    )
    config.load()
    config.lifespan_class = _OwnedLifespan
    server = _LocalServer(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if task.done():
                await task
                raise RuntimeError("local MCP server exited before starting")
            if server.started:
                return server, task
            await asyncio.sleep(0.05)
        raise RuntimeError("local MCP server did not start")
    except BaseException:
        server.should_exit = True
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await _finish_local_server(server)
        raise


def hello_frame(pending: list[str] | None = None) -> dict:
    return {
        "type": "hello",
        "version": __version__,
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "resume": 1,  # this agent keeps calls across a dropped connection (see Calls)
        "pending": list(pending or []),
    }


class AlreadyRunning(RuntimeError):
    pass


class Replaced(RuntimeError):
    """Another agent took over this relay; reconnecting would only fight over it."""


StatusCallback = Callable[..., None]


@contextlib.contextmanager
def single_instance(lock_path: Path):
    """Refuse to start a second `winhand connect` on this machine: two agents would
    keep replacing each other at the relay and break every in-flight request."""
    import psutil

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            pid = int(lock_path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and psutil.pid_exists(pid):
            try:
                alive = "winhand" in " ".join(psutil.Process(pid).cmdline()).lower()
            except psutil.Error:
                alive = False
            if alive:
                raise AlreadyRunning(f"`winhand connect` is already running (pid {pid}); stop it first")
    lock_path.write_text(str(os.getpid()))
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            if lock_path.read_text().strip() == str(os.getpid()):
                lock_path.unlink()


async def run_forever(
    url: str, token: str, *, mcp=None, services=None, stop: asyncio.Event | None = None
) -> None:
    """Keep a tunnel to the relay up until `stop` is set (or forever)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from .config import home

    with single_instance(home() / "connect.lock"):
        try:
            await run_tunnel(url, token, mcp=mcp, services=services, stop=stop)
        except Replaced as exc:
            log.error("%s", exc)
            raise SystemExit(3) from None


async def run_tunnel(
    url: str,
    token: str,
    *,
    mcp=None,
    services=None,
    stop: asyncio.Event | None = None,
    on_status: StatusCallback | None = None,
) -> None:
    """The reconnect loop. `on_status(state, **detail)` hears every transition:
    connecting, online, offline, refused, replaced, stopped."""

    def status(state: str, **detail) -> None:
        if on_status is not None:
            with contextlib.suppress(Exception):
                on_status(state, **detail)

    if mcp is None:
        from .server import build_server

        mcp = build_server()
    from . import transfer

    transfer.set_relay(url)  # one-time file links point at the relay's public origin
    port = _free_port()
    from .mcp_bridge import compose_app

    # the port answers only this tunnel (a secret made for this run) and checks Host/Origin, so
    # neither another program on the machine nor a web page (DNS rebinding) can call the tools
    secret = secrets.token_urlsafe(32)
    app = compose_app(mcp.http_app(host_origin_protection="auto"), services, secret)
    server, server_task = await serve_local(app, port)
    backoff = 1.0
    attempt = 0
    stop = stop or asyncio.Event()
    calls = Calls()

    async def sweeping() -> None:
        while True:
            await asyncio.sleep(5)
            ended = calls.sweep()
            if ended:
                await asyncio.gather(*ended, return_exceptions=True)

    sweeper = asyncio.create_task(sweeping())

    def check_replaced(code: int | None) -> None:
        if code == REPLACED_CODE:
            status("replaced")
            raise Replaced(
                "another winhand agent (possibly on another machine) took over this relay; "
                "stopped instead of fighting over the connection"
            )

    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None) as http:
            while not stop.is_set():
                attempt += 1
                status("connecting", attempt=attempt)
                reason = None
                connected_at = None
                try:
                    async with connect(
                        url,
                        additional_headers={"Authorization": f"Bearer {token}"},
                        max_size=None,
                        ping_interval=None,
                        open_timeout=20,
                        close_timeout=5,
                        # Race IPv6/IPv4: half-working IPv6 is common and otherwise stalls the connect.
                        happy_eyeballs_delay=0.25,
                    ) as ws:
                        log.info("connected to relay %s", url)
                        connected_at = time.monotonic()
                        await ws.send(json.dumps(hello_frame(calls.pending())))
                        status("online", connected_at=time.time())
                        tunnel = asyncio.create_task(Tunnel(ws, http, calls, secret).run())
                        stopper = asyncio.create_task(stop.wait())
                        try:
                            done, _ = await asyncio.wait(
                                {tunnel, stopper}, return_when=asyncio.FIRST_COMPLETED
                            )
                            if stopper in done:
                                await ws.close()
                                break
                            if not tunnel.cancelled() and tunnel.exception() is not None:
                                log.warning("relay connection lost: %s", tunnel.exception())
                                reason = f"connection lost: {tunnel.exception()}"
                            check_replaced(ws.close_code)
                            log.info("relay connection closed")
                            reason = reason or f"relay closed the connection ({ws.close_code})"
                        finally:
                            stopper.cancel()
                            tunnel.cancel()
                            await asyncio.gather(tunnel, stopper, return_exceptions=True)
                except InvalidStatus as exc:
                    code = exc.response.status_code
                    hint = ": check the device token" if code == 401 else ""
                    log.error("relay refused the connection (HTTP %s)%s", code, hint)
                    reason = f"HTTP {code}{hint}"
                    if code == 401:
                        backoff = max(backoff, 30.0)
                except ConnectionClosed as exc:
                    check_replaced(exc.rcvd.code if exc.rcvd is not None else None)
                    log.warning("relay connection failed: %s", exc)
                    reason = str(exc) or type(exc).__name__
                except (OSError, InvalidURI, TimeoutError) as exc:
                    log.warning("relay connection failed: %s", exc)
                    reason = str(exc) or type(exc).__name__
                except Replaced:
                    raise
                except Exception as exc:  # never let one bad connection end the agent
                    log.exception("unexpected relay error, reconnecting: %s", exc)
                    reason = f"unexpected error: {exc}"
                finally:
                    if connected_at is not None and time.monotonic() - connected_at >= STABLE_AFTER_S:
                        backoff, attempt = 1.0, 0
                if stop.is_set():
                    break
                retry_in_s = min(backoff * random.uniform(1.0, 1.2), 30.0)
                status(
                    "refused" if reason and reason.startswith("HTTP 401") else "offline",
                    reason=reason,
                    retry_in_s=retry_in_s,
                )
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=retry_in_s)
                backoff = min(backoff * 2, 30.0)
    finally:
        sweeper.cancel()
        await asyncio.gather(sweeper, return_exceptions=True)
        await calls.close()
        status("stopped")
        server.should_exit = True
        try:
            await server_task
        finally:
            await _finish_local_server(server)
