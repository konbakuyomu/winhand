"""Guard rules (~/.winhand/guards.toml, <project>/.winhand/guards.toml) and the actions they judge."""

from __future__ import annotations

import os
import sys
import textwrap
import time

import pytest
from fastmcp.client import Client
from mcp.types import TextContent

from winhand import actions, guard, secret_prompt
from winhand.config import Config
from winhand.server import build_server
from winhand.session import SessionSpec

from .conftest import FAKE_APP


def write_rules(path, body: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


@pytest.fixture
async def client():
    async with Client(build_server(Config())) as c:
        yield c


NO_BATCH_DELETE = """\
    [[guard]]
    on = ["command", "input"]
    match = '(?i)(\\brm\\s+-\\w*r\\w*f|Remove-Item\\b.*-Recurse)'
    decision = "deny"
    reason = "no batch deletes"
"""


# ------------------------------------------------------------------ actions


def test_describe_maps_tools_to_actions(tmp_path):
    d = str(tmp_path)
    run = actions.describe("run", {"command": "git", "args": ["push"], "cwd": d})
    assert run == [actions.Action("command", "run", (), "git push", d)]
    assert actions.describe("run", {"script": "a\nb", "shell": "pwsh"})[0].text == "a\nb"
    assert actions.describe("remote_run", {"host": "pi", "script": "ls"})[0].host == "pi"
    gdb = actions.describe("session_start", {"profile": "gdb", "args": ["fw.elf"]})[0]
    assert gdb.kind == "command" and gdb.text.endswith("-q -nx fw.elf")
    copy = actions.describe("fs_op", {"action": "copy", "path": f"{d}/a", "to": f"{d}/b"})
    assert [(a.kind, a.paths) for a in copy] == [
        ("read", (os.path.join(d, "a"),)),
        ("write", (os.path.join(d, "b"),)),
    ]
    move = actions.describe("fs_op", {"action": "move", "path": f"{d}/a", "to": f"{d}/b"})[0]
    assert move.kind == "move" and len(move.paths) == 2
    assert actions.describe("fs_op", {"action": "delete", "path": d})[0].kind == "delete"
    assert actions.describe("fs_stat", {"path": d}) == []
    assert actions.describe("screenshot", {}) == []


def test_describe_patch_lists_every_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.c").write_text("int a;\n")
    patch = textwrap.dedent(
        """\
        diff --git a/src/a.c b/src/a.c
        --- a/src/a.c
        +++ b/src/a.c
        @@ -1 +1 @@
        -int a;
        +int a = 1;
        diff --git a/src/new.c b/src/new.c
        new file mode 100644
        --- /dev/null
        +++ b/src/new.c
        @@ -0,0 +1 @@
        +int b;
        """
    )
    (edit,) = actions.describe("fs_apply_patch", {"root": str(tmp_path), "patch": patch})
    assert edit.kind == "edit"
    assert set(edit.paths) == {str(tmp_path / "src" / "a.c"), str(tmp_path / "src" / "new.c")}
    (fallback,) = actions.describe("fs_apply_patch", {"root": str(tmp_path), "patch_file": "/nope.diff"})
    assert fallback.paths == (str(tmp_path),)


def test_shell_sessions_are_recognised():
    def spec(argv, **kw):
        return SessionSpec(transport=kw.pop("transport", "pty"), argv=argv, **kw)

    assert actions.is_shell(spec([]))
    assert actions.is_shell(spec([r"C:\Program Files\PowerShell\7\pwsh.exe", "-NoLogo"]))
    assert actions.is_shell(spec(["cmd.exe"])) and actions.is_shell(spec(["/bin/bash", "-l"]))
    assert actions.is_shell(spec(["python"], profile="shell"))
    assert not actions.is_shell(spec(["arm-none-eabi-gdb", "-q"]))
    assert not actions.is_shell(spec([], transport="serial"))


# ------------------------------------------------------------------ deny (A2, A3)


async def test_batch_delete_command_is_refused_and_not_run(client, winhand_home, tmp_path):
    rules = write_rules(winhand_home / "guards.toml", NO_BATCH_DELETE)
    marker = tmp_path / "ran.txt"
    code = f"open({str(marker)!r}, 'w').write('x')  # rm -rf"
    refused = await client.call_tool(
        "run", {"command": sys.executable, "args": ["-c", code]}, raise_on_error=False
    )
    assert refused.is_error and not marker.exists()
    text = text_of(refused)
    assert rules in text and "rule 1" in text and "no batch deletes" in text
    ps = await client.call_tool("run", {"command": r"Remove-Item -Recurse C:\tmp\x"}, raise_on_error=False)
    assert ps.is_error and "no batch deletes" in text_of(ps)
    ok = await client.call_tool("run", {"command": sys.executable, "args": ["-c", "print('fine')"]})
    assert "fine" in ok.structured_content["stdout"]
    calls = (await client.call_tool("history", {"limit": 3})).structured_content["calls"]
    assert [(c["tool"], c["status"]) for c in calls] == [("run", "ok"), ("run", "error"), ("run", "error")]


async def test_reading_env_files_is_refused_everywhere(client, winhand_home, tmp_path):
    write_rules(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "read"
        path = ["**/.env"]
        decision = "deny"
        """,
    )
    (tmp_path / ".env").write_text("TOKEN=1")
    (tmp_path / "README.md").write_text("hello")
    env = str(tmp_path / ".env")
    for tool in ("fs_read", "fs_send", "fs_download_url"):
        result = await client.call_tool(tool, {"path": env}, raise_on_error=False)
        assert result.is_error and "guards.toml rule 1" in text_of(result), tool
    copied = await client.call_tool(
        "fs_op", {"action": "copy", "path": env, "to": str(tmp_path / "x")}, raise_on_error=False
    )
    assert copied.is_error and not (tmp_path / "x").exists()
    readme = await client.call_tool("fs_read", {"path": str(tmp_path / "README.md")})
    assert "hello" in readme.structured_content["content"]


# ------------------------------------------------------------------ project rules (A4)


async def test_project_rules_cover_only_their_folder(client, tmp_path):
    proj, other = tmp_path / "proj", tmp_path / "other"
    write_rules(
        proj / ".winhand" / "guards.toml",
        """\
        [[guard]]
        on = "write"
        path = ["build/**"]
        decision = "deny"
        reason = "build/ is generated"
        """,
    )
    inside = await client.call_tool(
        "fs_write", {"path": str(proj / "build" / "a.txt"), "content": "x"}, raise_on_error=False
    )
    assert inside.is_error and "build/ is generated" in text_of(inside)
    assert not (proj / "build" / "a.txt").exists()
    await client.call_tool("fs_write", {"path": str(proj / "src" / "a.txt"), "content": "x"})
    await client.call_tool("fs_write", {"path": str(other / "build" / "a.txt"), "content": "x"})
    assert (other / "build" / "a.txt").exists()


async def test_a_project_cannot_loosen_the_global_rules(client, winhand_home, tmp_path):
    write_rules(winhand_home / "guards.toml", NO_BATCH_DELETE)
    write_rules(
        tmp_path / ".winhand" / "guards.toml",
        """\
        [[guard]]
        on = "command"
        decision = "note"
        reason = "project note"
        """,
    )
    refused = await client.call_tool(
        "run", {"command": "rm -rf build", "cwd": str(tmp_path)}, raise_on_error=False
    )
    assert refused.is_error
    noted = await client.call_tool(
        "run", {"command": sys.executable, "args": ["-c", "print(1)"], "cwd": str(tmp_path)}
    )
    assert "project note" in text_of(noted)


# ------------------------------------------------------------------ ask (A5)


@pytest.mark.parametrize(
    ("answer", "done", "says"),
    [
        (True, True, None),
        (False, False, "said no"),
        (secret_prompt.PromptUnavailable("nobody answered the dialog within 1s"), False, "nobody answered"),
        (
            secret_prompt.PromptUnavailable("cannot open a dialog on this machine: no display"),
            False,
            "no display",
        ),
    ],
)
async def test_ask_runs_only_after_a_yes(client, winhand_home, tmp_path, monkeypatch, answer, done, says):
    write_rules(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "delete"
        decision = "ask"
        reason = "deleting needs my OK"
        timeout_s = 1
        """,
    )
    asked = []

    def fake_confirm(title, message, timeout_s=120):
        asked.append((message, timeout_s))
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(secret_prompt, "ask_confirm", fake_confirm)
    victim = tmp_path / "old.log"
    victim.write_text("x")
    result = await client.call_tool(
        "fs_op", {"action": "delete", "path": str(victim), "permanent": True}, raise_on_error=False
    )
    assert asked and "deleting needs my OK" in asked[0][0] and str(victim) in asked[0][0]
    assert asked[0][1] == 1
    assert victim.exists() is not done
    assert result.is_error is not done
    if says:
        assert says in text_of(result)


# ------------------------------------------------------------------ note (A6), call() (A7)


async def test_note_rules_let_the_call_run_and_say_so(client, winhand_home, tmp_path):
    write_rules(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "edit"
        path = ["**/*.c"]
        decision = "note"
        reason = "run the unit tests after editing C"
        """,
    )
    src = tmp_path / "main.c"
    src.write_text("int a = 1;\n")
    result = await client.call_tool("fs_edit", {"path": str(src), "old": "a = 1", "new": "a = 2"})
    assert src.read_text() == "int a = 2;\n"
    assert "run the unit tests after editing C" in text_of(result)
    assert result.structured_content["guard_notes"] == ["run the unit tests after editing C"]


async def test_calls_through_call_are_judged_too(client, winhand_home, tmp_path):
    write_rules(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "write"
        path = ["**/locked/**"]
        decision = "deny"
        """,
    )
    target = tmp_path / "locked" / "a.txt"
    result = await client.call_tool(
        "call", {"tool": "fs_write", "arguments": {"path": str(target), "content": "x"}}, raise_on_error=False
    )
    assert result.is_error and "guard" in text_of(result) and not target.exists()


# ------------------------------------------------------------------ shell input (A8)


async def test_lines_typed_into_a_shell_session_are_judged(client, winhand_home):
    write_rules(winhand_home / "guards.toml", NO_BATCH_DELETE)
    fake = [sys.executable, "-u", FAKE_APP]
    shell = (
        await client.call_tool(
            "session_start",
            {"profile": "shell", "command": fake, "prompts": [r"^fake> ?$"], "startup_wait_s": 10},
        )
    ).structured_content["session"]["id"]
    plain = (
        await client.call_tool(
            "session_start", {"command": fake, "prompts": [r"^fake> ?$"], "startup_wait_s": 10}
        )
    ).structured_content["session"]["id"]
    try:
        refused = await client.call_tool(
            "session_send", {"id": shell, "text": "echo rm -rf /tmp/x", "submit": True}, raise_on_error=False
        )
        assert refused.is_error and "no batch deletes" in text_of(refused)
        sneaky = await client.call_tool(
            "session_send", {"id": shell, "text": "echo rm -rf /tmp/y\n"}, raise_on_error=False
        )
        assert sneaky.is_error
        ok = await client.call_tool("session_send", {"id": shell, "text": "echo safe", "submit": True})
        assert "safe" in ok.structured_content["output"]
        assert "rm -rf" not in ok.structured_content["output"]
        other = await client.call_tool(
            "session_send", {"id": plain, "text": "echo rm -rf /tmp/x", "submit": True}
        )
        assert not other.is_error and "rm -rf /tmp/x" in other.structured_content["output"]
    finally:
        for sid in (shell, plain):
            await client.call_tool("session_stop", {"id": sid, "force": True})


# ------------------------------------------------------------------ broken files (A9)


async def test_a_broken_rule_file_refuses_until_fixed(client, winhand_home):
    rules = winhand_home / "guards.toml"
    write_rules(rules, '[[guard]]\non = "command"\ndecision = "deny\n')
    echo = {"command": sys.executable, "args": ["-c", "print('hi')"]}
    refused = await client.call_tool("run", echo, raise_on_error=False)
    text = text_of(refused)
    assert refused.is_error and str(rules) in text and "line 3" in text
    time.sleep(0.01)
    write_rules(rules, '[[guard]]\non = "command"\nmatch = "never-matches-anything"\ndecision = "deny"\n')
    assert "hi" in (await client.call_tool("run", echo)).structured_content["stdout"]


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ('[[guard]]\non = "command"\ndecison = "deny"\n', "unknown key"),
        ('[[guard]]\non = "comand"\ndecision = "deny"\n', "unknown action kind"),
        ('[[guard]]\non = "command"\ndecision = "allow"\n', "decision must be one of"),
        ('[[guard]]\non = "command"\nmatch = "("\ndecision = "deny"\n', "rule 1"),
        ('[guard]\non = "command"\ndecision = "deny"\n', "[[guard]]"),
        ("rules = 1\n", "unknown top-level"),
    ],
)
def test_rule_files_are_checked_strictly(tmp_path, body, says):
    path = tmp_path / "guards.toml"
    path.write_text(body, encoding="utf-8")
    loaded = guard.load(path)
    assert loaded is not None and loaded.rules == [] and says in loaded.error


