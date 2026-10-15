"""Per-program approval for desktop operations.

Before the AI clicks, types into or operates a program for the first time, the person at the
machine says so in a dialog: allow until winhand restarts, always allow, or refuse. "Always", and
the switch that turns asking off, go to ~/.winhand/desktop-apps.toml (written only by that dialog,
the desktop app's settings and `winhand apps`; the file tools may not touch it, see guard.py);
"until restart" lives in this process. Terminals are refused without asking:
typing into one would get around the guard rules, which judge commands run with `run` and
sessions. A terminal is any console window (whatever program it shows) and the terminal
programs below. Reading (screenshots, ocr, ui inspect) is never asked about.

This keeps an AI from operating programs the person did not hand over, by mistake or by
overreach. It is not a security boundary: whatever can run commands can get around it.
"""

from __future__ import annotations

import os
import re
import threading
import time
import tomllib
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import config
from .desktop import DesktopError

FILE_NAME = "desktop-apps.toml"
WAIT_S = 40.0  # a call answers within the client's ~60 s limit; the dialog stays open longer
DIALOG_S = 120.0
DENIED_FOR_S = 600.0

CONSOLE_CLASSES = frozenset({"ConsoleWindowClass", "PseudoConsoleWindow"})  # cmd, PowerShell, python ...
TERMINALS = frozenset(
    {
        "windowsterminal.exe", "wt.exe", "openconsole.exe", "conhost.exe", "powershell_ise.exe",
        "mintty.exe", "wslhost.exe", "putty.exe", "kitty.exe", "alacritty.exe", "wezterm-gui.exe",
        "tabby.exe", "conemu.exe", "conemu64.exe", "hyper.exe", "mobaxterm.exe", "xshell.exe",
        "windterm.exe", "termius.exe",
    }
)  # fmt: skip
CAUTIONS: dict[str, tuple[frozenset[str], str]] = {
    "shell": (
        frozenset(
            {
                "cmd.exe",
                "powershell.exe",
                "pwsh.exe",
                "bash.exe",
                "sh.exe",
                "wsl.exe",
                "python.exe",
                "py.exe",
                "node.exe",
            }
        ),  # fmt: skip
        "这是命令行程序：选“一直允许”后，AI 也能在它的终端窗口里输入并执行命令，不经过守卫规则。",
    ),
    "browser": (
        frozenset(
            {
                "msedge.exe",
                "chrome.exe",
                "firefox.exe",
                "brave.exe",
                "opera.exe",
                "vivaldi.exe",
                "360se.exe",
                "360chrome.exe",
                "qqbrowser.exe",
                "sogouexplorer.exe",
                "iexplore.exe",
                "arc.exe",
            }
        ),  # fmt: skip
        "这是浏览器：AI 能以你已登录的身份操作网站。",
    ),
    "passwords": (
        frozenset(
            {
                "1password.exe",
                "keepass.exe",
                "keepassxc.exe",
                "bitwarden.exe",
                "dashlane.exe",
                "enpass.exe",
                "roboform.exe",
                "nordpass.exe",
                "lastpass.exe",
            }
        ),  # fmt: skip
        "这是密码管理器：AI 能看到并使用你保存的密码。",
    ),
    "chat": (
        frozenset(
            {
                "weixin.exe",
                "wechat.exe",
                "wxwork.exe",
                "qq.exe",
                "tim.exe",
                "dingtalk.exe",
                "feishu.exe",
                "lark.exe",
                "telegram.exe",
                "slack.exe",
                "teams.exe",
                "ms-teams.exe",
                "discord.exe",
                "whatsapp.exe",
                "signal.exe",
            }
        ),  # fmt: skip
        "这是聊天软件：AI 能以你的名义发消息。",
    ),
    "system": (
        frozenset(
            {"systemsettings.exe", "control.exe", "mmc.exe", "regedit.exe", "taskmgr.exe", "explorer.exe"}
        ),  # fmt: skip
        "这是系统工具：AI 能更改系统设置、移动或删除文件，不经过守卫规则。",
    ),
}


class NotOperable(DesktopError):
    """The AI may not operate this program now; nothing was sent."""


