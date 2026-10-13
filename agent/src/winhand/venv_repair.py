"""A local MCP server started with `uv run` in a project whose virtual environment points to a Python
that no longer exists (a version manager such as mise removed it after an upgrade): uv does not
notice, so `uv run` fails on every start. winhand checks before starting such a server and, unless
the server's `repair_env` is false, rebuilds the environment with `uv venv --clear` and `uv sync`.
See docs/comet/changes/local-mcp-reliability/research.md.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import winenv
from .config import ServerEntry

REPAIR_TIMEOUT_S = 600.0
UV_NAMES = ("uv", "uv.exe")
# uv's global options that take a value (before `run`)
GLOBAL_VALUED = ("--directory", "--project", "--config-file", "--cache-dir", "--color")
# options of `uv run` that take a value: knowing them lets the scan below step over their values
RUN_VALUED = (
    "--python",
    "-p",
    "--with",
    "--with-editable",
    "--with-requirements",
    "--extra",
    "--group",
    "--only-group",
    "--no-group",
    "--package",
    "--env-file",
    "--index",
    "--default-index",
    "--index-url",
    "-i",
    "--extra-index-url",
    "--find-links",
    "-f",
    "--prerelease",
    "--resolution",
    "--index-strategy",
    "--link-mode",
    "--exclude-newer",
    "--python-platform",
    *GLOBAL_VALUED,
)
# `uv run` does not use the project's environment with these
NO_PROJECT = ("--no-project", "--isolated", "--script", "--with-requirements")
PASSED_FLAGS = ("--managed-python", "--no-managed-python")
LOCK_FLAGS = ("--locked", "--frozen")
_NO_PYTHON = re.compile(r"No Python at '(.+?)'\s*$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass
class UvProject:
    """Where `uv run` keeps a server's environment, and the options a rebuild must repeat."""

    project: str
    venv: str
    python_args: list[str] = field(default_factory=list)  # --python X, --managed-python ...
    lock_args: list[str] = field(default_factory=list)  # --locked / --frozen


def _split(arg: str) -> tuple[str, str | None]:
    if arg.startswith("--") and "=" in arg:
        key, value = arg.split("=", 1)
        return key, value
    return arg, None


def uv_project(entry: ServerEntry) -> UvProject | None:
    """The project whose environment `uv run` uses for this server, or None when it is not one."""
    if entry.kind != "stdio" or not entry.command:
        return None
    if os.path.basename(entry.command.replace("\\", "/")).lower() not in UV_NAMES:
        return None
    args = list(entry.args)
    options: dict[str, str] = {}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        key, value = _split(args[i])
        if key in GLOBAL_VALUED and value is None:
            i += 1
            value = args[i] if i < len(args) else ""
        if value is not None:
            options[key] = value
        i += 1
    if i >= len(args) or args[i] != "run":
        return None
    python_args: list[str] = []
    lock_args: list[str] = []
    i += 1
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        key, value = _split(args[i])
        if key in NO_PROJECT:
            return None
        if key in RUN_VALUED and value is None:
            i += 1
            value = args[i] if i < len(args) else ""
        if key in GLOBAL_VALUED:
            options[key] = value or ""
        elif key in ("--python", "-p"):
            python_args += ["--python", value or ""]
        elif key in PASSED_FLAGS:
            python_args.append(key)
        elif key in LOCK_FLAGS:
            lock_args.append(key)
        i += 1
    base = os.path.expanduser(entry.cwd) if entry.cwd else os.getcwd()
    chosen = options.get("--directory") or options.get("--project")
    if chosen:
        project = os.path.join(base, os.path.expanduser(chosen))
    elif entry.cwd:
        project = base
    else:
        return None
    named = (entry.env or {}).get("UV_PROJECT_ENVIRONMENT") or os.environ.get("UV_PROJECT_ENVIRONMENT")
    venv = os.path.join(project, os.path.expanduser(named)) if named else os.path.join(project, ".venv")
    return UvProject(os.path.normpath(project), os.path.normpath(venv), python_args, lock_args)


