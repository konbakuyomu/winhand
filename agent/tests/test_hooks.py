"""The owner's Codex and Claude Code hooks run before winhand acts, only when those tools trust them."""

from __future__ import annotations

import json
import os
import sys
import time
import tomllib
from pathlib import Path

import psutil
import pytest
from fastmcp.client import Client
from mcp.types import TextContent

from winhand import hooks, secret_prompt, workspace
from winhand.actions import Action
from winhand.config import Config
from winhand.server import build_server

from .conftest import FAKE_APP

SCRIPT = str(Path(__file__).parent / "hook_script.py")


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A machine of our own: Codex and Claude Code homes, a project, hooks on."""
    home = tmp_path / "home"
    home.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("WINHAND_HOOK_MARK", str(tmp_path / "calls.jsonl"))
    if os.name != "nt":
        monkeypatch.setenv("SHELL", "/bin/sh")
    settings = {"sources": ["codex", "claude-code"], "timeout_s": 20}
    monkeypatch.setattr(hooks, "settings", lambda: settings)
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    return {"tmp": tmp_path, "root": root, "settings": settings, "mark": tmp_path / "calls.jsonl"}


def calls_made(env) -> list[dict]:
    if not env["mark"].exists():
        return []
    return [json.loads(line) for line in env["mark"].read_text(encoding="utf-8").splitlines()]


def codex_command(mode: str) -> dict:
    py = sys.executable
    return {
        "type": "command",
        "command": f'"{py}" "{SCRIPT}" {mode}',
        "commandWindows": f"& '{py}' '{SCRIPT}' {mode}; exit $LASTEXITCODE",
    }


def codex_project_hook(env, mode: str, matcher: str, trust: bool = True, trust_project: bool = True) -> dict:
    root = env["root"]
    handler = codex_command(mode)
    path = root / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [handler]}]}}), encoding="utf-8"
    )
    lines = []
    if trust_project:
        lines += [f"[projects.{json.dumps(str(root))}]", 'trust_level = "trusted"', ""]
    if trust:
        key = f"{path}:pre_tool_use:0:0"
        lines += [
            f"[hooks.state.{json.dumps(key)}]",
            f'trusted_hash = "{hooks.codex_hash(matcher, handler)}"',
        ]
    config = env["tmp"] / "codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return handler


def claude_project_hook(env, mode: str, matcher: str, trust: bool = True) -> None:
    root = env["root"]
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    handler = {"type": "command", "command": sys.executable, "args": [SCRIPT, mode]}
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [handler]}]}}))
    state = env["tmp"] / "claude" / ".claude.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    projects = {str(root).replace("\\", "/"): {"hasTrustDialogAccepted": True}} if trust else {}
    state.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def py_command(text: str) -> dict:
    return {"command": sys.executable, "args": ["-c", f"print({text!r})"]}


# ------------------------------------------------------------------ A1


async def test_a_trusted_codex_project_hook_refuses_a_write(env):
    codex_project_hook(env, "deny-src", "Write|Edit")
    root = env["root"]
    async with Client(build_server(Config())) as client:
        refused = await client.call_tool(
            "fs_write", {"path": str(root / "src" / "a.c"), "content": "int x;"}, raise_on_error=False
        )
        assert refused.is_error and "no writes in src/ in this phase" in text_of(refused)
        assert "pre_tool_use:0:0" in text_of(refused) and not (root / "src" / "a.c").exists()
        ok = await client.call_tool("fs_write", {"path": str(root / "docs" / "a.md"), "content": "# a"})
        assert not ok.is_error and (root / "docs" / "a.md").exists()
    seen = calls_made(env)
    assert [c["tool_name"] for c in seen] == ["apply_patch", "apply_patch"]
    assert "*** Add File: src/a.c" in seen[0]["tool_input"]["command"]
    assert seen[0]["cwd"] == str(root) and seen[0]["hook_event_name"] == "PreToolUse"


# ------------------------------------------------------------------ A2


async def test_claude_code_hook_answers(env, monkeypatch):
    claude_project_hook(env, "by-command", "Bash")
    asked = []
    monkeypatch.setattr(secret_prompt, "ask_confirm", lambda *a, **k: asked.append(a) or False)
    root = str(env["root"])
    async with Client(build_server(Config())) as client:

        async def run(word):
            return await client.call_tool("run", {**py_command(word), "cwd": root}, raise_on_error=False)

        denied = await run("DENY")
        assert (
            denied.is_error
            and "the hook says no" in text_of(denied)
            and "Claude Code hook" in text_of(denied)
        )
        held = await run("ASK")
        assert held.is_error and "said no" in text_of(held) and asked and "check this one" in asked[0][1]
        noted = await run("NOTE")
        assert not noted.is_error and "remember the house style" in text_of(noted)
        assert "NOTE" in noted.structured_content["stdout"]
        rewritten = await run("REWRITE")
        assert rewritten.is_error and "echo safer" in text_of(rewritten)
    assert {c["tool_name"] for c in calls_made(env)} == {"Bash"}


# ------------------------------------------------------------------ A3


@pytest.mark.parametrize(
    "case", ["no trusted_hash", "changed since trusted", "project not trusted", "claude: no workspace trust"]
)
async def test_untrusted_hooks_do_not_run(env, case):
    root = env["root"]
    if case.startswith("claude"):
        claude_project_hook(env, "by-command", "Bash", trust=False)
    else:
        handler = codex_project_hook(
            env,
            "by-command",
            "Bash",
            trust=case != "no trusted_hash",
            trust_project=case != "project not trusted",
        )
        if case == "changed since trusted":
            path = root / ".codex" / "hooks.json"
            changed = {**handler, "timeout": 5}
            path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [changed]}]}}))
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("run", {**py_command("DENY"), "cwd": str(root)})
        assert not result.is_error and "DENY" in result.structured_content["stdout"]
        status = (await client.call_tool("context", {"path": str(root)})).structured_content["hooks"]
    assert calls_made(env) == []
    (hook,) = status["hooks"]
    want = {"changed since trusted": "modified"}.get(case, "untrusted")
    assert hook["status"] == want and hook["runs"] is False and hook["reason"]


# ------------------------------------------------------------------ A4


def test_the_hash_is_codexs():
    value = tomllib.loads('rows = [{ z = 2, a = 1 }, { z = 4, a = 3 }]\n[nested]\nz = "text"\na = true\n')
    expected = "sha256:a5e4c8fbdbc75d185b0f2b5291bfb2de10e8c523d6c89248876fd78a97c83176"
    assert hooks.fingerprint(value) == expected  # codex-rs/config/src/fingerprint_tests.rs
    handler = {"type": "command", "command": "python3 C:/hooks/guard.py"}
    same = {**handler, "timeout": 600, "async": False, "additionalContextLimit": 2500}
    assert hooks.codex_hash("Bash", handler) == hooks.codex_hash("Bash", same)
    assert hooks.codex_hash("Bash", handler) != hooks.codex_hash("Edit", handler)


# ------------------------------------------------------------------ A5


@pytest.mark.parametrize("mode", ["sleep", "exit1", "badjson"])
async def test_a_failing_hook_lets_the_call_through(env, mode):
    env["settings"]["timeout_s"] = 2
    claude_project_hook(env, mode, "Bash")
    began = time.monotonic()
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("run", {**py_command("hello"), "cwd": str(env["root"])})
    assert not result.is_error and "hello" in result.structured_content["stdout"]
    assert "failed" in text_of(result)
    if mode == "sleep":
        assert time.monotonic() - began < 15
        pid = int(Path(str(env["mark"]) + ".pid").read_text())
        deadline = time.monotonic() + 5
        while psutil.pid_exists(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


# ------------------------------------------------------------------ A6


async def test_a_line_typed_into_a_shell_is_shown_to_hooks(env):
    claude_project_hook(env, "deny-word", "Bash")
    fake = [sys.executable, "-u", FAKE_APP]
    async with Client(build_server(Config())) as client:
        sid = (
            await client.call_tool(
                "session_start",
                {
                    "profile": "shell",
                    "command": fake,
                    "prompts": [r"^fake> ?$"],
                    "startup_wait_s": 10,
                    "cwd": str(env["root"]),
                },
            )
        ).structured_content["session"]["id"]
        try:
            refused = await client.call_tool(
                "session_send",
                {"id": sid, "text": "echo FORBIDDEN-WORD build", "submit": True},
                raise_on_error=False,
            )
            assert refused.is_error and "no forbidden word" in text_of(refused)
            ok = await client.call_tool("session_send", {"id": sid, "text": "echo fine", "submit": True})
            assert (
                "fine" in ok.structured_content["output"]
                and "FORBIDDEN-WORD" not in ok.structured_content["output"]
            )
        finally:
            await client.call_tool("session_stop", {"id": sid, "force": True})


# ------------------------------------------------------------------ A7


async def test_context_lists_hooks_and_why_they_do_not_run(env):
    root = env["root"]
    codex_project_hook(env, "by-command", "Bash")
    hooks_json = root / ".codex" / "hooks.json"
    data = json.loads(hooks_json.read_text())
    data["hooks"]["PreToolUse"].append({"matcher": "Read", "hooks": [codex_command("exit1")]})
    hooks_json.write_text(json.dumps(data))
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "echo hi"}]}]}})
    )
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("context", {"path": str(root)})).structured_content["hooks"]
    rows = [(h["source"], h["status"], h["runs"]) for h in out["hooks"]]
    assert rows == [
        ("codex", "trusted", True),
        ("codex", "untrusted", False),
        ("claude-code", "untrusted", False),
    ]
    assert all(h.get("reason") for h in out["hooks"] if not h["runs"])


# ------------------------------------------------------------------ A8, A9


async def test_a_global_command_rule_decides(env, winhand_home):
    script = f'"{sys.executable}" "{SCRIPT}" by-command'
    if os.name == "nt":
        script = f"& '{sys.executable}' '{SCRIPT}' by-command; exit $LASTEXITCODE"
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "guards.toml").write_text(
        f'[[guard]]\non = "command"\ncommand = {json.dumps(script)}\n', encoding="utf-8"
    )
    async with Client(build_server(Config())) as client:
        denied = await client.call_tool("run", py_command("DENY"), raise_on_error=False)
        assert (
            denied.is_error
            and "the hook says no" in text_of(denied)
            and "guards.toml rule 1" in text_of(denied)
        )
        fine = await client.call_tool("run", py_command("fine"))
        assert not fine.is_error


async def test_a_project_command_rule_is_refused(env):
    root = env["root"]
    rules = root / ".winhand" / "guards.toml"
    rules.parent.mkdir(parents=True)
    rules.write_text('[[guard]]\non = "write"\ncommand = "echo hi"\n', encoding="utf-8")
    async with Client(build_server(Config())) as client:
        refused = await client.call_tool(
            "fs_write", {"path": str(root / "a.txt"), "content": "x"}, raise_on_error=False
        )
    assert refused.is_error and "only in the global guards.toml" in text_of(refused)
    assert not (root / "a.txt").exists()


# ------------------------------------------------------------------ A10


async def test_hooks_can_be_turned_off(env):
    claude_project_hook(env, "by-command", "Bash")
    env["settings"]["sources"] = []
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("run", {**py_command("DENY"), "cwd": str(env["root"])})
        status = (await client.call_tool("context", {"path": str(env["root"])})).structured_content["hooks"]
    assert not result.is_error and calls_made(env) == [] and status["hooks"] == []


# ------------------------------------------------------------------ pieces


def test_matchers():
    def hook(source, matcher):
        return hooks.Hook(source, "f", 0, 0, matcher, {"command": "x"})

    assert hooks.matches(hook("codex", None), ("Bash",)) and hooks.matches(hook("codex", "*"), ("Bash",))
    assert hooks.matches(hook("codex", "Write|Edit"), ("apply_patch", "Write", "Edit"))
    assert not hooks.matches(hook("codex", "Bash"), ("apply_patch", "Write", "Edit"))
    assert hooks.matches(hook("claude-code", "Bash, Read"), ("Read",))
    assert hooks.matches(hook("codex", "^Ba"), ("Bash",)) and not hooks.matches(
        hook("codex", "mcp__.*"), ("Bash",)
    )
    assert not hooks.matches(hook("codex", "(unclosed"), ("Bash",))


@pytest.mark.parametrize(
    ("source", "run", "decision"),
    [
        ("claude-code", hooks.Run(code=2, stderr="no"), "deny"),
        ("codex", hooks.Run(code=2, stderr=""), "note"),
        ("claude-code", hooks.Run(code=2, stderr=""), "deny"),
        ("codex", hooks.Run(code=0, stdout='{"hookSpecificOutput": {"permissionDecision": "ask"}}'), "note"),
        (
            "claude-code",
            hooks.Run(code=0, stdout='{"hookSpecificOutput": {"permissionDecision": "ask"}}'),
            "ask",
        ),
        ("codex", hooks.Run(code=0, stdout='{"hookSpecificOutput": {"permissionDecision": "deny"}}'), "note"),
        (
            "codex",
            hooks.Run(
                code=0,
                stdout='{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "x"}}',
            ),
            "deny",
        ),
        ("claude-code", hooks.Run(code=0, stdout='{"decision": "block", "reason": "old style"}'), "deny"),
        ("claude-code", hooks.Run(code=0, stdout='{"continue": false, "stopReason": "stop"}'), "deny"),
        ("codex", hooks.Run(code=0, stdout='{"continue": false}'), "note"),
        (
            "claude-code",
            hooks.Run(code=1, stdout='{"hookSpecificOutput": {"permissionDecision": "deny"}}'),
            "deny",
        ),
        ("codex", hooks.Run(code=1, stdout=""), "note"),
        ("claude-code", hooks.Run(code=0, stdout="plain words"), None),
        (
            "claude-code",
            hooks.Run(code=0, stdout='{"hookSpecificOutput": {"permissionDecision": "allow"}}'),
            None,
        ),
        ("claude-code", hooks.Run(timed_out=True), "note"),
    ],
)
def test_answers_are_read_as_each_tool_reads_them(source, run, decision):
    hook = hooks.Hook(source, "f", 0, 0, None, {"command": "x"})
    assert hooks.interpret(hook, run, 30).decision == decision


def test_on_failure_block():
    hook = hooks.Hook("claude-code", "f", 0, 0, None, {"command": "x", "onFailure": "block"})
    assert hooks.interpret(hook, hooks.Run(timed_out=True), 30).decision == "deny"


def test_what_each_tool_is_shown(tmp_path):
    root = str(tmp_path)
    target = str(tmp_path / "src" / "a.c")
    write = Action("write", "fs_write", paths=(target,))
    (codex,) = hooks.calls(write, "codex", "fs_write", {"content": "int x;"}, root)
    assert codex[0] == "apply_patch" and "*** Add File: src/a.c\n+int x;" in codex[1]["command"]
    (claude,) = hooks.calls(write, "claude-code", "fs_write", {"content": "int x;"}, root)
    assert claude[:2] == ("Write", {"file_path": target, "content": "int x;"})
    read = Action("read", "fs_read", paths=(target,))
    assert hooks.calls(read, "codex", "fs_read", {}, root) == []
    edit = Action("edit", "fs_edit", paths=(target,))
    (claude_edit,) = hooks.calls(edit, "claude-code", "fs_edit", {"old": "a", "new": "b"}, root)
    assert claude_edit[1] == {"file_path": target, "old_string": "a", "new_string": "b", "replace_all": False}
    delete = Action("delete", "fs_op", paths=(target,))
    assert (
        hooks.calls(delete, "codex", "fs_op", {}, root)[0][1]["command"]
        == f"Remove-Item -LiteralPath '{target}'"
    )


def test_codex_trust_keys_compare_like_the_file_system():
    assert hooks._state_key("C:\\x\\hooks.json:pre_tool_use:0:1").endswith(":pre_tool_use:0:1")
    if os.name == "nt":
        assert hooks._state_key("C:\\X\\Hooks.json:pre_tool_use:0:0") == hooks._state_key(
            "c:\\x\\hooks.json:pre_tool_use:0:0"
        )


def test_workspace_is_imported():  # hooks reuse context()'s view of where Codex and Claude Code live
    assert hooks.workspace is workspace


# ------------------------------------------------------------------ round 2


def codex_hook_in(env, folder: Path, mode: str = "by-command", matcher: str = "Bash") -> None:
    """A trusted-by-hash Codex hook in `folder`/.codex; the trust of folders is set by each test."""
    handler = codex_command(mode)
    path = folder / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": [handler]}]}}), encoding="utf-8"
    )
    config = env["tmp"] / "codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    key = f"{path}:pre_tool_use:0:0"
    with open(config, "a", encoding="utf-8") as fh:
        fh.write(f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{hooks.codex_hash(matcher, handler)}"\n')


def trust_levels(env, levels: dict) -> None:
    config = env["tmp"] / "codex" / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    with open(config, "a", encoding="utf-8") as fh:
        for folder, level in levels.items():
            fh.write(f'[projects.{json.dumps(str(folder))}]\ntrust_level = "{level}"\n')


@pytest.mark.parametrize(
    ("hook_in", "levels", "runs"),
    [
        ("root", {"root": "untrusted", "sub": "trusted"}, False),
        ("sub", {"root": "trusted", "sub": "untrusted"}, False),
        ("root", {"sub": "trusted"}, False),
        ("sub", {"sub": "trusted"}, True),
        ("sub", {"root": "trusted"}, True),
        ("sub", {"root": "untrusted", "sub": "trusted"}, True),  # Codex: the folder's own decision first
    ],
)
def test_codex_trust_is_decided_for_each_codex_folder(env, hook_in, levels, runs):
    root = env["root"]
    sub = root / "sub"
    sub.mkdir()
    folders = {"root": root, "sub": sub}
    codex_hook_in(env, folders[hook_in])
    trust_levels(env, {folders[k]: v for k, v in levels.items()})
    found, _ = hooks.discover(str(sub))
    (hook,) = [h for h in found if h.file.startswith(str(root))]
    assert hook.runs is runs, (hook.status, hook.reason)


async def test_a_strange_timeout_does_not_break_anything(env):
    root = env["root"]
    handler = {**codex_command("by-command"), "timeout": "5"}
    (root / ".codex").mkdir()
    (root / ".codex" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [handler]}]}})
    )
    trust_levels(env, {root: "trusted"})
    claude_project_hook(env, "by-command", "Bash")
    settings = root / ".claude" / "settings.json"
    data = json.loads(settings.read_text())
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = "abc"
    data["hooks"]["PreToolUse"].append({"matcher": "Ba(sh", "hooks": [{"command": "echo hi"}]})
    settings.write_text(json.dumps(data))
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("run", {**py_command("DENY"), "cwd": str(root)})
        report = (await client.call_tool("context", {"path": str(root)})).structured_content["hooks"]
    assert not result.is_error and calls_made(env) == []
    listed = report["hooks"]
    assert [(h["source"], h["status"]) for h in listed] == [("claude-code", "unsupported")] * 2
    assert "timeout" in listed[0]["reason"] and "regular expression" in listed[1]["reason"]
    codex_error = report["errors"][0]["error"]  # Codex rejects the whole file over the timeout
    assert "Codex would not load it" in codex_error and "timeout" in codex_error


def test_a_device_is_not_read_as_a_hooks_file(env):
    if os.name == "nt":
        pytest.skip("no /dev/zero")
    (env["root"] / ".codex").mkdir()
    os.symlink("/dev/zero", env["root"] / ".codex" / "hooks.json")
    began = time.monotonic()
    found, errors = hooks.discover(str(env["root"]))
    assert time.monotonic() - began < 2 and "regular file" in errors[0]["error"]


async def test_a_refusal_counts_even_if_the_hook_leaves_something_running(env):
    env["settings"]["timeout_s"] = 3
    claude_project_hook(env, "deny-and-linger", "Bash")
    async with Client(build_server(Config())) as client:
        result = await client.call_tool(
            "run", {**py_command("hi"), "cwd": str(env["root"])}, raise_on_error=False
        )
    assert result.is_error and "something lingers" in text_of(result)
    pid = int(Path(str(env["mark"]) + ".pid").read_text())
    deadline = time.monotonic() + 5
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


async def test_codex_home_inside_a_project_runs_once(env, monkeypatch):
    root = env["root"]
    monkeypatch.setenv("CODEX_HOME", str(root / ".codex"))
    (root / ".codex").mkdir()
    handler = codex_command("by-command")
    (root / ".codex" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [handler]}]}})
    )
    key = f"{root / '.codex' / 'hooks.json'}:pre_tool_use:0:0"
    (root / ".codex" / "config.toml").write_text(
        f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{hooks.codex_hash("Bash", handler)}"\n'
    )
    async with Client(build_server(Config())) as client:
        await client.call_tool("run", {**py_command("hello"), "cwd": str(root)})
    assert len(calls_made(env)) == 1


def test_claude_trust_does_not_reach_a_nested_repository(env, tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    (inner / ".git").mkdir(parents=True)
    plain = outer / "notes"
    plain.mkdir()
    state = env["tmp"] / "claude" / ".claude.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"projects": {str(outer): {"hasTrustDialogAccepted": True}}}))
    assert not hooks._claude_trusted(str(inner)) and hooks._claude_trusted(str(plain))


def test_managed_disable_all_hooks_turns_off_managed_ones_too(env, tmp_path, monkeypatch):
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(hooks, "claude_managed_dir", lambda: str(managed))
    (managed / "managed-settings.json").write_text(
        json.dumps({"disableAllHooks": True, "hooks": {"PreToolUse": [{"hooks": [{"command": "echo hi"}]}]}})
    )
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "claude-code"]
    assert hook.status == "disabled"


@pytest.mark.parametrize(
    "stdout",
    [
        '{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "x", "extra": 1}}',
        '{"stopReason": "no", "hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "x"}}',
        '{"unknown": true}',
    ],
)
def test_codex_reads_its_output_strictly(stdout):
    hook = hooks.Hook("codex", "f", 0, 0, None, {"command": "x"})
    assert hooks.interpret(hook, hooks.Run(code=0, stdout=stdout), 30).decision == "note"


async def test_with_hooks_on_and_none_written_nothing_changes(env):
    root = env["root"]
    async with Client(build_server(Config())) as client:
        wrote = await client.call_tool("fs_write", {"path": str(root / "a.txt"), "content": "x"})
        read = await client.call_tool("fs_read", {"path": str(root / "a.txt")})
        ran = await client.call_tool("run", {**py_command("hello"), "cwd": str(root)})
    for result in (wrote, read, ran):
        text = text_of(result)
        assert (
            not result.is_error
            and "guard notes" not in text
            and "Code hook" not in text
            and "Codex hook" not in text
        )


async def test_on_windows_a_refusal_by_exit_code_survives_powershell(env):
    if os.name != "nt":
        pytest.skip("PowerShell's -Command exit codes are a Windows matter here")
    root = env["root"]
    handler = {
        "type": "command",
        "command": "x",
        "commandWindows": f"& '{sys.executable}' '{SCRIPT}' deny-src",
    }
    path = root / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [handler]}]}}))
    trust_levels(env, {root: "trusted"})
    with open(env["tmp"] / "codex" / "config.toml", "a", encoding="utf-8") as fh:
        key = f"{path}:pre_tool_use:0:0"
        fh.write(f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{hooks.codex_hash("Write", handler)}"\n')
    async with Client(build_server(Config())) as client:
        refused = await client.call_tool(
            "fs_write", {"path": str(root / "src" / "a.c"), "content": "x"}, raise_on_error=False
        )
    assert refused.is_error and "no writes in src/" in text_of(refused)


def test_powershell_commands_pass_the_exit_code_on():
    hook = hooks.Hook("codex", "f", 0, 0, None, {"command": "node x.mjs", "commandWindows": "node x.mjs"})
    line = hooks.argv(hook)
    if os.name == "nt" and isinstance(line, list):
        assert line[-1].startswith("node x.mjs\nif (-not $?)")


def test_the_global_guard_file_reached_through_a_link_is_still_global(tmp_path, monkeypatch):
    from winhand import guard

    if os.name == "nt":
        pytest.skip("symbolic links need a privilege on Windows")
    real = tmp_path / "realhome"
    (real / ".winhand").mkdir(parents=True)
    (real / ".winhand" / "guards.toml").write_text('[[guard]]\non = "write"\ncommand = "true"\n')
    link = tmp_path / "home"
    os.symlink(real, link)
    monkeypatch.setenv("WINHAND_HOME", str(link / ".winhand"))
    files = guard.files_for([str(real / "proj" / "a.txt")])
    assert [f.error for f in files] == [None]


def test_with_winhand_home_elsewhere_the_home_file_is_a_project_file(tmp_path, monkeypatch):
    from winhand import guard

    home = tmp_path / "home"
    (home / ".winhand").mkdir(parents=True)
    (home / ".winhand" / "guards.toml").write_text('[[guard]]\non = "write"\ndecision = "deny"\n')
    monkeypatch.setenv("WINHAND_HOME", str(tmp_path / "elsewhere"))
    files = guard.files_for([str(home / "proj" / "a.txt")])
    assert any(f.path == str(home / ".winhand" / "guards.toml") for f in files)


# ------------------------------------------------------------------ round 3


def test_codex_feature_switch_turns_its_hooks_off(env):
    codex_project_hook(env, "by-command", "Bash")
    with open(env["tmp"] / "codex" / "config.toml", "a", encoding="utf-8") as fh:
        fh.write("[features]\nhooks = false\n")
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "codex"]
    assert hook.status == "disabled" and "features" in hook.reason


def test_nan_timeouts_never_escape_the_cap(env, winhand_home):
    root = env["root"]
    settings = root / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text('{"hooks": {"PreToolUse": [{"hooks": [{"command": "x", "timeout": NaN}]}]}}')
    found, errors = hooks.discover(str(root))
    assert not [h for h in found if h.source == "claude-code"] and "NaN" in errors[0]["error"]
    from winhand import guard

    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "guards.toml").write_text('[[guard]]\non = "write"\ncommand = "x"\ntimeout_s = nan\n')
    assert guard.load(str(winhand_home / "guards.toml")).error


def test_codex_timeout_zero_is_one_second():
    hook = hooks.Hook("codex", "f", 0, 0, None, {"command": "x", "timeout": 0})
    assert hooks._seconds(hook, 30) == 1.0
    claude = hooks.Hook("claude-code", "f", 0, 0, None, {"command": "x", "timeout": 0})
    assert hooks._seconds(claude, 30) == 30.0


def test_a_hooks_file_codex_would_reject_is_not_used(env):
    root = env["root"]
    codex_project_hook(env, "by-command", "Bash")
    path = root / ".codex" / "hooks.json"
    data = json.loads(path.read_text())
    data["surprise"] = True
    path.write_text(json.dumps(data))
    found, errors = hooks.discover(str(root))
    assert not [h for h in found if h.file == str(path)] and "Codex would not load it" in errors[0]["error"]


def make_worktree(main: Path, worktree: Path) -> None:
    """What `git worktree add` leaves: the worktree's .git file, and git's records of it."""
    gitdir = main / ".git" / "worktrees" / worktree.name
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "gitdir").write_text(f"{worktree / '.git'}\n")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}\n")


def test_a_worktree_uses_the_main_checkouts_hooks(env, tmp_path):
    main = env["root"]
    codex_project_hook(env, "by-command", "Bash")
    worktree = tmp_path / "wt"
    make_worktree(main, worktree)
    found, _ = hooks.discover(str(worktree))
    assert not [h for h in found if h.source == "codex"]  # Codex needs the worktree's own .codex folder
    (worktree / ".codex").mkdir()
    found, _ = hooks.discover(str(worktree))
    assert [h.file for h in found if h.source == "codex"] == [str(main / ".codex" / "hooks.json")]
    assert all(h.runs for h in found if h.source == "codex")  # the main checkout is trusted
    trust_levels(env, {worktree: "untrusted"})  # the worktree's own decision comes first
    found, _ = hooks.discover(str(worktree))
    assert [h.status for h in found if h.source == "codex"] == ["untrusted"]


def test_claude_code_trust_of_a_worktree_is_the_main_checkouts(env, tmp_path):
    main = env["root"]
    claude_project_hook(env, "by-command", "Bash")  # trusts the main checkout
    worktree = main / ".claude" / "worktrees" / "w1"
    worktree.parent.mkdir(parents=True)
    make_worktree(main, worktree)
    (worktree / ".claude").mkdir()
    (worktree / ".claude" / "settings.json").write_text((main / ".claude" / "settings.json").read_text())
    found, _ = hooks.discover(str(worktree))
    assert [h.status for h in found if h.source == "claude-code"] == ["trusted"]


def test_a_lone_surrogate_does_not_stop_the_hooks(env):
    claude_project_hook(env, "by-command", "Write")
    action = Action("write", "fs_write", paths=(str(env["root"] / "a.txt"),))
    assert hooks.check([action], "fs_write", {"content": "bad \ud800 text"}) == []


# ------------------------------------------------------------------ round 4


def codex_file(env, data, raw: bytes | None = None) -> Path:
    """A trusted project's .codex/hooks.json holding `data`, each command handler trusted by hash."""
    root = env["root"]
    path = root / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw if raw is not None else json.dumps(data).encode())
    trust_levels(env, {root: "trusted"})
    with open(env["tmp"] / "codex" / "config.toml", "a", encoding="utf-8") as fh:
        for g, group in enumerate(data["hooks"].get("PreToolUse", [])):
            for i, handler in enumerate(group.get("hooks") or []):
                key = f"{path}:pre_tool_use:{g}:{i}"
                digest = hooks.codex_hash(group.get("matcher"), handler)
                fh.write(f'[hooks.state.{json.dumps(key)}]\ntrusted_hash = "{digest}"\n')
    return path


