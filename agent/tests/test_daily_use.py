"""winhand 0.7.0: fixes for what tripped agents in daily use (seen in the machine's audit logs)."""

from __future__ import annotations

import json
from pathlib import Path

from winhand import proc


def _no_standard_git(monkeypatch, tmp_path):
    for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA", "SCOOP"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    proc._bash_beside_git.cache_clear()


def test_git_bash_behind_a_scoop_shim_outside_the_home_folder(monkeypatch, tmp_path):
    # scoop on D:\scoop: PATH has D:\scoop\shims\git.exe, SCOOP is not set and ~\scoop does not exist
    shim = tmp_path / "scoop" / "shims" / "git.exe"
    bash = tmp_path / "scoop" / "apps" / "git" / "current" / "bin" / "bash.exe"
    for f in (shim, bash):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")
    _no_standard_git(monkeypatch, tmp_path)
    monkeypatch.setattr(proc.shutil, "which", lambda name, path=None: str(shim) if name == "git" else None)
    assert proc._git_bash(None) == str(bash)


def test_git_bash_found_from_gits_exec_path(monkeypatch, tmp_path):
    # a launcher git.exe far from the installation: ask git where it lives
    launcher = tmp_path / "tools" / "git.exe"
    root = tmp_path / "Git-2.56"
    bash = root / "bin" / "bash.exe"
    for f in (launcher, bash):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"")
    (root / "cmd").mkdir()
    exec_path = root / "ucrt64" / "libexec" / "git-core"
    _no_standard_git(monkeypatch, tmp_path)
    monkeypatch.setattr(
        proc.shutil, "which", lambda name, path=None: str(launcher) if name == "git" else None
    )

    class Done:
        stdout = (str(exec_path).replace("\\", "/") + "\n").encode()

    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: Done())
    assert proc._git_bash(None) == str(bash)


# ------------------------------------------------------------------ arguments as models send them

import os  # noqa: E402
import sys  # noqa: E402
import textwrap  # noqa: E402

import pytest  # noqa: E402
from fastmcp.client import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from mcp.types import TextContent  # noqa: E402

from winhand.aliases import Normalized  # noqa: E402
from winhand.config import Config  # noqa: E402
from winhand.server import build_server  # noqa: E402


