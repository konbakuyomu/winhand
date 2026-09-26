"""Real-machine Windows scenarios: shells, code pages, quoting, wrappers, prompts,
interrupts, REPLs, debuggers, serial ports, awkward paths and process trees.

Runs only on Windows; each scenario skips itself when the tool it needs is absent,
so the same file is useful on a bare CI runner and on a fully equipped dev box.
Set WINHAND_COM_PAIR=COM1,COM2 to exercise a virtual serial port pair.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from winhand import fs, proc
from winhand.session import SessionSpec, wait_for

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only scenarios"),
]


def need(tool: str) -> str:
    path = shutil.which(tool)
    if not path:
        pytest.skip(f"{tool} not installed")
    return path


async def settle(manager, s, timeout=30, **kw):
    kw.setdefault("states", ["awaiting_input", "needs_user", "exited"])
    return await wait_for(manager, [s.id], quiet_after_output=True, timeout_s=timeout, since={s.id: 0}, **kw)


async def send(manager, s, text, timeout=30, **kw):
    since = s.read_cursor
    s.send(text, submit=True)
    kw.setdefault("states", ["awaiting_input", "needs_user", "exited"])
    return await wait_for(manager, [s.id], timeout_s=timeout, since={s.id: since}, **kw)


@pytest.fixture
def weird_dir(tmp_path):
    d = tmp_path / "中文 目录 [test] (x86)"
    d.mkdir()
    return d


# ------------------------------------------------------------------ shells


@pytest.mark.parametrize("shell", ["pwsh", "powershell", "cmd"])
async def test_shell_prompt_unicode_and_cwd(manager, weird_dir, shell):
    exe = need(shell)
    from winhand import winenv

    if shell == "cmd":
        argv = [exe, "/K", "chcp 65001>nul"]
    else:
        argv = winenv.powershell_interactive(exe, str(weird_dir), load_profile=False)
    s = manager.create(SessionSpec(transport="pty", argv=argv, cwd=str(weird_dir)))
    r = await settle(manager, s)
    assert r["sessions"][s.id]["state"] == "awaiting_input", r["sessions"][s.id]
    echo = "echo 你好-世界 ✓" if shell == "cmd" else "Write-Output '你好-世界 ✓'"
    r = await send(manager, s, echo)
    snap = r["sessions"][s.id]
    assert "你好-世界" in snap["output"] and snap["state"] == "awaiting_input", snap
    r = await send(manager, s, "cd" if shell == "cmd" else "(Get-Location).Path")
    assert "中文 目录 [test] (x86)" in r["sessions"][s.id]["output"]


async def test_default_shell_survives_user_profile(manager):
    """The real default shell loads the user's profile (oh-my-posh etc.) yet ends on a plain prompt."""
    s = manager.create(SessionSpec(transport="pty"))
    r = await settle(manager, s, timeout=45)
    snap = r["sessions"][s.id]
    assert snap["state"] == "awaiting_input" and snap.get("kind") == "prompt", snap


async def test_git_bash(manager):
    git = need("git")
    bash = Path(git).resolve().parents[1] / "bin" / "bash.exe"
    if not bash.exists():
        bash = Path(git).resolve().parents[1] / "usr" / "bin" / "bash.exe"
    if not bash.exists():
        pytest.skip("git bash not found next to git")
    s = manager.create(
        SessionSpec(
            transport="pty",
            argv=[str(bash), "--noprofile", "--norc", "-i"],
            env={"PS1": "gb$ "},
            prompts=[r"^gb\$ ?$"],
        )
    )
    r = await settle(manager, s)
    assert r["sessions"][s.id]["state"] == "awaiting_input", r["sessions"][s.id]
    r = await send(manager, s, "echo $((6*7)) 中文")
    assert "42 中文" in r["sessions"][s.id]["output"]


# ---------------------------------------------------------- code pages


async def test_gbk_program_output_through_pipe(manager):
    code = "import sys; sys.stdout.buffer.write('编码测试 GBK\\n'.encode('gbk')); sys.stdout.flush()"
    s = manager.create(SessionSpec(transport="pipe", argv=[sys.executable, "-c", code], encoding="gbk"))
    r = await wait_for(manager, [s.id], patterns=["编码测试 GBK"], timeout_s=15, since={s.id: 0})
    assert r["hit"]["condition"] in ("pattern", "exited") and "编码测试 GBK" in r["sessions"][s.id]["output"]


def test_run_decodes_legacy_codepage_output():
    code = "import sys; sys.stdout.buffer.write('旧代码页输出'.encode('gbk'))"
    res = proc.run(sys.executable, ["-c", code])
    assert res["stdout"] == "旧代码页输出"


# --------------------------------------------------- quoting & wrappers


