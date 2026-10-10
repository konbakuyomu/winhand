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
# a line the shell may join to the next one: `\` (sh), a backtick (PowerShell), `^` (cmd), a
# trailing pipe or operator, or an open quote or bracket
_CONTINUATION_CHARS = ("\\", "`", "^")
_CONTINUES = re.compile(r"(\\|`|\^|\||&|,|\(|\{|\[)\s*$")
# bash/zsh history expansion (`!!`, `!-2:s/x/r/`, `^old^new`): the line that runs is not the one typed
_HISTORY = re.compile(r"(^\s*\^)|!(?=[^\s=(])")
UNKNOWN = "\x00"  # stands for typed text the guard could not follow: judged as editing keys are


@dataclass(frozen=True)
class LineState:
    """What a shell session has typed and not run yet: the line being typed, and earlier lines the
    shell may still join to it."""

    line: str = ""
    carried: tuple[str, ...] = ()


UNKNOWN_STATE = LineState(UNKNOWN)


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
            what += (
                " (typed with editing keys, or after input winhand could not follow, so its final text cannot be known)"
                if self.opaque
                else ""
            )
        else:
            what = f"{self.kind}: {' -> '.join(self.paths)}"
        return f"{self.tool} → {what}"


def _abs(path: str | None, expand_vars: bool = False, vars_first: bool = False) -> str | None:
    """The path a tool will use: fs_* tools take it as given (~ expanded, relative to winhand's
    folder); the one-time link tools also expand %VARS% and $VARS, and secret_prompt expands the
    variables before ~. Judging any other spelling would let `$X/../locked` slip past a rule on
    `locked`."""
    if not path:
        return None
    path = str(path)
    if vars_first:
        return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))
    path = os.path.expanduser(path)
    return os.path.abspath(os.path.expandvars(path) if expand_vars else path)


def _join(argv: list[str]) -> str:
    return " ".join(str(a) for a in argv)


def _program(spec) -> str:
    argv = list(getattr(spec, "argv", None) or [])
    if not argv:
        return ""
    name = re.split(r"[\\/]", str(argv[0]))[-1].lower()
    stem, _, ext = name.rpartition(".")
    return stem if stem and ext in ("exe", "cmd", "bat") else name


def is_posix_shell(spec) -> bool:
    """bash, sh, zsh or an ssh login (most often one of them): history expansion applies."""
    if getattr(spec, "profile", None) == "ssh":
        return True
    name = _program(spec)
    return name in ("bash", "sh", "zsh", "ssh") or (not name and os.name != "nt")


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
    """An edit of every file the patch touches, and a read of each of them too: a hunk that does
    not fit is answered with the lines the file has there, and a rename hands the content out.
    The patch file itself is read as well."""
    root = _abs(args.get("root") or ".") or os.getcwd()
    try:
        text = args.get("patch")
        if text is None and args.get("patch_file"):
            text, _ = fs.read_text(args["patch_file"])
        patches = patching.parse(text or "")
        n = args.get("strip")
        n = n if n is not None else patching.choose_strip(patches, root)
        paths: list[str] = []

        def target(side: str | None) -> str | None:
            rel = patching._strip(side, n) if side else None
            return os.path.normpath(os.path.join(root, rel)) if rel else None

        for p in patches:
            for t in (target(p.old_path), target(p.new_path)):
                if t and t not in paths:
                    paths.append(t)
        touched = tuple(paths) or (root,)
        out = [Action("edit", tool, touched), Action("read", tool, touched)]
    except Exception:  # a patch that cannot be read is judged by its folder; the tool reports why
        out = [Action("edit", tool, (root,)), Action("read", tool, (root,))]
    if args.get("patch_file"):
        out.append(Action("read", tool, (_abs(args["patch_file"]),)))
    return out


def _destination(source: str | None, to: str | None) -> str | None:
    """Where fs_op copy/move writes: into `to` when it is an existing folder, as fs.fs_op does."""
    dest = _abs(to)
    src = _abs(source)
    if dest and src and os.path.isdir(dest):
        return os.path.join(dest, os.path.basename(src.rstrip("\\/")))
    return dest


def describe(
    tool: str, args: dict | None, sessions=None, typed: dict[str, LineState] | None = None
) -> list[Action]:
    """The actions a call performs. `typed` holds, per shell session, what was typed there but not
    yet run, so a line finished by a later Enter is judged whole."""
    a = args or {}

    def one(kind: str, *paths: str | None, expand_vars: bool = False, **kw) -> list[Action]:
        resolved = (_abs(x, expand_vars) for x in paths)
        return [Action(kind, tool, tuple(p for p in resolved if p), **kw)]

    if tool == "run":
        argv = [a.get("script") or a.get("command") or "", *(a.get("args") or [])]
        text = _join(argv).strip()
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
        return [Action("write", tool, (_abs(a.get("save_to"), vars_first=True),))]
    if tool == "fs_edit":  # editing needs the content: a read rule covers it too
        return one("edit", a.get("path")) + one("read", a.get("path"))
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


