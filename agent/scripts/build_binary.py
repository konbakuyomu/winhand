"""Build winhand as a standalone binary (PyInstaller onedir) and prove it needs no Python.

    uv run --group build python scripts/build_binary.py [--smoke] [--zip]

Each run writes only below a fresh `agent/.build/winhand-<stamp>/` directory. The smoke
test starts the packaged executable with every Python/uv location removed from PATH and
PYTHON* variables cleared, then drives it the way real clients do: CLI commands, an MCP
stdio handshake, a one-shot `run`, and an interactive shell session (ConPTY on Windows).
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

AGENT = Path(__file__).resolve().parents[1]
REPO = AGENT.parent
NAME = "winhand"
IS_WINDOWS = os.name == "nt"
ICON = REPO / "desktop/windows/Assets/winhand.ico"
# packages whose data files, native libraries or metadata PyInstaller cannot see statically
COLLECT_ALL = ["winhand", "pyte"] + (["winpty"] if IS_WINDOWS else [])
COLLECT_DATA = ["fastmcp", "mcp"]  # not collect-all: their optional CLIs import typer/rich extras
HIDDEN = ["tkinter", "serial.urlhandler.protocol_loop", "serial.urlhandler.protocol_socket"]
COPY_METADATA = ["fastmcp", "mcp", "pydantic", "pydantic-core", "httpx", "uvicorn", "websockets", "starlette"]


def project_version() -> str:
    text = (AGENT / "pyproject.toml").read_text(encoding="utf-8")
    return re.search(r'^version = "([^"]+)"', text, re.MULTILINE).group(1)


def windows_version_file(path: Path, version: str) -> None:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    numbers = (*map(int, version.split(".")[:3]), 0)
    strings = {
        "CompanyName": "winhand",
        "ProductName": "winhand",
        "FileDescription": "winhand agent",
        "FileVersion": version,
        "ProductVersion": version,
        "InternalName": NAME,
        "OriginalFilename": f"{NAME}.exe",
    }
    info = VSVersionInfo(
        ffi=FixedFileInfo(filevers=numbers, prodvers=numbers, mask=0x3F, flags=0, OS=0x40004, fileType=1),
        kids=[
            StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings.items()])]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
    path.write_text(str(info), encoding="utf-8")


def build(run_dir: Path, version: str) -> Path:
    entry = run_dir / "entry.py"
    entry.write_text("from winhand.cli import main\nraise SystemExit(main())\n", encoding="utf-8")
    command = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onedir", "--console",
        "--name", NAME,
        "--distpath", str(run_dir / "dist"),
        "--workpath", str(run_dir / "work"),
        "--specpath", str(run_dir),
    ]  # fmt: skip
    for module in HIDDEN:
        command += ["--hidden-import", module]
    for package in COLLECT_DATA:
        command += ["--collect-data", package]
    for package in COLLECT_ALL:
        command += ["--collect-all", package]
    for package in COPY_METADATA:
        command += ["--copy-metadata", package]
    if IS_WINDOWS:
        version_file = run_dir / "version.txt"
        windows_version_file(version_file, version)
        command += ["--version-file", str(version_file), "--icon", str(ICON)]
    subprocess.run([*command, str(entry)], cwd=AGENT, check=True)
    executable = run_dir / "dist" / NAME / (NAME + (".exe" if IS_WINDOWS else ""))
    if not executable.is_file():
        raise RuntimeError(f"PyInstaller did not produce {executable}")
    return executable


def python_free_env(home: Path) -> dict[str, str]:
    """Environment with no route to any Python install, so a hidden dependency fails loudly."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.upper().startswith(("PYTHON", "VIRTUAL_ENV", "UV_", "CONDA"))
    }
    if not IS_WINDOWS:  # /usr/bin always holds a system python3; the Windows job is the real check
        env["HOME"] = str(home)
        return env
    keep = []
    for entry in env.get("PATH", "").split(os.pathsep):
        folder = Path(entry)
        has_python = any((folder / n).exists() for n in ("python.exe", "python3", "python", "uv.exe", "uv"))
        if entry and not has_python:
            keep.append(entry)
    env["PATH"] = os.pathsep.join(keep)
    env["HOME"] = env["USERPROFILE"] = str(home)  # empty ~/.winhand: no user config leaks in
    return env


