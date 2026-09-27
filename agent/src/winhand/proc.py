"""One-shot commands, process inspection and machine facts."""

from __future__ import annotations

import getpass
import itertools
import locale
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import threading
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


# MCP clients give up on a tool call after about a minute (Claude Code: 60 s). `run` answers
# before that; a command still going by then keeps running and is picked up with job_status.
REPLY_WITHIN_S = 45.0
_KEEP_FINISHED = 20


class _Running:
    """A command `run` handed back before it finished: output keeps being collected."""

    def __init__(self, rid: str, proc: subprocess.Popen, argv: list[str], deadline: float) -> None:
        self.id, self.proc, self.argv, self.deadline = rid, proc, argv, deadline
        self.started = time.monotonic()
        self.out = bytearray()
        self.err = bytearray()
        self.lock = threading.Lock()
        self.readers = [
            threading.Thread(target=self._pump, args=(proc.stdout, self.out), daemon=True),
            threading.Thread(target=self._pump, args=(proc.stderr, self.err), daemon=True),
        ]
        for t in self.readers:
            t.start()
        self.timed_out = False
        self.stopped = False

    def _pump(self, stream, sink: bytearray) -> None:
        for chunk in iter(lambda: stream.read1(65536), b""):
            with self.lock:
                sink.extend(chunk)

    def wait(self, seconds: float) -> bool:
        try:
            self.proc.wait(timeout=max(seconds, 0))
        except subprocess.TimeoutExpired:
            return False
        for t in self.readers:
            t.join(timeout=5)
        return True

    def kill(self) -> None:
        _kill_tree(self.proc.pid)
        self.wait(5)

    def text(self) -> tuple[str, str]:
        with self.lock:
            return _decode(bytes(self.out)), _decode(bytes(self.err))


_running: dict[str, _Running] = {}
_run_ids = itertools.count(1)


def run(
    command: str,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 120,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    output_limit: int = 30000,
    reply_within_s: float = REPLY_WITHIN_S,
) -> dict:
    """Run to completion. With `args` the program is executed directly (no shell
    quoting issues); without, `command` is a command line for the platform shell.
    Still running after `reply_within_s`, it keeps going (until `timeout_s`) and the
    answer says so; job_status(id) returns the rest."""
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
    job = _Running(f"run-{next(_run_ids)}", proc, argv, time.monotonic() + timeout_s)
    if stdin is not None:
        threading.Thread(target=_feed, args=(proc, stdin), daemon=True).start()
    if job.wait(min(timeout_s, reply_within_s)):
        return _result(job, output_limit)
    if timeout_s <= reply_within_s:
        job.timed_out = True
        job.kill()
        return _result(job, output_limit)
    _running[job.id] = job
    threading.Thread(target=_expire, args=(job,), daemon=True).start()
    out, err = job.text()
    stdout, _ = clip(out, output_limit, keep="both")
    stderr, _ = clip(err, output_limit // 2, keep="both")
    return {
        "still_running": True,
        "id": job.id,
        "pid": proc.pid,
        "stdout": stdout,
        "stderr": stderr,
        "cursor": len(out),
        "duration_ms": int((time.monotonic() - job.started) * 1000),
        "next": (
            f"Still running after {reply_within_s:.0f}s, so this is its output so far; it keeps running "
            f"(killed after timeout_s={timeout_s:.0f}s). job_status(id='{job.id}', since=cursor) returns "
            f"the rest and the exit code; job_stop(id='{job.id}') ends it."
        ),
    }


def _feed(proc: subprocess.Popen, text: str) -> None:
    try:
        assert proc.stdin is not None
        proc.stdin.write(text.encode())
        proc.stdin.close()
    except (OSError, ValueError):
        pass


def _expire(job: _Running) -> None:
    if not job.wait(job.deadline - time.monotonic()):
        job.timed_out = True
        job.kill()
    finished = [j for j in _running.values() if j.proc.poll() is not None]
    for old in finished[:-_KEEP_FINISHED]:
        _running.pop(old.id, None)


def _result(job: _Running, output_limit: int) -> dict:
    out, err = job.text()
    stdout, cut_out = clip(out, output_limit, keep="both")
    stderr, cut_err = clip(err, output_limit // 2, keep="both")
    result = {
        "exit_code": job.proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "duration_ms": int((time.monotonic() - job.started) * 1000),
    }
    if job.timed_out:
        result["timed_out"] = True
        result["hint"] = (
            "Killed after timeout. For long-running or interactive programs use "
            "session_start so you can watch and answer it."
        )
    if job.stopped:
        result["stopped"] = True
    if cut_out or cut_err:
        result["truncated"] = {"stdout": cut_out, "stderr": cut_err}
    return result


def run_status(rid: str, since: int = 0, max_chars: int = 20_000) -> dict:
    """The rest of a command `run` handed back while it was still running."""
    job = _running.get(rid)
    if job is None:
        raise ValueError(
            f"no running command {rid!r} (winhand keeps the last {_KEEP_FINISHED} after they finish)"
        )
    done = job.proc.poll() is not None
    if done:
        job.wait(5)
    out, err = job.text()
    text = out[since:]
    omitted = max(len(text) - max_chars, 0)
    result = {
        "id": rid,
        "state": "finished" if done else "running",
        "command": subprocess.list2cmdline(job.argv),
        "output": text[-max_chars:] if max_chars else "",
        "cursor": len(out),
        "duration_ms": int((time.monotonic() - job.started) * 1000),
    }
    if omitted:
        result["omitted_chars"] = omitted
    if done:
        result["exit_code"] = job.proc.returncode
        result["stderr"] = clip(err, max_chars // 2, keep="both")[0]
        if job.timed_out:
            result["timed_out"] = True
        if job.stopped:
            result["stopped"] = True
    return result


def run_stop(rid: str) -> dict:
    job = _running.get(rid)
    if job is None:
        raise ValueError(f"no running command {rid!r}")
    if job.proc.poll() is None:
        job.stopped = True
        job.kill()
    return run_status(rid)


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