def test_keys_codex_ignores_do_not_stop_its_hooks(env):
    handler = {**codex_command("deny-src"), "shell": "bash", "args": ["x"], "if": "never"}
    codex_file(
        env,
        {
            "description": "comet",
            "hooks": {
                "PreToolUse": [{"matcher": None, "description": "phase guard", "hooks": [handler]}],
                "Notification": "not an event Codex knows",
            },
        },
    )
    found, errors = hooks.discover(str(env["root"]))
    assert errors == [] and [h.status for h in found if h.source == "codex"] == ["trusted"]
    action = Action("write", "fs_write", paths=(str(env["root"] / "src" / "a.c"),))
    (finding,) = hooks.check([action], "fs_write", {"content": "x"})
    assert finding.decision == "deny" and "no writes in src/" in finding.text


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d["hooks"]["PreToolUse"][0]["hooks"].append({"type": "http", "url": "x"}),
        lambda d: d["hooks"]["PreToolUse"][0]["hooks"].append({"type": "command"}),
        lambda d: d["hooks"]["PreToolUse"][0]["hooks"].append(
            {"type": "command", "command": "x", "timeout": 1.5}
        ),
        lambda d: d["hooks"]["PreToolUse"][0]["hooks"].append({"type": "mcp_tool", "tool": "t"}),
        lambda d: d["hooks"].update(PostToolUse=[{"hooks": [{"type": "command", "command": 5}]}]),
        lambda d: d.update(description=3),
        lambda d: d["hooks"]["PreToolUse"][0].update(hooks=None),
    ],
)
def test_files_codex_rejects_are_not_used(env, change):
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [codex_command("by-command")]}]}}
    change(data)
    path = codex_file(env, data)
    found, errors = hooks.discover(str(env["root"]))
    assert not [h for h in found if h.file == str(path)] and "Codex would not load it" in errors[0]["error"]