def _text(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


@pytest.fixture
async def client():
    async with Client(build_server(Config())) as c:
        yield c


async def test_claude_code_edit_names_work(client, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("alpha\nbeta\n", encoding="utf-8")
    await client.call_tool("fs_edit", {"file_path": str(f), "old_string": "alpha", "new_string": "ALPHA"})
    await client.call_tool("fs_edit", {"path": str(f), "edits": [{"old_str": "beta", "new_str": "BETA"}]})
    assert f.read_text(encoding="utf-8") == "ALPHA\nBETA\n"


async def test_natural_names_for_find_op_and_read(client, tmp_path):
    (tmp_path / "report-1.md").write_text("x", encoding="utf-8")
    found = (await client.call_tool("fs_find", {"pattern": "report-*.md", "path": str(tmp_path)})).data
    assert any("report-1.md" in json.dumps(found, ensure_ascii=False) for _ in [0])
    await client.call_tool("fs_op", {"op": "mkdir", "path": str(tmp_path / "new")})
    assert (tmp_path / "new").is_dir()
    gbk = tmp_path / "gbk.c"
    gbk.write_bytes("/* 中文注释 */\n".encode("gbk"))
    read = await client.call_tool("fs_read", {"file_path": str(gbk), "encoding": "gbk", "max_chars": 400})
    data = read.structured_content
    assert "中文注释" in data["content"] and data["encoding"] == "gbk"
    assert "max_chars=400" in data["args_note"] and "used 1000" in _text(read)


async def test_grep_style_search(client, tmp_path):
    (tmp_path / "m.py").write_text("a\nneedle\nb\n", encoding="utf-8")
    (tmp_path / "m.txt").write_text("needle\n", encoding="utf-8")
    hit = await client.call_tool(
        "fs_search", {"pattern": "needle", "path": str(tmp_path), "-A": 1, "type": "py", "-n": True}
    )
    text = json.dumps(hit.structured_content, ensure_ascii=False)
    assert "m.py" in text and "m.txt" not in text and "b" in text
    files = await client.call_tool(
        "fs_search",
        {"pattern": "needle", "path": str(tmp_path), "output_mode": "files_with_matches", "context": 30},
    )
    data = files.structured_content
    assert "context=30" in data["args_note"]


async def test_bash_style_run(client):
    done = await client.call_tool(
        "run",
        {
            "command": sys.executable,
            "args": ["-c", "print('ok')"],
            "timeout": 120000,
            "description": "say ok",
        },
    )
    assert done.structured_content["exit_code"] == 0 and "ok" in done.structured_content["stdout"]


async def test_unknown_argument_names_the_real_parameters(client, tmp_path):
    with pytest.raises(ToolError) as exc:
        await client.call_tool("fs_stat", {"path": str(tmp_path), "nope": 1})
    message = str(exc.value)
    assert "nope: not a parameter of fs_stat" in message and "Its parameters: path, hash" in message


async def test_an_aliased_call_is_judged_like_the_real_one(tmp_path, winhand_home):
    (winhand_home).mkdir(parents=True, exist_ok=True)
    (winhand_home / "guards.toml").write_text(
        textwrap.dedent(
            """\
            [[guard]]
            on = "edit"
            path = ["**/locked.txt"]
            decision = "deny"
            reason = "locked"
            """
        ),
        encoding="utf-8",
    )
    f = tmp_path / "locked.txt"
    f.write_text("keep", encoding="utf-8")
    async with Client(build_server(Config())) as c:
        with pytest.raises(ToolError, match="locked"):
            await c.call_tool("fs_edit", {"file_path": str(f), "old_string": "keep", "new_string": "gone"})
    assert f.read_text(encoding="utf-8") == "keep"


def test_renames_only_where_the_tool_lacks_the_alias():
    props = {"path": {}, "pattern": {"type": "string"}, "depth": {"type": "integer"}}
    fixed = Normalized("fs_list", props, {"pattern": "*.py", "file_path": "C:/x", "glob": "*.md"})
    fixed.run()
    # fs_list has its own `pattern`: kept; glob is an alias of it but pattern was given -> noted
    assert fixed.args == {"pattern": "*.py", "path": "C:/x"}
    assert fixed.notes == ["glob ignored: pattern was given too"]
    wait = Normalized(
        "session_wait",
        {
            "ids": {"anyOf": [{"type": "array"}, {"type": "string"}]},
            "patterns": {"anyOf": [{"type": "array"}, {"type": "null"}]},
            "timeout_s": {"type": "number"},
        },
        {"session_id": "s-1", "pattern": "READY", "timeout": 30000},
    )
    wait.run()
    assert wait.args == {"ids": "s-1", "patterns": ["READY"], "timeout_s": 30.0}


# ------------------------------------------------------------------ run: interpreters and cwd


def test_command_with_script_runs_it_with_that_interpreter(tmp_path):
    done = proc.run(command=sys.executable, script="import sys\nprint('中文', sys.argv[1:])", args=["a b"])
    assert done["exit_code"] == 0, done
    assert "中文 ['a b']" in done["stdout"]
    refused = proc.run(command="git", script="echo hi")
    assert "is not one" in refused["error"]


def test_relative_program_is_found_in_cwd(tmp_path):
    tool = tmp_path / "proj" / "bin"
    tool.mkdir(parents=True)
    program = tool / ("hello.cmd" if sys.platform == "win32" else "hello")
    program.write_text(
        "@echo hello from proj\n" if sys.platform == "win32" else "#!/bin/sh\necho hello from proj\n"
    )
    program.chmod(0o755)
    rel = str(program.relative_to(tmp_path / "proj"))
    done = proc.run(command=rel, args=[], cwd=str(tmp_path / "proj"))
    assert done.get("exit_code") == 0 and "hello from proj" in done["stdout"], done


def test_interpreter_names():
    assert proc.interpreter_kind(r"D:\proj\.venv\Scripts\python.exe") == "python"
    assert proc.interpreter_kind("python3.13") == "python"
    assert proc.interpreter_kind("pwsh") == "pwsh"
    assert proc.interpreter_kind(r"C:\Program Files\nodejs\node.exe") == "node"
    assert proc.interpreter_kind("Rscript.exe") == "rscript"
    assert proc.interpreter_kind("git") is None


# ------------------------------------------------------------------ paths with variables


async def test_percent_variables_in_file_paths(client, tmp_path, monkeypatch):
    monkeypatch.setenv("WH_TEST_DIR", str(tmp_path))
    (tmp_path / "v.txt").write_text("via variable", encoding="utf-8")
    read = await client.call_tool("fs_read", {"path": "%WH_TEST_DIR%/v.txt"})
    assert "via variable" in read.structured_content["content"]
    listed = await client.call_tool("fs_list", {"path": "$env:WH_TEST_DIR"})
    assert any(e["path"] == "v.txt" for e in listed.structured_content["entries"])


async def test_a_variable_cannot_hide_a_path_from_the_guard(tmp_path, winhand_home, monkeypatch):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "guards.toml").write_text(
        '[[guard]]\non = "read"\npath = ["**/vault/**"]\ndecision = "deny"\nreason = "vault"\n',
        encoding="utf-8",
    )
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "s.txt").write_text("secret", encoding="utf-8")
    monkeypatch.setenv("WH_VAULT", str(tmp_path / "vault"))
    async with Client(build_server(Config())) as c:
        with pytest.raises(ToolError, match="vault"):
            await c.call_tool("fs_read", {"path": "%WH_VAULT%/s.txt"})


