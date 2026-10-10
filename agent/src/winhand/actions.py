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
# anything that moves the cursor, recalls history or edits the line in place: after one of these
# the typed characters no longer spell the line that runs
_EDITING = re.compile(r"[\x00-\x08\x0b-\x1f]")
_CONTINUED = ("\\", "`")  # bash and PowerShell carry a line ending in these on to the next


@dataclass(frozen=True)
class Action:
    kind: str
    tool: str
    paths: tuple[str, ...] = ()
    text: str | None = None
    cwd: str | None = None
    host: str | None = None
    # a typed line edited with cursor keys: its final text is unknown, so every `match` applies
    opaque: bool = False

    def label(self) -> str:
        """One line for a dialog or an error: `run → command: git push`, `fs_write → write: C:\\a.txt`."""
        if self.kind in ("command", "input"):
            first = (self.text or "").strip().splitlines()
            where = f" (on {self.host})" if self.host else ""
            what = f"{self.kind}{where}: {first[0] if first else '(interactive)'}"
            what += " ..." if len(first) > 1 else ""
            what += " (typed with editing keys, so its final text cannot be known)" if self.opaque else ""
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


def _patch_actions(tool: str, args: dict) -> list[Action]:
    """An edit of every file the patch touches, and a read of every file it renames: renaming a
    file hands out its content as much as copying it."""
    root = _abs(args.get("root") or ".") or os.getcwd()
    try:
        text = args.get("patch")
        if text is None and args.get("patch_file"):
            text, _ = fs.read_text(args["patch_file"])
        patches = patching.parse(text or "")
        n = args.get("strip")
        n = n if n is not None else patching.choose_strip(patches, root)
        paths: list[str] = []
        renamed: list[str] = []

        def target(side: str | None) -> str | None:
            rel = patching._strip(side, n) if side else None
            return os.path.normpath(os.path.join(root, rel)) if rel else None

        for p in patches:
            old, new = target(p.old_path), target(p.new_path)
            paths += [t for t in (old, new) if t and t not in paths]
            if old and new and old != new:
                renamed.append(old)
        out = [Action("edit", tool, tuple(paths) or (root,))]
        return out + ([Action("read", tool, tuple(renamed))] if renamed else [])
    except Exception:  # a patch that cannot be read is judged by its folder; the tool reports why
        return [Action("edit", tool, (root,))]


def _destination(source: str | None, to: str | None) -> str | None:
    """Where fs_op copy/move writes: into `to` when it is an existing folder, as fs.fs_op does."""
    dest = _abs(to)
    if dest and source and os.path.isdir(dest):
        return os.path.join(dest, os.path.basename(str(source).rstrip("\\/")))
    return dest


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
    if tool in ("session_send", "session_stop", "session_prompt_user"):
        return session_input(a, sessions, typed, tool)[0]
    if tool in ("fs_write", "fs_write_bytes"):
        return one("write", a.get("path"))
    if tool == "fs_upload_url":
        return one("write", a.get("path"), expand_vars=True)
    if tool == "secret_prompt" and a.get("save_to"):
        return one("write", a.get("save_to"), expand_vars=True)
    if tool == "fs_edit":
        return one("edit", a.get("path"))
    if tool == "fs_apply_patch":
        return _patch_actions(tool, a)
    if tool in ("fs_read", "fs_send"):
        return one("read", a.get("path"))
    if tool == "fs_download_url":
        return one("read", a.get("path"), expand_vars=True)
    if tool == "fs_search":  # reads the files below path
        return one("read", a.get("path") or ".")
    if tool == "fs_op":
        op = (a.get("action") or "").lower()
        if op == "copy":
            return one("read", a.get("path")) + one("write", _destination(a.get("path"), a.get("to")))
        if op == "mkdir":
            return one("write", a.get("path"))
        if op == "delete":
            return one("delete", a.get("path"))
        if op == "move":  # moving a file creates it at the destination and hands out its content
            dest = _destination(a.get("path"), a.get("to"))
            return one("move", a.get("path"), dest) + one("read", a.get("path")) + one("write", dest)
    return []


def typed_lines(
    text: str | None, keys: list[str] | None, submit: bool, before: str = "", ctrl_c_clears: bool = True
) -> tuple[list[str], str]:
    """What a shell runs from one session_send, as typed: every line ended by a line break in the
    text, an Enter key or submit (the first one starting with what was typed `before`), and the
    unfinished rest that stays typed. A line ending in a continuation character (`\\` or a
    backtick) runs together with the next one. Backspace drops the last character and Ctrl+C the
    whole line (in a terminal; through plain pipes it is just a byte); everything else is kept as
    typed, editing keys included (see clean_line)."""
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
    for piece in _LINE_END.split("".join(chunks)):
        if _LINE_END.fullmatch(piece):
            if current.rstrip().endswith(_CONTINUED):
                current = current.rstrip()[:-1] + " "
                continue
            done.append(current)
            current = ""
            continue
        for ch in piece:
            if ch in ("\x7f", "\x08"):
                current = current[:-1]
            elif ch == "\x03" and ctrl_c_clears:
                current = ""
            else:
                current += ch
    return done, current


def clean_line(raw: str) -> tuple[str, bool]:
    """(the text to judge, whether editing keys make the real line unknowable): cursor keys,
    history recall and Tab completion all change what runs in ways the typed text does not show."""
    plain = _CSI.sub("", raw)
    opaque = plain != raw or bool(_EDITING.search(plain)) or "\t" in plain
    return _EDITING.sub("", plain).replace("\t", ""), opaque


def session_input(
    a: dict, sessions, typed: dict[str, str] | None = None, tool: str = "session_send"
) -> tuple[list[Action], str | None]:
    """(actions, what stays typed afterwards) for input into a session; (no actions, None) when the
    session's input is not shell commands. session_stop types the exit command and Enter after
    whatever is still typed; session_prompt_user types the person's secret after it."""
    if sessions is None:
        return [], None
    try:
        session = sessions.get(a.get("id"))
        spec = session.spec
    except (KeyError, TypeError, AttributeError):
        return [], None
    if not is_shell(spec):
        return [], None
    before = (typed or {}).get(a.get("id"), "")
    pty = getattr(spec, "transport", "pty") == "pty"
    if tool == "session_stop":
        exit_line = getattr(spec, "exit_command", None)
        if a.get("force") or not exit_line or not getattr(session, "alive", True):
            return [], ""
        lines, rest = typed_lines(exit_line, None, True, before, pty)
    elif tool == "session_prompt_user":
        if not before.strip():
            return [], ("" if a.get("submit", True) else before)
        lines, rest = typed_lines("«secret»", None, bool(a.get("submit", True)), before, pty)
    else:
        lines, rest = typed_lines(a.get("text"), a.get("keys"), bool(a.get("submit")), before, pty)
    cwd = _abs(getattr(spec, "cwd", None))
    out = []
    for raw in lines:
        text, opaque = clean_line(raw)
        if text.strip() or opaque:
            out.append(Action("input", tool, text=text, cwd=cwd, opaque=opaque))
    return out, rest