def test_a_byte_order_mark_is_refused_as_codex_does(env):
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [codex_command("by-command")]}]}}
    path = codex_file(env, data, raw=b"\xef\xbb\xbf" + json.dumps(data).encode())
    found, errors = hooks.discover(str(env["root"]))
    assert not [h for h in found if h.file == str(path)] and "byte order mark" in errors[0]["error"]


@pytest.mark.parametrize(
    ("user", "project", "pinned", "on"),
    [
        ("hooks = false", "hooks = true", None, True),
        ("hooks = true", "hooks = false", None, False),
        (None, "hooks = false\ncodex_hooks = true", None, False),
        ("hooks = true", None, "hooks = false", False),
        ("hooks = false", None, "codex_hooks = true", True),
    ],
)
def test_codex_feature_switch_follows_its_layers(env, tmp_path, monkeypatch, user, project, pinned, on):
    codex_project_hook(env, "by-command", "Bash")
    if user:
        with open(env["tmp"] / "codex" / "config.toml", "a", encoding="utf-8") as fh:
            fh.write(f"[features]\n{user}\n")
    if project:
        (env["root"] / ".codex" / "config.toml").write_text(f"[features]\n{project}\n")
    if pinned:
        system = tmp_path / "codex-system"
        system.mkdir()
        (system / "requirements.toml").write_text(f"[features]\n{pinned}\n")
        monkeypatch.setattr(hooks, "codex_system_dir", lambda: str(system))
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "codex"]
    assert hook.runs is on