async def test_listing_a_file_describes_it(client, tmp_path):
    f = tmp_path / "one.log"
    f.write_text("x" * 10, encoding="utf-8")
    listed = (await client.call_tool("fs_list", {"path": str(f)})).structured_content
    assert listed["entries"][0]["path"] == "one.log" and listed["entries"][0]["size"] == 10
    assert "is a file" in listed["note"]
    missing = (await client.call_tool("fs_list", {"path": str(tmp_path / "missing")})).structured_content
    assert missing["error"].startswith("no such folder")


def test_expand_path_leaves_dollar_paths_alone(monkeypatch):
    from winhand.aliases import expand_path

    monkeypatch.setenv("WH_X", "C:/x")
    assert expand_path("%WH_X%/a") == "C:/x/a"
    assert expand_path("%NO_SUCH_VAR_WH%/a") == "%NO_SUCH_VAR_WH%/a"
    assert expand_path(r"C:\$Recycle.Bin\x") == r"C:\$Recycle.Bin\x"
    assert expand_path(r"\\server\c$\share") == r"\\server\c$\share"


# ------------------------------------------------------------------ context(repo=...) finds nested clones


def _clone(folder: Path, url: str) -> None:
    (folder / ".git").mkdir(parents=True)
    (folder / ".git" / "config").write_text(f'[remote "origin"]\n\turl = {url}\n', encoding="utf-8")
    (folder / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")


def test_repo_found_near_a_known_project(tmp_path, monkeypatch):
    import json as _json

    from winhand import workspace

    dev = tmp_path / "Dev"
    known = dev / "01_board" / "dev-board"
    _clone(known, "https://github.com/me/dev-board")
    target = dev / "20_personal" / "winhand"
    _clone(target, "git@github.com:me/winhand.git")
    _clone(
        dev / "20_personal" / "winhand" / "node_modules" / "x", "https://github.com/me/winhand"
    )  # never entered
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "config.toml").write_text(
        f"[projects.{_json.dumps(str(known))}]\ntrust_level = 'trusted'\n", encoding="utf-8"
    )
    monkeypatch.setenv("CODEX_HOME", str(codex))
    # the home folder elsewhere: on Windows tmp_path is inside AppData, which is never scanned
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path / "home"))
    monkeypatch.setattr(workspace, "claude_state_file", lambda: str(tmp_path / "none.json"))
    workspace._scans.clear()
    found = workspace.find_repo(Config(context={"skills": []}), "me/winhand")
    assert found["checkout"] == str(target) and found["source"] == "nearby"


