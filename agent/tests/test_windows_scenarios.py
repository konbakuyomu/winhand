"""Real-machine Windows scenarios: shells, code pages, quoting, wrappers, prompts,
interrupts, REPLs, debuggers, serial ports, awkward paths and process trees.

Runs only on Windows; each scenario skips itself when the tool it needs is absent,
so the same file is useful on a bare CI runner and on a fully equipped dev box.
Set WINHAND_COM_PAIR=COM1,COM2 to exercise a virtual serial port pair.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from conftest import wait_until_exited

from winhand import fs, proc
from winhand.config import Config
from winhand.server import build_server
from winhand.session import SessionSpec, wait_for

pytestmark = [
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
    """Programs that ignore UTF-8 print in the machine's ANSI code page (cp936 on Chinese
    Windows, cp1252 on English ones); `run` must still return readable text."""
    import locale

    enc = locale.getpreferredencoding(False)
    text = next(t for t in ("旧代码页输出", "Ünïcödé façade", "legacy") if _encodable(t, enc))
    code = f"import sys; sys.stdout.buffer.write({text!r}.encode({enc!r}))"
    res = proc.run(sys.executable, ["-c", code])
    assert res["stdout"] == text, (enc, res)


def _encodable(text: str, enc: str) -> bool:
    try:
        text.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


# --------------------------------------------------- quoting & wrappers


def test_run_args_with_quotes_spaces_and_cjk(weird_dir):
    tricky = ['a "quoted" arg', "with space", "中文参数", r"C:\path\ending\\", "&|<>^%"]
    res = proc.run(
        sys.executable,
        ["-c", "import sys, json; print(json.dumps(sys.argv[1:], ensure_ascii=False))", *tricky],
        cwd=str(weird_dir),
    )

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


def test_nested_powershell_command_line_keeps_its_variables():
    need("powershell")
    res = proc.run('powershell -NoProfile -Command "$p = \'a b\'; Write-Output \\"[$p]\\""')
    assert res["exit_code"] == 0 and "[a b]" in res["stdout"], res
    assert res.get("notes"), res


def test_powershell_with_args_prints_utf8():
    need("powershell")
    res = proc.run("powershell", ["-NoProfile", "-Command", "Write-Output '中文 😀 done'"])
    assert "中文 😀 done" in res["stdout"], res


def test_scripts_in_every_windows_shell():
    res = proc.run(script="$x = \"a $(1+1)\"\nWrite-Output $x\nWrite-Output '中文 😀'", shell="powershell")
    assert "a 2" in res["stdout"] and "中文 😀" in res["stdout"], res
    res = proc.run(script="@echo off\necho 中文 %~nx0", shell="cmd")
    assert "中文 run-" in res["stdout"], res
    if shutil.which("pwsh"):
        res = proc.run(script="Get-Item -LiteralPath . | ForEach-Object { $_.GetType().Name }")
        assert res["stdout"].strip() == "DirectoryInfo", res


def test_batch_file_refuses_multiline_arguments(weird_dir):
    bat = weird_dir / "shim.cmd"
    bat.write_text("@echo off\r\necho %*\r\n", encoding="ascii")
    assert "line breaks" in proc.run(str(bat), ["one\ntwo"])["error"]


def test_delete_goes_to_the_recycle_bin(weird_dir):
    target = weird_dir / "回收站测试.txt"
    target.write_text("x", encoding="utf-8")
    out = fs.fs_op("delete", str(target))
    assert out["recycled"] and not target.exists(), out


def test_atomic_write_keeps_crlf_and_replaces_in_place(weird_dir):
    p = weird_dir / "keep.ini"
    p.write_bytes(b"a=1\r\n")
    fs.fs_edit(str(p), "a=1", "a=2")
    assert p.read_bytes() == b"a=2\r\n"
    assert sorted(x.name for x in weird_dir.iterdir()) == ["keep.ini"]


def test_secret_file_is_readable_only_by_this_user(weird_dir):
    from winhand import secret_store

    out = secret_store.save_to_file(str(weird_dir / "token.env"), "s3cret-value", key="TOKEN")
    assert out["saved"]
    # icacls prints in the console code page (cp936 on Chinese Windows), not UTF-8
    acl = proc._decode(subprocess.run(["icacls", str(weird_dir / "token.env")], capture_output=True).stdout)
    assert "(I)" not in acl and os.environ["USERNAME"].lower() in acl.lower(), acl


def test_secret_goes_into_the_credential_manager():
    import ctypes
    import uuid

    from winhand import secret_store

    target = f"winhand-test-{uuid.uuid4().hex[:8]}"
    out = secret_store.save_to_credential_manager(target, "value-123")
    assert out == {"saved": True, "credential": target, "chars": 9}
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    assert advapi32.CredDeleteW(ctypes.c_wchar_p(target), 1, 0), ctypes.get_last_error()


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
    # cmd expands %errorlevel% when it parses a line, so ask for it in a separate command
    r = await send(manager, s, 'choice /M "Continue"')
    snap = r["sessions"][s.id]
    assert snap["state"] == "awaiting_input" and snap.get("kind") in ("confirm", "prompt_guess"), snap
    s.send(keys=["y"])
    await wait_for(manager, [s.id], states=["awaiting_input"], timeout_s=15)
    r = await send(manager, s, "echo errorlevel=%errorlevel%")
    assert "errorlevel=1" in r["sessions"][s.id]["output"], r["sessions"][s.id]


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
    r = await wait_until_exited(manager, s)
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
    # the reason is localized (e.g. 由于目标计算机积极拒绝) so only check the command was answered
    assert snap["state"] == "awaiting_input" and "localhost:1" in snap["output"], snap
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


def test_user_default_environment_restores_standard_variables():
    from winhand import winenv

    defaults = winenv.user_default_environment()
    names = {name.upper() for name in defaults}
    assert {"PROGRAMFILES(X86)", "PROGRAMDATA", "USERPROFILE", "PATH"} <= names


# ---------------------------------------------------------------- desktop

_FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form -Property @{ Text = 'winhand-ui-test'; Width = 420; Height = 220; TopMost = $true }
$box = New-Object Windows.Forms.TextBox -Property @{ Name = 'NameBox'; Left = 20; Top = 20; Width = 360 }
$btn = New-Object Windows.Forms.Button -Property @{ Name = 'Go'; Text = 'Press me'; Left = 20; Top = 70; Width = 160; Height = 50 }
$chk = New-Object Windows.Forms.CheckBox -Property @{ Text = 'Remember me'; Left = 200; Top = 80; Width = 180 }
$pwd = New-Object Windows.Forms.TextBox -Property @{ Left = 20; Top = 130; Width = 200; UseSystemPasswordChar = $true; Text = 'hunter2' }
$btn.Add_Click({ $f.Text = 'winhand-ui-test pressed: ' + $box.Text + ' / ' + $chk.Checked })
$f.Controls.AddRange(@($box, $btn, $chk, $pwd))
[void]$f.ShowDialog()
"""