def test_an_untrusted_projects_features_do_not_count(env):
    codex_project_hook(env, "by-command", "Bash")
    sub = env["root"] / "sub"
    (sub / ".codex").mkdir(parents=True)
    (sub / ".codex" / "config.toml").write_text("[features]\nhooks = false\n")
    trust_levels(env, {sub: "untrusted"})
    (hook,) = [h for h in hooks.discover(str(sub))[0] if h.source == "codex"]
    assert hook.runs


def test_the_legacy_managed_config_holds_managed_hooks(env, monkeypatch, tmp_path):
    legacy = tmp_path / "managed_config.toml"
    command = codex_command("by-command")["command"].replace("\\", "\\\\").replace('"', '\\"')
    legacy.write_text(
        f'[[hooks.PreToolUse]]\nmatcher = "Bash"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "{command}"\n'
    )
    monkeypatch.setattr(hooks, "codex_legacy_managed_config", lambda: str(legacy))
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "codex"]
    assert hook.status == "managed"


def test_codex_matchers_are_compared_as_written():
    def hook(matcher):
        return hooks.Hook("codex", "f", 0, 0, matcher, {"command": "x"})

    assert not hooks.matches(hook(" "), ("Bash",))
    assert not hooks.matches(hook("Bash "), ("Bash",))
    assert hooks.matches(hook("Write|Bash"), ("Bash",))
    claude = hooks.Hook("claude-code", "f", 0, 0, " ", {"command": "x"})
    assert hooks.matches(claude, ("Bash",))


