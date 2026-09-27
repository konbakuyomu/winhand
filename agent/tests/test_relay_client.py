"""The tunnel client must survive dropped connections and step aside when replaced."""

from __future__ import annotations

import asyncio
import base64
import json
import sys

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


async def _rpc(ws, request, session=None, *, wait=True):
    """One HTTP MCP exchange over the real tunnel framing (JSON or SSE response)."""
    rid = str(request.get("id", "notification"))
    headers = [
        ["content-type", "application/json"],
        ["accept", "application/json, text/event-stream"],
        ["mcp-protocol-version", "2025-06-18"],
    ]
    if session:
        headers.append(["mcp-session-id", session])
    await ws.send(
        json.dumps({"type": "req", "id": rid, "method": "POST", "path": "/mcp", "headers": headers})
    )
    await ws.send(
        json.dumps(
            {"type": "req_body", "id": rid, "data": base64.b64encode(json.dumps(request).encode()).decode()}
        )
    )
    await ws.send(json.dumps({"type": "req_end", "id": rid}))
    if not wait:
        return None
    body, head = bytearray(), {}
    while True:
        message = await asyncio.wait_for(ws.recv(), 15)
        if message == "ping":
            await ws.send("pong")
            continue
        frame = json.loads(message)
        assert frame["id"] == rid, frame
        if frame["type"] == "head":
            head = frame
        elif frame["type"] == "body":
            body.extend(base64.b64decode(frame["data"]))
        elif frame["type"] == "end":
            break
        elif frame["type"] == "error":
            pytest.fail(str(frame))
    text = body.decode()
    assert head["status"] in (200, 202), (head, text)
    if text.startswith("{"):
        result = json.loads(text)
    else:
        events = [json.loads(line[5:].strip()) for line in text.splitlines() if line.startswith("data:")]
        result = next((event for event in events if event.get("id") == request.get("id")), {})
    return {k.lower(): v for k, v in head["headers"]}, result


async def test_drop_during_command_preserves_session_output_and_does_not_replay(tmp_path, manager):
    marker = tmp_path / "executions.txt"
    command = """import sys, time
from pathlib import Path
print('ready> ', end='', flush=True)
for line in sys.stdin:
    with Path(sys.argv[1]).open('a') as file:
        file.write(line)
    print('started', flush=True)
    time.sleep(1)
    print('finished\\nready> ', end='', flush=True)
"""
    completed = asyncio.get_running_loop().create_future()
    connections = 0
    saved = {}

    async def handler(ws):
        nonlocal connections
        connections += 1
        try:
            assert json.loads(await ws.recv())["type"] == "hello"
            headers, _ = await _rpc(
                ws,
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "recovery-test", "version": "1"},
                    },
                },
            )
            mcp_session = headers.get("mcp-session-id")
            await _rpc(ws, {"jsonrpc": "2.0", "method": "notifications/initialized"}, mcp_session)

            async def call(number, name, arguments, **kw):
                return await _rpc(
                    ws,
                    {
                        "jsonrpc": "2.0",
                        "id": number,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                    mcp_session,
                    **kw,
                )

            if connections == 1:
                _, started = await call(
                    1,
                    "session_start",
                    {
                        "transport": "pipe",
                        "command": [sys.executable, "-u", "-c", command, str(marker)],
                        "prompts": [r"^ready> ?$"],
                    },
                )
                start = started["result"]["structuredContent"]
                saved.update(id=start["session"]["id"], cursor=start["cursor"], pid=start["session"]["pid"])
                await call(
                    2,
                    "session_send",
                    {"id": saved["id"], "text": "go", "submit": True, "wait_s": 10},
                    wait=False,
                )
                for _ in range(100):
                    if marker.exists():
                        break
                    await asyncio.sleep(0.05)
                assert marker.exists(), "command did not start"
                ws.transport.abort()  # side effect happened, but its tool response was not delivered
                return
            _, read = await call(3, "session_read", {"id": saved["id"], "since": saved["cursor"]})
            output = read["result"]["structuredContent"]["output"]
            if "finished" not in output:
                await call(4, "session_wait", {"ids": saved["id"], "patterns": ["finished"], "timeout_s": 5})
                _, read = await call(5, "session_read", {"id": saved["id"], "since": saved["cursor"]})
                output = read["result"]["structuredContent"]["output"]
            assert "started" in output and "finished" in output, output
            session = manager.get(saved["id"])
            assert session.alive and session.transport.pid == saved["pid"]
            assert len(manager.list()) == 1 and marker.read_text().splitlines() == ["go"]
            completed.set_result(None)
            await ws.wait_closed()
        except Exception as exc:
            if not completed.done():
                completed.set_exception(exc)
            raise

    relay = await serve(handler, "127.0.0.1", 0)
    port = relay.sockets[0].getsockname()[1]
    stop = asyncio.Event()
    agent = asyncio.create_task(
        run_forever(f"ws://127.0.0.1:{port}", "tok", mcp=build_server(Config(), manager), stop=stop)
    )
    try:
        await asyncio.wait_for(completed, 30)
    finally:
        stop.set()
        await asyncio.wait_for(agent, 10)
        relay.close()
        await relay.wait_closed()
    assert connections == 2
