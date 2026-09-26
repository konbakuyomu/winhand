"""Dialogs for the person sitting at the machine.

- a masked prompt for a secret: whatever is typed goes straight into the session and is never
  returned to the model;
- a file/folder picker, so the person chooses what to hand over ("upload") to the AI.

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
    title, message = request
    value = simpledialog.askstring(title, message, show="*", parent=root)
    sys.stdout.write(json.dumps({"value": value}))
    return 0


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