def missing_python(venv: str) -> str | None:
    """The base Python a virtual environment points to, when it no longer exists; else None."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8", errors="replace") as fh:
            text = fh.read(64 * 1024)
    except OSError:
        return None
    home = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            home = value.strip()
            break
    if not home:
        return None
    names = ("python.exe",) if winenv.IS_WINDOWS else ("python3", "python")
    if any(os.path.isfile(os.path.join(home, name)) for name in names):
        return None
    return os.path.join(home, names[0])


def manual_fix(project: UvProject) -> str:
    extra = " ".join(project.python_args)
    lock = " ".join(project.python_args + project.lock_args)
    return (
        f'in "{project.project}" run `uv venv --clear{" " + extra if extra else ""} "{project.venv}"`, '
        f"then `uv sync{' ' + lock if lock else ''}`"
    )


def broken_text(entry: ServerEntry, project: UvProject, missing: str) -> str:
    return (
        f"{entry.name}'s Python environment {project.venv} points to {missing}, which no longer exists "
        "(removed by an upgrade of the Python it was built with)"
    )


def _lock(venv: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(venv))
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _log(entry: ServerEntry, text: str) -> None:
    from .config import home

    try:
        folder = home() / "logs" / "mcp"
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / f"{entry.name}.log", "a", encoding="utf-8") as fh:
            fh.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def _tail(output: str, lines: int = 5) -> str:
    kept = [line for line in output.splitlines() if line.strip()][-lines:]
    return " | ".join(kept)


def prepare(entry: ServerEntry) -> str | None:
    """Before starting `entry`: when its uv project environment points to a missing Python, rebuild
    it (or, with repair_env false, refuse). Returns a note when it repaired something; raises
    RuntimeError when the server must not be started. Blocking: call it from a worker thread."""
    project = uv_project(entry)
    if project is None:
        return None
    with _lock(project.venv):  # another start may be rebuilding this same environment right now
        missing = missing_python(project.venv)
        if missing is None:
            return None
        what = broken_text(entry, project, missing)
        if not getattr(entry, "repair_env", True):
            raise RuntimeError(
                f"{what}; automatic repair is off (repair_env = false). To fix it, {manual_fix(project)}"
            )
        env = winenv.build_env(entry.env)
        uv = winenv.resolve_executable(entry.command, env)
        commands = [
            [uv, "venv", "--clear", *project.python_args, project.venv],
            [uv, "sync", *project.python_args, *project.lock_args],
        ]
        _log(entry, f"{what}; rebuilding it")
        deadline = time.monotonic() + REPAIR_TIMEOUT_S
        kwargs: dict = {"creationflags": 0x08000000} if winenv.IS_WINDOWS else {}  # CREATE_NO_WINDOW
        for command in commands:
            shown = " ".join(command[1:])
            try:
                done = subprocess.run(
                    command,
                    cwd=project.project,
                    env=env,
                    capture_output=True,
                    timeout=max(1.0, deadline - time.monotonic()),
                    stdin=subprocess.DEVNULL,
                    **kwargs,
                )
            except subprocess.TimeoutExpired:
                _log(entry, f"uv {shown}: timed out")
                raise RuntimeError(
                    f"{what}; rebuilding it did not finish within {REPAIR_TIMEOUT_S:.0f} s (uv {shown}). "
                    f"To fix it by hand, {manual_fix(project)}"
                ) from None
            except OSError as exc:
                raise RuntimeError(
                    f"{what}; cannot run uv to rebuild it ({exc}). To fix it, {manual_fix(project)}"
                ) from None
            output = (done.stdout + done.stderr).decode("utf-8", errors="replace")
            _log(entry, f"uv {shown} (exit {done.returncode}):\n{output.strip()}")
            if done.returncode != 0:
                raise RuntimeError(
                    f"{what}; rebuilding it failed: uv {shown} exited with {done.returncode}: {_tail(output)}. "
                    f"To fix it by hand, {manual_fix(project)}"
                )
        if missing_python(project.venv) is not None:
            raise RuntimeError(f"{what}; it still points to a missing Python after `uv venv --clear`")
        return f"repaired its Python environment ({time.strftime('%Y-%m-%d %H:%M')}): {what}"


def explain(entry: ServerEntry, output_lines) -> str | None:
    """A sentence for a failed start whose output says `No Python at '...'`: what broke, how to fix it."""
    for line in reversed(list(output_lines or [])):
        found = _NO_PYTHON.search(str(line).strip())
        if not found:
            continue
        missing = found.group(1).strip().strip('"')
        project = uv_project(entry)
        if project is None:
            return (
                f"its Python virtual environment points to {missing}, which no longer exists: rebuild that "
                "environment (or point the server at an existing Python)"
            )
        how = (
            "winhand rebuilds it before the next start (repair_env is on)"
            if getattr(entry, "repair_env", True)
            else f"automatic repair is off (repair_env = false); to fix it, {manual_fix(project)}"
        )
        return f"its Python environment {project.venv} points to {missing}, which no longer exists; {how}"
    return None


def checks(entries) -> list[dict]:
    """For sys_info(checks=true): each enabled uv-run server whose environment is broken."""
    out = []
    for entry in entries:
        if not entry.enabled:
            continue
        project = uv_project(entry)
        missing = missing_python(project.venv) if project else None
        if not missing:
            continue
        fix = (
            "winhand rebuilds it the next time the server starts (repair_env is on); or "
            if getattr(entry, "repair_env", True)
            else "automatic repair is off (repair_env = false); "
        ) + manual_fix(project)
        out.append(
            {
                "check": f"mcp_env:{entry.name}",
                "ok": False,
                "detail": broken_text(entry, project, missing),
                "fix": fix,
            }
        )
    return out
