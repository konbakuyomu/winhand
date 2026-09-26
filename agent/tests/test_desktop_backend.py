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
    missing = next(e for e in hub.snapshot() if e["title"] == "fs_read")
    assert missing["status"] == "error" and "no such file" in missing["summary"]
    assert hub.counts["error"] == 1
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


async def test_local_mcp_servers_are_managed_from_the_app(tmp_path):
    from pathlib import Path

    fake = str(Path(__file__).parent / "fake_mcp_server.py")
    (tmp_path / "config.toml").write_text(
        "[relay]\nurl = 'wss://relay.example/agent'\ndevice_token = 'tok'\n", encoding="utf-8"
    )
    user = tmp_path / "user"
    (user / ".codex").mkdir(parents=True)
    (user / ".codex" / "config.toml").write_text(
        "[mcp_servers.camera]\ncommand = 'uv'\nargs = ['run', 'camera-mcp']\n", encoding="utf-8"
    )
    env = {**os.environ, "WINHAND_HOME": str(tmp_path), "HOME": str(user), "USERPROFILE": str(user)}
    env.pop("APPDATA", None)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "winhand.cli", "desktop-backend",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env,
    )  # fmt: skip
    backend = BackendProcess(proc)
    try:
        init = (await backend.call("initialize", protocol_version=1, connect=False))["result"]
        assert init["mcp"] == {"base_url": "https://relay.example/mcp", "servers": []}

        entry = {
            "name": "fake",
            "command": sys.executable,
            "args": ["-u", fake],
            "env": {"API_KEY": "s3cret", "LEVEL": "1"},
        }
        state = (await backend.call("mcp_save", entry=entry))["result"]
        (saved,) = state["servers"]
        assert saved["public_url"] == "https://relay.example/mcp/fake"
        assert saved["env"] == {"API_KEY": "••••••••", "LEVEL": "1"}  # secrets never reach the app

        # the app sends the mask back for a secret it did not touch: the stored value stays
        entry.update(env=saved["env"], description="测试服务")
        await backend.call("mcp_save", entry=entry, original_name="fake")
        stored = tomllib.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
        assert stored["mcp_servers"]["fake"]["env"] == {"API_KEY": "s3cret", "LEVEL": "1"}
        assert stored["relay"]["device_token"] == "tok"

        bad = await backend.call("mcp_save", entry={**entry, "name": "has space"})
        assert bad["error"]["code"] == "invalid_name"
        dup = await backend.call("mcp_save", entry=entry)
        assert dup["error"]["code"] == "duplicate"

        tested = (await backend.call("mcp_test", name="fake"))["result"]
        assert tested["ok"] and {"echo", "slow"} <= {t["name"] for t in tested["tools"]}

        await backend.call("mcp_start", name="fake")
        running = await backend.event(
            "mcp", lambda d: d["servers"] and d["servers"][0]["status"].get("state") == "running"
        )
        assert running["servers"][0]["status"]["server"]["name"] == "fake-local"
        off = (await backend.call("mcp_set_enabled", name="fake", enabled=False))["result"]
        assert off["servers"][0]["enabled"] is False and off["servers"][0]["status"]["state"] == "stopped"

        listed = (await backend.call("mcp_list"))["result"]
        assert [(c["name"], c["source"], c["already"]) for c in listed["candidates"]] == [
            ("camera", "Codex", False)
        ]
        imported = (await backend.call("mcp_import", names=["camera"]))["result"]
        assert imported["added"] == ["camera"] and [s["name"] for s in imported["servers"]] == [
            "fake",
            "camera",
        ]

        left = (await backend.call("mcp_delete", name="fake"))["result"]
        assert [s["name"] for s in left["servers"]] == ["camera"]
        titles = [
            a["title"] for a in (await backend.call("get_state"))["result"]["activity"] if a["kind"] == "mcp"
        ]
        assert "添加了 MCP 服务 fake" in titles and "删除了 MCP 服务 fake" in titles
    finally:
        backend.proc.stdin.close()
        await asyncio.wait_for(backend.proc.wait(), 15)