def test_an_empty_command_windows_is_what_codex_runs():
    assert hooks._codex_windows_command({"command": "x", "commandWindows": ""}) == ""
    assert hooks._codex_windows_command({"command": "x"}) == "x"


def test_context_stays_small_when_a_hooks_file_is_huge(env):
    path = env["root"] / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({f"key{i}": i for i in range(8000)} | {"hooks": {}}))
    report = hooks.describe(str(env["root"]))
    assert len(json.dumps(report)) < 12_000 and "Codex would not load it" in report["errors"][0]["error"]


def test_a_strange_hooks_timeout_falls_back(monkeypatch):
    assert hooks.timeout_cap({"timeout_s": float("inf")}) == hooks.DEFAULT_TIMEOUT_S


# ------------------------------------------------------------------ round 5


def test_optional_fields_set_to_null_count_as_not_set(env):
    handler = {**codex_command("deny-src"), "statusMessage": None}
    codex_file(env, {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [handler]}]}})
    action = Action("write", "fs_write", paths=(str(env["root"] / "src" / "a.c"),))
    (finding,) = hooks.check([action], "fs_write", {"content": "x"})
    assert finding.decision == "deny"
    for key in ("commandWindows", "command_windows"):
        assert hooks._unsupported(hooks.Hook("codex", "f", 0, 0, None, {"command": "x", key: None})) is None
    claude = hooks.Hook("claude-code", "f", 0, 0, None, {"command": "x", "args": None, "statusMessage": None})
    assert hooks._unsupported(claude) is None


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
def test_a_project_reached_through_a_link_keeps_its_trust(env, tmp_path):
    codex_project_hook(env, "by-command", "Bash")
    claude_project_hook(env, "by-command", "Bash")
    link = tmp_path / "linked"
    os.symlink(env["root"], link)
    found, _ = hooks.discover(str(link))
    assert sorted(h.status for h in found) == ["trusted", "trusted"]


