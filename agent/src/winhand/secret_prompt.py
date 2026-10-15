"""Dialogs for the person sitting at the machine.

- a masked prompt for a secret: whatever is typed goes straight into the session and is never
  returned to the model;
- a file/folder picker, so the person chooses what to hand over ("upload") to the AI;
- a yes/no question, when a guard rule wants the person's OK before an action (guard.py);
- the question whether the AI may operate a program on the desktop (approvals.py).

Each dialog runs in a short-lived child process (tkinter), the frozen winhand.exe included.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable

SHOW_S = 20.0  # a dialog's window appears within a few seconds, the frozen winhand.exe and Tk included
PICK_SHOW_S = 60.0  # a file dialog first opens its folder, which can be a slow or unreachable share
SCREENS = {
    "locked": "the screen at the machine is locked",
    "secure": "a secure screen (a UAC prompt or the like) is in front at the machine",
    "disconnected": "the remote desktop session to the machine is disconnected",
}


def dialog_main(payload: str) -> int:
    """Child-process side: show the masked dialog and print the answer as JSON."""
    import tkinter as tk
    from tkinter import simpledialog

    request = json.loads(payload)
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if isinstance(request, dict) and request.get("kind") == "pick":
        from tkinter import filedialog

        options = {"parent": root, "title": request.get("title") or "选择文件"}
        if request.get("initial"):
            options["initialdir"] = request["initial"]
        mode = request.get("mode", "file")
        if mode == "files":
            value = list(filedialog.askopenfilenames(**options))
        elif mode == "folder":
            value = filedialog.askdirectory(**options) or None
        elif mode == "save":
            value = filedialog.asksaveasfilename(**options) or None
        else:
            value = filedialog.askopenfilename(**options) or None
        sys.stdout.write(json.dumps({"value": value}, ensure_ascii=False))
        return 0
    if isinstance(request, dict) and request.get("kind") == "approve":
        sys.stdout.write(json.dumps({"value": _approval_dialog(root, request)}))
        return 0
    if isinstance(request, dict) and request.get("kind") == "confirm":
        from tkinter import messagebox

        # "No" has the focus: the dialog pops up over whatever the person is typing into, and a
        # stray Enter or Space must not approve the action
        value = messagebox.askyesno(
            request.get("title") or "winhand",
            request.get("message") or "",
            icon="warning",
            default="no",
            parent=root,
        )
        sys.stdout.write(json.dumps({"value": bool(value)}))
        return 0
    title, message = request
    value = simpledialog.askstring(title, message, show="*", parent=root)
    sys.stdout.write(json.dumps({"value": value}))
    return 0


def _approval_dialog(root, request: dict) -> str:
    """Three answers: deny (the default: a stray Enter or Space must not approve), session, always."""
    import tkinter as tk

    answer = {"value": "deny"}

    def choose(value: str) -> None:
        answer["value"] = value
        root.destroy()

    root.title("WinHand：AI 想操作一个程序")
    root.resizable(False, False)
    body = tk.Frame(root, padx=20, pady=16)
    body.pack(fill="both", expand=True)
    program = request.get("name") or request.get("exe") or "?"
    if request.get("exe") and request.get("exe") != program.lower():
        program = f"{program}（{request['exe']}）"
    lines = [f"AI 想操作：{program}"]
    if request.get("title"):
        lines.append(f"窗口：{request['title']}")
    if request.get("action"):
        lines.append(f"这次要做：{request['action']}")
    lines.append("")
    lines.append("允许后，AI 可以点击、输入、操作这个程序的控件和窗口。截图和读字不受这里限制。")
    tk.Label(body, text="\n".join(lines), justify="left", anchor="w", wraplength=460).pack(fill="x")
    if request.get("warning"):
        tk.Label(
            body, text=request["warning"], fg="#b00020", justify="left", anchor="w", wraplength=460
        ).pack(fill="x", pady=(8, 0))
    row = tk.Frame(body, pady=14)
    row.pack(fill="x")
    buttons = [
        tk.Button(row, text="拒绝", width=10, command=lambda: choose("deny")),
        tk.Button(row, text="允许到 WinHand 重启", command=lambda: choose("session")),
        tk.Button(row, text="一直允许", command=lambda: choose("always")),
    ]
    for button in buttons:
        button.pack(side="left", padx=(0, 8))
        button.bind("<Return>", lambda event: event.widget.invoke())
    root.bind("<Escape>", lambda _: choose("deny"))
    root.protocol("WM_DELETE_WINDOW", lambda: choose("deny"))
    root.deiconify()
    root.lift()
    buttons[0].focus_set()
    root.mainloop()
    return answer["value"]


def _dialog_command(payload: str) -> list[str]:
    # In the frozen binary sys.executable is winhand.exe itself; there is no Python to run.
    if getattr(sys, "frozen", False):
        return [sys.executable, "secret-dialog", payload]
    return [sys.executable, "-m", "winhand.cli", "secret-dialog", payload]


class PromptUnavailable(RuntimeError):
    pass


class DialogNotShown(PromptUnavailable):
    """The dialog's process started, but no window of it appeared on the screen: nobody was asked."""