@pytest.fixture
def test_form(tmp_path):
    script = tmp_path / "form.ps1"
    script.write_text(_FORM, encoding="utf-8-sig")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    from winhand import desktop

    try:
        for _ in range(60):
            try:
                window = desktop.find_window("winhand-ui-test")
                break
            except desktop.DesktopError:
                time.sleep(0.25)
        else:
            pytest.fail("test form did not appear")
        yield window
    finally:
        proc.kill()
        proc.wait(timeout=10)


async def test_ui_automation_fills_and_presses_controls(test_form):
    from fastmcp import Client

    from winhand import desktop

    async with Client(build_server(Config())) as client:
        found = (
            await client.call_tool("ui", {"window": "winhand-ui-test", "filter": "Press"})
        ).structured_content
        assert any(e["name"] == "Press me" and e["type"] == "Button" for e in found["elements"])
        changed = (
            await client.call_tool(
                "ui",
                {
                    "window": "winhand-ui-test",
                    "action": "set_value",
                    "control_type": "Edit",
                    "value": "你好 UIA",
                },
            )
        ).structured_content
        assert changed["verified"] is True and changed["element"]["value"] == "你好 UIA", changed
        toggled = (
            await client.call_tool(
                "ui",
                {
                    "window": "winhand-ui-test",
                    "action": "toggle",
                    "name": "Remember me",
                },
            )
        ).structured_content
        assert toggled["verified"] is True and toggled["element"]["toggle"] == "On", toggled
        await client.call_tool("ui", {"window": "winhand-ui-test", "action": "invoke", "name": "Press me"})
        time.sleep(0.5)
        assert desktop.find_window(test_form["hwnd"])["title"] == "winhand-ui-test pressed: 你好 UIA / True"
        found = (await client.call_tool("ui", {"window": "winhand-ui-test"})).structured_content
    edits = [e for e in found["elements"] if e["type"] == "Edit"]
    assert "hunter2" not in json.dumps(found) and {e.get("value") for e in edits} >= {"你好 UIA"}, edits
    assert next(e for e in found["elements"] if e["name"] == "Remember me")["toggle"] == "On"


