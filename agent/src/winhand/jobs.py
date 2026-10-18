"""Background jobs that outlive winhand itself.

A job is a PowerShell command run by the system's own powershell.exe, detached from winhand,
with output and exit code written under ~/.winhand/jobs/<id>/. That makes it the tool for
work that restarts or replaces winhand (reinstalling it, rebooting services it depends on)
and for anything long: winhand can go away and come back, and the job's result is still there.

Elevated commands run the same way, launched through a UAC prompt the person has to approve.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import config, winenv
from .session.text import fitting

IS_WINDOWS = sys.platform == "win32"


class JobError(RuntimeError):
    pass


STDERR_TAIL_BYTES = 6_000  # of a program run directly: progress lines (Codex writes them there)


def _root() -> Path:
    path = config.home() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _powershell() -> str:
    system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
    candidate = system / "powershell.exe"
    return (
        str(candidate)
        if candidate.exists()
        else (shutil.which("powershell") or shutil.which("pwsh") or "pwsh")
    )


def _script(command: str, directory: Path, cwd: str | None) -> Path:
    """The job body: run the command, capture every stream as UTF-8, record the exit code."""
    log = directory / "output.log"
    exit_file = directory / "exit.json"
    location = f"Set-Location -LiteralPath '{cwd.replace(chr(39), chr(39) * 2)}'\n" if cwd else ""
    body = f"""$ErrorActionPreference = 'Continue'
$__wh = New-Object System.Text.UTF8Encoding $false  # no BOM: see winenv._PWSH_UTF8
[Console]::OutputEncoding = $__wh
[Console]::InputEncoding = $__wh
$OutputEncoding = $__wh
Remove-Variable __wh
{location}$started = Get-Date
$ok = $true
try {{
    & {{
{command}
    }} *>&1 | ForEach-Object {{ "$_" }} | Out-File -LiteralPath '{log}' -Encoding utf8 -Append
    $code = $LASTEXITCODE
    if ($null -eq $code) {{ $code = if ($?) {{ 0 }} else {{ 1 }} }}
}} catch {{
    "$_" | Out-File -LiteralPath '{log}' -Encoding utf8 -Append
    $code = 1
}}
[ordered]@{{ exit_code = $code; finished = (Get-Date).ToString('o'); seconds = [int]((Get-Date) - $started).TotalSeconds }} |
    ConvertTo-Json | Set-Content -LiteralPath '{exit_file}' -Encoding utf8
"""
    path = directory / "job.ps1"
    path.write_text(body, encoding="utf-8-sig")  # PowerShell 5.1 needs the BOM for non-ASCII
    return path


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _program_script(program: str, args: list[str], directory: Path, cwd: str | None, stdin: bool) -> Path:
    """The job body for a program run directly: no PowerShell parsing of its arguments, stdin fed
    from a UTF-8 file and stdout / stderr written to files as the program writes them, byte for
    byte (a PowerShell pipe would re-encode them: Chinese becomes ? in Windows PowerShell)."""
    exit_file = directory / "exit.json"
    options = [
        f"-FilePath {_quote(program)}",
        "-NoNewWindow",
        "-PassThru",
        f"-RedirectStandardOutput {_quote(str(directory / 'output.log'))}",
        f"-RedirectStandardError {_quote(str(directory / 'stderr.log'))}",
    ]
    if args:
        options.append(f"-ArgumentList {_quote(subprocess.list2cmdline(args))}")
    if cwd:
        options.append(f"-WorkingDirectory {_quote(cwd)}")
    if stdin:
        options.append(f"-RedirectStandardInput {_quote(str(directory / 'stdin.txt'))}")
    body = f"""$ErrorActionPreference = 'Continue'
$started = Get-Date
try {{
    $p = Start-Process {" ".join(options)}
    $null = $p.Handle  # keeps ExitCode readable once it has exited (Windows PowerShell)
    $p.WaitForExit()
    $code = $p.ExitCode
}} catch {{
    "$_" | Out-File -LiteralPath {_quote(str(directory / "stderr.log"))} -Encoding utf8 -Append
    $code = 1
}}
[ordered]@{{ exit_code = $code; finished = (Get-Date).ToString('o'); seconds = [int]((Get-Date) - $started).TotalSeconds }} |
    ConvertTo-Json | Set-Content -LiteralPath {_quote(str(exit_file))} -Encoding utf8
