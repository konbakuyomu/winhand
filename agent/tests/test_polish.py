"""winhand 0.4.4: call() is one entry in history and the timeline; a short shims check."""

from __future__ import annotations

import sys

from fastmcp import Client

from winhand import proc
from winhand.activity import ActivityHub, ToolActivity
from winhand.config import Config
from winhand.server import build_server


async def test_a_call_through_call_is_one_history_entry():
    async with Client(build_server(Config())) as client:
        await client.call_tool(
            "call",
            {"tool": "run", "arguments": {"command": sys.executable, "args": ["-c", "raise SystemExit(3)"]}},
        )
        await client.call_tool("call", {"tool": "fs_stat", "arguments": {"nope": 1}})
        await client.call_tool("call", {"tool": "history", "arguments": {}})
        calls = (await client.call_tool("history", {"limit": 10})).structured_content["calls"]
    assert [c["tool"] for c in calls] == ["fs_stat", "run"]
    bad, ran = calls
    assert ran["via"] == "call" and ran["exit_code"] == 3 and ran["status"] == "ok"
    assert ran["args"]["command"] == sys.executable
    assert bad["via"] == "call" and bad["status"] == "error" and "nope" in bad["summary"]


async def test_a_call_through_call_is_one_timeline_entry(tmp_path):
    hub = ActivityHub(tmp_path)
    seen = []
    hub.listeners.append(lambda e: seen.append((e["id"], e["title"], e["status"])))
    mcp = build_server(Config())
    mcp.add_middleware(ToolActivity(hub))
    async with Client(mcp) as client:
        await client.call_tool("call", {"tool": "run", "arguments": {"command": "echo timeline"}})
    entries = hub.snapshot()
    assert [e["title"] for e in entries] == ["run"]
    assert entries[0]["args"] == {"command": "echo timeline"} and entries[0]["status"] == "ok"
    assert {i for i, _, _ in seen} == {entries[0]["id"]}
    assert ("run", "running") in [(t, s) for _, t, s in seen]
    assert hub.counts["ok"] == 1 and hub.counts["error"] == 0


def test_shims_check_is_one_short_line():
    folders = [
        r"D:\scoop\shims",
        r"C:\Users\me\AppData\Local\mise\shims\\",
        r"D:\scoop\apps\mise\current\bin",
        *[rf"C:\Users\me\AppData\Local\mise\installs\tool{i}\latest\bin" for i in range(19)],
        r"C:\Windows\system32",
        "",
        r"D:\scoop\shims",
    ]
    line = proc._shim_summary(folders)
    assert line.startswith(r"D:\scoop\shims; C:\Users\me\AppData\Local\mise\shims (+19 mise tool folders)")
    assert "installs" not in line and len(line) < 200
    assert proc._shim_summary([r"C:\Windows", "/usr/bin"]) == "no scoop/mise shim folders on PATH"
    assert proc._shim_summary(["/home/me/.local/share/mise/installs/node/22/bin"]).startswith(
        "no shims folder (+1 mise tool folder)"
    )
