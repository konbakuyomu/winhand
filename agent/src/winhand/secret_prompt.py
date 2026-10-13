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
    try:
        proc = subprocess.run(
            _dialog_command(payload),
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        raise PromptUnavailable(f"nobody answered the dialog within {int(timeout_s)}s") from exc
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise PromptUnavailable(
            "cannot open a dialog on this machine: " + (err[-1] if err else "unknown error")
        )
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))["value"]
    except (ValueError, KeyError) as exc:
        raise PromptUnavailable("dialog returned no answer") from exc