def ask_secret(title: str, message: str, timeout_s: float = 300) -> str | None:
    """Return what the person typed, or None if they cancelled."""
    return _run_dialog(json.dumps([title, message]), timeout_s)


def ask_pick(title: str, mode: str = "file", initial: str | None = None, timeout_s: float = 300):
    """Paths the person picked (a list for mode='files'), or None if they cancelled."""
    if mode not in ("file", "files", "folder", "save"):
        raise ValueError("mode is file, files, folder or save")
    payload = json.dumps(
        {"kind": "pick", "title": title, "mode": mode, "initial": initial}, ensure_ascii=False
    )
    return _run_dialog(payload, timeout_s, show_s=PICK_SHOW_S)


def ask_confirm(title: str, message: str, timeout_s: float = 120) -> bool:
    """True if the person clicked Yes, False for No; PromptUnavailable if nobody answered in time
    or no dialog can be shown here."""
    payload = json.dumps({"kind": "confirm", "title": title, "message": message}, ensure_ascii=False)
    return bool(_run_dialog(payload, timeout_s))


def ask_approval(
    name: str,
    exe: str,
    title: str,
    action: str,
    warning: str | None = None,
    timeout_s: float = 120,
    on_shown: Callable[[], None] | None = None,
) -> str:
    """'deny', 'session' or 'always' as the person answered; PromptUnavailable when nobody answered
    in time or no dialog can be shown here. `on_shown` hears when the dialog is on the screen."""
    payload = json.dumps(
        {"kind": "approve", "name": name, "exe": exe, "title": title, "action": action, "warning": warning},
        ensure_ascii=False,
    )
    value = _run_dialog(payload, timeout_s, on_shown=on_shown)
    return value if value in ("deny", "session", "always") else "deny"


def _run_dialog(
    payload: str,
    timeout_s: float,
    show_s: float | None = None,
    on_shown: Callable[[], None] | None = None,
):
    # A dialog nobody can see must not pass for one nobody answered: the AI would wait the full
    # timeout and then tell the person they did not click (0.6.0 did exactly that).
    if (state := _screen_state()) != "ok":
        raise DialogNotShown(f"no dialog can be seen now: {SCREENS.get(state, state)}")
    show_s = min(SHOW_S if show_s is None else show_s, timeout_s)
    started = time.monotonic()
    proc = subprocess.Popen(
        _dialog_command(payload),
        # never this process's own stdin: when another thread reads it (the desktop app's
        # backend reads its requests there, `winhand stdio` its MCP messages), a Python child
        # blocks at startup on the inherited pipe until that read ends, and no dialog appears
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    shown = False
    try:
        while True:
            # read its output all along: a child that writes much before its window appears
            # would otherwise block on a full pipe
            wait = max(0.0, started + timeout_s - time.monotonic()) if shown else 0.1
            try:
                out, err = proc.communicate(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                pass
            elapsed = time.monotonic() - started
            if not shown:
                visible = _window_visible(proc.pid)
                if visible is None or visible:  # None: cannot be checked here
                    shown = True
                    if on_shown is not None:
                        on_shown()
                elif elapsed >= show_s:
                    raise DialogNotShown(
                        f"the dialog did not appear on the screen within {show_s:g}s, "
                        "although its process was running"
                    )
            if shown and elapsed >= timeout_s:
                raise PromptUnavailable(f"nobody answered the dialog within {timeout_s:g}s")
    except BaseException:
        _close(proc)
        raise
    if proc.returncode != 0:
        lines = err.decode("utf-8", errors="replace").strip().splitlines()
        raise PromptUnavailable(
            "cannot open a dialog on this machine: " + (lines[-1] if lines else "unknown error")
        )
    try:
        return json.loads(out.decode("utf-8", errors="replace"))["value"]
    except (ValueError, KeyError) as exc:
        raise PromptUnavailable("dialog returned no answer") from exc


def _screen_state() -> str:
    """'ok', or why nobody can see a dialog at the machine now: locked, secure, disconnected."""
    if sys.platform != "win32":
        return "ok"
    try:
        from . import desktop

        return desktop.desktop_state()
    except Exception:  # noqa: BLE001 - a failed check must not stop the dialog itself
        return "ok"


def _window_visible(pid: int) -> bool | None:
    """Whether the dialog's process, or a child of it (a venv's python.exe starts the real one),
    shows a window on the screen; None where that cannot be checked."""
    if sys.platform != "win32":
        return None
    try:
        import psutil

        from . import desktop

        try:
            pids = {pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))}
        except psutil.Error:
            pids = {pid}
        return desktop.shows_window(pids)
    except Exception:  # noqa: BLE001 - a failed check must not stop the dialog itself
        return None


def _close(proc: subprocess.Popen) -> None:
    """End the dialog: its process and any child of it."""
    try:
        import psutil

        children = psutil.Process(proc.pid).children(recursive=True)
    except Exception:  # noqa: BLE001 - psutil.Error, or the process is gone already
        children = []
    for child in children:
        try:
            child.kill()
        except Exception:  # noqa: BLE001 - gone already
            pass
    proc.kill()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        pass  # something still holds its pipes; what it wrote is not needed