def _continues(line: str) -> bool:
    if _CONTINUES.search(line):
        return True
    opened = sum(line.count(c) for c in "([{") - sum(line.count(c) for c in ")]}")
    return opened > 0 or line.count('"') % 2 == 1 or line.count("'") % 2 == 1


def _joined(pieces: list[str]) -> list[str]:
    """The ways a shell may join continued lines: without a gap (sh and cmd drop `\\`/`^` and
    the line break) and with one (PowerShell, open quotes and brackets)."""
    parts = []
    for piece in pieces[:-1]:
        stripped = piece.rstrip()
        parts.append(stripped[:-1] if stripped.endswith(_CONTINUATION_CHARS) else stripped)
    parts.append(pieces[-1])
    return ["".join(parts), " ".join(parts)]


def typed_lines(
    text: str | None,
    keys: list[str] | None,
    submit: bool,
    state: LineState | None = None,
    ctrl_c_clears: bool = True,
    posix: bool = False,
) -> tuple[list[tuple[str, bool]], LineState]:
    """The lines a shell runs from one input (text, then keys, then submit), as (text to judge,
    opaque), and what stays typed afterwards.

    A line runs at a line break in the text, an Enter key (or Ctrl+M/J) or submit, starting with
    what was typed before. It is judged on its own and, when earlier lines may continue into it,
    joined with them both without and with a space. DEL removes the last character and Ctrl+C the
    whole line (in a terminal; through plain pipes it is just a byte). Keys after an unknown key
    name are not sent by the tool, so they are not followed either. Anything else that edits the
    line, and typed text the guard could not follow, makes the line opaque."""
    state = state or LineState()
    stream = text or ""
    complete = True
    for name in keys or []:
        try:
            stream += encode_key(name)
        except ValueError:  # Session.send stops here: later keys and submit never go out
            complete = False
            break
    if submit and complete:
        stream += "\r"
    judged: list[tuple[str, bool]] = []
    current, carried = state.line, list(state.carried)
    for piece in _LINE_END.split(stream):
        if _LINE_END.fullmatch(piece):
            candidates = [current] + (_joined([*carried, current]) if carried else [])
            for raw in candidates:
                item = clean_line(raw, posix)
                if (item[0].strip() or item[1]) and item not in judged:
                    judged.append(item)
            carried = [*carried, current] if _continues(" ".join([*carried, current])) else []
            current = ""
            continue
        for ch in piece:
            if ch == "\x7f":
                if current and not current.endswith(UNKNOWN):
                    current = current[:-1]
            elif ch == "\x03" and ctrl_c_clears:
                current, carried = "", []
            else:
                current += ch
    return judged, LineState(current, tuple(carried))


def clean_line(raw: str, posix: bool = False) -> tuple[str, bool]:
    """(the text to judge, whether the real line is unknowable): cursor keys, history recall and
    expansion, Tab completion and input the guard lost track of all change what runs in ways the
    typed text does not show."""
    plain = _CSI.sub("", raw)
    opaque = plain != raw or bool(_EDITING.search(plain)) or "\t" in plain
    text = _EDITING.sub("", plain).replace("\t", "")
    if posix and _HISTORY.search(text):
        opaque = True
    return text, opaque


def session_input(
    a: dict, sessions, typed: dict[str, LineState] | None = None, tool: str = "session_send"
) -> tuple[list[Action], LineState | None]:
    """(actions, what stays typed if the input goes out) for input into a session; (no actions,
    None) when the session's input is not shell commands. session_stop types the exit command and
    Enter after whatever is still typed; session_prompt_user types the person's secret after it."""
    if sessions is None:
        return [], None
    try:
        session = sessions.get(a.get("id"))
        spec = session.spec
    except (KeyError, TypeError, AttributeError):
        return [], None
    if not is_shell(spec):
        return [], None
    before = (typed or {}).get(a.get("id")) or LineState()
    pty = getattr(spec, "transport", "pty") == "pty"
    posix = is_posix_shell(spec)
    if tool == "session_stop":
        exit_line = getattr(spec, "exit_command", None)
        if a.get("force") or not exit_line or not getattr(session, "alive", True):
            return [], None
        lines, after = typed_lines(exit_line, None, True, before, pty, posix)
    elif tool == "session_prompt_user":
        submit = bool(a.get("submit", True))
        if not before.line and not before.carried:
            return [], LineState() if submit else LineState(UNKNOWN)
        lines, after = typed_lines("«secret»", None, submit, before, pty, posix)
    else:
        lines, after = typed_lines(a.get("text"), a.get("keys"), bool(a.get("submit")), before, pty, posix)
    cwd = _abs(getattr(spec, "cwd", None))
    return [Action("input", tool, text=t, cwd=cwd, opaque=o) for t, o in lines], after