"""
    path = directory / "job.ps1"
    path.write_text(body, encoding="utf-8-sig")
    return path


def _runner(shell: str | None) -> str:
    if not shell or shell == "powershell":
        return _powershell()
    if shell == "pwsh":
        found = shutil.which("pwsh")
        if not found:
            raise JobError("pwsh (PowerShell 7) is not installed; leave shell out for Windows PowerShell")
        return found
    raise JobError("shell is powershell (Windows PowerShell 5.1, the default) or pwsh (PowerShell 7)")


def start(
    command: str,
    cwd: str | None = None,
    name: str | None = None,
    elevated: bool = False,
    args: list[str] | None = None,
    stdin: str | None = None,
    shell: str | None = None,
    env: dict[str, str] | None = None,
) -> dict:
    """Start `command` (PowerShell) detached; with `args`, `command` is a program run directly.
    Elevated jobs show a UAC prompt first. `env` is added to its environment (not elevated jobs:
    Windows starts those with the administrator's own)."""
    if not IS_WINDOWS:
        raise JobError("background jobs are available on Windows")
    if cwd and not os.path.isdir(cwd):
        raise JobError(f"working directory does not exist: {cwd}")
    if stdin is not None and args is None:
        raise JobError("stdin goes to a program run directly: give its arguments as args (args=[] for none)")
    if env and elevated:
        raise JobError(
            "an elevated job starts with the administrator's environment: secrets and env cannot reach it"
        )
    runner = _runner(shell)
    program = None
    if args is not None:
        program = winenv.resolve_executable(command.strip().strip('"'), winenv.build_env(), cwd)
        if not os.path.isabs(program) or not os.path.exists(program):
            raise JobError(f"program not found: {command!r} (give its full path, or a name on PATH)")
    first = os.path.basename(program) if program else (command.split()[0] if command.strip() else "job")
    slug = re.sub(r"[^a-z0-9]+", "-", (name or first).lower()).strip("-")
    job_id = f"{slug[:24] or 'job'}-{time.strftime('%m%d%H%M%S')}"
    directory = _root() / job_id
    directory.mkdir()
    if program is not None:
        if stdin is not None:
            (directory / "stdin.txt").write_bytes(stdin.encode("utf-8"))
        script = _program_script(program, list(args or []), directory, cwd, stdin is not None)
    else:
        script = _script(command, directory, cwd)
    shell = runner
    argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    meta = {
        "id": job_id,
        "command": subprocess.list2cmdline([program, *(args or [])]) if program else command,
        "cwd": cwd,
        "elevated": elevated,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "directory": str(directory),
    }
    if program is not None:
        meta["program"] = True
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    env = winenv.build_env(env)
    if elevated:
        # Start-Process -Verb RunAs is the supported way to ask for elevation; it returns at once
        quoted = " ".join(f'"{a}"' if " " in a else a for a in argv[1:])
        launcher = [
            shell, "-NoProfile", "-NonInteractive", "-Command",
            f"$p = Start-Process -FilePath '{shell}' -ArgumentList '{quoted}' -Verb RunAs -WindowStyle Hidden -PassThru; "
            "$p.Id",
        ]  # fmt: skip
        done = subprocess.run(
            launcher,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            env=env,
            creationflags=0x08000000,
        )
        output = done.stdout.decode("utf-8", errors="replace").strip()
        if done.returncode != 0 or not output.isdigit():
            error = done.stderr.decode("utf-8", errors="replace")
            (directory / "exit.json").write_text(
                json.dumps({"exit_code": 1223, "declined": True}), encoding="utf-8"
            )
            meta["pid"] = None
            _write_meta(directory, meta)
            if "cancel" in error.lower() or "取消" in error:
                raise JobError("the person declined the administrator (UAC) prompt")
            raise JobError(f"could not start an elevated process: {error.strip()[-300:]}")
        meta["pid"] = int(output)
    else:
        for extra in (0x01000000, 0):  # CREATE_BREAKAWAY_FROM_JOB when allowed, so it outlives us
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd or None,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=flags | extra,
                    close_fds=True,
                )
                break
            except OSError:
                if not extra:
                    raise
        meta["pid"] = process.pid
    try:
        import psutil

        meta["pid_created"] = psutil.Process(meta["pid"]).create_time() if meta.get("pid") else None
    except Exception:  # gone already, or not ours to read: the pid alone decides then
        pass
    _write_meta(directory, meta)
    return status(job_id)


