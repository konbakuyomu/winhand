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

IS_WINDOWS = sys.platform == "win32"


class JobError(RuntimeError):
    pass


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
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$OutputEncoding = [Text.Encoding]::UTF8
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


def start(command: str, cwd: str | None = None, name: str | None = None, elevated: bool = False) -> dict:
    """Start `command` (PowerShell) detached. Elevated jobs show a UAC prompt first."""
    if not IS_WINDOWS:
        raise JobError("background jobs are available on Windows")
    if cwd and not os.path.isdir(cwd):
        raise JobError(f"working directory does not exist: {cwd}")
    slug = re.sub(
        r"[^a-z0-9]+", "-", (name or command.split()[0] if command.strip() else "job").lower()
    ).strip("-")
    job_id = f"{slug[:24] or 'job'}-{time.strftime('%m%d%H%M%S')}"
    directory = _root() / job_id
    directory.mkdir()
    script = _script(command, directory, cwd)
    shell = _powershell()
    argv = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    meta = {
        "id": job_id,
        "command": command,
        "cwd": cwd,
        "elevated": elevated,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "directory": str(directory),
    }
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    env = winenv.build_env()
    if elevated:
        # Start-Process -Verb RunAs is the supported way to ask for elevation; it returns at once
        quoted = " ".join(f'"{a}"' if " " in a else a for a in argv[1:])
        launcher = [
            shell, "-NoProfile", "-NonInteractive", "-Command",
            f"$p = Start-Process -FilePath '{shell}' -ArgumentList '{quoted}' -Verb RunAs -WindowStyle Hidden -PassThru; "
            "$p.Id",
        ]  # fmt: skip
        done = subprocess.run(launcher, capture_output=True, timeout=300, env=env, creationflags=0x08000000)
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
    _write_meta(directory, meta)
    return status(job_id)


def _write_meta(directory: Path, meta: dict) -> None:
    (directory / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def status(job_id: str, since: int = 0, max_chars: int = 20_000) -> dict:
    import psutil

    directory = _root() / job_id
    meta = _read_json(directory / "job.json")
    if meta is None:
        raise JobError(f"no job {job_id!r}; job_status() without an id lists them")
    finished = _read_json(directory / "exit.json")
    log = directory / "output.log"
    text = ""
    if log.exists():
        data = log.read_bytes()
        text = data[since:].decode("utf-8-sig" if since == 0 else "utf-8", errors="replace")
        cursor = len(data)
    else:
        cursor = 0
    omitted = 0
    if len(text) > max_chars:
        omitted = len(text) - max_chars
        text = text[-max_chars:]
    if finished is not None:
        state = "finished"
    elif meta.get("pid") and psutil.pid_exists(meta["pid"]):
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
