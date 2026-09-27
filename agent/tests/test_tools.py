from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from fastmcp.client import Client

from winhand import config, fs, proc, profiles
from winhand.config import Config, ServerEntry
from winhand.server import build_server

from .conftest import FAKE_APP

# -------------------------------------------------------------------- fs


def test_fs_read_numbers_lines_and_pages(tmp_path):
    p = tmp_path / "a.txt"
    p.write_text("\n".join(f"line {i}" for i in range(1, 11)), encoding="utf-8")
    out = fs.fs_read(str(p), offset=3, limit=2)
    assert out["content"].splitlines() == ["3\tline 3", "4\tline 4"]
    assert "offset=5" in out["more"] and out["total_lines"] == 10


def test_fs_reads_gbk_and_rejects_binary(tmp_path):
    gbk = tmp_path / "中文 目录" / "gbk.c"
    gbk.parent.mkdir()
    gbk.write_bytes("// 注释：初始化串口\n".encode("gbk"))
    out = fs.fs_read(str(gbk))
    assert out["encoding"] == "gb18030" and "初始化串口" in out["content"]
    binary = tmp_path / "fw.bin"
    binary.write_bytes(b"\x00\x01\x02" * 10)
    with pytest.raises(fs.FsError, match="binary"):
        fs.fs_read(str(binary))


def test_fs_edit_keeps_crlf_and_encoding(tmp_path):
    p = tmp_path / "main.c"
    p.write_bytes("int a = 1;\r\nint b = 2;\r\n// 中文\r\n".encode("gbk"))
    res = fs.fs_edit(str(p), "int a = 1;\nint b = 2;", "int a = 10;\nint b = 20;")
    assert res["replacements"] == 1 and res["line"] == 1
    assert p.read_bytes() == "int a = 10;\r\nint b = 20;\r\n// 中文\r\n".encode("gbk")


