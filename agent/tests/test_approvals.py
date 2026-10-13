"""Per-program approval for desktop operations: approvals.py, desktop.require_operable and where
the desktop tools, the guard, the CLI and the desktop app's backend meet it. The real dialog and
real windows are exercised in test_windows_scenarios.py."""

from __future__ import annotations

import json
import sys
import threading
import time
import tomllib
import types

import pytest
from fastmcp import Client
from test_desktop_backend import spawn

from winhand import approvals, cli, desktop, ocr, secret_prompt, uia
from winhand import tools_desktop as td
from winhand.approvals import App, NotOperable
from winhand.config import Config
from winhand.secret_prompt import PromptUnavailable
from winhand.server import build_server

NOTEPAD = App("notepad.exe", "记事本", "无标题 - 记事本", 42)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test is a newly started winhand: nothing allowed or refused yet, no dialog open."""
    monkeypatch.setattr(approvals, "_session", {})
    monkeypatch.setattr(approvals, "_denied", {})
    monkeypatch.setattr(approvals, "_pending", {})
    monkeypatch.setattr(approvals, "_file", {"stamp": None, "ask": True, "always": {}, "error": None})
    monkeypatch.setattr(approvals, "_listeners", [])


class Person:
    """Answers the approval dialog; remembers what it was shown."""

    def __init__(self, *answers: str):
        self.answers = list(answers)
        self.asked: list[tuple[App, str, str | None]] = []

    def __call__(self, app: App, action: str, warning: str | None) -> str:
        self.asked.append((app, action, warning))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def restart() -> None:
    """What a new winhand process forgets: answers kept in memory, the cached file."""
    approvals.forget_session()
    approvals._file["stamp"] = None


# ------------------------------------------------------------------ the answers (A1-A4)


def test_until_restart_allows_without_asking_again_in_this_process():
    person = Person("session", "session")
    approvals.check(NOTEPAD, "点击 (812, 440)", person)
    assert [(a.exe, action) for a, action, _ in person.asked] == [("notepad.exe", "点击 (812, 440)")]
    assert approvals.status("notepad.exe") == "session"
    approvals.check(NOTEPAD, "按键 ctrl+s", person)  # no second dialog
    assert len(person.asked) == 1
    assert not approvals.path().exists()  # nothing written for "until restart"
    restart()
    approvals.check(NOTEPAD, "按键 ctrl+s", person)
    assert len(person.asked) == 2


def test_always_is_written_and_holds_after_a_restart():
    person = Person("always")
    approvals.check(NOTEPAD, "控件操作：invoke「保存」", person)
    saved = tomllib.loads(approvals.path().read_text(encoding="utf-8"))
    assert saved["ask"] is True and "notepad.exe" in saved["always"]
    restart()
    approvals.check(NOTEPAD, "关闭窗口", person)  # Person has no answer left: asking would fail the test
    assert approvals.status("NOTEPAD.EXE") == "always"


def test_a_refusal_holds_for_ten_minutes_without_asking(monkeypatch):
    person = Person("deny", "session")
    with pytest.raises(NotOperable, match="refused to let the AI operate 记事本 \\(notepad.exe\\)"):
        approvals.check(NOTEPAD, "点击 (1, 2)", person)
    with pytest.raises(NotOperable, match="a short while ago.*10 min"):
        approvals.check(NOTEPAD, "点击 (1, 2)", person)
    assert len(person.asked) == 1 and approvals.status("notepad.exe") == "denied"
    later = time.monotonic() + approvals.DENIED_FOR_S + 1
    monkeypatch.setattr(approvals.time, "monotonic", lambda: later)
    approvals.check(NOTEPAD, "点击 (1, 2)", person)
    assert len(person.asked) == 2


def test_without_an_answer_the_call_returns_and_the_dialog_stays(monkeypatch):
    monkeypatch.setattr(approvals, "WAIT_S", 0.3)
    answered = threading.Event()
    asked = []

    def slow_person(app, action, warning):
        asked.append(action)
        answered.wait(10)
        return "session"

    errors = []

    def call():
        try:
            approvals.check(NOTEPAD, "点击 (1, 2)", slow_person)
        except NotOperable as exc:
            errors.append(str(exc))

    callers = [threading.Thread(target=call) for _ in range(2)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(5)
    assert len(errors) == 2 and all("Waiting for the person" in e and "Nothing was sent" in e for e in errors)
    assert asked == ["点击 (1, 2)"]  # one dialog for both calls
    answered.set()
    for _ in range(100):
        if not approvals._pending:
            break
        time.sleep(0.02)
    approvals.check(NOTEPAD, "点击 (1, 2)", Person())  # the late answer counts; no new dialog
    assert approvals.status("notepad.exe") == "session"


def test_one_call_waits_once_however_many_programs_it_touches(monkeypatch):
    monkeypatch.setattr(approvals, "WAIT_S", 0.3)
    answered = threading.Event()

    def slow_person(app, action, warning):
        answered.wait(10)
        return "deny"

    began = time.monotonic()
    with approvals.one_call():
        for exe in ("a.exe", "b.exe"):  # a drag from one program into another
            with pytest.raises(NotOperable, match="Waiting"):
                approvals.check(App(exe), "拖动", slow_person)
    assert time.monotonic() - began < 0.55  # not 0.3 s for each
    answered.set()


def test_an_unanswered_dialog_refuses_this_time_only():
    person = Person(PromptUnavailable("nobody answered the dialog within 120s"), "session")
    with pytest.raises(NotOperable, match="Nobody allowed.*nobody answered"):
        approvals.check(NOTEPAD, "点击 (1, 2)", person)
    assert approvals.status("notepad.exe") == "ask"
    approvals.check(NOTEPAD, "点击 (1, 2)", person)
    assert len(person.asked) == 2


# ------------------------------------------------------------------ terminals, cautions (A7, A11)


def test_terminals_are_refused_without_asking_until_always_allowed():
    person = Person("session")
    console = App("cmd.exe", "Windows 命令处理程序", "C:\\WINDOWS\\system32\\cmd.exe", 7, console=True)
    with pytest.raises(NotOperable, match="console window.*Use run or a session"):
        approvals.check(console, "按键 enter", person)
    with pytest.raises(NotOperable, match="a terminal.*winhand apps allow windowsterminal.exe"):
        approvals.check(App("WindowsTerminal.exe"), "点击 (1, 2)", person)
    with pytest.raises(NotOperable, match="console window"):  # python in a console is a terminal too
        approvals.check(App("python.exe", console=True), "输入文字（3 个字）", person)
    assert person.asked == []
    assert approvals.status("cmd.exe", console=True) == "terminal"
    # a PowerShell script's window (a form, not a console) is asked about, with a caution
    approvals.check(App("powershell.exe", title="winhand-ui-test"), "点击 (1, 2)", person)
    assert "终端窗口" in person.asked[0][2]
    approvals.allow_always("cmd.exe")
    approvals.check(console, "按键 enter", person)


def test_allowing_a_programs_window_until_restart_does_not_open_its_consoles():
    approvals.check(App("python.exe", title="tk"), "点击 (1, 2)", Person("session"))
    assert approvals.status("python.exe") == "session"
    with pytest.raises(NotOperable, match="console window"):  # a REPL in a console stays a terminal
        approvals.check(App("python.exe", console=True), "按键 enter", Person())
    assert "终端窗口" in approvals.caution("python.exe")


@pytest.mark.parametrize(
    "exe,word",
    [("msedge.exe", "浏览器"), ("KeePass.exe", "密码"), ("Weixin.exe", "聊天"), ("explorer.exe", "系统工具")],
)
def test_risky_programs_come_with_a_caution(exe, word):
    person = Person("session")
    approvals.check(App(exe), "点击 (1, 2)", person)
    assert word in person.asked[0][2]


def test_ordinary_programs_come_without_a_caution():
    person = Person("session")
    approvals.check(NOTEPAD, "点击 (1, 2)", person)
    assert person.asked[0][2] is None and approvals.caution("notepad.exe") is None


# ------------------------------------------------------------------ the file


def test_names_are_file_names_in_lower_case():
    assert approvals.normalize("Notepad") == "notepad.exe"
    assert approvals.normalize(r"C:\Windows\System32\NOTEPAD.EXE") == "notepad.exe"
    assert approvals.normalize("/opt/x/Code.exe ") == "code.exe"
    with pytest.raises(ValueError):
        approvals.normalize("  ")


def test_a_broken_file_asks_about_everything():
    approvals.path().parent.mkdir(parents=True, exist_ok=True)
    approvals.path().write_text('ask = false\n[always]\n"notepad.exe" = \n', encoding="utf-8")
    assert approvals.enabled() is True and approvals.status("notepad.exe") == "ask"
    assert "desktop-apps.toml" in approvals.snapshot()["error"]
    approvals.path().write_text('ask = "no"\n', encoding="utf-8")
    assert approvals.enabled() is True and "true or false" in approvals.file_error()
    # what the person wrote there is not overwritten; "always" in the dialog allows until restart
    with pytest.raises(approvals.FileBroken):
        approvals.allow_always("notepad.exe")
    with pytest.raises(approvals.FileBroken):
        approvals.set_enabled(False)
    approvals.check(NOTEPAD, "点击 (1, 2)", Person("always"))
    assert approvals.status("notepad.exe") == "session"
    assert approvals.path().read_text(encoding="utf-8") == 'ask = "no"\n'


def test_the_switch_lives_in_the_file_next_to_the_programs():
    approvals.allow_always("notepad.exe")
    approvals.set_enabled(False)
    saved = tomllib.loads(approvals.path().read_text(encoding="utf-8"))
    assert saved == {"ask": False, "always": {"notepad.exe": saved["always"]["notepad.exe"]}}
    assert approvals.enabled() is False and approvals.snapshot()["enabled"] is False
    approvals.set_enabled(True)
    assert approvals.enabled() is True and approvals.status("notepad.exe") == "always"


def test_revoke_takes_back_both_kinds_and_listeners_hear_of_it():
    heard = []
    approvals.subscribe(lambda: heard.append(approvals.snapshot()))
    approvals.allow_always("notepad.exe")
    approvals.allow_session("notepad.exe")
    approvals.allow_session("mspaint.exe")
    approvals.revoke("Notepad")
    state = approvals.snapshot()
    assert state["always"] == [] and [s["exe"] for s in state["session"]] == ["mspaint.exe"]
    assert len(heard) == 4 and approvals.status("notepad.exe") == "ask"


# ------------------------------------------------------------------ who may be operated (R1, R6)


def _window(monkeypatch, process: str, pid: int = 77, title: str = "x", admin: bool = False):
    monkeypatch.setattr(desktop, "_root", lambda hwnd: 5)
    monkeypatch.setattr(
        desktop, "_window_facts", lambda hwnd: {"hwnd": 5, "title": title, "process": process, "pid": pid}
    )
    monkeypatch.setattr(desktop, "is_elevated", lambda pid: admin)
    monkeypatch.setattr(desktop, "app_of", lambda hwnd: App(process.lower(), title=title, pid=pid))
    checked = []
    monkeypatch.setattr(approvals, "check", lambda app, what, ask=None: checked.append((app.exe, what)))
    return checked


def test_winhands_own_windows_are_never_operated_even_with_asking_off(monkeypatch):
    for process in ("winhand.exe", "WinHandDesktop.exe"):
        checked = _window(monkeypatch, process, title="WinHand：AI 想操作一个程序")
        approvals.set_enabled(False)
        with pytest.raises(NotOperable, match="WinHand's own"):
            desktop.require_operable(5, "按键 enter")
        assert checked == []


def test_an_administrator_window_is_refused_before_anyone_is_asked(monkeypatch):
    checked = _window(monkeypatch, "Setup.exe", admin=True)
    with pytest.raises(desktop.DesktopError, match="runs as administrator"):
        desktop.require_operable(5, "点击 (1, 2)")
    assert checked == []


def test_other_programs_go_through_approval_unless_it_is_off(monkeypatch):
    checked = _window(monkeypatch, "Notepad.exe")
    desktop.require_operable(5, "点击 (1, 2)")
    assert checked == [("notepad.exe", "点击 (1, 2)")]
    approvals.set_enabled(False)
    desktop.require_operable(5, "点击 (1, 2)")
    assert len(checked) == 1


def test_a_locked_screen_is_reported_instead_of_a_dialog_nobody_can_see(monkeypatch):
    _window(monkeypatch, "notepad.exe")
    monkeypatch.setattr(desktop, "desktop_state", lambda: "locked")
    monkeypatch.setattr(approvals, "check", lambda *a, **k: pytest.fail("a dialog was shown"))
    with pytest.raises(desktop.DesktopUnavailable):
        desktop.require_operable(5, "点击 (1, 2)")
    approvals.allow_always("notepad.exe")
    checked = _window(monkeypatch, "notepad.exe")  # allowed: no dialog needed, the tool says the rest
    desktop.require_operable(5, "点击 (1, 2)")
    assert checked == [("notepad.exe", "点击 (1, 2)")]


def test_winhand_dialogs_run_from_source_are_its_own(monkeypatch):
    import os

    import psutil

    lines = {
        101: [sys.executable, "-m", "winhand.cli", "secret-dialog", '{"kind": "approve"}'],
        102: [sys.executable, "-m", "http.server"],
    }

    class Process:
        def __init__(self, pid):
            self.pid = pid

        def cmdline(self):
            return lines[self.pid]

    monkeypatch.setattr(psutil, "Process", Process)
    assert desktop.is_own(os.getpid(), "whatever.exe")
    assert desktop.is_own(9, "WINHAND.EXE") and desktop.is_own(9, "winhanddesktop.exe")
    assert desktop.is_own(101, "python.exe") and not desktop.is_own(102, "python.exe")
    assert desktop.is_own(101, "python3.12.exe")  # the Microsoft Store's Python
    assert not desktop.is_own(103, "notepad.exe") and not desktop.is_own(0, "winhand.exe")


def test_each_kind_of_input_is_checked_against_the_right_window(monkeypatch):
    seen = []
    monkeypatch.setattr(desktop, "check_window_at", lambda x, y, what="": seen.append(("at", x, y, what)))
    monkeypatch.setattr(desktop, "check_foreground", lambda what="": seen.append(("foreground", what)))
    monkeypatch.setattr(desktop, "check_shell", lambda what: seen.append(("shell", what)))
    desktop._check_target("click", (5, 6), None)
    desktop._check_target("drag", (5, 6), (7, 8))
    desktop._check_target("keys", None, None, keys="ctrl+s")
    desktop._check_target("keys", None, None, keys="win+r")
    desktop._check_target("keys", None, None, keys="CMD+e enter")
    desktop._check_target("type", None, None, text="你好")
    desktop._check_target("type", (1, 2), None, text="abc")
    desktop._check_target("move", (1, 2), None)
    assert seen == [
        ("at", 5, 6, "点击 (5, 6)"),
        ("at", 5, 6, "拖动 (5, 6)"),
        ("at", 7, 8, "拖动到 (7, 8)"),
        ("foreground", "按键 ctrl+s"),
        ("shell", "按键 win+r"),  # the Start menu and Run belong to explorer.exe, not the window in front
        ("foreground", "按键 win+r"),
        ("shell", "按键 CMD+e enter"),
        ("foreground", "按键 CMD+e enter"),
        ("foreground", "输入文字（2 个字）"),
        ("at", 1, 2, "点击 (1, 2) 后输入文字（3 个字）"),
    ]


def test_nothing_is_sent_to_a_program_that_may_not_be_operated(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(desktop, "to_screen", lambda x, y, coordinates: (int(x), int(y)))
    monkeypatch.setattr(desktop, "_send", lambda *events: pytest.fail("input was sent"))

    def refuse(*a, **k):
        raise NotOperable("The person refused")

    monkeypatch.setattr(desktop, "check_window_at", refuse)
    monkeypatch.setattr(desktop, "check_foreground", refuse)
    with pytest.raises(NotOperable):
        desktop.input_action("click", 10, 10)
    with pytest.raises(NotOperable):
        desktop.input_action("keys", keys="ctrl+s")


def test_window_actions_ask_first_and_listing_does_not(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 5, "title": "x", "process": "x.exe"})
    monkeypatch.setattr(desktop, "_force_foreground", lambda hwnd: pytest.fail("the window was focused"))
    asked = []

    def refuse(hwnd, what):
        asked.append(what)
        raise NotOperable("The person refused")

    monkeypatch.setattr(desktop, "require_operable", refuse)
    for action in ("focus", "close", "minimize", "move"):
        with pytest.raises(NotOperable):
            desktop.window_action("x", action, left=1, top=1)
    assert asked == ["切到前台", "关闭窗口", "最小化窗口", "移动或缩放窗口"]


def test_the_window_list_says_what_operating_each_window_needs(monkeypatch):
    monkeypatch.setattr(desktop, "is_own", lambda pid, process: process == "winhand.exe")
    approvals.allow_always("notepad.exe")
    assert desktop._approval_of(1, 2, "winhand.exe", "Tk") == "winhand"
    assert desktop._approval_of(1, 2, "notepad.exe", "Notepad") == "always"
    assert desktop._approval_of(1, 2, "cmd.exe", "ConsoleWindowClass") == "terminal"
    assert desktop._approval_of(1, 2, "powershell.exe", "WindowsForms10.Window.8.app") == "ask"
    approvals.set_enabled(False)
    assert desktop._approval_of(1, 2, "notepad.exe", "Notepad") is None
    assert desktop._approval_of(1, 2, "winhand.exe", "Tk") == "winhand"


# ------------------------------------------------------------------ the tools (A5)


def _refusing(monkeypatch):
    asked = []

    def refuse(hwnd, what):
        asked.append(what)
        raise NotOperable("The person refused to let the AI operate app.exe. Nothing was sent.")

    monkeypatch.setattr(desktop, "require_operable", refuse)
    return asked


async def test_ui_reads_without_asking_and_operates_only_when_allowed(monkeypatch):
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 11, "title": "app", "process": "app.exe"})
    monkeypatch.setattr(desktop, "native_controls", lambda hwnd: [])
    button = {"index": 0, "type": "Button", "name": "保存", "automation_id": "", "hwnd": 0, "enabled": True,
              "rect": [1, 2, 3, 4], "patterns": ["Invoke"], "_runtime_id": [1], "_element": object()}  # fmt: skip
    monkeypatch.setattr(
        uia, "snapshot", lambda hwnd, timeout=0: {"window": "app", "count": 1, "elements": [button]}
    )
    monkeypatch.setattr(uia, "act", lambda *a, **k: pytest.fail("the control was operated"))
    asked = _refusing(monkeypatch)
    async with Client(build_server(Config())) as client:
        listed = (await client.call_tool("ui", {"window": "app"})).structured_content
        refused = (
            await client.call_tool("ui", {"window": "app", "action": "invoke", "name": "保存"})
        ).structured_content
    assert listed["elements"][0]["name"] == "保存"
    assert "refused" in refused["error"] and asked == ["控件操作：invoke「保存」"]


def test_ocr_reads_without_asking_and_clicks_only_when_allowed(monkeypatch):
    monkeypatch.setattr(
        ocr, "find", lambda *a, **k: {"matches": [{"text": "OK", "center": [5, 5]}], "window": "app"}
    )
    monkeypatch.setattr(desktop, "find_window", lambda _: {"hwnd": 11, "title": "app", "process": "a.exe"})
    monkeypatch.setattr(desktop, "window_action", lambda *a, **k: pytest.fail("the window was focused"))
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: pytest.fail("the mouse moved"))
    asked = _refusing(monkeypatch)
    with pytest.raises(NotOperable):
        ocr.act("click", "OK", window="app")
    assert asked == ["按文字点击「OK」"]
    moved = []
    monkeypatch.setattr(desktop, "input_action", lambda *a, **k: moved.append(a) or {})
    monkeypatch.setattr(desktop, "root_window_at", lambda x, y: 11)
    ocr.act("move", "OK")  # moving the pointer over it is not operating anything
    ocr.act("move", "OK", window="app")  # nor is it in a given window (not even focused for it)
    assert len(moved) == 2 and asked == ["按文字点击「OK」"]


async def test_reading_tools_never_ask(monkeypatch):
    _refusing(monkeypatch)
    monkeypatch.setattr(td.desktop, "monitors", lambda: [])
    monkeypatch.setattr(td.desktop, "windows", lambda: [{"hwnd": 1, "title": "app", "approval": "ask"}])
    monkeypatch.setattr(td.desktop, "desktop_state", lambda: "ok")
    async with Client(build_server(Config())) as client:
        listed = (await client.call_tool("window", {})).structured_content
    assert listed["windows"][0]["approval"] == "ask"


# ------------------------------------------------------------------ guard, CLI, dialog, app (A9, A10)


async def test_file_tools_cannot_change_who_may_be_operated(tmp_path):
    backup = tmp_path / "backup" / approvals.path().parent.name
    backup.mkdir(parents=True)
    (backup / approvals.FILE_NAME).write_text('ask = false\n[always]\n"cmd.exe" = "x"\n', encoding="utf-8")
    approvals.allow_always("notepad.exe")
    before = approvals.path().read_text(encoding="utf-8")
    target = str(approvals.path())
    async with Client(build_server(Config())) as client:
        calls = [
            ("fs_write", {"path": target, "content": "ask = false\n"}),
            ("fs_edit", {"path": target, "old": "ask = true", "new": "ask = false"}),
            ("fs_op", {"action": "delete", "path": target}),
            ("fs_op", {"action": "move", "path": target, "to": target + ".bak"}),
            ("fs_op", {"action": "copy", "path": target + ".bak", "to": target, "overwrite": True}),
            ("fs_op", {"action": "move", "path": str(approvals.path().parent), "to": target + ".dir"}),
            ("fs_op", {"action": "delete", "path": str(approvals.path().parent), "recursive": True}),
            # a folder copied over the one holding it ("restore my settings from a backup")
            (
                "fs_op",
                {
                    "action": "copy",
                    "path": str(backup),
                    "to": str(approvals.path().parent.parent),
                    "overwrite": True,
                },
            ),
            ("fs_write", {"path": str(approvals.path().parent / "." / "desktop-apps.toml"), "content": ""}),
        ]
        for tool, arguments in calls:
            result = await client.call_tool(tool, arguments, raise_on_error=False)
            assert result.is_error and "only the person at the machine" in result.content[0].text, tool
        elsewhere = await client.call_tool(
            "fs_write", {"path": str(approvals.path().parent / "notes.txt"), "content": "hi"}
        )
    assert not elsewhere.is_error
    assert approvals.path().read_text(encoding="utf-8") == before


def test_cli_allow_and_off_need_the_persons_yes(monkeypatch, capsys):
    answers = []
    monkeypatch.setattr(secret_prompt, "ask_confirm", lambda title, message, timeout_s=120: answers.pop(0))
    answers.append(False)
    assert cli.main(["apps", "allow", "Notepad"]) == 1
    assert approvals.status("notepad.exe") == "ask"
    answers.append(True)
    assert cli.main(["apps", "allow", "Notepad"]) == 0
    assert approvals.status("notepad.exe") == "always"
    assert cli.main(["apps", "revoke", "notepad.exe"]) == 0  # taking back needs no dialog
    assert approvals.status("notepad.exe") == "ask"
    answers.append(False)
    assert cli.main(["apps", "off"]) == 1 and approvals.enabled() is True
    answers.append(True)
    assert cli.main(["apps", "off"]) == 0 and approvals.enabled() is False
    assert cli.main(["apps", "on"]) == 0 and approvals.enabled() is True
    assert cli.main(["apps"]) == 0
    out = capsys.readouterr().out
    assert "程序授权：开启" in out and "desktop-apps.toml" in out and "windowsterminal.exe" in out


def test_the_dialog_starts_on_refuse(monkeypatch, capsys):
    widgets = []

    class Widget:
        def __init__(self, parent=None, **options):
            self.options = options
            self.focused = False
            widgets.append(self)

        def pack(self, **k):
            pass

        def bind(self, *a):
            pass

        def focus_set(self):
            self.focused = True

        def invoke(self):
            self.options["command"]()

    class Root(Widget):
        def withdraw(self):
            pass

        def attributes(self, *a):
            pass

        def title(self, text):
            self.options["title"] = text

        def resizable(self, *a):
            pass

        def protocol(self, *a):
            pass

        def deiconify(self):
            pass

        def lift(self):
            pass

        def destroy(self):
            pass

        def mainloop(self):
            if pressing["text"]:  # the person clicks that button; else it closes unanswered
                next(w for w in widgets if w.options.get("text") == pressing["text"]).invoke()

    tk = types.ModuleType("tkinter")
    tk.Tk, tk.Frame, tk.Label, tk.Button = Root, Widget, Widget, Widget
    tk.simpledialog = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    monkeypatch.setitem(sys.modules, "tkinter.simpledialog", tk.simpledialog)
    payload = json.dumps(
        {"kind": "approve", "name": "Microsoft Edge", "exe": "msedge.exe", "title": "新标签页",
         "action": "点击 (1, 2)", "warning": "这是浏览器"}, ensure_ascii=False,
    )  # fmt: skip
    pressing: dict = {}
    for press, answer in ((None, "deny"), ("允许到 WinHand 重启", "session"), ("一直允许", "always")):
        pressing["text"] = press
        widgets.clear()
        assert secret_prompt.dialog_main(payload) == 0
        assert json.loads(capsys.readouterr().out) == {"value": answer}
        buttons = [w for w in widgets if "command" in w.options]
        assert [b.options["text"] for b in buttons] == ["拒绝", "允许到 WinHand 重启", "一直允许"]
        assert buttons[0].focused and not any(b.focused for b in buttons[1:])
        text = " ".join(str(w.options.get("text", "")) for w in widgets)
        assert "Microsoft Edge（msedge.exe）" in text and "点击 (1, 2)" in text and "这是浏览器" in text


def test_ask_approval_sends_the_question_and_reads_the_answer(monkeypatch):
    sent = []
    replies = ["always", "something else"]
    monkeypatch.setattr(
        secret_prompt, "_run_dialog", lambda payload, timeout_s: sent.append(payload) or replies.pop(0)
    )
    assert secret_prompt.ask_approval("记事本", "notepad.exe", "无标题", "点击 (1, 2)") == "always"
    assert secret_prompt.ask_approval("记事本", "notepad.exe", "无标题", "点击 (1, 2)") == "deny"
    request = json.loads(sent[0])
    assert (
        request["kind"] == "approve"
        and request["exe"] == "notepad.exe"
        and request["action"] == "点击 (1, 2)"
    )


async def test_desktop_app_manages_approvals(tmp_path):
    backend = await spawn(tmp_path)
    try:
        init = (await backend.call("initialize", protocol_version=1))["result"]
        assert init["apps"]["enabled"] is True and init["apps"]["always"] == []
        allowed = (await backend.call("apps_allow", exe="Notepad"))["result"]
        assert [a["exe"] for a in allowed["always"]] == ["notepad.exe"]
        assert allowed["added"] == "notepad.exe" and "caution" not in allowed
        terminal = (await backend.call("apps_allow", exe="WindowsTerminal"))["result"]
        assert terminal["added"] == "windowsterminal.exe" and "不经过守卫规则" in terminal["caution"]
        await backend.call("apps_revoke", exe="windowsterminal.exe")
        event = await backend.event("apps", lambda d: [a["exe"] for a in d["always"]] == ["notepad.exe"])
        assert event["file"].endswith("desktop-apps.toml")
        off = (await backend.call("apps_set_enabled", enabled=False))["result"]
        assert off["enabled"] is False
        saved = tomllib.loads((tmp_path / "desktop-apps.toml").read_text(encoding="utf-8"))
        assert saved["ask"] is False and "notepad.exe" in saved["always"]
        revoked = (await backend.call("apps_revoke", exe="notepad.exe"))["result"]
        assert revoked["always"] == [] and revoked["session"] == []
        bad = await backend.call("apps_allow", exe=" ")
        assert bad["error"]["code"] == "invalid_exe"
        got = (await backend.call("apps_get"))["result"]
        assert got["enabled"] is False and "running" in got and got["terminals"]
        state = (await backend.call("get_state"))["result"]
        titles = [a["title"] for a in state["activity"] if a["kind"] == "settings"]
        assert "在应用中允许 AI 一直操作 notepad.exe" in titles and "程序授权：已关闭" in titles
        assert (await backend.call("shutdown"))["result"]["ok"] is True
    finally:
        if backend.proc.returncode is None:
            backend.proc.kill()