def test_a_folder_that_only_claims_to_be_a_worktree_is_not_one(env, tmp_path):
    claude_project_hook(env, "by-command", "Bash")  # trusts env["root"]
    fake = tmp_path / "unpacked"
    (fake / "fakegit").mkdir(parents=True)
    (fake / "fakegit" / "commondir").write_text(f"{env['root'] / '.git'}\n")
    (fake / ".git").write_text("gitdir: fakegit\n")
    (fake / ".claude").mkdir()
    (fake / ".claude" / "settings.json").write_text((env["root"] / ".claude" / "settings.json").read_text())
    assert hooks._main_checkout(str(fake)) == str(fake)
    found, _ = hooks.discover(str(fake))
    assert [h.status for h in found if h.source == "claude-code"] == ["untrusted"]


def test_the_legacy_managed_file_can_decide_trust(env, monkeypatch, tmp_path):
    codex_project_hook(env, "by-command", "Bash", trust_project=False)
    legacy = tmp_path / "managed_config.toml"
    legacy.write_text(f'[projects.{json.dumps(str(env["root"]))}]\ntrust_level = "trusted"\n')
    monkeypatch.setattr(hooks, "codex_legacy_managed_config", lambda: str(legacy))
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "codex"]
    assert hook.status == "trusted"