class Mcp:
    """Minimal MCP stdio client, enough to prove the frozen server works end to end."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: Path):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.next_id = 0

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(b"")

    def send(self, method: str, params: dict | None = None, *, notify: bool = False) -> dict | None:
        message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()
        if notify:
            return None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=deadline - time.monotonic())
            except queue.Empty:
                break
            if not line:
                raise RuntimeError(
                    "server exited: " + self.proc.stderr.read().decode(errors="replace")[-2000:]
                )
            reply = json.loads(line)
            if reply.get("id") == self.next_id:
                if "error" in reply:
                    raise RuntimeError(f"{method} failed: {reply['error']}")
                return reply["result"]
        raise RuntimeError(f"no reply to {method} within 60s")

    def call(self, tool: str, **arguments) -> dict:
        result = self.send("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"{tool} returned an error: {result}")
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def close(self) -> None:
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def smoke(executable: Path, run_dir: Path, version: str) -> list[str]:
    home = run_dir / "smoke-home"
    home.mkdir()
    env = python_free_env(home)
    checks = []

    def cli(*args: str) -> str:
        done = subprocess.run(
            [str(executable), *args], env=env, cwd=home, capture_output=True, timeout=60, check=False
        )
        out = done.stdout.decode("utf-8", errors="replace")
        if done.returncode != 0:
            raise RuntimeError(
                f"`winhand {' '.join(args)}` exit {done.returncode}: {done.stderr.decode(errors='replace')[-2000:]}"
            )
        return out

    if cli("--version").strip() != f"winhand {version}":
        raise RuntimeError("packaged binary reports a different version")
    checks.append("version")
    if "gdb" not in cli("profiles"):
        raise RuntimeError("built-in profiles are missing from the bundle")
    checks.append("profiles")
    json.loads(cli("doctor"))
    checks.append("doctor")
    if IS_WINDOWS:
        cli("secret-dialog", "--check")  # tkinter + Tcl/Tk data for session_prompt_user
        checks.append("tk")

    mcp = Mcp([str(executable), "stdio"], env, home)
    try:
        init = mcp.send(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        )
        if init["serverInfo"]["name"] != NAME:
            raise RuntimeError(f"unexpected server: {init['serverInfo']}")
        mcp.send("notifications/initialized", notify=True)
        tools = {t["name"] for t in mcp.send("tools/list")["tools"]}
        missing = {"run", "session_start", "session_wait", "fs_read", "sys_info"} - tools
        if missing:
            raise RuntimeError(f"tools missing: {missing}")
        checks.append(f"mcp:{len(tools)} tools")

        ran = mcp.call("run", command="echo winhand-smoke")
        if "winhand-smoke" not in ran.get("stdout", ""):
            raise RuntimeError(f"run did not execute: {ran}")
        checks.append("run")

        started = mcp.call("session_start", startup_wait_s=30)
        sid = started["session"]["id"]
        if started.get("state") != "awaiting_input":
            raise RuntimeError(f"shell never reached its prompt: {started}")
        text = "echo pty-$(6*7)" if IS_WINDOWS else "echo pty-$((6*7))"
        sent = mcp.call("session_send", id=sid, text=text, submit=True, wait_s=30)
        if "pty-42" not in sent.get("output", ""):
            waited = mcp.call("session_wait", ids=sid, patterns=["pty-42"], timeout_s=30)
            if (waited.get("hit") or {}).get("condition") != "pattern":
                raise RuntimeError(f"interactive shell did not answer: {sent} / {waited}")
        mcp.call("session_stop", id=sid)
        checks.append("pty-session")
    finally:
        mcp.close()
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--smoke", action="store_true", help="verify the bundle without any Python on PATH")
    parser.add_argument("--zip", action="store_true", help="also write winhand-<version>-<platform>.zip")
    parser.add_argument("--result-file", type=Path, help="write a JSON manifest of the build")
    args = parser.parse_args()

    version = project_version()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = AGENT / ".build" / f"{NAME}-{stamp}-{os.getpid()}"
    run_dir.mkdir(parents=True)
    executable = build(run_dir, version)
    result = {"version": version, "bundle": str(executable.parent), "executable": str(executable)}
    if args.smoke:
        result["smoke"] = smoke(executable, run_dir, version)
    if args.zip:
        platform = f"{'windows' if IS_WINDOWS else sys.platform}-{os.environ.get('PROCESSOR_ARCHITECTURE', os.uname().machine if hasattr(os, 'uname') else 'x64').lower()}"
        archive = shutil.make_archive(
            str(run_dir / f"{NAME}-{version}-{platform}"), "zip", executable.parent.parent, NAME
        )
        result["zip"] = archive
    if args.result_file:
        args.result_file.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"binary build failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
