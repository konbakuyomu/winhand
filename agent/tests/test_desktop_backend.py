"""The tray app's backend: JSON-lines protocol, live status and the tool-call timeline."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tomllib

from fastmcp import Client
from test_relay_client import FakeRelay, _start

from winhand.activity import ActivityHub, ToolActivity, preview
from winhand.config import Config
from winhand.server import build_server


class BackendProcess:
    def __init__(self, proc: asyncio.subprocess.Process):
        self.proc = proc
        self.next_id = 0
        self.events: list[dict] = []
        self.replies: dict[int, dict] = {}
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        while line := await self.proc.stdout.readline():
            message = json.loads(line)  # anything that is not protocol JSON fails the test here
            if "event" in message:
                self.events.append(message)
            else:
                self.replies[message["id"]] = message

    async def call(self, method: str, **params) -> dict:
        self.next_id += 1
        rid = self.next_id
        self.proc.stdin.write(json.dumps({"id": rid, "method": method, "params": params}).encode() + b"\n")
        await self.proc.stdin.drain()
        for _ in range(300):
            if rid in self.replies:
                return self.replies.pop(rid)
            await asyncio.sleep(0.05)
        raise AssertionError(f"no reply to {method}")

    async def event(self, name: str, predicate=lambda d: True, timeout=15) -> dict:
        for _ in range(int(timeout / 0.05)):
            for message in self.events:
                if message["event"] == name and predicate(message["data"]):
                    return message["data"]
            await asyncio.sleep(0.05)
        raise AssertionError(f"no {name} event; saw {[e['event'] for e in self.events]}")


async def spawn(tmp_path) -> BackendProcess:
    env = {**os.environ, "WINHAND_HOME": str(tmp_path)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "winhand.cli", "desktop-backend",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
    )  # fmt: skip
    return BackendProcess(proc)


async def test_backend_reports_status_and_follows_relay_settings(tmp_path):
    relay = FakeRelay(["hold"])
    server, url = await _start(relay)
    backend = await spawn(tmp_path)
    try:
        init = (await backend.call("initialize", protocol_version=1))["result"]
        assert init["protocol_version"] == 1 and init["generation"]
        assert init["status"]["state"] == "unconfigured"
        assert init["paths"]["home"] == str(tmp_path)

        bad = await backend.call("set_relay", url="https://nope", token="tok")
        assert bad["error"]["code"] == "invalid_url"

        await backend.call("set_relay", url=url, token="tok")
        online = await backend.event("status", lambda d: d["state"] == "online")
        assert online["relay"] == "127.0.0.1" and online["generation"] == init["generation"]
        assert relay.connections == 1
        saved = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert saved["relay"] == {"url": url, "device_token": "tok"}

        paused = (await backend.call("disconnect"))["result"]["status"]
        assert paused["state"] == "paused"
        state = (await backend.call("get_state"))["result"]
        titles = [a["title"] for a in state["activity"] if a["kind"] == "connection"]
        assert "已连接到中转" in titles and "已手动断开" in titles

        assert (await backend.call("shutdown"))["result"]["ok"] is True
        assert await asyncio.wait_for(backend.proc.wait(), 15) == 0
    finally:
        if backend.proc.returncode is None:
            backend.proc.kill()
        server.close()


async def test_backend_exits_when_the_app_goes_away(tmp_path):
    backend = await spawn(tmp_path)
    await backend.call("initialize", protocol_version=1)
    backend.proc.stdin.close()
    assert await asyncio.wait_for(backend.proc.wait(), 15) == 0


async def test_rejects_other_protocol_versions(tmp_path):
    backend = await spawn(tmp_path)
    reply = await backend.call("initialize", protocol_version=99)
    assert reply["error"]["code"] == "protocol_mismatch"
    backend.proc.stdin.close()
    await asyncio.wait_for(backend.proc.wait(), 15)


async def test_tool_calls_are_recorded_live_and_on_disk(tmp_path):
    hub = ActivityHub(tmp_path)
    seen = []
    hub.listeners.append(lambda e: seen.append((e["title"], e["status"])))
    mcp = build_server(Config())
    mcp.add_middleware(ToolActivity(hub))
    async with Client(mcp) as client:
        await client.call_tool("run", {"command": "echo timeline"})
        await client.call_tool("fs_read", {"path": str(tmp_path / "missing.txt")}, raise_on_error=False)
    assert ("run", "running") in seen and ("run", "ok") in seen
    run = next(e for e in hub.snapshot() if e["title"] == "run")
    assert run["args"] == {"command": "echo timeline"} and "exit_code=0" in run["summary"]
    assert run["duration_ms"] >= 0
    lines = [json.loads(x) for x in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()]
    assert [x["status"] for x in lines if x["title"] == "run"] == ["ok"]  # only finished calls are persisted

    reloaded = ActivityHub(tmp_path)
    reloaded.load_today()
    assert [e["title"] for e in reloaded.snapshot()] == [e["title"] for e in lines]


def test_argument_preview_hides_secrets_and_truncates():
    shown = preview({"content": "x" * 1000, "env": {"TOKEN": "abc"}, "args": list(range(30))})
    assert shown["env"] == "•••"
    assert shown["content"].endswith("(1000 chars)") and len(shown["content"]) < 300
    assert shown["args"][-1] == "… (30 items)"
