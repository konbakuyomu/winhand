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
        "commandWindows": f"& '{py}' '{SCRIPT}' {mode}",
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
    claude_project_hook(env, "deny-rm", "Bash")
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
                "session_send", {"id": sid, "text": "echo rm -rf build", "submit": True}, raise_on_error=False
            )
            assert refused.is_error and "no rm -rf" in text_of(refused)
            ok = await client.call_tool("session_send", {"id": sid, "text": "echo fine", "submit": True})
            assert (
                "fine" in ok.structured_content["output"] and "rm -rf" not in ok.structured_content["output"]
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
        script = f"& '{sys.executable}' '{SCRIPT}' by-command"
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
