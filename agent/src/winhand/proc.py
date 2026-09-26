"""One-shot commands, process inspection and machine facts."""

from __future__ import annotations

import getpass
import locale
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import time

import psutil

from . import winenv
from .session.text import clip

_TOOLS = (
    "git",
    "node",
    "npm",
    "python",
    "uv",
    "rg",
    "pwsh",
    "powershell",
    "pyocd",
    "openocd",
    "arm-none-eabi-gdb",
    "JLink",
    "scons",
    "cmake",
    "make",
    "ninja",
    "gh",
)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (locale.getpreferredencoding(False), "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _kill_tree(pid: int) -> None:
    try:
        parent = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = parent.children(recursive=True) + [parent]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=3)


def split_command(command: str) -> list[str]:
    """Split a command string into argv without eating Windows backslashes."""
    if winenv.IS_WINDOWS:
        parts = shlex.split(command, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(command)


def run(
    command: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 120,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    output_limit: int = 30000,
) -> dict:
    """Run to completion. With `args` the program is executed directly (no shell
    quoting issues); without, `command` is a command line for the platform shell."""
    full_env = winenv.build_env(env)
    if args is None:
        argv = winenv.shell_command(command)
    else:
        argv = [winenv.resolve_executable(command, full_env), *args]
    if cwd and not os.path.isdir(os.path.expanduser(cwd)):
        return {"error": f"working directory does not exist: {cwd}"}
    kwargs: dict = {}
    if winenv.IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=os.path.expanduser(cwd) if cwd else None,
            env=full_env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
    except OSError as exc:
        return {"error": f"cannot start {argv[0]!r}: {exc}", "argv": argv}
    timed_out = False
    try:
        out, err = proc.communicate(stdin.encode() if stdin is not None else None, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        out, err = proc.communicate()
    stdout, cut_out = clip(_decode(out), output_limit, keep="both")
    stderr, cut_err = clip(_decode(err), output_limit // 2, keep="both")
    result = {
        "exit_code": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
    if timed_out:
        result["timed_out"] = True
        result["hint"] = (
            "Killed after timeout. For long-running or interactive programs use "
            "session_start so you can watch and answer it."
        )
    if cut_out or cut_err:
        result["truncated"] = {"stdout": cut_out, "stderr": cut_err}
    return result


def proc_list(name: str | None = None, limit: int = 100) -> dict:
    rows = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "create_time", "status"]):
        info = p.info
        pname = info.get("name") or ""
        cmd = " ".join(info.get("cmdline") or [])
        if name and name.lower() not in pname.lower() and name.lower() not in cmd.lower():
            continue
        mem = info.get("memory_info")
        rows.append(
            {
                "pid": info["pid"],
                "name": pname,
                "status": info.get("status"),
                "rss_mb": round(mem.rss / 1048576, 1) if mem else None,
                "started": time.strftime("%H:%M:%S", time.localtime(info["create_time"]))
                if info.get("create_time")
                else None,
                "cmdline": cmd[:300],
            }
        )
    rows.sort(key=lambda r: (r["name"].lower(), r["pid"]))
    out = {"count": len(rows), "processes": rows[:limit]}
    if len(rows) > limit:
        out["truncated"] = f"showing {limit} of {len(rows)}; filter with name=..."
    return out


def proc_kill(pid: int, tree: bool = True, force: bool = False) -> dict:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except psutil.NoSuchProcess:
        return {"pid": pid, "killed": False, "error": "no such process"}
    if pid == os.getpid():
        return {"pid": pid, "killed": False, "error": "refusing to kill winhand itself"}
    targets = (proc.children(recursive=True) if tree else []) + [proc]
    for p in targets:
        try:
            p.kill() if force else p.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(targets, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    return {"pid": pid, "name": name, "killed": True, "processes": len(targets)}


def sys_info() -> dict:
    info = {
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "hostname": socket.gethostname(),
        "user": getpass.getuser(),
        "home": str(os.path.expanduser("~")),
        "cwd": os.getcwd(),
        "python": sys.version.split()[0],
        "default_shell": winenv.default_shell()[0],
        "cpu_count": os.cpu_count(),
        "memory_gb": round(psutil.virtual_memory().total / 1073741824, 1),
        "tools": {t: shutil.which(t) for t in _TOOLS if shutil.which(t)},
        "redirection_guard": winenv.redirection_guard_enforced(),
    }
    if winenv.IS_WINDOWS:
        import ctypes

        info["codepage"] = {"ansi": ctypes.windll.kernel32.GetACP(), "oem": ctypes.windll.kernel32.GetOEMCP()}
    try:
        import serial.tools.list_ports

        info["serial_ports"] = [f"{p.device} - {p.description}" for p in serial.tools.list_ports.comports()]
    except Exception:
        pass
    return info