async def test_real_mouse_click_lands_where_the_screenshot_shows(test_form):
    from winhand import desktop

    try:
        desktop.window_action(test_form["hwnd"], "focus")
    except desktop.DesktopError:
        pass  # the form is topmost: visible and clickable either way
    data, fmt, facts = desktop.screenshot(window=test_form["hwnd"])
    assert facts["width"] > 100 and fmt in ("png", "jpeg")
    # the button sits at client (20..180, 70..120); click its middle in screenshot pixels
    # (client area starts below the title bar, so aim a little low)
    button = next(e for e in _uia_elements(test_form) if e["name"] == "Press me")
    left, top, width, height = button["rect"]
    x = (left + width / 2 - facts["screen_left"]) * facts["scale"]
    y = (top + height / 2 - facts["screen_top"]) * facts["scale"]
    desktop.input_action("click", x, y)
    time.sleep(0.5)
    assert desktop.find_window(test_form["hwnd"])["title"].startswith("winhand-ui-test pressed")


def _uia_elements(window):
    from winhand.tools_desktop import _inspect_controls

    return _inspect_controls(window["hwnd"])["elements"]


async def test_ui_through_the_powershell_script_still_works(test_form, monkeypatch):
    from fastmcp import Client

    from winhand import uia

    def unavailable(hwnd, timeout=0):
        raise uia.Unavailable("simulated")

    monkeypatch.setattr(uia, "snapshot", unavailable)
    async with Client(build_server(Config())) as client:
        found = (
            await client.call_tool("ui", {"window": "winhand-ui-test", "filter": "Press"})
        ).structured_content
        assert found["backend"] == "powershell", found
        assert any(e["name"] == "Press me" and e["type"] == "Button" for e in found["elements"])
        done = (
            await client.call_tool(
                "ui", {"window": "winhand-ui-test", "action": "toggle", "name": "Remember me"}
            )
        ).structured_content
    assert done["verified"] is True, done


# A classic Win32 menu (MainMenu makes a real HMENU), a list view, a text box, and a label that
# changes 1.5 s after "Start" is pressed.
_CLASSIC_FORM = r"""
Add-Type -AssemblyName System.Windows.Forms
$f = New-Object Windows.Forms.Form -Property @{ Text = 'winhand-classic-test'; Width = 620; Height = 380; TopMost = $true }
$menu = New-Object Windows.Forms.MainMenu
$file = $menu.MenuItems.Add('文件(&F)')
$about = $file.MenuItems.Add('关于(&A)')
$about.Add_Click({ $f.Text = 'winhand-classic-test about' })
$f.Menu = $menu
$list = New-Object Windows.Forms.ListView -Property @{ Left = 10; Top = 10; Width = 300; Height = 150; View = 'Details'; FullRowSelect = $true; HideSelection = $false }
[void]$list.Columns.Add('Item', 140)
[void]$list.Columns.Add('Value', 140)
foreach ($row in @(@('CPU', 'Intel'), @('内存', '32 GB'), @('Disk', 'SSD'))) { $item = New-Object Windows.Forms.ListViewItem($row[0]); [void]$item.SubItems.Add($row[1]); [void]$list.Items.Add($item) }
$box = New-Object Windows.Forms.TextBox -Property @{ Left = 330; Top = 10; Width = 250 }
$start = New-Object Windows.Forms.Button -Property @{ Text = 'Start'; Left = 330; Top = 50; Width = 120; Height = 40 }
$label = New-Object Windows.Forms.Label -Property @{ Left = 10; Top = 190; Width = 580; Height = 60; Text = 'Status: waiting'; Font = New-Object Drawing.Font('Segoe UI', 18) }
$timer = New-Object Windows.Forms.Timer -Property @{ Interval = 1500 }
$timer.Add_Tick({ $label.Text = 'Status: Done 已完成'; $timer.Stop() })
$start.Add_Click({ $timer.Start() })
$f.Controls.AddRange(@($list, $box, $start, $label))
[void]$f.ShowDialog()
"""


