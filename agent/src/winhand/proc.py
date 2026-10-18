"""One-shot commands, process inspection and machine facts."""

from __future__ import annotations

import functools
import getpass
import itertools
import locale
import os
import platform
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil

from . import config, winenv
from .session.text import clip, strip_ansi

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


def _legacy_encodings() -> list[str]:
    """Code pages a program that ignores UTF-8 may print in: the machine's ANSI and OEM pages
    (cp936 on Chinese Windows), then GB18030 as the superset that reads most CJK output."""
    found = []
    if winenv.IS_WINDOWS:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        found += [f"cp{kernel32.GetACP()}", f"cp{kernel32.GetOEMCP()}"]
    found += [locale.getpreferredencoding(False), "gb18030"]
    return [e for e in dict.fromkeys(found) if e.lower().replace("-", "") not in ("utf8", "cp65001")]


def _decode_line(line: bytes, legacy: list[str], last: bool) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError as exc:
        # output read while the program is still writing can end in half a character
        if last and exc.reason == "unexpected end of data":
            try:
                return line[: exc.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
    for enc in legacy:
        try:
            return line.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return line.decode("utf-8", errors="replace")


def _decode(data: bytes) -> str:
    """UTF-8 when it is; otherwise decided line by line, so one program printing in the
    legacy code page does not garble the UTF-8 lines around it."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    legacy = _legacy_encodings()
    lines = data.splitlines(keepends=True)
    return "".join(_decode_line(line, legacy, i == len(lines) - 1) for i, line in enumerate(lines))


# Windows reports crashes as NTSTATUS codes, which Python shows as large unsigned numbers.
_NTSTATUS = {
    0xFFFFFFFF: "-1: the process ended abnormally (for ssh: the connection dropped or the remote side failed)",
    0xC000013A: "STATUS_CONTROL_C_EXIT: interrupted by Ctrl+C or its console was closed",
    0xC0000005: "STATUS_ACCESS_VIOLATION: the program crashed",
    0xC0000409: "STATUS_STACK_BUFFER_OVERRUN: the program aborted itself (fail-fast crash)",
    0xC00000FD: "STATUS_STACK_OVERFLOW: the program crashed",
    0xC000001D: "STATUS_ILLEGAL_INSTRUCTION: the program crashed",
    0xC0000017: "STATUS_NO_MEMORY: out of memory",
    0xC0000135: "STATUS_DLL_NOT_FOUND: a DLL the program needs is missing",
    0xC0000142: "STATUS_DLL_INIT_FAILED: a DLL failed to initialise (often no desktop/session for a GUI part)",
    0x40010004: "DBG_TERMINATE_PROCESS: the process was killed",
}


def exit_info(code: int | None) -> tuple[int | None, str | None]:
    """A process exit code as a signed number, plus what it means when it is a known crash code."""
    if code is None:
        return None, None
    if code < 0 and not winenv.IS_WINDOWS:
        try:
            return code, f"killed by signal {signal.Signals(-code).name}"
        except ValueError:
            return code, None
    unsigned = code & 0xFFFFFFFF
    signed = unsigned - (1 << 32) if unsigned >= 1 << 31 else unsigned
    note = _NTSTATUS.get(unsigned) if winenv.IS_WINDOWS else None
    if note and unsigned != 0xFFFFFFFF:
        note = f"0x{unsigned:08X} {note}"
    return signed, note


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


# ------------------------------------------------------------------ scripts

SCRIPT_SHELLS = ("pwsh", "powershell", "cmd", "python", "bash", "wsl")


def default_script_shell() -> str:
    if winenv.IS_WINDOWS:
        return "pwsh" if shutil.which("pwsh") else "powershell"
    return "bash"


def _git_bash(path: str | None) -> str | None:
    """Git for Windows' bash. The bash.exe in System32 (and WindowsApps) is WSL, not this."""
    found = shutil.which("bash", path=path)
    if found and not re.search(r"(?i)[\\/](system32|windowsapps)[\\/]", found):
        return found
    roots = [os.environ.get(k) for k in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")]
    candidates = [Path(r) / "Git" / "bin" / "bash.exe" for r in roots if r]
    candidates.append(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "bin" / "bash.exe")
    scoop = os.environ.get("SCOOP") or str(Path.home() / "scoop")
    candidates.append(Path(scoop) / "apps" / "git" / "current" / "bin" / "bash.exe")
    git = shutil.which("git", path=path)
    if git:
        if Path(git).parent.name.lower() == "shims":  # a scoop shim: <scoop>\shims\git.exe
            candidates.append(Path(git).parent.parent / "apps" / "git" / "current" / "bin" / "bash.exe")
        candidates.append(Path(git).resolve().parent.parent / "bin" / "bash.exe")
    found = next((str(c) for c in candidates if c.is_file()), None)
    return found or (_bash_beside_git(git) if git else None)


@functools.cache
def _bash_beside_git(git: str) -> str | None:
    """bash.exe of the Git that `git` runs, found from its exec path (<root>\\mingw64\\libexec\\git-core):
    shims and launchers (mise, chocolatey, a copied git.exe) hide where Git is installed."""
    try:
        done = subprocess.run(
            [git, "--exec-path"],
            capture_output=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    exec_path = Path(done.stdout.decode("utf-8", "replace").strip())
    for root in exec_path.parents:
        if (root / "bin" / "bash.exe").is_file() and (root / "cmd").is_dir():
            return str(root / "bin" / "bash.exe")
    return None


def _python(path: str | None) -> str:
    """A real Python: the python.exe in WindowsApps is only the Microsoft Store installer stub."""
    for directory in (path or os.environ.get("PATH", "")).split(os.pathsep):
        if not directory or re.search(r"(?i)[\\/]windowsapps[\\/]?$", directory):
            continue
        found = shutil.which("python", path=directory) or shutil.which("python3", path=directory)
        if found:
            return found
    py = shutil.which("py", path=path) if winenv.IS_WINDOWS else None
    if py:
        return py
    if not getattr(sys, "frozen", False):
        return sys.executable
    raise ValueError("Python is not installed (only the Microsoft Store placeholder was found)")


def _script_dir() -> Path:
    directory = config.home() / "tmp"
    directory.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 86400
    for old in directory.glob("run-*"):  # left behind by a crash or a reinstall
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    return directory


# interpreters `run(command=<program>, script=...)` knows: the program's name -> how its script is run
_INTERPRETER = re.compile(
    r"^(?:(?P<python>python[\d.]*w?|py|pyw)|(?P<pwsh>pwsh|powershell)|(?P<cmd>cmd)|(?P<bash>bash|sh|zsh)"
    r"|(?P<node>node|nodejs)|(?P<other>ruby|perl|php|lua|rscript|tclsh|wish))(?:\.exe)?$",
    re.IGNORECASE,
)
_OTHER_SUFFIX = {"ruby": ".rb", "perl": ".pl", "php": ".php", "lua": ".lua", "rscript": ".R", "tclsh": ".tcl"}


def interpreter_kind(program: str) -> str | None:
    """'python', 'pwsh', 'cmd', 'bash', 'node' or another interpreter's name for a program path."""
    m = _INTERPRETER.match(re.split(r"[\\/]", program.strip().strip('"'))[-1])
    if not m:
        return None
    kind = m.lastgroup
    return m.group(kind).lower().removesuffix(".exe") if kind == "other" else kind


def prepare_script(
    script: str,
    shell: str | None,
    env: dict[str, str],
    args: list[str] | None = None,
    program: str | None = None,
) -> tuple[list[str], Path | None, str | None]:
    """Write `script` to a temporary file for `shell` and return (argv, file, stdin).
    `program` names the interpreter to use instead (a venv's python.exe, node, pwsh ...).

    The script never passes through a command line, so quotes, `$variables`, backslashes and
    non-ASCII text arrive exactly as written."""
    exe: str | None = None
    if program:
        kind = interpreter_kind(program)
        if kind is None:
            raise ValueError(
                f"command + script runs the script with an interpreter (python, pwsh, powershell, cmd, "
                f"bash, node, ruby, perl ...); {program!r} is not one. Give the script alone (shell=...), "
                "or command with args"
            )
        exe = program
        if kind == "python" and re.search(r"(?i)[\\/]windowsapps[\\/]", program):
            exe = _python(env.get("PATH"))  # the Microsoft Store stub, not a Python
        if kind not in SCRIPT_SHELLS:
            directory = _script_dir()
            suffix = ".js" if kind == "node" else _OTHER_SUFFIX.get(kind, ".txt")
            path = directory / f"run-{uuid.uuid4().hex[:12]}{suffix}"
            path.write_text(script.replace("\r\n", "\n"), encoding="utf-8")
            return [exe, str(path), *(args or [])], path, None
        shell = "powershell" if kind == "pwsh" and "powershell" in program.lower() else kind
    shell = (shell or default_script_shell()).lower()
    if shell not in SCRIPT_SHELLS:
        raise ValueError(f"shell must be one of {', '.join(SCRIPT_SHELLS)}")
    path_var = env.get("PATH")
    extra = list(args or [])
    body = script.replace("\r\n", "\n")
    if shell == "wsl":
        exe = shutil.which("wsl", path=path_var)
        if not exe:
            raise ValueError("WSL is not installed (wsl.exe not found)")
        return [exe, "-e", "bash", "-s", "--", *extra], None, body
    name = f"run-{uuid.uuid4().hex[:12]}"
    directory = _script_dir()
    if shell in ("pwsh", "powershell"):
        exe = exe or shutil.which(shell, path=path_var)
        if not exe:
            raise ValueError(f"{shell} is not installed")
        path = directory / f"{name}.ps1"
        # Windows PowerShell 5.1 reads a script without BOM in the ANSI code page
        path.write_text(winenv._PWSH_UTF8 + "\n" + body, encoding="utf-8-sig")
        argv = [
            exe,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ]
    elif shell == "cmd":
        if not winenv.IS_WINDOWS:
            raise ValueError("cmd scripts run on Windows")
        path = directory / f"{name}.cmd"
        path.write_bytes(("@chcp 65001>nul\n" + body).replace("\n", "\r\n").encode("utf-8"))
        comspec = exe or env.get("ComSpec") or env.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
        argv = [comspec, "/d", "/c", str(path)]
    elif shell == "python":
        exe = exe or _python(path_var)
        path = directory / f"{name}.py"
        path.write_text(body, encoding="utf-8")
        argv = [exe, "-X", "utf8", str(path)]
    else:  # bash
        exe = exe or (_git_bash(path_var) if winenv.IS_WINDOWS else shutil.which("bash", path=path_var))
        if not exe:
            raise ValueError(
                "bash not found (on Windows this needs Git for Windows; use shell='wsl' for WSL)"
            )
        path = directory / f"{name}.sh"
        path.write_bytes(body.encode("utf-8"))
        argv = [exe, str(path).replace("\\", "/")]
    return [*argv, *extra], path, None


# A command line that is itself one PowerShell invocation: `powershell -NoProfile -Command "..."`.
# Running it through the outer shell expands its $variables before the inner PowerShell sees them.
_NESTED_PS = re.compile(
    r"""^\s*(?:&\s*)?
    (?P<exe>"[^"]*?(?:pwsh|powershell)(?:\.exe)?"|'[^']*?(?:pwsh|powershell)(?:\.exe)?'|[^\s"']*(?:pwsh|powershell)(?:\.exe)?)
    (?P<opts>(?:\s+-(?!(?:c|command)\b)[A-Za-z]+(?:\s+(?!-)[^\s"';|&]+)?)*)
    \s+-(?:c|command)\s+
    (?P<body>"(?:[^"\\]|\\.|"")*")
    \s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)
_LOOKS_NESTED = re.compile(r"(?i)\b(?:pwsh|powershell)(?:\.exe)?\b.*\s-(?:c|command)\s+\"")


def _windows_arg(quoted: str) -> str:
    """One double-quoted argument as the C runtime (and PowerShell itself) parses it."""
    text, out, i, inside = quoted, [], 0, False
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            j = i
            while j < len(text) and text[j] == "\\":
                j += 1
            run_len = j - i
            if j < len(text) and text[j] == '"':
                out.append("\\" * (run_len // 2))
                if run_len % 2:
                    out.append('"')
                    i = j + 1
                else:
                    i = j
                continue
            out.append("\\" * run_len)
            i = j
            continue
        if ch == '"':
            if inside and i + 1 < len(text) and text[i + 1] == '"':
                out.append('"')
                i += 2
                continue
            inside = not inside
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def nested_powershell(command: str, env: dict[str, str] | None = None) -> list[str] | None:
    """argv for a command line that is exactly one `powershell ... -Command "..."` call, so it can
    run without an outer shell; None when it is anything more (pipes, several statements)."""
    m = _NESTED_PS.match(command)
    if not m:
        return None
    exe = winenv.resolve_executable(m.group("exe").strip("\"'"), env)
    if not os.path.dirname(exe):
        return None
    body = _windows_arg(m.group("body"))
    return [exe, *m.group("opts").split(), "-Command", f"{winenv._PWSH_UTF8}; {body}"]


_PYTHON_EXE = re.compile(r"(?i)^python(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
_POWERSHELL_EXE = re.compile(r"(?i)^(?:pwsh|powershell)(?:\.exe)?$")


def _python_isolated(args: list[str]) -> bool:
    """Does this python command line use -I/-E (ignore PYTHON* variables) without -X utf8?"""
    isolated = False
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-") or a in ("-", "--"):
            break
        if a.startswith(("-X", "-W")):
            value = a[2:] or (args[i + 1] if i + 1 < len(args) else "")
            if a.startswith("-X") and value.split("=")[0] == "utf8":
                return False
            i += 1 if a[2:] else 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        letters = a[1:]
        isolated = isolated or "I" in letters or "E" in letters
        if "c" in letters or "m" in letters:
            break
        i += 1
    return isolated


def adjust_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Small fixes for programs started directly (with `args`). Returns (argv, notes)."""
    exe, args = argv[0], argv[1:]
    name = re.split(r"[\\/]", exe)[-1]
    notes: list[str] = []
    if _POWERSHELL_EXE.match(name):
        for i, a in enumerate(args[:-1]):
            if a.lower() in ("-command", "-c") and args[i + 1] != "-" and "OutputEncoding" not in args[i + 1]:
                args = [*args[: i + 1], f"{winenv._PWSH_UTF8}; {args[i + 1]}", *args[i + 2 :]]
                break
    elif _PYTHON_EXE.match(name) and _python_isolated(args):
        args = ["-X", "utf8", *args]
        notes.append("added -X utf8: -I/-E make Python ignore PYTHONUTF8/PYTHONIOENCODING")
    elif name.lower().endswith((".cmd", ".bat")) and any("\n" in a or "\r" in a for a in args):
        raise ValueError(
            f"{name} is a batch file and Windows cannot pass it arguments that contain line breaks; "
            "send long text through stdin or a file, or use run(script=...)"
        )
    return [exe, *args], notes


def _failure_hint(command: str | None, nested_direct: bool, stderr: str) -> str | None:
    if "batch file arguments are invalid" in stderr:
        return (
            "a .cmd/.bat shim (mise, scoop, npm ...) refused an argument with line breaks or special "
            "characters: pass long text through stdin or a file, or use run(script=...)"
        )
    if (
        command
        and not nested_direct
        and _LOOKS_NESTED.search(command)
        and re.search(r"ParserError|is not recognized|empty pipe element|InvalidVariableReference", stderr)
    ):
        return (
            "this command line is run by PowerShell, which expanded the $variables inside the nested "
            'powershell -Command "..." before the inner PowerShell saw them. Use run(script=...), or '
            "run(command='powershell', args=['-NoProfile', '-Command', '...'])"
        )
    return None


# ------------------------------------------------------------------ run

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
        self.raw = False
        self.temp_file: Path | None = None
        self.notes: list[str] = []
        self.command: str | None = None
        self.nested_direct = False

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
        self._cleanup()
        return True

    def _cleanup(self) -> None:
        if self.temp_file is not None:
            try:
                self.temp_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return  # still locked: _script_dir sweeps it later
            self.temp_file = None

    def kill(self) -> None:
        _kill_tree(self.proc.pid)
        self.wait(5)

    def text(self) -> tuple[str, str]:
        with self.lock:
            out, err = _decode(bytes(self.out)), _decode(bytes(self.err))
        if not self.raw:
            out, err = strip_ansi(out), strip_ansi(err)
        return out, err


_running: dict[str, _Running] = {}
_run_ids = itertools.count(1)


def run(
    command: str | None = None,
    args: list[str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 120,
    env: dict[str, str] | None = None,
    stdin: str | None = None,
    output_limit: int = 30000,
    reply_within_s: float = REPLY_WITHIN_S,
    *,
    script: str | None = None,
    shell: str | None = None,
    raw: bool = False,
    lang: str | None = None,
) -> dict:
    """Run to completion. With `args` the program is executed directly (no shell
    quoting issues); without, `command` is a command line for the platform shell;
    `script` is written to a file and run by `shell`.
    Still running after `reply_within_s`, it keeps going (until `timeout_s`) and the
    answer says so; job_status(id) returns the rest."""
    extra_env = dict(env or {})
    if lang:
        if lang.lower() != "en":
            return {"error": "lang supports 'en' (English messages from dotnet/MSBuild/Visual Studio)"}
        extra_env = {**winenv.ENGLISH, **extra_env}
    full_env = winenv.build_env(extra_env)
    notes: list[str] = []
    temp_file: Path | None = None
    nested_direct = False
    try:
        from .redact import refuse_placeholders

        refuse_placeholders(command, script, stdin, *(args or []))
        if script is not None:
            # command + script: the script runs with that interpreter (a project's venv python ...)
            program = winenv.resolve_executable(command, full_env, cwd) if command else None
            if program and shell:
                return {"error": "give the interpreter as command or as shell, not both"}
            argv, temp_file, script_stdin = prepare_script(script, shell, full_env, args, program)
            if script_stdin is not None:
                if stdin is not None:
                    return {"error": "shell='wsl' reads the script from stdin, so stdin cannot be used too"}
                stdin = script_stdin
        elif not command:
            return {"error": "give command (with or without args) or script"}
        elif args is None:
            direct = nested_powershell(command, full_env)
            if direct:
                argv, nested_direct = direct, True
                notes.append(
                    "ran the nested PowerShell command directly, without an outer shell, so its "
                    "$variables reached it unchanged"
                )
            else:
                argv = winenv.shell_command(command)
        else:
            argv, notes = adjust_argv([winenv.resolve_executable(command, full_env, cwd), *args])
    except (ValueError, OSError) as exc:
        return {"error": str(exc)}
    if cwd and not os.path.isdir(os.path.expanduser(cwd)):
        if temp_file:
            temp_file.unlink(missing_ok=True)
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
        if temp_file:
            temp_file.unlink(missing_ok=True)
        return {"error": f"cannot start {argv[0]!r}: {exc}", "argv": argv}
    job = _Running(f"run-{next(_run_ids)}", proc, argv, time.monotonic() + timeout_s)
    job.raw, job.temp_file, job.notes = raw, temp_file, notes
    job.command, job.nested_direct = command, nested_direct
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
    result = {
        "still_running": True,
        "id": job.id,
        "pid": proc.pid,
        "stdout": stdout,
        "stderr": stderr,
        "cursor": len(out),
        "duration_ms": int((time.monotonic() - job.started) * 1000),
        "next": (
            f"Still running after {reply_within_s:.0f}s, so this is its output so far; it keeps running "
            f"(killed after timeout_s={timeout_s:.0f}s). job_status(id='{job.id}', since=cursor, wait_s=40) "
            f"waits for the rest and the exit code; job_stop(id='{job.id}') ends it."
        ),
    }
    if notes:
        result["notes"] = notes
    return result


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


def _exit_fields(job: _Running, stderr: str) -> dict:
    code, note = exit_info(job.proc.returncode)
    fields: dict = {"exit_code": code}
    if note:
        fields["exit_note"] = note
    if code:
        hint = _failure_hint(job.command, job.nested_direct, stderr)
        if hint:
            fields["hint"] = hint
    return fields


def _result(job: _Running, output_limit: int) -> dict:
    out, err = job.text()
    stdout, cut_out = clip(out, output_limit, keep="both")
    stderr, cut_err = clip(err, output_limit // 2, keep="both")
    result = {
        **_exit_fields(job, err),
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
    if job.notes:
        result["notes"] = job.notes
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
        result.update(_exit_fields(job, err))
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


def _shim_summary(folders: list[str]) -> str:
    """The scoop/mise shim folders on PATH, in one short line: the shim folders themselves, and
    only a count of mise's per-tool install folders (mise activate puts one per tool on PATH)."""
    shims, tools = [], 0
    for folder in dict.fromkeys(f.strip().rstrip("\\/") for f in folders if f.strip()):
        if re.split(r"[\\/]", folder)[-1].lower() == "shims":
            shims.append(folder)
        elif re.search(r"(?i)[\\/]mise[\\/]installs[\\/]", folder):
            tools += 1
    if not shims and not tools:
        return "no scoop/mise shim folders on PATH"
    found = "; ".join(shims) or "no shims folder"
    if tools:
        found += f" (+{tools} mise tool folder{'s' if tools != 1 else ''})"
    return found + " — .cmd shims cannot take multi-line arguments; use run(script=...)"


def doctor() -> list[dict]:
    """Checks for the Windows setups that tripped AI sessions before, each with a fix."""
    checks: list[dict] = []

    def add(check: str, ok: bool, detail: str, fix: str = "") -> None:
        item = {"check": check, "ok": ok, "detail": detail}
        if fix and not ok:
            item["fix"] = fix
        checks.append(item)

    path = os.environ.get("PATH", "")
    if winenv.IS_WINDOWS:
        import ctypes
        import winreg

        pwsh = shutil.which("pwsh")
        add(
            "pwsh",
            bool(pwsh),
            pwsh or "only Windows PowerShell 5.1",
            "winget install Microsoft.PowerShell — PowerShell 7 handles UTF-8 and native arguments better",
        )
        acp, oem = ctypes.windll.kernel32.GetACP(), ctypes.windll.kernel32.GetOEMCP()
        add(
            "code_page",
            acp == 65001,
            f"ANSI cp{acp}, OEM cp{oem}",
            "winhand decodes legacy output, but programs that print raw bytes may still garble text; "
            "Region settings > 'Use Unicode UTF-8 for worldwide language support' makes it UTF-8",
        )
        add(
            "redirection_guard",
            not winenv.redirection_guard_enforced(),
            "enforced" if winenv.redirection_guard_enforced() else "not enforced",
            "restart winhand from the Start menu (an installer started it with RedirectionGuard, which "
            "breaks scoop/mise shims)",
        )
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem"
            ) as k:
                long_paths = winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
        except OSError:
            long_paths = False
        add(
            "long_paths",
            long_paths,
            "enabled" if long_paths else "disabled",
            "winhand's own file tools cope, but other programs fail past 260 characters; enable "
            "LongPathsEnabled (an admin job: job_start elevated=true)",
        )
        bash = _git_bash(path)
        add("git_bash", bool(bash), bash or "not found", "install Git for Windows for run(shell='bash')")
        wsl = shutil.which("wsl")
        add("wsl", bool(wsl), wsl or "not installed", "wsl --install, if Linux tooling is needed")
        add("shims", True, _shim_summary(path.split(os.pathsep)))
    ssh = shutil.which("ssh")
    ssh_config = os.path.join(os.path.expanduser("~"), ".ssh", "config")
    add(
        "ssh",
        bool(ssh),
        f"{ssh or 'not found'}; config {'present' if os.path.isfile(ssh_config) else 'absent'}",
        "install the OpenSSH client (Settings > Optional features) for remote_run",
    )
    rg = shutil.which("rg")
    add(
        "ripgrep",
        bool(rg),
        rg or "not found",
        "winget install BurntSushi.ripgrep.MSVC (fs_search is slower without it)",
    )
    home = os.path.expanduser("~")
    uv = shutil.which("uv")
    add(
        "non_ascii_home",
        home.isascii() or not uv,
        home,
        "uv-created virtual environments can fail with 'trampoline failed to canonicalize script path' "
        "under non-ASCII folders; keep projects and UV_PROJECT_ENVIRONMENT under an ASCII path",
    )
    return checks