def test_repo_roots_are_searched_a_few_levels_down(tmp_path, monkeypatch):
    from winhand import workspace

    root = tmp_path / "code"
    deep = root / "work" / "client" / "app"
    _clone(deep, "https://github.com/acme/app")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(workspace, "claude_state_file", lambda: str(tmp_path / "none.json"))
    workspace._scans.clear()
    found = workspace.find_repo(Config(context={"skills": [], "repo_roots": [str(root)]}), "acme/app")
    assert found["checkout"] == str(deep) and found["source"] == "repo_roots"


# ------------------------------------------------------------------ job_status waits on several jobs


async def test_job_status_waits_for_whichever_job_ends_first(client):
    slow = proc.run(command=sys.executable, args=["-c", "import time; time.sleep(30)"], reply_within_s=0.2)
    quick = proc.run(
        command=sys.executable,
        args=["-c", "import time; print('quick done', flush=True); time.sleep(1.5)"],
        reply_within_s=0.2,
    )
    assert slow["still_running"] and quick["still_running"]
    try:
        out = (
            await client.call_tool("job_status", {"id": [slow["id"], quick["id"]], "wait_s": 20})
        ).structured_content
        assert out["hit"]["id"] == quick["id"] and out["hit"]["condition"] == "finished"
        assert out["jobs"][quick["id"]]["exit_code"] == 0
        assert "quick done" in out["jobs"][quick["id"]]["output"]
        assert out["running"] == [slow["id"]] and quick["id"] in out["next"]
        again = (
            await client.call_tool(
                "job_status",
                {"id": [slow["id"]], "since": out["cursors"], "wait_s": 0.5, "patterns": ["never"]},
            )
        ).structured_content
        assert again["state"] == "running" and again["id"] == slow["id"]
    finally:
        proc.run_stop(slow["id"])


def test_a_job_named_run_something_is_not_taken_for_a_run(monkeypatch):
    from winhand import tools_desktop

    seen = []
    monkeypatch.setattr(
        tools_desktop.jobs, "status", lambda i, *a: seen.append(i) or {"id": i, "state": "finished"}
    )
    assert tools_desktop._job_fetch("run-all-sim-1016122643", 0, 100)["state"] == "finished"
    assert seen == ["run-all-sim-1016122643"]


# ------------------------------------------------------------------ PowerShell: no BOM, clear hints


def test_powershell_prologue_has_no_bom():
    from winhand import winenv

    assert "UTF8Encoding $false" in winenv._PWSH_UTF8 and "[Text.Encoding]::UTF8" not in winenv._PWSH_UTF8


def test_hints_for_crlf_curly_quotes_and_bash_escapes():
    crlf = proc._failure_hint(None, False, "bash: line 1: $'K=abc\\r': command not found")
    assert "CRLF" in crlf and "remote_run" in crlf
    curly = proc._failure_hint(None, False, "ParserError: missing the terminator", "Write-Host “你好”")
    assert "curly quotes" in curly
    escaped = proc._failure_hint(None, False, "ParserError: Unexpected token", 'echo "a \\"b\\""')
    assert 'write it twice ("")' in escaped
    assert proc._failure_hint(None, False, "fine", "x") is None


# ------------------------------------------------------------------ job_start runs a program directly


def test_job_start_with_args_is_judged_with_its_arguments_and_stdin():
    from winhand import actions

    (act,) = actions.describe("job_start", {"command": "git", "args": ["push", "--force"], "stdin": "y"})
    assert act.kind == "command" and act.text == "git push --force\ny"


def test_whole_characters_keeps_a_half_written_one_for_next_time():
    from winhand.jobs import _whole_characters

    text = "进度 50%".encode()
    assert _whole_characters(text) == len(text)
    assert _whole_characters(text[:-4] + "完".encode()[:2]) == len(text) - 4
    assert _whole_characters(b"abc") == 3 and _whole_characters(b"") == 0


