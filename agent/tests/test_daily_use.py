"""winhand 0.7.0: fixes for what tripped agents in daily use (seen in the machine's audit logs)."""

from __future__ import annotations

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

import json  # noqa: E402
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