def test_mcp_input_numbers_are_judged_as_serde_reads_them():
    assert hooks._toml_value({"n": 2**70}) and hooks._toml_value({"n": -(2**63)})
    assert not hooks._toml_value({"n": 2**63})


# ------------------------------------------------------------------ round 6


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are a POSIX thing")
@pytest.mark.parametrize("record", ["commondir", "gitdir"])
def test_a_pipe_among_the_git_records_never_blocks(env, tmp_path, record):
    import concurrent.futures

    folder = tmp_path / "unpacked"
    gitdir = env["root"] / ".git" / "worktrees" / "unpacked"
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text("../..\n")
    (gitdir / "gitdir").write_text(f"{folder / '.git'}\n")
    (gitdir / record).unlink()
    os.mkfifo(gitdir / record)
    folder.mkdir()
    (folder / ".git").write_text(f"gitdir: {gitdir}\n")
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(hooks.discover, str(folder))
        try:
            future.result(timeout=10)
        finally:
            if not future.done():  # unblock the reader so the test can end
                with open(gitdir / record, "w"):
                    pass
    assert hooks._main_checkout(str(folder)) == str(folder)


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need a privilege on Windows")
def test_a_linked_codex_folder_does_not_borrow_another_projects_trust(env, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    codex_hook_in(env, other)  # trusted by hash under other's key
    trust_levels(env, {other: "trusted", env["root"]: "trusted"})
    os.symlink(other / ".codex", env["root"] / ".codex")
    (hook,) = [h for h in hooks.discover(str(env["root"]))[0] if h.source == "codex"]
    assert hook.status == "untrusted"


@pytest.mark.parametrize("number", ["1e400", "-0"])
def test_numbers_serde_cannot_read_make_codex_drop_the_file(env, number):
    handler = codex_command("by-command")
    raw = json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [handler]}]}})
    raw = raw.replace('"type": "command"', f'"type": "command", "timeout": {number}')
    path = codex_file(env, {"hooks": {}}, raw=raw.encode())
    found, errors = hooks.discover(str(env["root"]))
    assert not [h for h in found if h.file == str(path)] and errors and errors[0]["file"] == str(path)


