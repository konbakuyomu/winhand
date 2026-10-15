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

SHOW_S = 15.0  # a dialog's window appears within a few seconds, the frozen winhand.exe and Tk included


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
    return _run_dialog(payload, timeout_s)


def ask_confirm(title: str, message: str, timeout_s: float = 120) -> bool:
    """True if the person clicked Yes, False for No; PromptUnavailable if nobody answered in time
    or no dialog can be shown here."""
    payload = json.dumps({"kind": "confirm", "title": title, "message": message}, ensure_ascii=False)
    return bool(_run_dialog(payload, timeout_s))


def ask_approval(
    name: str, exe: str, title: str, action: str, warning: str | None = None, timeout_s: float = 120
) -> str:
    """'deny', 'session' or 'always' as the person answered; PromptUnavailable when nobody answered
    in time or no dialog can be shown here."""
    payload = json.dumps(
        {"kind": "approve", "name": name, "exe": exe, "title": title, "action": action, "warning": warning},
        ensure_ascii=False,
    )
    value = _run_dialog(payload, timeout_s)
    return value if value in ("deny", "session", "always") else "deny"


def _run_dialog(payload: str, timeout_s: float):
    deadline = time.monotonic() + timeout_s
    proc = subprocess.Popen(
        _dialog_command(payload),
        # never this process's own stdin: when another thread reads it (the desktop app's
        # backend reads its requests there, `winhand stdio` its MCP messages), a Python child
        # blocks at startup on the inherited pipe until that read ends, and no dialog appears
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # A dialog that never shows must not pass for one nobody answered: the AI would wait the
        # full timeout and then tell the person they did not click (0.6.0 did exactly that).
        _wait_shown(proc, min(SHOW_S, timeout_s))
        out, err = proc.communicate(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as exc:
        _close(proc)
        raise PromptUnavailable(f"nobody answered the dialog within {int(timeout_s)}s") from exc
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


def _wait_shown(proc: subprocess.Popen, limit: float) -> None:
    """Return once the dialog is on the screen or its process ended (its output tells the rest);
    raise DialogNotShown when neither happens within `limit` seconds."""
    until = time.monotonic() + limit
    while proc.poll() is None:
        visible = _window_visible(proc.pid)
        if visible is None or visible:
            return
        if time.monotonic() >= until:
            raise DialogNotShown(
                f"the dialog did not appear on the screen within {int(limit)}s, "
                "although its process was running"
            )
        time.sleep(0.1)


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

        for child in psutil.Process(proc.pid).children(recursive=True):
            child.kill()
    except Exception:  # noqa: BLE001 - psutil.Error, or the process is gone already
        pass
    proc.kill()
    proc.communicate()
