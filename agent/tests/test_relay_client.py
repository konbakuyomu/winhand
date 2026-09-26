"""The tunnel client must survive dropped connections and step aside when replaced."""

from __future__ import annotations

import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from winhand.config import Config
from winhand.relay_client import REPLACED_CODE, run_forever
from winhand.server import build_server


class FakeRelay:
    """Accepts agents; each new connection is handled by the next scripted behaviour."""

    def __init__(self, behaviours):
        self.behaviours = list(behaviours)
        self.connections = 0
        self.hellos = []

    async def handler(self, ws):
        self.connections += 1
        assert ws.request.headers["Authorization"] == "Bearer tok"
        self.hellos.append(json.loads(await ws.recv()))
        behaviour = self.behaviours.pop(0) if self.behaviours else "hold"
        if behaviour == "drop":
            ws.transport.abort()  # like a network failure: no close frame
        elif behaviour == "replace":
            await ws.close(REPLACED_CODE, "replaced by a new agent connection")
        elif behaviour == "rpc":
            body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "t", "version": "1"},
                    },
                }
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "req",
                        "id": "r1",
                        "method": "POST",
                        "path": "/mcp",
                        "headers": [
                            ["content-type", "application/json"],
                            ["accept", "application/json, text/event-stream"],
                        ],
                    }
                )
            )
            import base64

            await ws.send(
                json.dumps({"type": "req_body", "id": "r1", "data": base64.b64encode(body.encode()).decode()})
            )
            await ws.send(json.dumps({"type": "req_end", "id": "r1"}))
            frames = []
            async for msg in ws:
                if msg == "ping":
                    continue
                frame = json.loads(msg)
                frames.append(frame)
                if frame["type"] in ("end", "error"):
                    break
            self.rpc_frames = frames
            await ws.wait_closed()
        else:
            await ws.wait_closed()


async def _start(relay):
    server = await serve(relay.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"ws://127.0.0.1:{port}/agent"


async def test_reconnects_after_drop_and_serves_requests():
    relay = FakeRelay(["drop", "rpc"])
    server, url = await _start(relay)
    stop = asyncio.Event()
    agent = asyncio.create_task(run_forever(url, "tok", mcp=build_server(Config()), stop=stop))
    for _ in range(200):
        if getattr(relay, "rpc_frames", None):
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(agent, 10)
    server.close()
    assert relay.connections >= 2, "agent did not reconnect after the drop"
    head = relay.rpc_frames[0]
    assert head["type"] == "head" and head["status"] == 200, relay.rpc_frames
    assert relay.hellos[0]["type"] == "hello"


async def test_exits_when_replaced_by_another_agent():
    relay = FakeRelay(["replace"])
    server, url = await _start(relay)
    with pytest.raises(SystemExit) as exc:
        await asyncio.wait_for(run_forever(url, "tok", mcp=build_server(Config())), 15)
    server.close()
    assert exc.value.code == 3 and relay.connections == 1