def test_fs_edit_errors_are_actionable(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("x = 1\nx = 1\n    y = 2\n")
    with pytest.raises(fs.FsError, match="2 times"):
        fs.fs_edit(str(p), "x = 1", "x = 3")
    with pytest.raises(fs.FsError, match="near line 3"):
        fs.fs_edit(str(p), "y = 2\n  z", "q")
    assert fs.fs_edit(str(p), "x = 1", "x = 3", replace_all=True)["replacements"] == 2


def test_fs_write_list_search_stat(tmp_path):
    fs.fs_write(str(tmp_path / "deep" / "dir" / "note.md"), "hello needle\n")
    fs.fs_write(str(tmp_path / "other.txt"), "no match\n")
    listing = fs.fs_list(str(tmp_path), depth=3)
    assert {"path": "deep/", "type": "dir"} in listing["entries"]
    found = fs.fs_search("needle", str(tmp_path))
    assert found["count"] == 1 and "note.md:1:hello needle" in found["matches"][0]
    assert fs.fs_stat(str(tmp_path / "nope"))["exists"] is False


# ------------------------------------------------------------------ proc


def test_run_with_args_and_shell():
    res = proc.run(sys.executable, ["-c", "import sys; print('a b'); sys.exit(4)"])
    assert res["exit_code"] == 4 and res["stdout"].strip() == "a b"
    res = proc.run("echo shell-ok")
    assert "shell-ok" in res["stdout"] and res["exit_code"] == 0


def test_run_timeout_kills_process():
    res = proc.run(
        sys.executable, ["-c", "import time; print('start', flush=True); time.sleep(30)"], timeout_s=1
    )
    assert res["timed_out"] and "start" in res["stdout"] and res["duration_ms"] < 10000


def test_run_answers_in_time_and_the_rest_follows_by_id():
    code = (
        "import sys, time\n"
        "print('first', flush=True)\n"
        "time.sleep(1.5)\n"
        "print('second', flush=True)\n"
        "sys.exit(3)\n"
    )
    res = proc.run(sys.executable, ["-c", code], timeout_s=30, reply_within_s=0.5)
    assert res["still_running"] and res["id"].startswith("run-") and "first" in res["stdout"]
    assert "job_status" in res["next"] and res["duration_ms"] < 5000
    status = proc.run_status(res["id"], since=res["cursor"])
    assert status["state"] == "running"
    deadline = time.monotonic() + 10
    while status["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.2)
        status = proc.run_status(res["id"], since=res["cursor"])
    assert status["state"] == "finished" and status["exit_code"] == 3
    assert "second" in status["output"] and "first" not in status["output"]


async def test_job_tools_follow_and_stop_a_run_that_is_still_going():
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(60)"], timeout_s=120, reply_within_s=0.3)
    async with Client(build_server(Config())) as client:
        status = (await client.call_tool("job_status", {"id": res["id"]})).structured_content
        assert status["state"] == "running"
        stopped = (await client.call_tool("job_stop", {"id": res["id"]})).structured_content
        assert stopped["state"] == "finished" and stopped["stopped"]
        missing = (await client.call_tool("job_status", {"id": "run-999999"})).structured_content
        assert "no running command" in missing["error"]


def test_run_still_kills_at_its_timeout_after_answering():
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(60)"], timeout_s=1.5, reply_within_s=0.3)
    assert res["still_running"]
    time.sleep(3)
    status = proc.run_status(res["id"])
    assert status["state"] == "finished" and status["timed_out"]


def test_run_passes_stdin():
    res = proc.run(sys.executable, ["-c", "import sys; print(sys.stdin.read().upper())"], stdin="hello")
    assert res["stdout"].strip() == "HELLO" and res["exit_code"] == 0


def test_split_command_keeps_windows_paths(monkeypatch):
    monkeypatch.setattr(proc.winenv, "IS_WINDOWS", True)
    assert proc.split_command(r'"C:\Program Files\x\a.exe" -f D:\a\b.cfg') == [
        r"C:\Program Files\x\a.exe",
        "-f",
        r"D:\a\b.cfg",
    ]


def test_sys_info_and_proc_list():
    info = proc.sys_info()
    assert info["python"] and "tools" in info
    assert proc.proc_list(name="python")["count"] >= 1


# ------------------------------------------------------- config & profiles


def test_import_from_other_clients_and_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        textwrap.dedent(r"""
        [mcp_servers.pyocd-debug]
        command = "uv"
        args = ["--directory", 'D:\Dev\20_个人项目\PYOCD调试MCP', "run", "pyocd-debug-mcp"]
        startup_timeout_sec = 90
        [mcp_servers.cam]
        command = 'C:\Users\me\uv.exe'
        args = []
        [mcp_servers.cam.env]
        LEVEL = "debug"
        [mcp_servers.node_repl]
        command = 'C:\\Users\\me\\AppData\\Local\\OpenAI\\Codex\\runtimes\\node\\node_repl.exe'
        [mcp_servers.node_repl.env]
        CODEX_HOME = 'C:\\Users\\me\\.codex'
    """),
        encoding="utf-8",
    )
    (tmp_path / "AppData" / "Claude").mkdir(parents=True)
    (tmp_path / "AppData" / "Claude" / "claude_desktop_config.json").write_text(
        '{"mcpServers": {"docs server": {"type": "http", "url": "http://127.0.0.1:9000/mcp"},'
        ' "old": {"type": "sse", "url": "http://127.0.0.1:9001/sse"}}}',
        encoding="utf-8",
    )
    sources = {(c["name"], c["source"]) for c in config.import_candidates()}
    assert sources == {
        ("pyocd-debug", "Codex"),
        ("cam", "Codex"),
        ("node_repl", "Codex"),
        ("docs server", "Claude Desktop"),
    }
    assert [c["name"] for c in config.import_candidates() if c["internal"]] == ["node_repl"]

    cfg, added = config.import_servers(None, Config())
    assert added == ["pyocd-debug", "cam", "docs-server"]  # names become URL-safe
    assert config.import_servers(None, cfg)[1] == []  # nothing twice, and Codex's own helper only by name
    assert config.import_servers(["node_repl"], cfg)[1] == ["node_repl"]
    path = config.save(cfg, tmp_path / "out.toml")
    again = {s.name: s for s in config.load(path).servers}
    assert again["pyocd-debug"].args[1] == r"D:\Dev\20_个人项目\PYOCD调试MCP"
    assert again["pyocd-debug"].startup_timeout_s == 90
    assert again["cam"].command == r"C:\Users\me\uv.exe" and again["cam"].env == {"LEVEL": "debug"}
    assert again["docs-server"].kind == "http" and again["docs-server"].url == "http://127.0.0.1:9000/mcp"


def test_legacy_gateway_section_still_loads(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[gateway.servers.cam]\ncommand = "uv"\nargs = ["run", "cam"]\n', encoding="utf-8")
    (entry,) = config.load(path).servers
    assert entry.name == "cam" and entry.args == ["run", "cam"] and entry.enabled
    config.save(config.load(path), path)
    assert "[mcp_servers.cam]" in path.read_text(encoding="utf-8")


def test_profiles_fill_vars_and_user_overrides(winhand_home):
    spec, extras = profiles.build_spec(profiles.get_profile("rtthread-msh"), variables={"port": "COM7"})
    assert spec.transport == "serial" and spec.port == "COM7" and spec.line_ending == "\r"
    with pytest.raises(ValueError, match="needs vars"):
        profiles.build_spec(profiles.get_profile("serial"))
    spec, extras = profiles.build_spec(profiles.get_profile("gdb"), extra_args=["fw.axf"])
    assert spec.argv[-1] == "fw.axf" and "set pagination off" in extras["init"]
    user = Path(winhand_home) / "profiles"
    user.mkdir(parents=True)
    (user / "mydev.toml").write_text(
        'transport = "tcp"\nhost = "10.0.0.2"\ntcp_port = 23\n'
        "description = \"lab device\"\nprompts = ['^MYDEV# $']\n"
    )
    names = {p["name"] for p in profiles.list_profiles()}
    assert {"mydev", "gdb", "menuconfig", "pyocd-gdbserver"} <= names


# ------------------------------------------------------------ MCP surface

CHILD = textwrap.dedent("""
    from fastmcp import FastMCP
    m = FastMCP("child")
    @m.tool
    def probe_list() -> list[str]:
        return ["probe-A"]
    m.run(show_banner=False)
""")


@pytest.mark.asyncio
async def test_mcp_tools_end_to_end(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(CHILD)
    cfg = Config(
        servers=[
            ServerEntry("pyocd", sys.executable, [str(child)]),
            ServerEntry("off", "nothing", enabled=False),
        ]
    )
    mcp = build_server(cfg)
    async with Client(mcp) as c:
        names = {t.name for t in await c.list_tools()}
        assert {"session_start", "session_wait", "fs_edit", "run"} <= names
        assert not any("probe_list" in n for n in names)  # local servers get their own endpoint
        info = (await c.call_tool("sys_info", {})).structured_content
        assert info["local_mcp_servers"]["names"] == ["pyocd"]

        start = (
            await c.call_tool(
                "session_start",
                {
                    "command": [sys.executable, "-u", FAKE_APP],
                    "prompts": [r"^fake> ?$"],
                    "name": "fake",
                },
            )
        ).structured_content
        sid = start["session"]["id"]
        assert start["state"] == "awaiting_input", start

        sent = (
            await c.call_tool("session_send", {"id": sid, "text": "echo hi there", "submit": True})
        ).structured_content
        assert "hi there" in sent["output"] and sent["state"] == "awaiting_input" and sent["next"]

        sent = (
            await c.call_tool("session_send", {"id": sid, "text": "secret", "submit": True})
        ).structured_content
        assert sent["state"] == "needs_user" and "session_prompt_user" in sent["next"]

        listed = (await c.call_tool("session_list", {})).structured_content["result"]
        assert listed[0]["id"] == sid

        stopped = (await c.call_tool("session_stop", {"id": sid, "force": True})).structured_content
        assert stopped["stopped"]

        bad = (await c.call_tool("session_start", {"profile": "no-such-profile"})).structured_content
        assert "unknown profile" in bad["error"]

        edited = (
            await c.call_tool("fs_edit", {"path": str(tmp_path / "missing.txt"), "old": "a", "new": "b"})
        ).structured_content
        assert "no such file" in edited["error"]


@pytest.mark.parametrize("transport", ["pty", "pipe"])
async def test_secret_dialog_masks_echo_in_results_screen_and_log(manager, fake_spec, monkeypatch, transport):
    import json

    from winhand.session.text import clean

    secret = "test-秘密-8374"
    monkeypatch.setattr("winhand.server.ask_secret", lambda *args: secret)
    session = manager.create(fake_spec(transport=transport, screen=True))
    async with Client(build_server(Config(), manager)) as client:
        await client.call_tool("session_wait", {"ids": session.id})
        await client.call_tool("session_send", {"id": session.id, "text": "secret-echo", "submit": True})
        result = (
            await client.call_tool("session_prompt_user", {"id": session.id, "message": "Test password"})
        ).structured_content
        assert result["sent"] and "accepted" in result["output"], result
        screen = (await client.call_tool("session_screen", {"id": session.id})).structured_content
        output = (await client.call_tool("session_read", {"id": session.id, "since": 0})).structured_content
    assert secret not in json.dumps([result, screen, output], ensure_ascii=False)
    assert secret not in clean(session.buffer.read(0).text)
    assert secret not in clean(session.buffer.log_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "outcome,verified",
    [("changed", True), ("unchanged", False), ("replaced", None), ("closed", None), ("unreadable", None)],
)
async def test_ui_reads_back_without_replaying_an_action(monkeypatch, outcome, verified):
    from winhand import tools_desktop as td

    target = {
        "index": 0,
        "hwnd": 44,
        "type": "Edit",
        "name": "",
        "value": "old",
        "native": True,
        "enabled": True,
        "patterns": [],
    }
    calls, reads = [], []

    def inspect(hwnd, **kwargs):
        reads.append(hwnd)
        current = dict(target)
        if calls:
            assert 0 < kwargs.get("timeout_s", 60) <= 3  # read-back cannot use the old 60-second UIA timeout
            if outcome == "closed":
                raise td.desktop.DesktopError("window closed")
            if outcome == "changed" and len(reads) >= 3:
                current["value"] = "new"
            elif outcome == "replaced":
                current.update(hwnd=55, value="new")
            elif outcome == "unreadable":
                current["value"] = None
        return {"window": "test", "elements": [current]}

    def act(control, action, value):
        calls.append((control["hwnd"], action, value))
        return "value set"

    monkeypatch.setattr(td.desktop, "find_window", lambda _: {"hwnd": 11, "title": "test"})
    monkeypatch.setattr(td, "_inspect_controls", inspect)
    monkeypatch.setattr(td.desktop, "native_act", act)
    async with Client(build_server(Config())) as client:
        result = (
            await client.call_tool(
                "ui",
                {
                    "window": "test",
                    "action": "set_value",
                    "control_type": "Edit",
                    "value": "new",
                    "verify_wait_s": 1 if outcome == "changed" else 0,
                },
            )
        ).structured_content
    assert result["verified"] is verified, result
    assert calls == [(44, "set_value", "new")]
    if outcome == "changed":
        assert result["element"]["value"] == "new" and len(reads) == 3
    elif outcome in ("replaced", "closed"):
        assert result["element"] is None and "before repeating" in result["verification"]


def test_connect_is_single_instance(tmp_path):
    from winhand.relay_client import AlreadyRunning, single_instance

    lock = tmp_path / "connect.lock"
    with single_instance(lock):
        assert lock.read_text() == str(os.getpid())
    assert not lock.exists()
    # a lock left by a dead process is taken over
    lock.write_text("999999")
    with single_instance(lock):
        pass
    # a live winhand-looking process blocks a second instance
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "winhand"])
    try:
        lock.write_text(str(child.pid))
        with pytest.raises(AlreadyRunning), single_instance(lock):
            pass
    finally:
        child.kill()
