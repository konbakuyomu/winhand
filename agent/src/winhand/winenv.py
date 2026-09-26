"""Process environment and command resolution that survive quirky Windows setups.

Every child process winhand starts goes through here, so fixes live in one place:
missing ComSpec/SystemRoot (seen when a parent launched us with a stripped
environment), non-UTF-8 code pages, pagers that block forever, `.cmd` shims such
as npm.cmd, and paths with spaces, CJK characters or brackets.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# Variables that make CLI tools behave well when nobody is watching the screen.
_QUIET_DEFAULTS = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
    "GIT_PAGER": "cat",
    "PAGER": "cat",
    "MANPAGER": "cat",
    "LESS": "-FRX",
}


def build_env(extra: dict[str, str] | None = None, *, terminal: bool = False) -> dict[str, str]:
    """Return a complete environment for a child process.

    Starts from the full current environment (never a filtered copy), repairs the
    Windows variables that break cmd.exe and npm when missing, and applies
    non-interactive defaults unless the caller overrides them.
    """
    env = dict(os.environ)
    if IS_WINDOWS:
        system_root = env.get("SystemRoot") or env.get("SYSTEMROOT") or r"C:\Windows"
        env.setdefault("SystemRoot", system_root)
        if not env.get("ComSpec") and not env.get("COMSPEC"):
            env["ComSpec"] = str(Path(system_root) / "System32" / "cmd.exe")
        env.setdefault("PATHEXT", ".COM;.EXE;.BAT;.CMD;.VBS;.VBE;.JS;.JSE;.WSF;.WSH;.MSC;.PY;.PYW")
    elif terminal:
        env.setdefault("TERM", "xterm-256color")
    for key, value in _QUIET_DEFAULTS.items():
        env.setdefault(key, value)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def resolve_executable(name: str, env: dict[str, str] | None = None) -> str:
    """Resolve a command name to a path, honouring PATHEXT (npm -> npm.cmd).

    Returns the name unchanged when it cannot be resolved so the OS error
    surfaces with the original spelling.
    """
    if os.path.dirname(name):
        return name
    path = (env or os.environ).get("PATH")
    return shutil.which(name, path=path) or name


def default_shell() -> list[str]:
    """argv for an interactive shell that prints UTF-8."""
    if IS_WINDOWS:
        for candidate in ("pwsh", "powershell"):
            exe = shutil.which(candidate)
            if exe:
                return [exe, "-NoLogo", "-NoExit", "-Command", _PWSH_UTF8]
        comspec = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/K", "chcp 65001>nul"]
    shell = os.environ.get("SHELL") or shutil.which("bash") or "/bin/sh"
    return [shell]


def shell_command(command: str) -> list[str]:
    """argv that runs one command line through the platform shell."""
    if IS_WINDOWS:
        for candidate in ("pwsh", "powershell"):
            exe = shutil.which(candidate)
            if exe:
                return [
                    exe,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"{_PWSH_UTF8}; {command}",
                ]
        comspec = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
        return [comspec, "/d", "/s", "/c", f"chcp 65001>nul & {command}"]
    return [os.environ.get("SHELL") or "/bin/sh", "-c", command]


_PWSH_UTF8 = (
    "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
    "[Console]::InputEncoding=[Text.Encoding]::UTF8;"
    "$OutputEncoding=[Text.Encoding]::UTF8;"
    "$ProgressPreference='SilentlyContinue'"
)


def command_line(argv: list[str]) -> str:
    """Join argv into one command line (what ConPTY needs) with correct quoting."""
    if IS_WINDOWS:
        return subprocess.list2cmdline(argv)
    import shlex

    return shlex.join(argv)


def long_path(path: str | os.PathLike[str]) -> str:
    """Absolute path, prefixed with \\\\?\\ on Windows when it exceeds MAX_PATH."""
    resolved = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if IS_WINDOWS and len(resolved) >= 248 and not resolved.startswith("\\\\?\\"):
        if resolved.startswith("\\\\"):
            return "\\\\?\\UNC\\" + resolved[2:]
        return "\\\\?\\" + resolved
    return resolved


def display_path(path: str) -> str:
    """Strip the long-path prefix for messages shown to humans and models."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path