def test_run_args_with_quotes_spaces_and_cjk(weird_dir):
    tricky = ['a "quoted" arg', "with space", "中文参数", r"C:\path\ending\\", "&|<>^%"]
    res = proc.run(
        sys.executable,
        ["-c", "import sys, json; print(json.dumps(sys.argv[1:], ensure_ascii=False))", *tricky],
        cwd=str(weird_dir),
    )
    import json

    assert json.loads(res["stdout"]) == tricky, res


def test_cmd_and_bat_wrappers(weird_dir):
    bat = weird_dir / "hello wrapper.cmd"
    bat.write_text("@echo off\r\necho wrapper-ok %1\r\n", encoding="ascii")
    res = proc.run(str(bat), ["arg1"])
    assert "wrapper-ok arg1" in res["stdout"], res
    npm = shutil.which("npm")
    if npm:
        res = proc.run("npm", ["--version"])  # resolved to npm.cmd via PATHEXT
        assert res["exit_code"] == 0 and res["stdout"].strip()[0].isdigit(), res


def test_shell_command_line_with_pipes_and_cjk():
    res = proc.run("Write-Output '甲','乙','丙' | Sort-Object -Descending | Select-Object -First 1")
    assert res["stdout"].strip() in ("丙", "乙", "甲") and res["exit_code"] == 0, res


# ------------------------------------------------------------ prompts


async def test_powershell_confirm_readhost_and_secure_prompt(manager):
    exe = need("pwsh")
    s = manager.create(SessionSpec(transport="pty", argv=[exe, "-NoLogo", "-NoProfile"]))
    await settle(manager, s)
    r = await send(manager, s, "$a = Read-Host 'Enter name'; \"hi $a\"")
    assert r["sessions"][s.id]["state"] == "awaiting_input", r["sessions"][s.id]
    r = await send(manager, s, "小明")
    assert "hi 小明" in r["sessions"][s.id]["output"]
    r = await send(manager, s, "$p = Read-Host 'Password' -AsSecureString; 'len=' + $p.Length")
    assert r["sessions"][s.id]["state"] == "needs_user", r["sessions"][s.id]
    r = await send(manager, s, "s3cret")
    assert "len=6" in r["sessions"][s.id]["output"]
    r = await send(manager, s, "Remove-Item C:\\definitely\\missing -Confirm")
    assert r["sessions"][s.id]["state"] in ("awaiting_input", "idle"), r["sessions"][s.id]


async def test_cmd_choice_yes_no(manager):
    exe = need("cmd")
    s = manager.create(SessionSpec(transport="pty", argv=[exe, "/K", "chcp 65001>nul"]))
    await settle(manager, s)
    r = await send(manager, s, 'choice /M "Continue" & echo errorlevel=%errorlevel%')
    snap = r["sessions"][s.id]
    assert snap["state"] == "awaiting_input" and snap.get("kind") in ("confirm", "prompt_guess"), snap
    s.send(keys=["y"])
    r = await wait_for(manager, [s.id], patterns=["errorlevel=1"], timeout_s=15)
    assert r["hit"]["condition"] == "pattern"


# ------------------------------------------------- interrupt & output


async def test_ctrl_c_stops_long_command_in_pty(manager):
    exe = need("pwsh")
    s = manager.create(SessionSpec(transport="pty", argv=[exe, "-NoLogo", "-NoProfile"]))
    await settle(manager, s)
    s.send("1..100000 | ForEach-Object { Start-Sleep -Milliseconds 200; $_ }", submit=True)
    await wait_for(manager, [s.id], patterns=[r"^3$"], timeout_s=20)
    s.send(keys=["Ctrl+C"])
    r = await wait_for(manager, [s.id], states=["awaiting_input"], timeout_s=20)
    assert r["sessions"][s.id]["state"] == "awaiting_input", r["sessions"][s.id]


async def test_flood_of_output_is_complete(manager):
    s = manager.create(
        SessionSpec(
            transport="pty",
            argv=[sys.executable, "-u", "-c", "for i in range(20000): print(f'line {i} 中文')"],
        )
    )
    r = await wait_for(manager, [s.id], states=["exited"], timeout_s=60, since={s.id: 0})
    assert r["hit"]["condition"] == "exited"
    text = s.read(since=0, limit=10_000_000)["output"]
    assert "line 0 中文" in text and "line 19999 中文" in text


# ---------------------------------------------------------------- REPLs


async def test_node_repl(manager):
    need("node")
    s = manager.create(SessionSpec(transport="pty", argv=["node"], prompts=[r"^> ?$"]))
    await settle(manager, s)
    r = await send(manager, s, "[1,2,3].map(x => x * 14).join('+') + ' 完成'")
    assert "14+28+42 完成" in r["sessions"][s.id]["output"]
    r = await send(manager, s, ".exit")
    assert r["sessions"][s.id]["state"] == "exited"


