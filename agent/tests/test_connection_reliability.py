"""Deterministic connection races: no production URLs, commands or credentials."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.responses import StreamingResponse
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from winhand import desktop_backend as db
from winhand import mcp_bridge as mb
from winhand import relay_client as rc
from winhand.config import Config, ServerEntry


async def turns(n=16):
    for _ in range(n):
        await asyncio.sleep(0)


async def cancel(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def proxy_asyncio():
    return SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waiters = []

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future))
        await future

    def advance(self, now):
        self.now = now
        for deadline, future in self.waiters:
            if deadline <= now and not future.done():
                future.set_result(None)


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent, self.closed = [], []

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed.append(code)
        self.queue.put_nowait(None)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    api = proxy_asyncio()
    api.sleep = clock.sleep
    monkeypatch.setattr(rc, "asyncio", api)
    monkeypatch.setattr(rc, "time", SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now))
    return clock


@pytest.mark.parametrize("order", ["watchdog_first", "pong_first"])
async def test_pause_with_queued_pong_keeps_connection(clock, order):
    ws = Socket()
    tunnel = rc.Tunnel(ws, None)
    task = asyncio.create_task(tunnel.run())
    try:
        await turns()
        clock.advance(20)
        await turns()
        ws.queue.put_nowait("pong")
        await turns()
        if order == "watchdog_first":
            clock.advance(100)
            ws.queue.put_nowait("pong")
        else:
            clock.now = 100
            ws.queue.put_nowait("pong")
            clock.advance(100)
        await turns()
        assert ws.closed == []
        assert tunnel.last_seen == 100
        clock.advance(120)
        await turns()
        assert ws.closed == []
    finally:
        await cancel(task)


@pytest.mark.parametrize("paused", [False, True])
async def test_actual_dead_connection_still_closes(clock, paused):
    ws = Socket()
    task = asyncio.create_task(rc.Tunnel(ws, None).run())
    try:
        await turns()
        if paused:
            clock.advance(100)
            await turns()
            assert ws.closed == []  # one fresh probe after a local pause
            assert ws.sent == ["ping"]
            clock.advance(120)
        else:
            for tick in (20, 40, 60, 80):
                clock.advance(tick)
                await turns()
        await turns()
        assert ws.closed == [4001]
        assert task.done()
    finally:
        await cancel(task)


async def test_healthy_heartbeats(clock):
    ws = Socket()
    task = asyncio.create_task(rc.Tunnel(ws, None).run())
    try:
        await turns()
        for tick in (20, 40, 60, 80, 100):
            clock.advance(tick)
            await turns()
            ws.queue.put_nowait("pong")
            await turns()
        assert ws.sent == ["ping"] * 5 and not ws.closed
    finally:
        await cancel(task)


async def test_heartbeat_failure_is_observed(clock):
    ws = Socket()

    async def failed_send(_):
        raise OSError("fake send failure")

    ws.send = failed_send
    task = asyncio.create_task(rc.Tunnel(ws, None).run())
    try:
        await turns()
        clock.advance(20)
        await turns()
        assert task.done(), "heartbeat task must not silently die while receiver stays alive"
        with pytest.raises(OSError, match="fake send"):
            task.result()
    finally:
        await cancel(task)


class LocalServer:
    """Works with both pre-fix and fixed serve_local return values for regression proof."""

    def __init__(self):
        self.finished = asyncio.Event()
        self.task = asyncio.create_task(self.finished.wait())

    @property
    def should_exit(self):
        return self.finished.is_set()

    @should_exit.setter
    def should_exit(self, value):
        if value:
            self.finished.set()

    def __iter__(self):
        return iter((self, self.task))


async def run_script(monkeypatch, script, *, stop_online=False):
    now, waits, attempts, sockets = [0.0], [], [], []
    stop = asyncio.Event()
    server = LocalServer()
    api = proxy_asyncio()

    async def wait_for(awaitable, timeout):
        awaitable.close()
        waits.append(timeout)
        if len(waits) >= len(script):
            stop.set()
        raise TimeoutError

    async def no_delay(_):
        await asyncio.sleep(0)

    api.wait_for, api.sleep = wait_for, no_delay
    monkeypatch.setattr(rc, "asyncio", api)
    monkeypatch.setattr(rc, "time", SimpleNamespace(monotonic=lambda: now[0], time=lambda: now[0]))
    if hasattr(rc, "random"):
        monkeypatch.setattr(rc.random, "uniform", lambda *_: 1.0)
    monkeypatch.setattr(rc, "_free_port", lambda: 0)
    monkeypatch.setattr(rc, "hello_frame", lambda *_: {"type": "hello"})
    monkeypatch.setattr(mb, "compose_app", lambda app, services: app)

    async def serve_local(*_):
        return server

    monkeypatch.setattr(rc, "serve_local", serve_local)

    class Context:
        def __init__(self, value):
            self.value = value

        async def __aenter__(self):
            return self.value

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(rc, "httpx", SimpleNamespace(AsyncClient=lambda **_: Context(None)))

    class ScriptSocket(Socket):
        def __init__(self, scenario):
            super().__init__()
            self.scenario = scenario
            self.close_code = scenario.get("code", 1006)

        async def send(self, data):
            if self.scenario.get("hello_error"):
                raise ConnectionClosedError(Close(self.close_code, "fake"), None, None)
            await super().send(data)

        async def __anext__(self):
            now[0] += self.scenario.get("duration", 0)
            raise StopAsyncIteration

    def connect(*_, **__):
        scenario = script[min(len(sockets), len(script) - 1)]
        socket = ScriptSocket(scenario)
        sockets.append(socket)
        return Context(socket)

    monkeypatch.setattr(rc, "connect", connect)

    def status(state, **detail):
        if state == "connecting":
            attempts.append(detail["attempt"])
        if state == "online" and stop_online:
            stop.set()

    outcome = None
    try:
        await rc.run_tunnel(
            "ws://offline.invalid",
            "fake",
            mcp=SimpleNamespace(http_app=lambda: None),
            stop=stop,
            on_status=status,
        )
    except rc.Replaced:
        outcome = "replaced"
    finally:
        server.should_exit = True
        await server.task
    return SimpleNamespace(waits=waits, attempts=attempts, sockets=sockets, outcome=outcome)


@pytest.mark.parametrize("hello_error", [False, True])
async def test_replacement_always_stops(monkeypatch, hello_error):
    result = await run_script(monkeypatch, [{"code": 4000, "hello_error": hello_error}])
    assert result.outcome == "replaced" and len(result.sockets) == 1 and result.waits == []


@pytest.mark.parametrize("code", [1000, 1006, 4001])
async def test_other_closes_still_reconnect(monkeypatch, code):
    result = await run_script(monkeypatch, [{"code": code, "hello_error": True}] * 3)
    assert result.outcome is None and result.waits == [1, 2, 4]


async def test_short_connections_back_off(monkeypatch):
    result = await run_script(monkeypatch, [{}] * 4)
    assert result.waits == [1, 2, 4, 8] and result.attempts == [1, 2, 3, 4]


async def test_stable_connection_resets_backoff(monkeypatch):
    result = await run_script(monkeypatch, [{}, {}, {"duration": 61}, {}])
    assert result.waits == [1, 2, 1, 2] and result.attempts == [1, 2, 3, 1]


async def test_user_stop_does_not_retry(monkeypatch):
    result = await run_script(monkeypatch, [{}] * 3, stop_online=True)
    assert len(result.sockets) == 1 and result.waits == []


async def test_request_error_does_not_close_socket():
    ws = Socket()

    async def response(request):
        if request.url.path == "/bad":
            raise httpx.ConnectError("fake request failure")
        return httpx.Response(200, stream=httpx.ByteStream(b"ok"))

    async with httpx.AsyncClient(
        base_url="http://offline.invalid", transport=httpx.MockTransport(response)
    ) as http:
        tunnel = rc.Tunnel(ws, http)
        for rid, path in (("bad", "/bad"), ("good", "/good")):
            await tunnel._serve(rid, {"headers": [], "method": "GET", "path": path, "body": b""})
    frames = [json.loads(raw) for raw in ws.sent]
    assert [f["type"] for f in frames] == ["error", "head", "body", "end"]
    assert frames[-1]["id"] == "good" and not ws.closed


async def test_cancel_is_per_request_and_shutdown_awaits_cleanup():
    ws = Socket()
    tunnel = rc.Tunnel(ws, None)
    started, finished = set(), set()

    async def serve(rid, _):
        started.add(rid)
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.add(rid)

    tunnel._serve = serve
    task = asyncio.create_task(tunnel.run())
    try:
        for rid in ("one", "two"):
            tunnel._on_frame({"type": "req", "id": rid})
            tunnel._on_frame({"type": "req_end", "id": rid})
        await turns()
        assert started == {"one", "two"}
        tunnel._on_frame({"type": "cancel", "id": "one"})
        await turns()
        assert finished == {"one"} and not ws.closed
        await cancel(task)
        assert finished == {"one", "two"} and not tunnel.tasks
    finally:
        await cancel(task)


@pytest.fixture
def backend(monkeypatch):
    backend = db.Backend(lambda _: None)
    backend.cfg = Config(relay_url="ws://offline.invalid", device_token="fake")
    return backend


async def test_overlapping_reconnects_are_serialized(backend, monkeypatch):
    stopping, release = asyncio.Event(), asyncio.Event()
    active, peak, calls = set(), [0], []

    async def fake_tunnel(*_, stop, on_status, **__):
        index = len(calls)
        calls.append(on_status)
        active.add(index)
        peak[0] = max(peak[0], len(active))
        on_status("online")
        try:
            await stop.wait()
            if index == 0:
                stopping.set()
                await release.wait()
        finally:
            active.remove(index)

    monkeypatch.setattr(db, "run_tunnel", fake_tunnel)
    await backend.start_tunnel()
    await turns()
    first = asyncio.create_task(backend.start_tunnel())
    await stopping.wait()
    second = asyncio.create_task(backend.start_tunnel())
    try:
        await turns()
        assert len(calls) == 1, "second start bypassed the first shutdown"
    finally:
        release.set()
        await asyncio.gather(first, second)
        await turns()
        await backend.stop_tunnel()
    assert peak[0] == 1 and not active


async def test_stale_callbacks_do_not_change_current_connection(backend, monkeypatch):
    callbacks = []

    async def fake_tunnel(*_, stop, on_status, **__):
        callbacks.append(on_status)
        on_status("online")
        await stop.wait()

    monkeypatch.setattr(db, "run_tunnel", fake_tunnel)
    await backend.start_tunnel()
    await turns()
    await backend.start_tunnel()
    await turns()
    try:
        callbacks[0]("offline", reason="old generation")
        assert backend.status["state"] == "online"
        old = asyncio.get_running_loop().create_future()
        old.set_exception(rc.Replaced("old task"))
        backend._on_tunnel_done(old)
        assert backend.status["state"] == "online" and backend.tunnel is not None
    finally:
        await backend.stop_tunnel()


async def test_disconnect_and_exit_do_not_resurrect(backend, monkeypatch):
    calls = []

    async def fake_tunnel(*_, stop, on_status, **__):
        calls.append(on_status)
        on_status("online")
        await stop.wait()

    monkeypatch.setattr(db, "run_tunnel", fake_tunnel)
    await backend.start_tunnel()
    await turns()
    await backend.stop_tunnel()
    await turns()
    assert backend.tunnel is None and backend.status["state"] == "paused" and len(calls) == 1
    await backend.close()
    await backend.start_tunnel()
    await turns()
    try:
        assert backend.tunnel is None and len(calls) == 1
    finally:
        await backend.stop_tunnel(quiet=True)


async def test_stopping_an_already_cancelled_child_is_not_caller_cancellation(backend):
    old = asyncio.create_task(asyncio.sleep(100))
    old.cancel()
    await asyncio.gather(old, return_exceptions=True)
    backend.tunnel = old
    backend.tunnel_stop = asyncio.Event()
    await backend.stop_tunnel()
    assert backend.status["state"] == "paused" and backend.tunnel is None


async def test_cancelling_stop_propagates_and_finishes_child_cleanup(backend):
    finished = []

    async def child():
        try:
            await asyncio.Event().wait()
        finally:
            finished.append(True)

    backend.tunnel = asyncio.create_task(child())
    backend.tunnel_stop = asyncio.Event()
    await turns()
    stopper = asyncio.create_task(backend.stop_tunnel())
    await turns()
    stopper.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopper
    assert finished == [True] and backend.tunnel is None and backend.lock is None


@pytest.mark.parametrize("timeout", [False, True])
async def test_local_http_startup_failure_reclaims_lifespan(monkeypatch, timeout):
    entered, exited = asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            exited.set()

    if timeout:
        api = proxy_asyncio()

        async def fast_poll(_):
            await asyncio.sleep(0)

        api.sleep = fast_poll
        monkeypatch.setattr(rc, "asyncio", api)
    task = asyncio.create_task(rc.serve_local(app, 0))
    await entered.wait()
    if timeout:
        with pytest.raises(RuntimeError, match="did not start"):
            await task
    else:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert exited.is_set(), "ASGI lifespan must not survive a cancelled/failed startup"


async def test_startup_cancellation_closes_bound_listener_and_active_request(monkeypatch):
    ready, entered, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    lifetime_exited = asyncio.Event()
    captured = []
    server_class = getattr(rc, "_LocalServer", rc.uvicorn.Server)

    class Server(server_class):
        async def startup(self, sockets=None):
            await super().startup(sockets)
            ready.set()

    def make_server(config):
        server = Server(config)
        captured.append(server)
        return server

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            try:
                assert (await receive())["type"] == "lifespan.startup"
                await send({"type": "lifespan.startup.complete"})
                await receive()
                await send({"type": "lifespan.shutdown.complete"})
            finally:
                lifetime_exited.set()
        else:
            entered.set()
            try:
                await send({"type": "http.response.start", "status": 200, "headers": []})
                await asyncio.Event().wait()
            finally:
                exited.set()

    api = proxy_asyncio()

    async def pause_poll(_):
        await asyncio.Event().wait()

    api.sleep = pause_poll
    monkeypatch.setattr(rc, "asyncio", api)
    monkeypatch.setattr(rc, "uvicorn", SimpleNamespace(Config=rc.uvicorn.Config, Server=make_server))
    monkeypatch.setattr(rc, "_LocalServer", make_server, raising=False)
    starter = asyncio.create_task(rc.serve_local(app, 0))
    writer = None
    try:
        await asyncio.wait_for(ready.wait(), 2)
        server = captured[0]
        listener = server.servers[0]
        host, port = listener.sockets[0].getsockname()
        reader, writer = await asyncio.open_connection(host, port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(entered.wait(), 2)
        assert not starter.done() and listener.is_serving()
        starter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starter
        assert not listener.is_serving() and exited.is_set() and lifetime_exited.is_set()
        await asyncio.wait_for(reader.read(), 2)
        assert reader.at_eof()
        with pytest.raises(OSError):
            await asyncio.open_connection(host, port)
    finally:
        starter.cancel()
        await asyncio.gather(starter, return_exceptions=True)
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        # Baseline reproduction also owns any listener left by the old code.
        for server in captured:
            for listener in getattr(server, "servers", ()):
                listener.close()
            for connection in list(server.server_state.connections):
                connection.transport.close()
            for task in list(server.server_state.tasks):
                task.cancel()
            await asyncio.gather(*list(server.server_state.tasks), return_exceptions=True)


async def test_cancel_between_socket_binding_and_uvicorn_registration(monkeypatch):
    loop = asyncio.get_running_loop()
    create_server = loop.create_server
    bound, release = asyncio.Event(), asyncio.Event()
    listeners = []

    async def bind_before_registration(*args, **kwargs):
        listener = await create_server(*args, **kwargs)
        listeners.append(listener)
        bound.set()
        await release.wait()
        return listener

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        await receive()
        await send({"type": "lifespan.startup.complete"})
        await receive()
        await send({"type": "lifespan.shutdown.complete"})

    monkeypatch.setattr(loop, "create_server", bind_before_registration)
    starter = asyncio.create_task(rc.serve_local(app, 0))
    try:
        await asyncio.wait_for(bound.wait(), 2)
        host, port = listeners[0].sockets[0].getsockname()
        starter.cancel()
        await turns()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await starter
        assert not listeners[0].is_serving()
        with pytest.raises(OSError):
            await asyncio.open_connection(host, port)
    finally:
        release.set()
        starter.cancel()
        await asyncio.gather(starter, return_exceptions=True)
        for listener in listeners:
            listener.close()
            await listener.wait_closed()


async def test_startup_bind_failure_can_cancel_lifespan_shutdown():
    shutting, release, exited = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def app(scope, receive, send):
        assert scope["type"] == "lifespan"
        try:
            await receive()
            await send({"type": "lifespan.startup.complete"})
            await receive()
            shutting.set()
            await release.wait()
            await send({"type": "lifespan.shutdown.complete"})
        finally:
            exited.set()

    with rc.socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        starter = asyncio.create_task(rc.serve_local(app, occupied.getsockname()[1]))
        try:
            await asyncio.wait_for(shutting.wait(), 2)
            starter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(starter, 1)
            assert exited.is_set()
        finally:
            release.set()
            starter.cancel()
            await asyncio.gather(starter, return_exceptions=True)


@pytest.mark.parametrize("blocked_pipe", [False, True])
async def test_streaming_disconnect_cleans_every_pending_request(monkeypatch, blocked_pipe):
    service = mb.Service(ServerEntry(name="fake", command="unused"))
    stream = mb._Stream(due={1, 2, 3})
    session = mb._Session("fake", streams=[stream])
    service.pending = {uid: mb._Pending(session, uid, stream) for uid in stream.due}
    release = threading.Event()
    if not blocked_pipe:
        release.set()
    loop_thread = threading.get_ident()
    notifications = []

    def send_notice(message):
        assert threading.get_ident() != loop_thread, "pipe writes must not block the event loop"
        notifications.append(message["params"]["requestId"])
        release.wait(2)

    service.process = SimpleNamespace(alive=lambda: True, send=send_notice)
    monkeypatch.setattr(mb, "CANCEL_NOTIFY_TIMEOUT_S", 0.05, raising=False)
    started = asyncio.Event()

    async def send(message):
        if message["type"] == "http.response.start":
            started.set()

    async def receive():
        await started.wait()
        await asyncio.sleep(0)
        return {"type": "http.disconnect"}

    before = time.monotonic()
    try:
        await StreamingResponse(service._sse(session, stream))(
            {"type": "http", "asgi": {"spec_version": "2.3"}}, receive, send
        )
        assert not service.pending and not session.streams and not stream.due
        assert time.monotonic() - before < 0.75
        if not blocked_pipe:
            assert sorted(notifications) == [1, 2, 3]
    finally:
        release.set()
        await asyncio.sleep(0.02)


async def test_stop_during_initialized_notification_cannot_restore_running(monkeypatch):
    service = mb.Service(ServerEntry(name="fake", command="unused"))
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    class Process:
        pid = 123
        stderr = []
        live = True

        def alive(self):
            return self.live

        def stop(self):
            self.live = False

        def send(self, message):
            entered.set()
            assert threading.get_ident() != loop_thread
            release.wait(2)

    process = Process()
    monkeypatch.setattr(mb, "StdioProcess", lambda *_: process)

    async def initialized(*_):
        return {"result": {"serverInfo": {"name": "fake", "version": "1"}}}

    service._internal = initialized
    starter = asyncio.create_task(service.ensure_started())
    try:
        while not entered.is_set():
            await asyncio.sleep(0)
        await service.stop()
        release.set()
        with pytest.raises(mb.ServiceError, match="stopped during initialization"):
            await starter
        assert service.state == "stopped" and service.process is None and service.init_result is None
    finally:
        release.set()
        await asyncio.gather(starter, return_exceptions=True)