@pytest.mark.skipif(sys.platform != "win32", reason="background jobs are Windows-only")
def test_a_program_job_gets_utf8_stdin_and_shows_stderr_while_running(tmp_path):
    import time

    from winhand import jobs

    code = (
        "import sys, time\n"
        "data = sys.stdin.buffer.read()\n"
        "sys.stderr.write('progress ' + data.decode('utf-8') + '\\n'); sys.stderr.flush()\n"
        "time.sleep(2)\n"
        "print(data.hex())\n"
    )
    script = tmp_path / "p.py"
    script.write_text(code, encoding="utf-8")
    started = jobs.start(sys.executable, args=["-u", str(script)], stdin="中文 brief", name="prog")
    deadline = time.time() + 30
    status = started
    while time.time() < deadline:
        status = jobs.status(started["id"])
        if "progress" in status.get("stderr", "") or status["state"] != "running":
            break
        time.sleep(0.3)
    assert "progress 中文 brief" in status.get("stderr", ""), status
    while status["state"] == "running" and time.time() < deadline:
        time.sleep(0.3)
        status = jobs.status(started["id"])
    assert status["exit_code"] == 0 and "中文 brief".encode().hex() in status["output"], status


# ------------------------------------------------------------------ fs_search in GBK sources


@pytest.fixture(params=["ripgrep", "python"])
def gbk_tree(tmp_path, monkeypatch, request):
    """A firmware tree with GBK and UTF-8 sources, searched with ripgrep and with the Python
    fallback that answers the same way where ripgrep is not installed (CI's Ubuntu)."""
    import shutil

    from winhand import fs

    if request.param == "ripgrep" and not shutil.which("rg"):
        pytest.skip("ripgrep is not installed")
    if request.param == "python":
        real = shutil.which
        monkeypatch.setattr(
            fs.shutil, "which", lambda name, *a, **k: None if name == "rg" else real(name, *a, **k)
        )
    root = tmp_path / "10_客户项目" / "固件"
    (root / "user" / "sub").mkdir(parents=True)
    (root / "projects" / "source").mkdir(parents=True)
    (root / "user" / "process.c").write_bytes("TestOut_Pulse(); //切换至工作模式完成\nint x;\n".encode("gbk"))
    (root / "user" / "sub" / "gpio.h").write_text("#define PIN 1\n", encoding="utf-8")
    (root / "projects" / "source" / "main.c").write_text("int x; // 中文 UTF-8\n", encoding="utf-8")
    (root / "notes.md").write_text("int x 不该出现\n", encoding="utf-8")
    return root


def test_search_keeps_paths_readable_beside_gbk_lines(gbk_tree):
    from winhand import fs

    found = fs.fs_search("TestOut_Pulse|PIN", str(gbk_tree))
    assert sorted(found["matches"]) == [
        os.path.join("user", "process.c") + ":1:TestOut_Pulse(); //切换至工作模式完成",
        os.path.join("user", "sub", "gpio.h") + ":1:#define PIN 1",
    ]


def test_a_chinese_pattern_finds_gbk_and_utf8_files(gbk_tree):
    from winhand import fs

    found = fs.fs_search("切换至|中文", str(gbk_tree))
    texts = " ".join(found["matches"])
    assert "切换至工作模式" in texts and "中文 UTF-8" in texts
    assert found["engine"] in ("ripgrep + gb18030", "python")  # python reads GBK files as it goes


def test_globs_with_folders_and_counts_without_context_lines(gbk_tree):
    from winhand import fs

    found = fs.fs_search("int x", str(gbk_tree), glob="{user,projects/source}/**/*.{c,h}")
    assert len(found["matches"]) == 2 and not any("notes.md" in m for m in found["matches"])
    first = fs.fs_search("int x", str(gbk_tree), context=20, max_results=1)
    assert first["truncated"] and any(":1:" in m or ":2:" in m for m in first["matches"])