# ------------------------------------------------------------------ round 7


def test_an_odd_number_where_codex_does_not_look_leaves_the_file_alone(env):
    handler = codex_command("deny-src")
    data = {"hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [handler]}]}}
    raw = json.dumps(data).replace('"matcher": "Write"', '"matcher": "Write", "note": 1e400')
    raw = raw.replace('{"PreToolUse"', '{"Notification": [{"x": -1e400}], "PreToolUse"')
    codex_file(env, data, raw=raw.encode())
    found, errors = hooks.discover(str(env["root"]))
    assert errors == [] and [h.status for h in found if h.source == "codex"] == ["trusted"]


@pytest.mark.parametrize("where", ["handler", "input"])
def test_a_whole_number_too_long_for_serde_makes_codex_drop_the_file(env, where):
    long = "9" * 320
    data = {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [codex_command("by-command")]}]}}
    raw = json.dumps(data)
    if where == "handler":
        raw = raw.replace('"type": "command"', f'"type": "command", "weight": {long}')
    else:
        raw = raw.replace(
            '"hooks": [{"type"',
            f'"hooks": [{{"type": "mcp_tool", "server": "s", "tool": "t", "input": {{"n": {long}}}}}, {{"type"',
        )
    path = codex_file(env, data, raw=raw.encode())
    found, errors = hooks.discover(str(env["root"]))
    assert not [h for h in found if h.file == str(path)] and "out of range" in errors[0]["error"]


def test_whole_numbers_read_as_serde_json_reads_them():
    assert hooks._serde_int("-0") == 0.0 and isinstance(hooks._serde_int("-0"), float)
    assert hooks._serde_int(str(2**64 - 1)) == 2**64 - 1
    assert isinstance(hooks._serde_int(str(2**64)), float)
    assert hooks._serde_int("9" * 400) is hooks.OUT_OF_RANGE
    assert hooks._serde_int("9" * 5000) is hooks.OUT_OF_RANGE


def test_a_large_git_config_still_names_the_remote(tmp_path):
    common = tmp_path / ".git"
    common.mkdir()
    sections = "".join(
        f'[branch "b{i}"]\n\tremote = origin\n\tmerge = refs/heads/b{i}\n' for i in range(30_000)
    )
    (common / "config").write_text('[remote "origin"]\n\turl = https://github.com/o/r.git\n' + sections)
    assert (common / "config").stat().st_size > 1_000_000
    assert workspace._remote_urls(str(common)) == ["https://github.com/o/r.git"]
