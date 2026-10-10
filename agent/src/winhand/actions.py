"""Tool calls described as actions on this machine: what the guard (guard.py) judges.

A call that reads or changes the machine becomes one or more actions: a command it runs, a line
typed into a shell session, a file it writes, edits, deletes, moves or reads. Calls that only look
(session_list, fs_stat ...) and the desktop tools are not actions.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from . import fs, patching, profiles
from .proc import split_command

KINDS = ("command", "input", "write", "edit", "delete", "move", "read")
_SHELLS = {"pwsh", "powershell", "cmd", "bash", "sh", "zsh", "ssh"}


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str
    paths: tuple[str, ...] = ()
    text: str | None = None
    cwd: str | None = None
    host: str | None = None

    def label(self) -> str:
        """One line for a dialog or an error: `command: git push` or `write: C:\\x\\a.txt`."""
        if self.kind in ("command", "input"):
            first = (self.text or "").strip().splitlines()
            where = f" (on {self.host})" if self.host else ""
            return f"{self.kind}{where}: {first[0] if first else '(interactive)'}" + (
                " ..." if len(first) > 1 else ""
            )
        return f"{self.kind}: {' -> '.join(self.paths)}"


def _abs(path: str | None) -> str | None:
    if not path:
        return None
    return os.path.abspath(os.path.expanduser(os.path.expandvars(str(path))))


def _join(argv: list[str]) -> str:
    return " ".join(str(a) for a in argv)


def is_shell(spec) -> bool:
    """A session whose lines are shell commands: the shell/ssh profiles, the default shell (no
    program given) or a program named like one (pwsh.exe, /bin/bash ...)."""
    if getattr(spec, "profile", None) in ("shell", "ssh"):
        return True
    if getattr(spec, "transport", "pty") not in ("pty", "pipe"):
        return False
    argv = list(getattr(spec, "argv", None) or [])
    if not argv:
        return True
    name = re.split(r"[\\/]", str(argv[0]))[-1].lower()
    stem, _, ext = name.rpartition(".")
    if stem and ext in ("exe", "cmd", "bat"):
        name = stem
    return name in _SHELLS


def _session_command(args: dict) -> str:
    extra = list(args.get("args") or [])
    command = args.get("command")
    argv = split_command(command) if isinstance(command, str) else list(command or [])
    if args.get("profile"):
        try:
            data = profiles.get_profile(args["profile"])
            if command is None:
                spec, _ = profiles.build_spec(data, variables=args.get("vars"), extra_args=extra)
                return _join(spec.argv)
        except (KeyError, ValueError):
            return _join([args["profile"], *argv, *extra])
    return _join([*argv, *extra])


def _patch_paths(args: dict) -> tuple[str, ...]:
    root = _abs(args.get("root") or ".") or os.getcwd()
    try:
        text = args.get("patch")
        if text is None and args.get("patch_file"):
            text, _ = fs.read_text(args["patch_file"])
        patches = patching.parse(text or "")
        n = args.get("strip")
        n = n if n is not None else patching.choose_strip(patches, root)
        paths = []
        for p in patches:
            for side in (p.old_path, p.new_path):
                rel = patching._strip(side, n) if side else None
                if rel and (target := os.path.normpath(os.path.join(root, rel))) not in paths:
                    paths.append(target)
        return tuple(paths) or (root,)
    except Exception:  # a patch that cannot be read is judged by its folder; the tool reports why
        return (root,)


def describe(tool: str, args: dict | None, sessions=None) -> list[Action]:
    a = args or {}

    def one(kind: str, *paths: str | None, **kw) -> list[Action]:
        return [Action(kind, tool, tuple(p for p in (_abs(x) for x in paths) if p), **kw)]

    if tool == "run":
        text = a.get("script") or _join([a.get("command") or "", *(a.get("args") or [])]).strip()
        return one("command", text=text, cwd=_abs(a.get("cwd")))
    if tool == "job_start":
        return one("command", text=a.get("command") or "", cwd=_abs(a.get("cwd")))
    if tool == "remote_run":
        return one("command", text=a.get("script") or "", host=a.get("host"))
    if tool == "session_start":
        return one("command", text=_session_command(a), cwd=_abs(a.get("cwd")))
    if tool == "session_send":
        return _session_input(a, sessions)
    if tool in ("fs_write", "fs_write_bytes", "fs_upload_url"):
        return one("write", a.get("path"))
    if tool == "fs_edit":
        return one("edit", a.get("path"))
    if tool == "fs_apply_patch":
        return [Action("edit", tool, _patch_paths(a))]
    if tool in ("fs_read", "fs_send", "fs_download_url"):
        return one("read", a.get("path"))
    if tool == "fs_op":
        op = (a.get("action") or "").lower()
        if op == "copy":
            return one("read", a.get("path")) + one("write", a.get("to"))
        if op == "mkdir":
            return one("write", a.get("path"))
        if op == "delete":
            return one("delete", a.get("path"))
        if op == "move":
            return one("move", a.get("path"), a.get("to"))
    return []


def _session_input(a: dict, sessions) -> list[Action]:
    """Lines a shell session will run: the text when submitted, and every line ended inside it."""
    text = a.get("text")
    if not text or sessions is None:
        return []
    try:
        spec = sessions.get(a.get("id")).spec
    except (KeyError, TypeError):
        return []
    if not is_shell(spec):
        return []
    lines = re.split(r"\r\n|\r|\n", text)
    if not a.get("submit"):
        lines = lines[:-1]  # the unfinished last line is typed, not run
    cwd = _abs(getattr(spec, "cwd", None))
    return [Action("input", "session_send", text=line, cwd=cwd) for line in lines if line.strip()]