def test_a_single_file_search_keeps_its_format(gbk_tree):
    from winhand import fs

    found = fs.fs_search("int", str(gbk_tree / "user" / "process.c"))
    assert found["matches"] == ["2:int x;"]
    named = fs.fs_search("int", str(gbk_tree / "user" / "process.c"), files_only=True)
    assert named["matches"] == ["process.c"]


def test_globs_read_like_ripgreps(gbk_tree):
    from winhand import fs

    def files(glob):
        return sorted(fs.fs_search("x|PIN", str(gbk_tree), glob=glob, files_only=True)["matches"])

    sep = os.sep
    assert files("*.{c,h}") == [
        f"projects{sep}source{sep}main.c",
        f"user{sep}process.c",
        f"user{sep}sub{sep}gpio.h",
    ]
    assert files("user/*.c") == [f"user{sep}process.c"]  # with a folder: from the searched folder
    assert files("!*.md") == files("*.{c,h}")
    # a folder left out, and everything else kept
    assert files("!sub") == ["notes.md", f"projects{sep}source{sep}main.c", f"user{sep}process.c"]


# ------------------------------------------------------------------ local MCP servers added by hand


async def test_a_server_added_to_config_toml_is_there_without_a_restart(winhand_home):
    from winhand import local_mcp

    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text(
        '[mcp_servers.first]\ncommand = "python"\nargs = []\n', encoding="utf-8"
    )
    await local_mcp.refresh()
    assert [e.name for e in local_mcp.enabled_entries()] == ["first"]
    with open(winhand_home / "config.toml", "a", encoding="utf-8") as fh:  # what fs_write(append) does
        fh.write('\n[mcp_servers.rigol-scope-mcp]\ncommand = "python"\nargs = ["-m", "scope"]\n')
    await local_mcp.refresh()
    assert sorted(e.name for e in local_mcp.enabled_entries()) == ["first", "rigol-scope-mcp"]


async def test_the_app_does_not_overwrite_changes_made_to_the_file(winhand_home, monkeypatch):
    from winhand import desktop_backend as db
    from winhand import local_mcp

    winhand_home.mkdir(parents=True, exist_ok=True)
    path = winhand_home / "config.toml"
    path.write_text('[mcp_servers.a]\ncommand = "python"\nargs = []\n', encoding="utf-8")
    backend = db.Backend(lambda _line: None)
    try:
        assert local_mcp.reload_hook == backend._reload_from_disk
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                '\n[context]\nrepo_roots = ["D:\\\\code"]\n\n[mcp_servers.b]\ncommand = "python"\nargs = []\n'
            )
        import os as _os

        stat = path.stat()
        _os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))  # a later write
        await backend.handle(
            json.dumps(
                {"id": 1, "method": "mcp_set_enabled", "params": {"name": "a", "enabled": False}}
            ).encode()
        )
        saved = path.read_text(encoding="utf-8")
        assert "[mcp_servers.b]" in saved and "repo_roots" in saved and "enabled = false" in saved
    finally:
        await backend.close()


# ------------------------------------------------------------------ sessions: escapes and device codes


def test_cursor_save_restore_and_keypad_escapes_are_stripped():
    from winhand.session.text import strip_ansi

    raw = "\x1b7      \x1b8? Authenticate Git with your GitHub credentials? (Y/n) \x1b=\x1b>no\x1b7\x1b8\x1bc"
    assert strip_ansi(raw) == "      ? Authenticate Git with your GitHub credentials? (Y/n) no"
    assert strip_ansi("\x1b#8line") == "line"


def test_a_device_code_sign_in_needs_the_person():
    from winhand.session.state import Detectors, infer

    screen = (
        "! First copy your one-time code: 2F1B-8C3A\n"
        "Press Enter to open github.com/login/device in your browser... "
    )
    got = infer(
        alive=True,
        exit_code=None,
        last_line=screen.splitlines()[-1],
        since_input=screen,
        idle_s=2.0,
        det=Detectors(),
    )
    assert got["state"] == "needs_user" and got["kind"] == "browser_auth", got
    code_only = "! First copy your one-time code: 2F1B-8C3A"
    got = infer(
        alive=True, exit_code=None, last_line=code_only, since_input=code_only, idle_s=2.0, det=Detectors()
    )
    assert got["state"] == "needs_user", got