@pytest.fixture
def classic_form(tmp_path):
    script = tmp_path / "classic.ps1"
    script.write_text(_CLASSIC_FORM, encoding="utf-8-sig")
    proc = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    from winhand import desktop

    try:
        for _ in range(60):
            try:
                window = desktop.find_window("winhand-classic-test")
                break
            except desktop.DesktopError:
                time.sleep(0.25)
        else:
            pytest.fail("classic test form did not appear")
        yield window
    finally:
        proc.kill()
        proc.wait(timeout=10)


async def test_native_ui_automation_reaches_classic_menus_and_lists(classic_form):
    from fastmcp import Client

    from winhand import desktop

    async with Client(build_server(Config())) as client:

        async def ui(**arguments):
            return (
                await client.call_tool("ui", {"window": "winhand-classic-test", **arguments})
            ).structured_content

        found = await ui()
        assert found["backend"] == "native", found
        menu = next(e for e in found["elements"] if e["type"] == "MenuItem" and e["name"] == "文件(F)")
        assert "ExpandCollapse" in menu["patterns"]
        rows = {e["name"]: e for e in found["elements"] if e["type"] == "ListItem"}
        assert {"CPU", "内存", "Disk"} <= rows.keys() and "SelectionItem" in rows["内存"]["patterns"]

        await ui(action="expand", name="文件(F)", control_type="MenuItem")
        opened = await ui(filter="关于")
        assert any(e["type"] == "MenuItem" and e["name"] == "关于(A)" for e in opened["elements"]), opened
        await ui(action="invoke", name="关于(A)", control_type="MenuItem")
        time.sleep(0.5)
        assert desktop.find_window(classic_form["hwnd"])["title"] == "winhand-classic-test about"

        chosen = await ui(action="select", name="内存", control_type="ListItem")
        assert chosen["verified"] is True and chosen["element"]["selected"] is True, chosen


def test_msinfo32_menus_and_tree_are_visible():
    from winhand import desktop
    from winhand.tools_desktop import _inspect_controls

    exe = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "msinfo32.exe"
    if not exe.is_file():
        pytest.skip("msinfo32 is not installed")
    proc = subprocess.Popen([str(exe)])
    try:
        for _ in range(80):
            try:
                window = desktop.find_window("msinfo32.exe")
                break
            except desktop.DesktopError:
                time.sleep(0.25)
        else:
            pytest.fail("msinfo32 did not open")
        snapshot = _inspect_controls(window["hwnd"])
        kinds = [e["type"] for e in snapshot["elements"]]
        assert snapshot["backend"] == "native"
        assert kinds.count("MenuItem") >= 4, kinds
        assert any(e["type"] == "TreeItem" and e["name"] for e in snapshot["elements"]), kinds
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)


def _ocr_or_skip(fn, *args, **kwargs):
    from winhand import ocr

    try:
        return fn(*args, **kwargs)
    except ocr.OcrError as exc:
        if "no OCR language" in str(exc) or "has no OCR language" in str(exc):
            pytest.skip(f"no OCR language on this machine: {exc}")
        raise