class FileBroken(ValueError):
    """The approvals file cannot be read; it is not rewritten until the person fixes it."""


@dataclass(frozen=True)
class App:
    exe: str  # lower-case file name: what approvals are keyed by
    name: str = ""  # what a person calls it (the file description), for dialogs and settings
    title: str = ""  # the window's title
    pid: int = 0
    console: bool = False  # a console window: a terminal, whatever program it belongs to

    @property
    def label(self) -> str:
        return f"{self.name} ({self.exe})" if self.name and self.name.lower() != self.exe else self.exe


def path() -> Path:
    return config.home() / FILE_NAME


_lock = threading.Lock()
_session: dict[str, float] = {}  # exe -> when allowed (time.time) until this process ends
_denied: dict[str, float] = {}  # exe -> when refused (time.monotonic)
_pending: dict[str, Future] = {}  # exe -> the open dialog's answer
_opening: set[str] = set()  # exes whose dialog has started but is not on the screen yet
_file: dict = {"stamp": None, "ask": True, "always": {}, "error": None}
_listeners: list[Callable[[], None]] = []


def _stamp(file: Path):
    try:
        info = file.stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


def _read() -> None:
    file = path()
    stamp = (str(file), _stamp(file))
    if stamp == _file["stamp"]:
        return
    ask, always, error = True, {}, None
    if stamp[1] is not None:
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
            table = data.get("always", {})
            if not isinstance(table, dict):
                raise ValueError("[always] is not a table")
            if not isinstance(data.get("ask", True), bool):
                raise ValueError("ask must be true or false")
            ask = data.get("ask", True)
            always = {str(k).lower(): str(v) for k, v in table.items()}
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            ask, always, error = True, {}, f"{file}: {exc}"  # broken: ask about everything
    _file.update(stamp=stamp, ask=ask, always=always, error=error)


def enabled() -> bool:
    """Whether the person is asked before the AI operates a program (`ask` in the file; on unless
    set to false)."""
    with _lock:
        _read()
        return _file["ask"]


def always() -> dict[str, str]:
    """exe -> when it was allowed, from the approvals file (empty when it is missing or broken)."""
    with _lock:
        _read()
        return dict(_file["always"])


def file_error() -> str | None:
    with _lock:
        _read()
        return _file["error"]


