from __future__ import annotations

import os
import subprocess
import sys
import textwrap
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


def test_import_codex_and_roundtrip(tmp_path):
    codex = tmp_path / "codex.toml"
    codex.write_text(
        textwrap.dedent(r"""
        [mcp_servers.pyocd-debug]
        command = "uv"
        args = ["--directory", 'D:\Dev\20_个人项目\PYOCD调试MCP', "run", "pyocd-debug-mcp"]
        [mcp_servers.cam]
        command = 'C:\Users\me\uv.exe'
        args = []
        [mcp_servers.cam.env]
        LEVEL = "debug"
    """),
        encoding="utf-8",
    )
    cfg, added = config.import_codex(codex, Config())
    assert added == ["pyocd-debug", "cam"]
    path = config.save(cfg, tmp_path / "out.toml")
    again = config.load(path)
    assert again.servers[0].args[1] == r"D:\Dev\20_个人项目\PYOCD调试MCP"
    assert again.servers[1].command == r"C:\Users\me\uv.exe" and again.servers[1].env == {"LEVEL": "debug"}


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
        assert {"session_start", "session_wait", "fs_edit", "run", "pyocd_probe_list"} <= names
        assert (await c.call_tool("pyocd_probe_list", {})).data == ["probe-A"]

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