def _write_meta(directory: Path, meta: dict) -> None:
    (directory / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _whole_characters(data: bytes) -> int:
    """How much of `data` ends on a whole UTF-8 character (output caught mid-write may not)."""
    for back in range(1, min(4, len(data)) + 1):
        byte = data[-back]
        if byte & 0xC0 != 0x80:  # the first byte of the last character
            need = 2 if byte >> 5 == 0b110 else 3 if byte >> 4 == 0b1110 else 4 if byte >> 3 == 0b11110 else 1
            return len(data) if back >= need else len(data) - back
    return len(data)


def _alive(meta: dict) -> bool:
    """The job's process still runs: same pid, and (when recorded) started when the job did, so a
    reused pid after a restart does not keep a dead job `running` forever."""
    import psutil

    pid = meta.get("pid")
    if not pid or not psutil.pid_exists(pid):
        return False
    created = meta.get("pid_created")
    if created is None:
        return True
    try:
        return abs(psutil.Process(pid).create_time() - created) < 2
    except psutil.Error:
        return False


def status(job_id: str, since: int = 0, max_chars: int = 20_000) -> dict:

    directory = _root() / job_id
    meta = _read_json(directory / "job.json")
    if meta is None:
        raise JobError(f"no job {job_id!r}; job_status() without an id lists them")
    finished = _read_json(directory / "exit.json")
    log = directory / "output.log"
    text = ""
    cursor = 0
    if log.exists():
        with open(log, "rb") as fh:  # only what is new: logs of long jobs grow large
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = min(max(since, 0), size)
            fh.seek(start)
            data = fh.read()
        data = data[: _whole_characters(data)]  # a character half written comes next time
        cursor = start + len(data)
        if since == 0 and data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        from .proc import _decode  # UTF-8, or the code page for programs that print GBK

        text = _decode(data)
    kept = fitting(text, max_chars, from_end=True)  # counted as width(): Chinese output counts 3
    omitted = len(text) - kept
    text = text[len(text) - kept :]
    if finished is not None:
        state = "finished"
    elif _alive(meta):
        state = "running"
    else:
        state = "vanished"  # killed, or the machine restarted before it could record an exit code
    result = {
        "id": job_id,
        "state": state,
        "command": meta["command"],
        "elevated": meta.get("elevated", False),
        "started": meta["started"],
        "output": text,
        "cursor": cursor,
        "log_file": str(log),
    }
    if omitted:
        result["omitted_chars"] = omitted
    errors = directory / "stderr.log"
    if meta.get("program") and errors.exists():  # a program run directly writes stderr on its own
        from .proc import _decode

        size = errors.stat().st_size
        result["stderr_size"] = size
        with open(errors, "rb") as fh:
            fh.seek(max(0, size - STDERR_TAIL_BYTES))
            tail = _decode(fh.read())
        result["stderr"] = tail
        if size > STDERR_TAIL_BYTES:
            result["stderr_note"] = f"the last {STDERR_TAIL_BYTES} bytes of {errors}"
    if finished:
        result.update({k: v for k, v in finished.items() if k != "finished"})
        result["finished"] = finished.get("finished")
    return result


def list_jobs(limit: int = 20) -> list[dict]:
    rows = []
    for directory in sorted(_root().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        try:
            s = status(directory.name, max_chars=0)
        except JobError:
            continue
        s.pop("output", None)
        rows.append(s)
    return rows


def stop(job_id: str) -> dict:
    import psutil

    s = status(job_id, max_chars=0)
    if s["state"] != "running":
        return s
    pid = _read_json(_root() / job_id / "job.json").get("pid")
    try:
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except psutil.Error as exc:
        raise JobError(f"could not stop job: {exc}") from exc
    (_root() / job_id / "exit.json").write_text(
        json.dumps({"exit_code": -1, "stopped": True}), encoding="utf-8"
    )
    return status(job_id)