def _toml_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write(entries: dict[str, str], ask: bool) -> None:
    file = path()
    if _file["error"]:  # rewriting it would throw away what the person wrote there
        raise FileBroken(f"fix or remove the approvals file first: {_file['error']}")
    file.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 程序授权：AI 用桌面工具（点击、输入、控件操作）操作一个程序前，先在电脑上问你。",
        "# 由 WinHand 的授权弹窗、桌面端设置页（程序授权）或 `winhand apps` 写入；AI 的文件工具改不了它。",
        "",
        "# false：不再问（WinHand 自己的窗口和管理员窗口仍然不能操作）",
        f"ask = {'true' if ask else 'false'}",
        "",
        "# 一直允许的程序：exe 文件名 = 加入的时间",
        "[always]",
    ]
    lines += [f"{_toml_string(exe)} = {_toml_string(since)}" for exe, since in sorted(entries.items())]
    temp = file.with_name(f"{file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(temp, file)
    _file["stamp"] = None  # read back on the next look


def _changed() -> None:
    for listener in list(_listeners):
        try:
            listener()
        except Exception:  # noqa: BLE001 - a listener's failure is not the caller's
            pass


def subscribe(listener: Callable[[], None]) -> None:
    """Call `listener` whenever approvals change (the desktop app shows them live)."""
    _listeners.append(listener)


def unsubscribe(listener: Callable[[], None]) -> None:
    if listener in _listeners:
        _listeners.remove(listener)


def normalize(exe: str) -> str:
    """The key approvals use: the file name, lower case, with .exe ('Notepad' -> 'notepad.exe')."""
    key = re.split(r"[\\/]", exe.strip())[-1].strip().lower()
    if not key:
        raise ValueError("give the program's file name, e.g. notepad.exe")
    return key if "." in key else key + ".exe"


def allow_always(exe: str) -> str:
    key = normalize(exe)
    with _lock:
        _read()
        entries = dict(_file["always"])
        entries[key] = datetime.now().isoformat(timespec="seconds")
        _write(entries, _file["ask"])
        _denied.pop(key, None)
    _changed()
    return key


def allow_session(exe: str) -> str:
    key = normalize(exe)
    with _lock:
        _session[key] = time.time()
        _denied.pop(key, None)
    _changed()
    return key


def revoke(exe: str) -> str:
    key = normalize(exe)
    with _lock:
        _read()
        _session.pop(key, None)
        broken = _file["error"]
        if not broken and key in _file["always"]:
            entries = dict(_file["always"])
            del entries[key]
            _write(entries, _file["ask"])
    _changed()
    if broken:  # whatever the file says about it cannot be read, so it cannot be taken out either
        raise FileBroken(f"fix or remove the approvals file first: {broken}")
    return key


def set_enabled(on: bool) -> None:
    """Turn asking on or off (the person's choice: settings page or `winhand apps on|off`)."""
    with _lock:
        _read()
        _write(dict(_file["always"]), bool(on))
    _changed()


def deny(exe: str) -> None:
    with _lock:
        _denied[normalize(exe)] = time.monotonic()
    _changed()


def forget_session() -> None:
    """Drop what was allowed until restart and what was refused (as a restart would)."""
    with _lock:
        _session.clear()
        _denied.clear()
    _changed()


def _denied_left(key: str) -> float:
    when = _denied.get(key)
    if when is None:
        return 0.0
    left = DENIED_FOR_S - (time.monotonic() - when)
    if left <= 0:
        _denied.pop(key, None)
        return 0.0
    return left


def is_terminal(exe: str, console: bool = False) -> bool:
    return console or normalize(exe) in TERMINALS


def _status(key: str, console: bool) -> str:
    _read()
    if key in _file["always"]:
        return "always"
    if is_terminal(
        key, console
    ):  # only "always" opens a terminal, not an answer about the program's other windows
        return "terminal"
    if key in _session:
        return "session"
    return "denied" if _denied_left(key) else "ask"


def status(exe: str, console: bool = False) -> str:
    """always | session | denied | terminal | ask (`console`: the window is a console window)"""
    key = normalize(exe)
    with _lock:
        return _status(key, console)


_call = threading.local()


@contextmanager
def one_call() -> Iterator[None]:
    """The approvals one tool call waits for (a drag across two programs, Win+key and the window in
    front) share one WAIT_S, so the call still answers within the client's time limit."""
    outer = getattr(_call, "deadline", None)
    if outer is None:
        _call.deadline = time.monotonic() + WAIT_S
    try:
        yield
    finally:
        if outer is None:
            _call.deadline = None


def caution(exe: str) -> str | None:
    key = normalize(exe)
    return next((text for names, text in CAUTIONS.values() if key in names), None)


def snapshot() -> dict:
    with _lock:
        _read()
        session = [{"exe": exe, "since": since} for exe, since in sorted(_session.items())]
        denied = [{"exe": exe, "left_s": round(_denied_left(exe))} for exe in sorted(_denied)]
        denied = [d for d in denied if d["left_s"] > 0]
        out = {
            "enabled": _file["ask"],
            "file": str(path()),
            "always": [{"exe": exe, "since": since} for exe, since in sorted(_file["always"].items())],
            "session": session,
            "denied": denied,
            "terminals": sorted(TERMINALS),
        }
        if _file["error"]:
            out["error"] = _file["error"]
    return out


# ------------------------------------------------------------------ asking


def _ask_person(app: App, action: str, warning: str | None) -> str:
    from .secret_prompt import ask_approval

    def short(text: str, limit: int) -> str:  # titles and control names come from the program
        text = " ".join(text.split())  # one line each: line breaks cannot stretch the dialog
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def shown() -> None:
        with _lock:
            _opening.discard(app.exe)

    name, title = short(app.name or app.exe, 80), short(app.title, 120)
    with _lock:
        _opening.add(app.exe)
    try:
        return ask_approval(
            name, app.exe, title, short(action, 160), warning, timeout_s=DIALOG_S, on_shown=shown
        )
    finally:
        shown()


def _dialog(app: App, action: str, future: Future, ask) -> None:
    from .secret_prompt import DialogNotShown

    try:
        answer = ask(app, action, caution(app.exe))
    except DialogNotShown as exc:
        answer = f"notshown: {exc}"
    except Exception as exc:  # noqa: BLE001 - PromptUnavailable (no answer, no desktop) and the like
        answer = f"unavailable: {exc}"
    try:
        if answer == "session":
            allow_session(app.exe)
        elif answer == "always":
            try:
                allow_always(app.exe)
            except (OSError, ValueError):  # cannot be saved (a broken file): allowed until restart
                allow_session(app.exe)
        elif answer == "deny":
            deny(app.exe)
    finally:
        with _lock:
            _pending.pop(app.exe, None)
        future.set_result(answer)


def check(app: App, action: str, ask: Callable[[App, str, str | None], str] | None = None) -> None:
    """Return when the AI may operate `app`; raise NotOperable (nothing sent) otherwise. May show
    the dialog and wait for it up to WAIT_S."""
    key = normalize(app.exe)
    app = App(key, app.name, app.title, app.pid, app.console)
    with _lock:  # deciding and opening the dialog at once: an answer cannot slip in between
        state = _status(key, app.console)
        left = _denied_left(key)
        future = None
        if state == "ask":
            future = _pending.get(key)
            if future is None:
                future = Future()
                _pending[key] = future
                threading.Thread(
                    target=_dialog,
                    args=(app, action, future, ask or _ask_person),
                    name="winhand-approval",
                    daemon=True,
                ).start()
    if state in ("always", "session"):
        return
    if state == "terminal":
        raise NotOperable(
            f"{app.label} ({'a console window' if app.console else 'a terminal'}): typing into it would "
            "get around this machine's guard rules, "
            "which judge the commands run with `run` and sessions. Use run or a session (session_start) "
            f"instead. If the person wants the AI to use this terminal window, they can allow {key} in "
            f"WinHand's settings (程序授权) or with `winhand apps allow {key}`."
        )
    if state == "denied":
        raise NotOperable(
            f"The person refused to let the AI operate {app.label} a short while ago; operations on it "
            f"are refused for another {max(1, round(left / 60))} min without asking again. Nothing was "
            "sent. Ask them in the conversation; they can allow it in WinHand's settings (程序授权)."
        )
    deadline = getattr(_call, "deadline", None)
    wait = WAIT_S if deadline is None else max(0.0, deadline - time.monotonic())
    try:
        answer = future.result(timeout=wait)
    except FutureTimeout:
        with _lock:
            opening = key in _opening
        if opening:  # this call's time ran out before the dialog was on the screen
            raise NotOperable(
                f"The dialog asking the person at the machine to allow operating {app.label} is still "
                "opening. Nothing was sent. Call again in a few seconds."
            ) from None
        raise NotOperable(
            f"Waiting for the person at the machine to allow operating {app.label}: a dialog is open "
            f"there for up to {int(DIALOG_S / 60)} minutes. Nothing was sent. Call again after they "
            "answered, or ask them in the conversation."
        ) from None
    if answer in ("session", "always"):
        return
    if answer == "deny":
        raise NotOperable(
            f"The person refused to let the AI operate {app.label}. Nothing was sent. Ask them in the "
            "conversation before trying again; they can allow it in WinHand's settings (程序授权)."
        )
    if answer.startswith("notshown: "):
        raise NotOperable(
            f"The dialog asking the person at the machine to allow {app.label} did not appear on the "
            f"screen ({answer.removeprefix('notshown: ')}). Nobody was asked, so this is not a refusal: "
            f"tell the person it failed. Nothing was sent. They can allow {key} in WinHand's settings "
            f"(程序授权) or with `winhand apps allow {key}`."
        )
    reason = answer.removeprefix("unavailable: ")
    raise NotOperable(
        f"Nobody allowed operating {app.label} ({reason}). Nothing was sent. Ask the person in the "
        "conversation, or to allow it in WinHand's settings (程序授权)."
    )
