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
from .session.keys import encode_key

KINDS = ("command", "input", "write", "edit", "delete", "move", "read")
_SHELLS = {"pwsh", "powershell", "cmd", "bash", "sh", "zsh", "ssh"}
_LINE_END = re.compile(r"(\r\n|\r|\n)")
_CSI = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|O.)")


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str
    paths: tuple[str, ...] = ()
    text: str | None = None
    cwd: str | None = None
    host: str | None = None

    def label(self) -> str:
        """One line for a dialog or an error: `run → command: git push`, `fs_write → write: C:\\a.txt`."""
        if self.kind in ("command", "input"):
            first = (self.text or "").strip().splitlines()
            where = f" (on {self.host})" if self.host else ""
            what = f"{self.kind}{where}: {first[0] if first else '(interactive)'}"
            what += " ..." if len(first) > 1 else ""
        else:
            what = f"{self.kind}: {' -> '.join(self.paths)}"
        return f"{self.tool} → {what}"


def _abs(path: str | None, expand_vars: bool = False) -> str | None:
    """The path a tool will use: fs_* tools take it as given (~ expanded, relative to winhand's
    folder); the one-time link tools also expand %VARS% and $VARS. Judging any other spelling
    would let `$X/../locked` slip past a rule on `locked`."""
    if not path:
        return None
    path = os.path.expanduser(str(path))
    return os.path.abspath(os.path.expandvars(path) if expand_vars else path)


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


def describe(
    tool: str, args: dict | None, sessions=None, typed: dict[str, str] | None = None
) -> list[Action]:
    """The actions a call performs. `typed` holds, per shell session, what was typed there but not
    yet run, so a line finished by a later Enter is judged whole."""
    a = args or {}

    def one(kind: str, *paths: str | None, expand_vars: bool = False, **kw) -> list[Action]:
        resolved = (_abs(x, expand_vars) for x in paths)
        return [Action(kind, tool, tuple(p for p in resolved if p), **kw)]

    if tool == "run":
        text = a.get("script") or _join([a.get("command") or "", *(a.get("args") or [])]).strip()
        if a.get("stdin"):  # what a shell or interpreter reads from stdin runs too
            text = f"{text}\n{a['stdin']}"
        return one("command", text=text, cwd=_abs(a.get("cwd")))
    if tool == "job_start":
        return one("command", text=a.get("command") or "", cwd=_abs(a.get("cwd")))
    if tool == "remote_run":
        text = _join([a.get("script") or "", *(a.get("args") or [])]).strip()
        return one("command", text=text, host=a.get("host"))
    if tool == "session_start":
        return one("command", text=_session_command(a), cwd=_abs(a.get("cwd")))
    if tool == "session_send":
        return session_input(a, sessions, typed)[0]
    if tool in ("fs_write", "fs_write_bytes"):
        return one("write", a.get("path"))
    if tool == "fs_upload_url":
        return one("write", a.get("path"), expand_vars=True)
    if tool == "secret_prompt" and a.get("save_to"):
        return one("write", a.get("save_to"))
    if tool == "fs_edit":
        return one("edit", a.get("path"))
    if tool == "fs_apply_patch":
        return [Action("edit", tool, _patch_paths(a))]
    if tool in ("fs_read", "fs_send"):
        return one("read", a.get("path"))
    if tool == "fs_download_url":
        return one("read", a.get("path"), expand_vars=True)
    if tool == "fs_search":  # reads the files below path
        return one("read", a.get("path") or ".")
    if tool == "fs_op":
        op = (a.get("action") or "").lower()
        if op == "copy":
            return one("read", a.get("path")) + one("write", a.get("to"))
        if op == "mkdir":
            return one("write", a.get("path"))
        if op == "delete":
            return one("delete", a.get("path"))
        if op == "move":  # moving a file elsewhere hands out its content as much as copying it
            return one("move", a.get("path"), a.get("to")) + one("read", a.get("path"))
    return []


def typed_lines(
    text: str | None, keys: list[str] | None, submit: bool, before: str = ""
) -> tuple[list[str], str]:
    """What a shell runs from one session_send: every line ended by a line break in the text, an
    Enter key or submit (the first one starting with what was typed `before`), and the unfinished
    rest that stays typed. Ctrl+C drops the line being typed, Backspace its last character; other
    editing keys are kept as typed, so a line is judged with at least everything that was typed."""
    chunks = [text or ""]
    for name in keys or []:
        try:
            chunks.append(encode_key(name))
        except ValueError:  # the tool refuses unknown keys itself
            pass
    if submit:
        chunks.append("\r")
    done: list[str] = []
    current = before
    for chunk in chunks:
        if chunk == "\x03":
            current = ""
            continue
        if chunk in ("\x7f", "\x08"):
            current = current[:-1]
            continue
        for piece in _LINE_END.split(chunk):
            if _LINE_END.fullmatch(piece):
                done.append(current)
                current = ""
            else:
                current += piece
    return [_CSI.sub("", line).replace("\t", "") for line in done], current


def session_input(a: dict, sessions, typed: dict[str, str] | None = None) -> tuple[list[Action], str | None]:
    """(actions, what stays typed afterwards) for a session_send; (no actions, None) for a session
    whose input is not shell commands."""
    if sessions is None:
        return [], None
    try:
        spec = sessions.get(a.get("id")).spec
    except (KeyError, TypeError):
        return [], None
    if not is_shell(spec):
        return [], None
    before = (typed or {}).get(a.get("id"), "")
    lines, rest = typed_lines(a.get("text"), a.get("keys"), bool(a.get("submit")), before)
    cwd = _abs(getattr(spec, "cwd", None))
    return [Action("input", "session_send", text=line, cwd=cwd) for line in lines if line.strip()], rest