async def test_ocr_reads_and_clicks_text(test_form):
    from fastmcp import Client

    from winhand import desktop, ocr

    found = _ocr_or_skip(ocr.find, "press  ME", window="winhand-ui-test")
    assert found["matches"], found
    button = next(e for e in _uia_elements(test_form) if e["name"] == "Press me")
    left, top, width, height = button["rect"]
    x, y = found["matches"][0]["center"]
    assert left <= x <= left + width and top <= y <= top + height, (found, button)
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool(
                "ocr", {"action": "click", "text": "Press me", "window": "winhand-ui-test"}
            )
        ).structured_content
    assert "click at" in done.get("done", ""), done
    time.sleep(0.5)
    assert desktop.find_window(test_form["hwnd"])["title"].startswith("winhand-ui-test pressed")


async def test_waiting_for_the_picture_and_for_text(classic_form):
    from fastmcp import Client

    from winhand import ocr

    async with Client(build_server(Config())) as client:

        async def shot(**arguments):
            result = await client.call_tool("screenshot", {"window": "winhand-classic-test", **arguments})
            return result.structured_content

        await client.call_tool("ui", {"window": "winhand-classic-test", "action": "invoke", "name": "Start"})
        await shot()  # the picture right after pressing Start
        changed = await shot(wait="change", timeout_s=10)
        assert changed["wait"]["matched"] is True and changed["wait"]["waited_s"] >= 0.3, changed
        settled = await shot(wait="stable", stable_ms=500, timeout_s=10)
        assert settled["wait"]["matched"] is True, settled
        unchanged = await shot(wait="change", timeout_s=1)
        assert unchanged["wait"]["matched"] is False and "did not change" in unchanged["wait"]["note"]
        # a focused text box blinks its cursor: that is not a change
        await client.call_tool(
            "ui", {"window": "winhand-classic-test", "action": "focus", "control_type": "Edit"}
        )
        blinking = await shot(wait="stable", stable_ms=1000, timeout_s=5)
        assert blinking["wait"]["matched"] is True, blinking
    done = _ocr_or_skip(ocr.wait, "Done", window="winhand-classic-test", timeout_s=5)
    assert done["matched"] is True, done
    gone = _ocr_or_skip(ocr.wait, "Done", window="winhand-classic-test", state="gone", timeout_s=1)
    assert gone["matched"] is False
    read = _ocr_or_skip(ocr.read, window="winhand-classic-test")
    assert any("Done" in line["text"] for line in read["lines"]), read


def test_window_screenshots_of_dpi_unaware_programs_fill_the_window(classic_form):
    from winhand import desktop

    scale = desktop._content_scale(classic_form["hwnd"])
    if scale <= 1.01:
        pytest.skip("the monitor is not scaled: nothing to enlarge")
    image, place = desktop.capture(window=classic_form["hwnd"])
    width, height = image.size
    corner = image.crop((width * 3 // 4, height * 3 // 4, width - 8, height - 8)).convert("L")
    assert corner.getextrema()[1] > 60, "the bottom-right quarter is black: the drawing was not enlarged"


def test_doctor_reports_native_ui_automation():
    from winhand import uia

    assert uia.status() == "native"


def test_screenshot_of_the_desktop_and_window_list():
    from winhand import desktop

    data, fmt, facts = desktop.screenshot()
    assert facts["desktop"] and len(data) > 1000 and facts["width"] <= 1568
    screens = desktop.monitors()
    assert screens and any(m["primary"] for m in screens)
    assert desktop.windows(), "at least one visible window"


def test_clipboard_text_roundtrip_restores_the_original():
    from winhand import desktop

    before = desktop.clipboard_get()
    try:
        desktop.clipboard_set(text="winhand 剪贴板 ✓")
        assert desktop.clipboard_get() == {"kind": "text", "text": "winhand 剪贴板 ✓"}
    finally:
        if before["kind"] == "text":
            desktop.clipboard_set(text=before["text"])


def test_background_job_reports_output_and_exit_code():
    from winhand import jobs

    started = jobs.start("Write-Output '你好 job'; cmd /c exit 3", name="winhand-test")
    for _ in range(80):
        status = jobs.status(started["id"])
        if status["state"] != "running":
            break
        time.sleep(0.25)
    assert status["state"] == "finished" and status["exit_code"] == 3, status
    assert "你好 job" in status["output"]
