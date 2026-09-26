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
import platform
import socket
import time

import httpx
import uvicorn
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from . import __version__

log = logging.getLogger("winhand.relay")

CHUNK_BYTES = 256 * 1024
PING_EVERY_S = 20
DEAD_AFTER_S = 70
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
        keepalive = asyncio.create_task(self._keepalive())
        try:
            async for message in self.ws:
                self.last_seen = time.monotonic()
                if message == "pong" or not isinstance(message, str):
                    continue
                try:
                    frame = json.loads(message)
                except ValueError:
                    continue
                self._on_frame(frame)
        finally:
            keepalive.cancel()
            for task in self.tasks.values():
                task.cancel()

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
            task.add_done_callback(lambda _t, r=rid: self.tasks.pop(r, None))
        elif kind == "cancel":
            self.requests.pop(rid, None)
            task = self.tasks.pop(rid, None)
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
        while True:
            await asyncio.sleep(PING_EVERY_S)
            if time.monotonic() - self.last_seen > DEAD_AFTER_S:
                log.warning("relay stopped answering; reconnecting")
                await self.ws.close(code=4001, reason="keepalive timeout")
                return
            async with self.send_lock:
                await self.ws.send("ping")


async def serve_local(app, port: int) -> uvicorn.Server:
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            return server
        await asyncio.sleep(0.05)
    raise RuntimeError("local MCP server did not start")


def hello_frame() -> dict:
    return {
        "type": "hello",
        "version": __version__,
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
    }


async def run_forever(url: str, token: str, *, mcp=None, stop: asyncio.Event | None = None) -> None:
    """Keep a tunnel to the relay up until `stop` is set (or forever)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if mcp is None:
        from .server import build_server

        mcp = build_server()
    port = _free_port()
    server = await serve_local(mcp.http_app(), port)
    backoff = 1.0
    stop = stop or asyncio.Event()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=None) as http:
            while not stop.is_set():
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
                        backoff = 1.0
                        await ws.send(json.dumps(hello_frame()))
                        tunnel = asyncio.create_task(Tunnel(ws, http).run())
                        stopper = asyncio.create_task(stop.wait())
                        done, _ = await asyncio.wait({tunnel, stopper}, return_when=asyncio.FIRST_COMPLETED)
                        stopper.cancel()
                        if stopper in done:
                            tunnel.cancel()
                            await ws.close()
                            break
                        log.info("relay connection closed")
                except InvalidStatus as exc:
                    status = exc.response.status_code
                    hint = ": check the device token" if status == 401 else ""
                    log.error("relay refused the connection (HTTP %s)%s", status, hint)
                    if status == 401:
                        backoff = max(backoff, 30.0)
                except (OSError, ConnectionClosed, InvalidURI, TimeoutError) as exc:
                    log.warning("relay connection failed: %s", exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                backoff = min(backoff * 2, 30.0)
    finally:
        server.should_exit = True
        await asyncio.sleep(0.2)