async def test_python_repl_multiline_block(manager):
    s = manager.create(SessionSpec(transport="pty", argv=[sys.executable, "-q"]))
    await settle(manager, s)
    for line in ["def f(x):", "    return x * 2", ""]:
        r = await send(manager, s, line)
    r = await send(manager, s, "f(21)")
    assert "42" in r["sessions"][s.id]["output"]


# ---------------------------------------------------------- embedded


async def test_real_gdb_prompt_and_pagination(manager):
    need("arm-none-eabi-gdb")
    from winhand import profiles

    spec, extras = profiles.build_spec(profiles.get_profile("gdb"))
    s = manager.create(spec)
    r = await settle(manager, s, timeout=60)
    assert r["sessions"][s.id]["state"] == "awaiting_input", r["sessions"][s.id]
    for cmd in extras["init"]:
        await send(manager, s, cmd)
    r = await send(manager, s, "show version")
    assert "GNU gdb" in r["sessions"][s.id]["output"]
    r = await send(manager, s, "target extended-remote localhost:1")  # nothing listens there
    snap = r["sessions"][s.id]
    assert snap["state"] == "awaiting_input" and (
        "refused" in snap["output"].lower() or "error" in snap["output"].lower()
    )
    s.stop()


async def test_pyocd_without_probe_reports_and_exits(manager):
    need("pyocd")
    s = manager.create(SessionSpec(transport="pty", argv=["pyocd", "list", "--probes"]))
    r = await wait_for(manager, [s.id], states=["exited"], timeout_s=90, since={s.id: 0})
    assert r["hit"]["condition"] == "exited", r["sessions"][s.id]


async def test_serial_pair(manager):
    pair = os.environ.get("WINHAND_COM_PAIR")
    if not pair:
        pytest.skip("set WINHAND_COM_PAIR=COM1,COM2")
    a_port, b_port = pair.split(",")
    a = manager.create(SessionSpec(transport="serial", port=a_port, prompts=[r"^msh />?$"], line_ending="\r"))
    b = manager.create(SessionSpec(transport="serial", port=b_port))
    b.send("\r\n \\ | /\r\n- RT -     Thread Operating System\r\nmsh />")
    r = await wait_for(manager, [a.id], timeout_s=10)
    assert r["sessions"][a.id]["state"] == "awaiting_input", r["sessions"][a.id]
    a.send("ps", submit=True)
    r = await wait_for(manager, [b.id], patterns=[r"ps\r?$"], timeout_s=10)
    assert r["hit"]["condition"] == "pattern"


# ---------------------------------------------------------------- files


def test_long_path_and_bracket_names(tmp_path):
    deep = tmp_path
    for i in range(12):
        deep = deep / f"很长的目录名称_{i:02d}_[segment]"
    target = deep / "文件 [1].txt"
    assert len(str(target)) > 300
    fs.fs_write(str(target), "第一行\r\n第二行\r\n")
    out = fs.fs_read(str(target))
    assert "第二行" in out["content"] and out["newline"] == "crlf"
    fs.fs_edit(str(target), "第二行", "改过的第二行")
    assert "改过的第二行" in fs.fs_read(str(target))["content"]
    assert fs.fs_search("改过", str(tmp_path))["count"] == 1


def test_bom_file_roundtrip(tmp_path):
    p = tmp_path / "bom.ini"
    p.write_bytes(b"\xef\xbb\xbf" + "[section]\r\nkey=值\r\n".encode())
    fs.fs_edit(str(p), "key=值", "key=新值")
    assert p.read_bytes() == b"\xef\xbb\xbf" + "[section]\r\nkey=新值\r\n".encode()


# ------------------------------------------------------------ processes


def test_proc_kill_takes_down_nested_tree(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(
        textwrap.dedent("""
        import subprocess, sys, time
        depth = int(sys.argv[1])
        if depth:
            subprocess.Popen([sys.executable, __file__, str(depth - 1)])
        time.sleep(120)
    """)
    )
    root = subprocess.Popen([sys.executable, str(script), "3"])
    import psutil

    for _ in range(100):
        if len(psutil.Process(root.pid).children(recursive=True)) >= 3:
            break
        time.sleep(0.1)
    kids = [c.pid for c in psutil.Process(root.pid).children(recursive=True)]
    assert len(kids) >= 3
    proc.proc_kill(root.pid, tree=True, force=True)
    time.sleep(1)
    assert not any(psutil.pid_exists(pid) for pid in [root.pid, *kids])


def test_run_timeout_kills_grandchildren(tmp_path):
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "print(p.pid, flush=True)\ntime.sleep(120)\n"
    )
    res = proc.run(sys.executable, [str(script)], timeout_s=3)
    import psutil

    grandchild = int(res["stdout"].split()[0])
    time.sleep(1)
    assert res["timed_out"] and not psutil.pid_exists(grandchild)
