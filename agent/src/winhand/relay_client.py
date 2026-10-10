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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    """Serves relay frames arriving on one WebSocket connection."""

    def __init__(self, ws: ClientConnection, http: httpx.AsyncClient) -> None:
        self.ws = ws
        self.http = http
        self.requests: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.send_lock = asyncio.Lock()
        self.last_seen = time.monotonic()

    async def send(self, frame: dict) -> None:
        async with self.send_lock:
            await self.ws.send(json.dumps(frame))

    async def run(self) -> None:
        receiver = asyncio.create_task(self._receive())
        keepalive = asyncio.create_task(self._keepalive())
        try:
            done, _ = await asyncio.wait({receiver, keepalive}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            tasks = [receiver, keepalive, *self.tasks.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.tasks.clear()
            self.requests.clear()

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
            task = asyncio.create_task(self._serve(rid, request))
            self.tasks[rid] = task

            def finished(done, r=rid):
                if self.tasks.get(r) is done:
                    self.tasks.pop(r, None)

            task.add_done_callback(finished)
        elif kind == "cancel":
            self.requests.pop(rid, None)
            task = self.tasks.get(rid)
            if task:
                task.cancel()

    async def _serve(self, rid: str, request: dict) -> None:
        headers = [(k, v) for k, v in request["headers"] if k.lower() != "host"]
        try:
            async with self.http.stream(
                request["method"], request["path"], headers=headers, content=bytes(request["body"])
            ) as response:
                head = [(k, v) for k, v in response.headers.multi_items() if k.lower() not in _DROP_RESPONSE]
                await self.send({"type": "head", "id": rid, "status": response.status_code, "headers": head})
                async for chunk in response.aiter_raw():
                    for i in range(0, len(chunk), CHUNK_BYTES):
                        part = chunk[i : i + CHUNK_BYTES]
                        await self.send({"type": "body", "id": rid, "data": base64.b64encode(part).decode()})
                await self.send({"type": "end", "id": rid})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # report instead of leaving the caller hanging
            log.warning("request %s failed: %s", rid, exc)
            with contextlib.suppress(Exception):
                await self.send({"type": "error", "id": rid, "message": f"winhand agent error: {exc}"})

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


def hello_frame() -> dict:
    return {
        "type": "hello",
        "version": __version__,
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
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

    server, server_task = await serve_local(compose_app(mcp.http_app(), services), port)
    backoff = 1.0
    attempt = 0
    stop = stop or asyncio.Event()

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
                        await ws.send(json.dumps(hello_frame()))
                        status("online", connected_at=time.time())
                        tunnel = asyncio.create_task(Tunnel(ws, http).run())
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
        status("stopped")
        server.should_exit = True
        try:
            await server_task
        finally:
            await _finish_local_server(server)