def test_no_rule_files_means_nothing_is_judged(tmp_path):
    verdict = guard.judge(actions.describe("run", {"command": "rm -rf /", "cwd": str(tmp_path)}))
    assert verdict.decision is None


def test_global_relative_patterns_match_anywhere_and_ignore_case_on_windows(tmp_path):
    rule = guard.Rule(file="g", index=1, on=("read",), decision="deny", path=("*.pem",))
    act = actions.Action("read", "fs_read", (str(tmp_path / "keys" / "id.pem"),))
    assert guard.matches(rule, act)
    if os.name == "nt":
        upper = actions.Action("read", "fs_read", (str(tmp_path / "keys" / "ID.PEM"),))
        assert guard.matches(rule, upper)


def test_the_confirm_dialog_defaults_to_no(monkeypatch, capsys):
    """The dialog pops up over whatever the person is typing into: a stray Enter must mean No."""
    import json
    import types

    seen = {}

    class Root:
        def withdraw(self):
            pass

        def attributes(self, *a):
            pass

    def askyesno(title, message, **kw):
        seen.update(title=title, message=message, **kw)
        return False

    tk = types.ModuleType("tkinter")
    tk.Tk = Root
    tk.messagebox = types.SimpleNamespace(askyesno=askyesno)
    tk.simpledialog = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.messagebox", tk.messagebox)
    monkeypatch.setitem(sys.modules, "tkinter.simpledialog", tk.simpledialog)
    payload = json.dumps({"kind": "confirm", "title": "t", "message": "删除？"}, ensure_ascii=False)
    assert secret_prompt.dialog_main(payload) == 0
    assert seen["default"] == "no" and seen["message"] == "删除？"
    assert json.loads(capsys.readouterr().out) == {"value": False}
