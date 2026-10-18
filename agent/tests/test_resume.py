"""winhand 0.7.0: a call in flight survives a dropped connection to the relay (resume)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx

from winhand import relay_client as rc


class Socket:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.sent = []
        self.closed = []
        self.fail = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.queue.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, data):
        if self.fail:
            from websockets.exceptions import ConnectionClosedError

            raise ConnectionClosedError(None, None)
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed.append(code)
        self.queue.put_nowait(None)

    def frames(self, kind=None):
        out = [json.loads(m) for m in self.sent if m not in ("ping", "pong")]
        return [f for f in out if kind is None or f["type"] == kind]


async def turns(n=30):
    for _ in range(n):
        await asyncio.sleep(0)


def slow_app(release: asyncio.Event, chunks=(b"one,", b"two,", b"three")):
    """A local server whose answer streams in parts, the last after `release`."""

    async def handler(request):
        async def body():
            yield chunks[0]
            await release.wait()
            for part in chunks[1:]:
                yield part

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream(body()))

    return httpx.MockTransport(handler)


class _Stream(httpx.AsyncByteStream):
    def __init__(self, gen):
        self.gen = gen

    async def __aiter__(self):
        async for part in self.gen:
            yield part


def request(ws, rid="r1"):
    for frame in (
        {"type": "req", "id": rid, "method": "POST", "path": "/mcp", "headers": []},
        {"type": "req_body", "id": rid, "data": base64.b64encode(b"{}").decode()},
        {"type": "req_end", "id": rid},
    ):
        ws.queue.put_nowait(json.dumps(frame))


def body_of(frames):
    return b"".join(base64.b64decode(f["data"]) for f in frames if f["type"] == "body")


async def test_a_call_survives_a_drop_and_is_resumed_where_the_relay_stopped():
    release = asyncio.Event()
    calls = rc.Calls()
    async with httpx.AsyncClient(base_url="http://local", transport=slow_app(release)) as http:
        first = Socket()
        first.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        request(first)
        tunnel = asyncio.create_task(rc.Tunnel(first, http, calls).run())
        await turns()
        assert [f["type"] for f in first.frames()] == ["head", "body"]
        first.queue.put_nowait(None)  # the connection drops
        await asyncio.wait_for(tunnel, 5)
        assert calls.pending() == ["r1"] and "r1" in calls.tasks and not calls.tasks["r1"].done()
        release.set()  # the answer goes on while nobody is connected
        await turns()

        second = Socket()
        second.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        second.queue.put_nowait(json.dumps({"type": "resume", "id": "r1", "from": 2}))  # got head + body 1
        tunnel = asyncio.create_task(rc.Tunnel(second, http, calls).run())
        await turns()
        frames = second.frames()
        assert [f["type"] for f in frames] == ["body", "body", "end"]
        assert body_of(first.frames()) + body_of(frames) == b"one,two,three"
        second.queue.put_nowait(json.dumps({"type": "ack", "id": "r1"}))
        await turns()
        assert calls.pending() == [] and not calls.frames
        second.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)


async def test_without_a_welcome_a_drop_ends_the_call_as_before():
    release = asyncio.Event()
    calls = rc.Calls()
    async with httpx.AsyncClient(base_url="http://local", transport=slow_app(release)) as http:
        ws = Socket()
        request(ws)
        tunnel = asyncio.create_task(rc.Tunnel(ws, http, calls).run())
        await turns()
        task = calls.tasks["r1"]
        ws.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)
        assert task.cancelled() and calls.pending() == [] and not calls.tasks


async def test_a_relay_that_gave_up_cancels_the_kept_call():
    release = asyncio.Event()
    calls = rc.Calls()
    async with httpx.AsyncClient(base_url="http://local", transport=slow_app(release)) as http:
        ws = Socket()
        ws.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        request(ws)
        tunnel = asyncio.create_task(rc.Tunnel(ws, http, calls).run())
        await turns()
        task = calls.tasks["r1"]
        ws.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)
        assert rc.hello_frame(calls.pending())["pending"] == ["r1"]
        again = Socket()
        again.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        again.queue.put_nowait(json.dumps({"type": "cancel", "id": "r1"}))
        tunnel = asyncio.create_task(rc.Tunnel(again, http, calls).run())
        await turns()
        assert task.cancelled() and calls.pending() == []
        again.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)


async def test_parked_calls_nobody_resumes_end_after_the_hold(monkeypatch):
    release = asyncio.Event()
    calls = rc.Calls()
    now = [1000.0]
    monkeypatch.setattr(rc.time, "monotonic", lambda: now[0])
    async with httpx.AsyncClient(base_url="http://local", transport=slow_app(release)) as http:
        ws = Socket()
        ws.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        request(ws)
        tunnel = asyncio.create_task(rc.Tunnel(ws, http, calls).run())
        await turns()
        task = calls.tasks["r1"]
        ws.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)
        assert calls.sweep() == []
        now[0] += rc.RESUME_HOLD_S + 1
        ended = calls.sweep()
        await asyncio.gather(*ended, return_exceptions=True)
        assert task.cancelled() and calls.pending() == []


async def test_an_answer_too_big_to_keep_ends_with_the_connection(monkeypatch):
    monkeypatch.setattr(rc, "RESUME_MAX_BYTES", 10)
    release = asyncio.Event()
    calls = rc.Calls()
    async with httpx.AsyncClient(base_url="http://local", transport=slow_app(release)) as http:
        ws = Socket()
        ws.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
        request(ws)
        tunnel = asyncio.create_task(rc.Tunnel(ws, http, calls).run())
        await turns()
        task = calls.tasks["r1"]
        assert "r1" in calls.unkept
        ws.queue.put_nowait(None)
        await asyncio.wait_for(tunnel, 5)
        assert task.cancelled() and calls.pending() == []


async def test_resume_of_an_unknown_call_answers_with_an_error():
    calls = rc.Calls()
    ws = Socket()
    ws.queue.put_nowait(json.dumps({"type": "welcome", "resume": 1}))
    ws.queue.put_nowait(json.dumps({"type": "resume", "id": "gone", "from": 0}))
    tunnel = asyncio.create_task(rc.Tunnel(ws, None, calls).run())
    await turns()
    (error,) = ws.frames("error")
    assert error["id"] == "gone" and "lost this call" in error["message"]
    ws.queue.put_nowait(None)
    await asyncio.wait_for(tunnel, 5)
