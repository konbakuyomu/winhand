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
import tomllib
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from . import winenv
from .config import ServerEntry

REPAIR_TIMEOUT_S = 600.0
UV_NAMES = ("uv", "uv.exe")
# uv's global options (accepted before or after the subcommand) that take a value
GLOBAL_VALUED = (
    "--directory",
    "--project",
    "--config-file",
    "--cache-dir",
    "--color",
    "--allow-insecure-host",
    "--python-preference",
    "--preview-features",
)
# global options a rebuild repeats: they decide where packages and Pythons come from
CARRIED = (
    "--directory",
    "--project",
    "--python-preference",
    "--config-file",
    "--cache-dir",
    "--allow-insecure-host",
    "--offline",
    "--no-config",
    "--native-tls",
    "--system-certs",
    "--managed-python",
    "--no-managed-python",
    "--no-python-downloads",
    "--allow-python-downloads",
    "--no-cache",
    "-n",
)
# options of `uv run` that take a value: knowing them lets the scan step over their values
RUN_VALUED = (
    *GLOBAL_VALUED,
    "--python",
    "-p",
    "--with",
    "-w",
    "--with-editable",
    "--with-requirements",
    "--extra",
    "--no-extra",
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
    "--upgrade-package",
    "-P",
    "--upgrade-group",
    "--reinstall-package",
    "--refresh-package",
    "--config-setting",
    "-C",
    "--config-settings-package",
    "--keyring-provider",
    "--fork-strategy",
    "--prerelease",
    "--resolution",
    "--index-strategy",
    "--link-mode",
    "--exclude-newer",
    "--exclude-newer-package",
    "--no-build-package",
    "--no-binary-package",
    "--no-build-isolation-package",
    "--no-sources-package",
    "--no-editable-package",
    "--python-platform",
)
SHORT_VALUED = ("-p", "-w", "-i", "-f", "-P", "-C")
# `uv run` does not use the project's environment with these
NO_PROJECT = (
    "--no-project",
    "--isolated",
    "--script",
    "-s",
    "--gui-script",
    "--active",
)
NO_PROJECT_GLOBAL = ("--isolated", "--no-project")  # also accepted before `run`
NO_PROJECT_ENV = ("UV_NO_PROJECT", "UV_ISOLATED")
LOCK_FLAGS = ("--locked", "--frozen")
_UNREACHABLE = (15, 53, 67)  # ERROR_INVALID_DRIVE, ERROR_BAD_NETPATH, ERROR_BAD_NET_NAME
_NO_PYTHON = re.compile(r"No Python at '(.+?)'\s*$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass
class UvProject:
    """Where `uv run` keeps a server's environment, and the options a rebuild must repeat."""

    project: str  # the project (a workspace member, maybe): where the rebuild runs
    venv: str  # the environment uv uses for it (a workspace's is at the workspace root)
    python_args: list[str] = field(default_factory=list)  # --python X
    lock_args: list[str] = field(default_factory=list)  # --locked / --frozen
    global_args: list[str] = field(default_factory=list)  # CARRIED options, as given
    cwd: str = ""  # where the server starts: uv resolves relative paths in those options from here


def _split(arg: str) -> tuple[str, str | None]:
    if arg.startswith("--") and "=" in arg:
        key, value = arg.split("=", 1)
        return key, value
    if not arg.startswith("--") and len(arg) > 2 and arg[:2] in SHORT_VALUED:
        return arg[:2], arg[2:].removeprefix("=")  # -p3.12, -p=3.12
    return arg, None


def _setting(entry: ServerEntry, name: str) -> str | None:
    return (entry.env or {}).get(name) or os.environ.get(name) or None


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _read_toml(path: str) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


def _strings(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _workspace_of(path: str):
    """[tool.uv.workspace] of a pyproject.toml when it is a table, else None (whatever the file holds)."""
    node = _read_toml(path)
    for key in ("tool", "uv", "workspace"):
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else None


def _workspace_root(project: str) -> str:
    """The root of the uv workspace `project` belongs to (the project itself when none)."""
    if _workspace_of(os.path.join(project, "pyproject.toml")) is not None:
        return project
    here = project
    while True:
        parent = os.path.dirname(here)
        if parent == here:
            return project
        here = parent
        path = os.path.join(here, "pyproject.toml")
        if not os.path.isfile(path):
            continue
        workspace = _workspace_of(path)
        if workspace is None:
            if isinstance(_read_toml(path).get("project"), dict):
                return project  # uv stops at an enclosing project that is not a workspace
            continue
        # matched as uv matches them: case-sensitive, on Windows too
        rel = PurePosixPath(os.path.relpath(project, here).replace(os.sep, "/"))
        members = _strings(workspace.get("members"))
        excluded = _strings(workspace.get("exclude"))
        if any(rel.full_match(m) for m in members) and not any(rel.full_match(m) for m in excluded):
            return here
        return project  # the nearest workspace does not take it in: a project of its own


def uv_project(entry: ServerEntry) -> UvProject | None:
    """The project whose environment `uv run` uses for this server, or None when it is not one."""
    if entry.kind != "stdio" or not entry.command:
        return None
    if os.path.basename(entry.command.replace("\\", "/")).lower() not in UV_NAMES:
        return None
    if any(_truthy(_setting(entry, name)) for name in NO_PROJECT_ENV):
        return None
    args = list(entry.args)
    options: dict[str, str] = {}
    python_args: list[str] = []
    lock_args: list[str] = []
    global_args: list[str] = []
    seen_run = False
    i = 0
    while i < len(args) and args[i] != "--":
        arg = args[i]
        if not arg.startswith("-"):
            if seen_run:
                break  # the command uv runs
            if arg != "run":
                return None  # another subcommand (tool run, pip ...)
            seen_run = True
            i += 1
            continue
        key, value = _split(arg)
        if key in (NO_PROJECT if seen_run else NO_PROJECT_GLOBAL):
            return None
        valued = key in (RUN_VALUED if seen_run else GLOBAL_VALUED)
        if valued and value is None:
            i += 1
            value = args[i] if i < len(args) else ""
        if key in GLOBAL_VALUED:
            options[key] = value or ""
        if key in ("--python", "-p"):
            python_args += ["--python", value or ""]
        elif key in LOCK_FLAGS:
            lock_args.append(key)
        if key in CARRIED:
            global_args += [key, value] if valued else [key]
        i += 1
    if not seen_run:
        return None
    base = os.path.expanduser(entry.cwd) if entry.cwd else os.getcwd()
    directory = options.get("--directory") or _setting(entry, "UV_WORKING_DIR")
    start = os.path.join(base, os.path.expanduser(directory)) if directory else (base if entry.cwd else None)
    named_project = options.get("--project") or _setting(entry, "UV_PROJECT")
    if start is None and not named_project:
        return None
    # As uv: it starts in its current directory, which POSIX reports with links resolved and Windows
    # as given, and joins --project to that without resolving links in it
    start = start or base
    start = os.path.abspath(start) if winenv.IS_WINDOWS else os.path.realpath(start)
    if named_project:
        start = os.path.normpath(os.path.join(start, os.path.expanduser(named_project)))
    # the project: the nearest folder up from there with a pyproject.toml, as uv finds it
    here = start
    while not os.path.isfile(os.path.join(here, "pyproject.toml")):
        parent = os.path.dirname(here)
        if parent == here:
            return None  # not a project: uv run does not build a project environment here
        here = parent
    root = _workspace_root(here)
    named = _setting(entry, "UV_PROJECT_ENVIRONMENT")
    venv = os.path.join(root, os.path.expanduser(named)) if named else os.path.join(root, ".venv")
    return UvProject(
        os.path.normpath(here),
        os.path.normpath(venv),
        python_args,
        lock_args,
        global_args,
        entry.cwd and base or "",
    )


def _has_python(folder: str) -> bool:
    try:
        names = [name.lower() for name in os.listdir(folder)]
    except (FileNotFoundError, NotADirectoryError) as exc:
        # Windows reports an unreachable share or drive as "not found" too: that is not proof it is gone
        return getattr(exc, "winerror", None) in _UNREACHABLE
    except (OSError, ValueError):  # cannot look (permissions; a NUL in the path): not proof that it is gone
        return True
    if winenv.IS_WINDOWS:  # python.exe, or only python3.13t.exe in a free-threaded build
        return any(name.startswith("python") and name.endswith(".exe") for name in names)
    return any(name == "python" or name.startswith("python3") for name in names)


def missing_python(venv: str) -> str | None:
    """The base Python a virtual environment points to, when it no longer exists; else None.
    Errs towards "sound": winhand rebuilds only an environment that cannot run."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8", errors="replace") as fh:
            text = fh.read(64 * 1024)
    except (OSError, ValueError):
        return None
    home = None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() == "home":
            home = value.strip()
            break
    if not home:
        return None
    if not winenv.IS_WINDOWS:
        # the environment's python is a link to the base one: if it still leads somewhere, it runs
        if any(os.path.exists(os.path.join(venv, "bin", name)) for name in ("python", "python3")):
            return None
    if _has_python(home):
        return None
    return os.path.join(home, "python.exe" if winenv.IS_WINDOWS else "python3")


def _commands(project: UvProject) -> list[list[str]]:
    """The two uv commands of a rebuild, without the uv executable."""
    return [
        [*project.global_args, "venv", "--clear", *project.python_args, project.venv],
        [*project.global_args, "sync", *project.python_args, *project.lock_args],
    ]


def _shown(args: list[str]) -> str:
    return " ".join(f'"{a}"' if (" " in a or not a) else a for a in args)


def manual_fix(project: UvProject) -> str:
    first, second = (_shown(c) for c in _commands(project))
    where = f'in "{project.cwd}" ' if project.cwd else ""
    return f"{where}run `uv {first}`, then `uv {second}`"


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


def _run(command: list[str], cwd: str | None, env: dict, deadline: float) -> tuple[int | None, str]:
    """Run one uv command until `deadline`: (exit code or None when it ran out of time, its output).
    An overdue one is ended with its whole process tree, so nothing it started keeps the pipes open."""
    kwargs: dict = {"creationflags": 0x08000000} if winenv.IS_WINDOWS else {}  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            **kwargs,
        )
    except OSError as exc:
        return 127, f"cannot run {command[0]}: {exc}"
    try:
        out, _ = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
        return proc.returncode, out.decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        from .proc import _kill_tree

        _kill_tree(proc.pid)
        partial = exc.output or b""
        try:
            out, _ = proc.communicate(timeout=5)
            partial = out or partial  # on POSIX this repeats what was already read, from the start
        except subprocess.TimeoutExpired:
            pass
        return None, partial.decode("utf-8", errors="replace")


def prepare(entry: ServerEntry) -> str | None:
    """Before starting `entry`: when its uv project environment points to a missing Python, rebuild
    it (or, with repair_env false, refuse). Returns a note when it repaired something; raises
    RuntimeError when the server must not be started. Blocking: call it from a worker thread."""
    try:
        project = uv_project(entry)
    except Exception:  # noqa: BLE001 - an odd configuration: start the server as before
        return None
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
        _log(entry, f"{what}; rebuilding it")
        deadline = time.monotonic() + REPAIR_TIMEOUT_S
        for args in _commands(project):
            shown = _shown(args)
            code, output = _run([uv, *args], project.cwd or None, env, deadline)  # where the server starts
            _log(entry, f"uv {shown} ({'timed out' if code is None else f'exit {code}'}):\n{output.strip()}")
            if code is None:
                raise RuntimeError(
                    f"{what}; rebuilding it did not finish within {REPAIR_TIMEOUT_S:.0f} s (uv {shown}"
                    + (f": {_tail(output)}" if output.strip() else "")
                    + f"). To fix it by hand, {manual_fix(project)}"
                )
            if code != 0:
                raise RuntimeError(
                    f"{what}; rebuilding it failed: uv {shown} exited with {code}: {_tail(output)}. "
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
        try:
            project = uv_project(entry)
        except Exception:  # noqa: BLE001
            project = None
        if project is None:
            return (
                f"its Python virtual environment points to {missing}, which no longer exists: rebuild that "
                "environment (or point the server at an existing Python)"
            )
        if missing_python(project.venv) is None:
            # the environment winhand checks is sound (or absent): uv used another one
            state = "on" if getattr(entry, "repair_env", True) else "off (repair_env = false)"
            return (
                f"its Python virtual environment points to {missing}, which no longer exists. The environment "
                f"winhand checks ({project.venv}) is sound or absent, and automatic repair is {state}; so this "
                "server runs from another one: rebuild that environment (in the folder it belongs to, run "
                "`uv venv --clear`, then `uv sync`)"
            )
        how = (
            "winhand rebuilds it before the next start (repair_env is on); to fix it now, "
            if getattr(entry, "repair_env", True)
            else "automatic repair is off (repair_env = false); to fix it, "
        )
        return (
            f"its Python environment {project.venv} points to {missing}, which no longer exists; "
            f"{how}{manual_fix(project)}"
        )
    return None


def checks(entries) -> list[dict]:
    """For sys_info(checks=true): each enabled uv-run server whose environment is broken."""
    out = []
    for entry in entries:
        if not entry.enabled:
            continue
        try:
            project = uv_project(entry)
            missing = missing_python(project.venv) if project else None
        except Exception:  # noqa: BLE001 - one odd configuration does not take the other checks down
            continue
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