# ------------------------------------------------- results that fit what a client takes


def test_wide_text_is_clipped_by_what_it_costs():
    from winhand.session.text import clip, fitting, width

    assert width("ab中") == 5 and fitting("中文abc", 7) == 3 and fitting("中文abc", 7, from_end=True) == 4
    chinese = "中" * 20_000  # 60,000 bytes: about 20,000 tokens, not 20,000 characters' worth
    kept, omitted = clip(chinese, 30_000)
    assert len(kept) == 10_000 and omitted == 10_000
    both, omitted = clip("头" * 50 + "尾" * 50, 90, keep="both")
    assert both.startswith("头" * 10) and both.endswith("尾" * 20) and omitted == 70
    assert clip("short", 30_000) == ("short", 0)


def test_a_chinese_file_is_read_in_parts_a_client_takes(tmp_path):
    from winhand import fs
    from winhand.session.text import width

    path = tmp_path / "说明.md"
    path.write_text("\n".join("这是一行中文说明文字，" * 4 for _ in range(1500)), encoding="utf-8")
    first = fs.fs_read(str(path))
    assert width(first["content"]) <= fs.READ_MAX_CHARS and "fs_read(path, offset=" in first["more"]
    shown = len(first["content"].splitlines())
    assert 200 < shown < 1500  # 60,000 characters of it used to come back at once
    second = fs.fs_read(str(path), offset=shown + 1)
    assert second["content"].splitlines()[0].lstrip().startswith(str(shown + 1))


def test_a_listing_of_long_chinese_names_fits_one_result(tmp_path):
    from winhand import fs

    for i in range(500):
        (tmp_path / f"{i:03d}_第三方合同附件扫描件与签字页_最终确认版本_客户回传.pdf").write_bytes(b"")
    listed = fs.fs_list(str(tmp_path))
    size = sum(len(json.dumps(e, ensure_ascii=False).encode()) for e in listed["entries"])
    assert size <= fs.OUTPUT_BUDGET and len(listed["entries"]) < 500
    assert "all one result holds" in listed["truncated"]


def test_search_results_fit_one_result(tmp_path):
    from winhand import fs

    (tmp_path / "日志.txt").write_text(
        "\n".join("错误：" + "数据校验失败" * 30 for _ in range(300)), encoding="utf-8"
    )
    found = fs.fs_search("错误", str(tmp_path), max_results=300)
    assert sum(len(m.encode()) for m in found["matches"]) <= fs.OUTPUT_BUDGET
    assert found["count"] == len(found["matches"]) < 300 and "one result holds" in found["truncated"]


def test_session_output_pages_by_what_it_costs():
    from winhand.session.buffer import OutputBuffer

    buffer = OutputBuffer()
    buffer.append("编译输出" * 5000)
    first = buffer.read(0, 3000)
    assert len(first.text) == 1000 and first.end == 1000
    rest = buffer.read(first.end, 100_000)
    assert rest.start == 1000 and len(rest.text) == 19_000


def test_a_running_commands_chinese_output_is_tailed_by_what_it_costs(monkeypatch):
    class Job:
        argv = ["build.exe"]
        started = 0.0
        timed_out = stopped = False
        notes: list = []
        command = "build.exe"
        nested_direct = False
        script = None

        class proc:
            @staticmethod
            def poll():
                return None

        def text(self):
            return "正在编译模块\n" * 5000, ""

    monkeypatch.setitem(proc._running, "run-77", Job())
    status = proc.run_status("run-77", max_chars=3000)
    assert len(status["output"].encode()) <= 3000 and status["cursor"] == len("正在编译模块\n" * 5000)
    assert status["omitted_chars"] == len("正在编译模块\n" * 5000) - len(status["output"])
