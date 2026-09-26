"""Ask the person sitting at the machine for a secret without the model seeing it.

A masked dialog opens on the desktop; whatever is typed goes straight into the
session. The value is never returned to the caller.
"""

from __future__ import annotations

import json
import subprocess
import sys

_DIALOG = r"""
import json, sys, tkinter as tk
from tkinter import simpledialog
title, message = json.loads(sys.argv[1])
root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
value = simpledialog.askstring(title, message, show="*", parent=root)
sys.stdout.write(json.dumps({"value": value}))
"""


class PromptUnavailable(RuntimeError):
    pass


def ask_secret(title: str, message: str, timeout_s: float = 300) -> str | None:
    """Return what the person typed, or None if they cancelled."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _DIALOG, json.dumps([title, message])],
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
        return json.loads(proc.stdout.decode("utf-8"))["value"]
    except (ValueError, KeyError) as exc:
        raise PromptUnavailable("dialog returned no answer") from exc
