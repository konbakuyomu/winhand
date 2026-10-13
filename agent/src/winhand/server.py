"""The MCP surface: a small set of flat tools over the session engine, files,
processes and the desktop."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, Field

from . import (
    __version__,
    coalesce,
    fs,
    local_mcp,
    media,
    patching,
    proc,
    profiles,
    remote,
    secret_store,
    tools_desktop,
    transfer,
    venv_repair,
    winenv,
    workspace,
)
from .config import Config
from .config import load as load_config
from .guard import GuardActions
from .history import RecentCalls
from .proc import split_command
from .redact import RedactOutput
from .secret_prompt import PromptUnavailable, ask_secret
from .session import SessionManager, SessionSpec, TransportError, wait_for
from .session.session import TRANSPORTS
from .session.wait import MAX_WAIT_S
from .tools_desktop import media_result

GUIDE_FULL = """\
winhand drives the Windows machine it runs on.

Quick rules
- One-shot command that finishes by itself -> `run`:
    run(command='git', args=['log', '-3'])        program + args, no shell, no quoting problems
    run(script='...', shell='pwsh')                a multi-line script (pwsh | powershell | cmd |
                                                   python | bash | wsl), written to a file, so $vars,
                                                   quotes and Chinese text arrive exactly as written
    run(command='...')                             one command line for PowerShell
  Never put `powershell -Command "..."` inside a command line: the outer PowerShell expands its
  $variables first. Use script= or args= instead. Output comes back as clean UTF-8 text.
  `run` answers within ~45 s; a command still going then comes back with `still_running` and an id:
  it keeps running; job_status(id, since=cursor, wait_s=40) waits for the rest and the exit code.
  Never sleep or poll in a loop to wait: job_status(wait_s=40, patterns=[...]) does it.
- Scripts for another machine over SSH: remote_run(host, script) uses this machine's keys and ssh
  config and says whether a failure was the connection, host key, key or the script.
  An interactive login: session_start(profile='ssh', vars={'host': ...}).
- Anything interactive, long-running or stateful (shells, REPLs, debuggers, gdbserver, installers,
  serial/telnet consoles, full-screen TUIs) -> a session:
    session_start -> session_send / session_wait -> session_read / session_screen -> session_stop
- Every session result carries `state` + `reason` + `next`:
    running | awaiting_input | needs_user | idle | blocked | exited
  `needs_user` means a password/one-time code/browser sign-in: never invent it. Give URLs to the user,
  or call session_prompt_user so they type a secret into a masked dialog on the machine.
- A token or password that must be stored on the machine: secret_prompt(message, save_to=...) lets the
  person type it into a masked dialog; you never see it. «redacted:…» in results marks masked values:
  never write such text back to a file.
- session_wait ORs its conditions across several sessions at once (e.g. gdb prompt OR serial "PANIC").
  A timeout is not a failure: call it again to keep waiting; already-returned output is not repeated.
  One wait at a time is enough: identical parallel waits share one answer.
- After a connection error, call history() before resending anything: it shows whether your call
  ran, its run id and exit code. For sessions also session_read(since=the last cursor you received).
- Full-screen programs (menuconfig, htop, vim, setup wizards): after keys, session_wait(screen_stable_ms=300)
  then session_screen; `highlighted` shows the selected row.
- Profiles (profile_list) know prompts, exit commands and quirks of common tools: gdb, pyocd-gdbserver,
  pyocd-commander, openocd, jlink, rtthread-msh, serial, telnet, ssh, menuconfig, python, shell.
- Files: fs_read (numbered; tail=N for logs) / fs_edit (exact replace, several edits at once) /
  fs_write / fs_search (content) / fs_find (names) / fs_list (sort='mtime', newer_than='2h') /
  fs_stat (hash='sha256') / fs_op (copy, move, delete to the Recycle Bin, mkdir) /
  fs_apply_patch (a unified diff onto a work tree: encoding, CRLF and -p handled, all or nothing).
  Prefer these over shell commands for files. fs_read returns sha256: pass it as expected_sha256
  to fs_write/fs_edit when someone else may change the file meanwhile.
  fs_read also shows images and reads PDF (scans as page images), Word, PowerPoint and Excel.
  fs_send hands any file to you as-is; fs_write_bytes writes binary content here;
  fs_pick lets the person choose files in a dialog (their way to "upload" to you).
  Moving more than a few KB between your own sandbox/shell and this machine: fs_upload_url /
  fs_download_url give a one-time link for curl, so the bytes never pass through the conversation
  (never paste base64 in parts).
- The desktop like a person: screenshot (desktop, monitor, region or a window) -> input
  (click/type/keys/scroll/drag in the screenshot's pixels) or, more reliably, ui (inspect a
  window's controls, then invoke/set_value/toggle by name; also classic Win32/WinForms/Delphi
  programs and installers, even in the background). window lists/focuses windows;
  clipboard reads/writes text, images and copied files.
  Need to see what the person sees, or whether a program/dialog/installer is in the right state?
  Take a screenshot yourself instead of asking them for one, and operate it with ui instead of
  telling them where to click (anything sensitive: ask first). ui(action='wait') and
  window(action='wait') wait for a control or window instead of sleeping.
  ui reads back after an action: verified=true confirms the observed state; false/null requires
  inspecting the result. A button being invoked does not prove its task finished; do not blindly retry.
- job_start runs PowerShell in the background, independent of winhand (long work, anything that
  restarts or reinstalls winhand, and elevated=true for admin tasks after the person approves UAC);
  follow it with job_status(id, wait_s=40).
- Something odd about this machine (garbled text, a shim that fails, missing tools)?
  sys_info(checks=true) lists the usual suspects with fixes.
- A tool named here but missing from your tool list (winhand was updated after the conversation
  started): call(tool, arguments) reaches every current tool; help() lists them.
- Skills on this machine (listed by context(), or at the start of a cloud session): when one fits the
  task, skill(name) loads its SKILL.md and lists its files; follow it, and run its scripts here.
- Local MCP servers set up in the winhand app (pyocd-debug, a camera ...): mcp_tools() lists them and
  their tools, mcp_tools(server) gives a server's tools with parameters (starting it if need be), and
  mcp_call(server, tool, arguments) calls one; a call still going after ~40 s returns a call_id for
  mcp_call(call_id=...). Each is also its own MCP endpoint at <relay>/mcp/<name>.
- The machine's owner may have written rules and skills for agents: before working in a folder, and
  again after the conversation is compacted, call context(path=<that folder>) and follow what it
  returns (repo=<owner/name> finds the folder of a repository you only know by name). Calls that read or change the machine are checked against the owner's guard rules
  (~/.winhand/guards.toml, <project>/.winhand/guards.toml): a refusal, or a call held until the person
  at the machine says yes, is the owner's wish; do not work around it.
"""

# What a client shows the model when it connects. Claude Code keeps only the first 4096 characters
# of a server's instructions, so this is the essentials; help() returns GUIDE_FULL.
GUIDE = """\
winhand drives the Windows machine it runs on. help() has the full guide: read it once.

- One-shot command that finishes by itself -> `run`:
    run(command='git', args=['log', '-3'])   program + args, no shell, no quoting problems
    run(script='...', shell='pwsh')           multi-line script (pwsh | powershell | cmd | python | bash
                                              | wsl) written to a file: $vars, quotes, Chinese arrive as is
    run(command='...')                        one command line for PowerShell
  Never nest `powershell -Command "..."` in a command line; use script= or args=. `run` answers within
  ~45 s; if still going you get `still_running` + an id, and job_status(id, since=cursor, wait_s=40)
  waits for the rest. Never sleep or poll in a loop.
- Interactive, long-running or stateful programs (shells, REPLs, gdb, gdbserver, installers, serial or
  telnet consoles, full-screen TUIs) -> a session: session_start -> session_send / session_wait ->
  session_read / session_screen -> session_stop. profile_list knows common tools (gdb, pyocd, openocd,
  jlink, serial, ssh, menuconfig ...).
- Session results carry `state` + `reason` + `next`: running | awaiting_input | needs_user | idle |
  blocked | exited. `needs_user` is a password, code or browser sign-in: never invent it; give URLs to
  the person or call session_prompt_user. session_wait waits on several sessions at once; a timeout is
  not a failure, call it again (already returned output is not repeated).
- A token or password that must be stored here: secret_prompt(message, save_to=...) asks the person in
  a masked dialog; you never see it. «redacted:…» marks masked values: never write them back to a file.
- Files: fs_read / fs_edit / fs_write / fs_search / fs_find / fs_list / fs_stat / fs_op / fs_apply_patch;
  prefer them to shell commands. Moving more than a few KB between your sandbox and this machine:
  fs_upload_url / fs_download_url (one-time curl links), never base64 in parts.
- Full-screen programs (menuconfig, htop, vim, setup wizards): after keys, session_wait(screen_stable_ms=300)
  then session_screen; `highlighted` shows the selected row.
- The desktop: take a screenshot yourself instead of asking the person for one; operate programs with ui
  (controls by name) or input (mouse and keys).
- job_start runs PowerShell in the background, independent of winhand (long work, anything that
  restarts winhand, elevated=true for admin tasks); follow it with job_status(id, wait_s=40).
- Something odd here (garbled text, a failing shim, a missing tool)? sys_info(checks=true) lists the
  usual suspects with fixes. A tool missing from your list: call(tool, arguments) reaches it.
- Skills here: skill(name) loads one (SKILL.md + files). Local MCP servers: mcp_tools() lists their
  tools, mcp_call(server, tool, arguments) calls one.
- After a connection error, call history() before resending anything.
"""

GUIDE_CONTEXT = """\
- The machine's owner may have written rules and skills for agents: before working in a folder, and
  again after the conversation is compacted, call context(path=<that folder>, or repo=<owner/name>)
  and follow what it returns. A call refused, or held until the person at the machine says yes, by the owner's guard
  rules is their wish: do not work around it.
"""


def instructions(cfg: Config, local_stdio: bool = False) -> str:
    """The instructions for this entry point: stdio clients on this machine read their own rules,
    so they get the context() hint only when [context] stdio_hint is set."""
    flag = (cfg.context or {}).get("stdio_hint")
    hint = not local_stdio or flag is True or str(flag).strip().lower() in ("true", "1", "yes", "on")
    return GUIDE + GUIDE_CONTEXT if hint else GUIDE


def _text(value: str) -> TextContent:
    return TextContent(type="text", text=value)


def _mcp_result(result: dict) -> ToolResult:
    """A local MCP server's tools/call answer as this tool's own result: content as given."""
    try:
        parsed = CallToolResult.model_validate(result)
    except ValueError:
        text = json.dumps(result, ensure_ascii=False, default=str)
        return ToolResult(content=[_text(text)], is_error=bool(result.get("isError")))
    return ToolResult(
        content=list(parsed.content) or [_text("(the tool returned no content)")],
        structured_content=parsed.structured_content,
        is_error=parsed.is_error,
    )


Ids = Annotated[list[str] | str, Field(description="Session id, or a list of ids to watch together")]


class Edit(BaseModel):
    old: str
    new: str
    replace_all: bool = False


def _spec_from_args(transport: str, command: list[str] | str | None, **kw: Any) -> SessionSpec:
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {TRANSPORTS}")
    argv = split_command(command) if isinstance(command, str) else list(command or [])
    return SessionSpec(transport=transport, argv=argv, **{k: v for k, v in kw.items() if v is not None})


def build_server(
    cfg: Config | None = None, manager: SessionManager | None = None, *, local_stdio: bool = False
) -> FastMCP:
    cfg = cfg if cfg is not None else load_config()
    sessions = manager or SessionManager()
    mcp = FastMCP("winhand", instructions=instructions(cfg, local_stdio), version=__version__)
    local_servers = [s.name for s in cfg.servers if s.enabled]
    recent = RecentCalls()
    mcp.add_middleware(recent)
    mcp.add_middleware(RedactOutput())  # inside RecentCalls: history only ever sees masked results
    # innermost: a refusal is masked and recorded like any result; call() runs through it again
    mcp.add_middleware(GuardActions(sessions))

    async def _progress(ctx: Context | None):
        if ctx is None:
            return None

        async def report(elapsed: float, total: float) -> None:
            try:
                await ctx.report_progress(elapsed, total)
            except Exception:
                pass

        return report

    # ------------------------------------------------------------------ meta

    @mcp.tool
    async def help() -> str:
        """How to use winhand well (read once at the start)."""
        names = sorted(t.name for t in await mcp.list_tools())
        return (
            GUIDE_FULL
            + f"\nTools on this machine (winhand {__version__}): {', '.join(names)}\n"
            + "If one of them is missing from your own tool list (your client loaded the list when the "
            "conversation started, before winhand was updated), reach it with call(tool, arguments).\n"
        )

    @mcp.tool
    async def call(
        tool: Annotated[str, Field(description="Name of any winhand tool, e.g. screenshot")],
        arguments: Annotated[dict | None, Field(description="Its arguments, as that tool takes them")] = None,
    ):
        """Call any winhand tool by name. For tools missing from your tool list: clients keep the
        list they loaded when the conversation started, so tools added by a winhand update only
        show up here (help() lists them all). Pictures come back as with the tool itself."""
        if tool == "call":
            return {"error": "call cannot call itself"}
        try:
            return await mcp.call_tool(tool, arguments or {})
        except Exception as exc:  # unknown tool, bad arguments: tell the model, do not raise
            names = sorted(t.name for t in await mcp.list_tools())
            return {"error": f"{type(exc).__name__}: {exc}", "tools": names}

    @mcp.tool
    async def sys_info(
        checks: Annotated[
            bool,
            Field(description="Also check for setups that break tools (PowerShell, code page, shims ...)"),
        ] = False,
    ) -> dict:
        """Machine facts: OS, shell, code page, available dev tools, serial ports, local MCP servers.
        With checks=true, a health check of the things that commonly trip work here, with fixes."""
        info = await asyncio.to_thread(proc.sys_info)
        if checks:
            info["checks"] = await asyncio.to_thread(proc.doctor)
            entries = [s.entry for s in local_mcp.services().services.values()]
            info["checks"] += await asyncio.to_thread(venv_repair.checks, entries)
        info["winhand"] = __version__
        info["local_mcp_servers"] = {
            "names": local_servers,
            "note": "mcp_tools() lists their tools, mcp_call(server, tool, arguments) calls one",
        }
        return info

    @mcp.tool
    async def context(
        path: Annotated[
            str | None,
            Field(
                description="The folder (or a file in it) you are about to work in; omit for the machine-wide "
                "part. With repo, a relative path is inside that repository's checkout"
            ),
        ] = None,
        repo: Annotated[
            str | None,
            Field(
                description="owner/name (or a git remote URL) of the repository you work on, when you do not "
                "know where it is checked out on this machine"
            ),
        ] = None,
    ) -> dict:
        """This machine's rules and skills for agents, read the way Codex and Claude Code read them:
        the global rule files, AGENTS.md / CLAUDE.md / .claude/rules from the project down to `path`
        (with their @imports), the skills here (read a SKILL.md with fs_read when one fits), the guard
        rules in force and the local MCP servers. Call it before working in a folder and again after
        the conversation is compacted."""
        return await asyncio.to_thread(workspace.context, cfg, path, repo)

    @mcp.tool
    async def skill(
        name: Annotated[str, Field(description="The skill's name, as listed at the start or by context()")],
        path: Annotated[
            str | None, Field(description="The folder you work in: its project's own skills come first")
        ] = None,
        repo: Annotated[
            str | None, Field(description="owner/name of the repository you work on, instead of path")
        ] = None,
    ) -> dict:
        """Load one of this machine's skills, as Claude Code's Skill tool does: its SKILL.md (the
        instructions to follow), its folder and the files in it. Use it when a listed skill fits the
        task; the scripts it names run on this machine."""
        return await asyncio.to_thread(workspace.skill, cfg, name, path, repo)

    @mcp.tool
    async def mcp_tools(
        server: Annotated[
            str | None, Field(description="A local MCP server's name; omit to see all of them")
        ] = None,
    ) -> dict:
        """The MCP servers set up in the winhand app on this machine and their tools. Without server:
        each enabled server, its state and its tools as last listed (nothing is started). With server:
        starts it if need be and lists every tool with its description and parameters (inputSchema).
        Call a tool with mcp_call."""
        if not server:
            return {
                "servers": await asyncio.to_thread(local_mcp.overview),
                "next": "mcp_tools(server) for a server's tools with their parameters; "
                "mcp_call(server, tool, arguments) calls one",
            }
        listed = await local_mcp.listing(server)
        if listed is None:
            return {
                "server": server,
                "still_running": True,
                "next": f"{server} is still starting (some take minutes); call mcp_tools({server!r}) again to "
                "keep waiting",
            }
        out = {"server": server, "tools": listed.get("tools") or []}
        if listed.get("instructions"):
            out["instructions"] = listed["instructions"]
        return out

    @mcp.tool
    async def mcp_call(
        server: Annotated[
            str | None, Field(description="The local MCP server, as mcp_tools names it")
        ] = None,
        tool: Annotated[str | None, Field(description="The name of one of its tools")] = None,
        arguments: Annotated[
            dict | None, Field(description="The tool's arguments, as its inputSchema describes them")
        ] = None,
        timeout_s: Annotated[
            float,
            Field(
                description="Give up on the tool after this many seconds (starting the server not counted)"
            ),
        ] = 120,
        call_id: Annotated[
            str | None,
            Field(
                description="Keep waiting for a call that came back still_running: its call_id, nothing else"
            ),
        ] = None,
    ):
        """Call a tool of a local MCP server on this machine (mcp_tools lists them). The answer comes
        back as the server gave it, text and pictures. A call still going after ~40 s comes back with
        still_running and a call_id; mcp_call(call_id=...) waits for the rest."""
        if call_id:
            result, pending = await local_mcp.wait_call(call_id)
        else:
            if not server or not tool:
                raise ValueError("give server and tool (mcp_tools lists them), or call_id to keep waiting")
            if timeout_s <= 0:
                raise ValueError("timeout_s must be more than 0")
            result, pending = await local_mcp.start_call(server, tool, arguments or {}, timeout_s)
        if pending is not None:
            return {
                "still_running": True,
                "call_id": pending,
                "next": f"mcp_call(call_id={pending!r}) waits up to 40 s more for the answer",
            }
        return _mcp_result(result)

    # ------------------------------------------------------------ processes

    @mcp.tool
    async def run(
        command: Annotated[
            str | None,
            Field(
                description="Program name (with `args`) or one command line for PowerShell (without `args`)"
            ),
        ] = None,
        args: Annotated[
            list[str] | None, Field(description="Arguments passed verbatim, no shell involved")
        ] = None,
        cwd: str | None = None,
        timeout_s: Annotated[
            float,
            Field(description="Kill the process tree after this many seconds (it may outlive this answer)"),
        ] = 120,
        env: dict[str, str] | None = None,
        stdin: Annotated[str | None, Field(description="Text piped to stdin")] = None,
        script: Annotated[
            str | None,
            Field(
                description="A multi-line script to run instead of `command`: written to a file and run "
                "by `shell`, so quotes, $variables and non-ASCII text need no escaping"
            ),
        ] = None,
        shell: Annotated[
            str | None,
            Field(
                description="For script: pwsh (default) | powershell | cmd | python | bash (Git Bash) | wsl"
            ),
        ] = None,
        raw: Annotated[bool, Field(description="Keep ANSI colour codes in the output")] = False,
        lang: Annotated[
            str | None, Field(description="'en': ask dotnet/MSBuild/Visual Studio for English messages")
        ] = None,
    ) -> dict:
        """Run a command or script to completion and return exit code, stdout and stderr. Answers
        within about 45 s: if it is still going then, the answer has `still_running` and an id, it
        keeps running, and job_status(id, wait_s=40) waits for the rest.
        Use a session instead for anything interactive or that must keep running."""
        return await asyncio.to_thread(
            lambda: proc.run(
                command, args, cwd, timeout_s, env, stdin, script=script, shell=shell, raw=raw, lang=lang
            )
        )

    @mcp.tool
    async def remote_run(
        host: Annotated[str, Field(description="An alias from this machine's ~/.ssh/config, or user@host")],
        script: Annotated[str, Field(description="The script to run there (sent through stdin)")],
        shell: Annotated[
            str, Field(description="Remote interpreter: bash (default), sh, zsh, python3 ...")
        ] = "bash",
        args: Annotated[list[str] | None, Field(description="Arguments for the script ($1, $2 ...)")] = None,
        env: Annotated[dict[str, str] | None, Field(description="Exported before the script runs")] = None,
        cwd: Annotated[str | None, Field(description="Remote folder to run in")] = None,
        timeout_s: float = 120,
    ) -> dict:
        """Run a script on another machine over SSH from this one, with this machine's keys and ssh
        config (aliases, ProxyJump). Non-interactive (BatchMode), with keep-alives. Answers like `run`
        (still_running + job_status for long ones); on failure `ssh_error` says whether it was the
        connection, the host key, the key, or the script itself."""
        return await asyncio.to_thread(remote.remote_run, host, script, shell, args, env, cwd, timeout_s)

    @mcp.tool
    async def proc_list(
        name: Annotated[str | None, Field(description="Filter by name/command line substring")] = None,
        limit: int = 100,
    ) -> dict:
        """List running processes (pid, name, memory, start time, command line)."""
        return await asyncio.to_thread(proc.proc_list, name, limit)

    @mcp.tool
    async def proc_kill(pid: int, tree: bool = True, force: bool = False) -> dict:
        """Terminate a process (and by default its children)."""
        return await asyncio.to_thread(proc.proc_kill, pid, tree, force)

    # -------------------------------------------------------------- sessions

    @mcp.tool
    def profile_list() -> list[dict]:
        """Session profiles (built-in + ~/.winhand/profiles/*.toml) with the vars each needs."""
        return profiles.list_profiles()

    @mcp.tool
    async def session_start(
        ctx: Context,
        profile: Annotated[
            str | None,
            Field(description="Profile name from profile_list (e.g. gdb, rtthread-msh, menuconfig)"),
        ] = None,
        command: Annotated[
            list[str] | str | None, Field(description="Program + args (pty/pipe). Empty = interactive shell")
        ] = None,
        args: Annotated[
            list[str] | None, Field(description="Extra args appended to the profile's command")
        ] = None,
        vars: Annotated[
            dict[str, str] | None, Field(description="Values for profile placeholders, e.g. {'port': 'COM5'}")
        ] = None,
        transport: Annotated[
            str, Field(description="pty (real terminal, default) | pipe | serial | tcp")
        ] = "pty",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        name: str | None = None,
        port: Annotated[
            str | None, Field(description="Serial port (COM3, /dev/ttyUSB0) or pyserial URL")
        ] = None,
        baudrate: int | None = None,
        host: str | None = None,
        tcp_port: int | None = None,
        encoding: str | None = None,
        prompts: Annotated[
            list[str] | None, Field(description="Regexes for this program's input prompt")
        ] = None,
        exit_command: str | None = None,
        restart_on_exit: Annotated[
            int | None,
            Field(
                ge=0,
                le=100,
                description="Start the program again (up to this many times) when it exits by itself, "
                "e.g. a `ssh host tail -F log` watcher after the connection drops",
            ),
        ] = None,
        cols: int | None = None,
        rows: int | None = None,
        startup_wait_s: Annotated[
            float, Field(description="How long to wait for the program to settle")
        ] = 15,
        lang: Annotated[
            str | None, Field(description="'en': ask dotnet/MSBuild/Visual Studio for English messages")
        ] = None,
    ) -> dict:
        """Start an interactive session (process in a real terminal, plain pipes, serial port or TCP socket),
        wait until it settles and return its first output and state."""
        if lang:
            if lang.lower() != "en":
                return {"error": "lang supports 'en'"}
            env = {**winenv.ENGLISH, **(env or {})}
        overrides = dict(
            transport=None if profile else transport,
            cwd=cwd,
            env=env,
            name=name,
            port=port,
            baudrate=baudrate,
            host=host,
            tcp_port=tcp_port,
            encoding=encoding,
            exit_command=exit_command,
            restart_on_exit=restart_on_exit,
            cols=cols,
            rows=rows,
        )
        extras: dict = {"init": [], "notes": ""}
        try:
            if profile:
                data = profiles.get_profile(profile)
                if command is not None:
                    overrides["argv"] = split_command(command) if isinstance(command, str) else command
                if prompts:
                    overrides["prompts"] = prompts + list(data.get("prompts", []))
                spec, extras = profiles.build_spec(data, variables=vars, extra_args=args, overrides=overrides)
            else:
                argv = split_command(command) if isinstance(command, str) else list(command or [])
                spec = _spec_from_args(
                    transport,
                    [*argv, *(args or [])],
                    prompts=prompts,
                    **{k: v for k, v in overrides.items() if k != "transport"},
                )
            session = await asyncio.to_thread(sessions.create, spec)
        except (TransportError, KeyError, ValueError) as exc:
            return {"error": str(exc)}

        ready = spec.ready or {}
        progress = await _progress(ctx)
        if ready.get("patterns"):
            result = await wait_for(
                sessions,
                [session.id],
                patterns=ready["patterns"],
                timeout_s=min(float(ready.get("timeout_s", startup_wait_s)), MAX_WAIT_S),
                progress=progress,
                since={session.id: 0},
            )
        else:
            result = await wait_for(
                sessions,
                [session.id],
                states=["awaiting_input", "needs_user", "exited"],
                quiet_ms=1500,
                quiet_after_output=True,
                timeout_s=startup_wait_s,
                progress=progress,
                since={session.id: 0},
            )
        snap = result["sessions"][session.id]
        init_log = []
        for cmd in extras.get("init", []):
            if not session.alive:
                break
            session.send(cmd, submit=True)
            r = await wait_for(sessions, [session.id], timeout_s=10)
            init_log.append({"sent": cmd, "state": r["sessions"][session.id]["state"]})
        if init_log:
            snap = session.snapshot()
            snap["init"] = init_log
        out = {"session": session.summary(), **snap}
        if result.get("hit"):
            out["startup"] = result["hit"]
        if extras.get("notes"):
            out["notes"] = extras["notes"].strip()
        return out

    @mcp.tool
    async def session_send(
        ctx: Context,
        id: str,
        text: Annotated[str | None, Field(description="Text to type (sent as-is)")] = None,
        keys: Annotated[
            list[str] | None,
            Field(
                description="Keys after the text: Enter, Tab, Esc, Up, Down, Left, Right, PageUp, PageDown, Home, End, F1-F12, Backspace, Delete, Space, Ctrl+C, Alt+X"
            ),
        ] = None,
        submit: Annotated[
            bool, Field(description="Press the session's line ending after text/keys (runs a command)")
        ] = False,
        wait_s: Annotated[
            float,
            Field(
                description="Then wait up to this long for a prompt/quiet before returning (0 = return immediately)"
            ),
        ] = 5,
        include_screen: Annotated[
            bool | None,
            Field(description="Add the virtual screen; default: when the program redraws a full-screen UI"),
        ] = None,
    ) -> dict:
        """Type into a session and return what it printed in response, with the resulting state.
        Full-screen programs (menuconfig, installers, TUIs) also get their screen, since their output
        is only redraw fragments."""
        try:
            session = sessions.get(id)
            since = session.read_cursor
            await asyncio.to_thread(session.send, text, keys, submit)
        except (KeyError, TransportError, ValueError) as exc:
            return {"error": str(exc)}
        if wait_s <= 0:
            screen = include_screen if include_screen is not None else session.redraws_screen(since)
            return session.snapshot(since=since, with_screen=screen)
        result = await wait_for(
            sessions,
            [id],
            states=["awaiting_input", "needs_user", "exited"],
            quiet_ms=1200,
            quiet_after_output=True,
            timeout_s=wait_s,
            since={id: since},
            progress=await _progress(ctx),
            include_screen=bool(include_screen),
        )
        snap = result["sessions"][id]
        if include_screen is None and session.screen is not None and session.redraws_screen(since):
            snap["screen"] = session.screen.snapshot()
        if result.get("timed_out"):
            snap["note"] = "still busy; use session_wait to keep waiting"
        return snap

    @mcp.tool
    async def session_wait(
        ctx: Context,
        ids: Ids,
        patterns: Annotated[
            list[str] | None, Field(description="Regexes (multiline) searched in new output")
        ] = None,
        ignore_case: bool = False,
        states: Annotated[
            list[str] | None,
            Field(
                description="Stop when a session reaches one of: awaiting_input, idle, blocked, running, exited"
            ),
        ] = None,
        quiet_ms: Annotated[
            int | None, Field(description="Stop once a session printed nothing for this long")
        ] = None,
        screen_stable_ms: Annotated[
            int | None, Field(description="Stop once a full-screen UI stopped changing for this long")
        ] = None,
        min_chars: Annotated[int | None, Field(description="Stop once this much new output arrived")] = None,
        timeout_s: Annotated[
            float, Field(description="Max wait for this call (capped at 45s; call again to keep waiting)")
        ] = 30,
        include_screen: bool = False,
    ) -> dict:
        """Wait until something happens in one or more sessions. Conditions are OR-ed across all sessions;
        with no condition it waits for a prompt, a request for the user, or exit. `needs_user` and exit
        always end the wait. Returns which session/condition fired plus each session's new output and state."""
        id_list = [ids] if isinstance(ids, str) else list(ids)
        key = coalesce.freeze(
            ("session_wait", id_list, patterns, ignore_case, states, quiet_ms, screen_stable_ms, min_chars)
            + (timeout_s, include_screen)
        )
        progress = await _progress(ctx)

        async def wait() -> dict:
            try:
                return await wait_for(
                    sessions,
                    id_list,
                    patterns=patterns,
                    ignore_case=ignore_case,
                    states=states,
                    quiet_ms=quiet_ms,
                    screen_stable_ms=screen_stable_ms,
                    min_chars=min_chars,
                    timeout_s=timeout_s,
                    include_screen=include_screen,
                    progress=progress,
                )
            except (KeyError, ValueError) as exc:
                return {"error": str(exc)}

        return await coalesce.shared(key, wait)

    @mcp.tool
    def session_read(
        id: str,
        since: Annotated[
            int | None, Field(description="Cursor to read from (default: everything unread)")
        ] = None,
        max_chars: int = 20000,
    ) -> dict:
        """Page through a session's output (escape codes removed). Returns a cursor for the next call.
        After a lost response, pass the last cursor you actually received to recover its output;
        use since=0 if unknown (old output outside the memory window is available in log_file)."""
        try:
            session = sessions.get(id)
        except KeyError as exc:
            return {"error": str(exc)}
        return {"id": id, "state": session.state()["state"], **session.read(since, max_chars)}

    @mcp.tool
    def session_screen(id: str) -> dict:
        """What a full-screen terminal program currently shows: lines, cursor, highlighted (selected) rows."""
        try:
            session = sessions.get(id)
        except KeyError as exc:
            return {"error": str(exc)}
        if session.screen is None:
            return {
                "error": f"session {id} has no virtual screen (only pty sessions or screen=true); "
                "use session_read"
            }
        return {"id": id, **session.state(), "screen": session.screen.snapshot()}

    @mcp.tool
    def session_list() -> list[dict]:
        """All sessions with transport, state, unread output and log file."""
        return [s.summary() for s in sessions.list()]

    @mcp.tool
    async def session_stop(
        id: str,
        force: bool = False,
        forget: Annotated[bool, Field(description="Remove it from session_list afterwards")] = True,
    ) -> dict:
        """End a session (sends the profile's exit command first unless force=true)."""
        try:
            session = sessions.get(id)
        except KeyError as exc:
            return {"error": str(exc)}
        final = session.collect()
        result = await asyncio.to_thread(session.stop, force)
        if forget:
            sessions.remove(id)
        return {**result, "final_output": final["output"]}

    @mcp.tool
    def session_resize(id: str, cols: int, rows: int) -> dict:
        """Resize a session's terminal (pty) and virtual screen."""
        try:
            session = sessions.get(id)
            session.resize(cols, rows)
        except (KeyError, TransportError) as exc:
            return {"error": str(exc)}
        return {"id": id, "cols": cols, "rows": rows}

    @mcp.tool
    async def session_prompt_user(
        id: str,
        message: Annotated[
            str, Field(description="What to ask the person at the machine, e.g. 'npm one-time password'")
        ],
        submit: bool = True,
    ) -> dict:
        """Open a masked input dialog on the machine's desktop and send the answer directly to the session.
        Literal echoes are masked in session output, virtual screens and logs, including split reads.
        Use for passwords/OTPs; this cannot protect secrets a program encodes or transforms."""
        try:
            session = sessions.get(id)
        except KeyError as exc:
            return {"error": str(exc)}
        try:
            value = await asyncio.to_thread(ask_secret, "winhand", message)
        except PromptUnavailable as exc:
            return {
                "error": str(exc),
                "hint": "Ask the user to type it in a terminal on the machine instead.",
            }
        if value is None:
            return {"id": id, "sent": False, "reason": "the user cancelled"}
        since = session.read_cursor
        try:
            await asyncio.to_thread(session.send_secret, value, submit)
        except TransportError as exc:
            return {
                "id": id,
                "sent": None,
                "error": str(exc),
                "hint": "Input delivery is uncertain; inspect the session before sending it again.",
            }
        result = await wait_for(
            sessions, [id], quiet_ms=1500, quiet_after_output=True, timeout_s=10, since={id: since}
        )
        return {"id": id, "sent": True, **result["sessions"][id]}

    @mcp.tool
    async def secret_prompt(
        message: Annotated[
            str, Field(description="What to ask for, e.g. 'Cloudflare API token (Zone DNS edit)'")
        ],
        save_to: Annotated[
            str | None, Field(description="File to store it in (only this user can read it)")
        ] = None,
        key: Annotated[
            str | None, Field(description="With save_to: store as KEY=value in that .env file (others kept)")
        ] = None,
        credential: Annotated[
            str | None, Field(description="Or: a Windows Credential Manager entry name to store it under")
        ] = None,
        validate: Annotated[
            str | None, Field(description="Regex the value must match (format check; asks again if not)")
        ] = None,
    ) -> dict:
        """Ask the person at the machine to type a secret (masked dialog) and store it there without it
        ever reaching you: you get back only where it was saved. It is also masked in later results.
        Use this whenever a token or password must exist on the machine."""

        def work():
            try:
                return secret_store.prompt_and_save(message, save_to, key, credential, validate)
            except (secret_store.SecretError, PromptUnavailable, fs.FsError, OSError) as exc:
                return {"error": str(exc)}

        return await asyncio.to_thread(work)

    # ----------------------------------------------------------------- files

    def _fs(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (fs.FsError, OSError, UnicodeError, ValueError) as exc:
            return {"error": str(exc)}

    @mcp.tool
    async def fs_read(
        path: str,
        offset: Annotated[int, Field(description="Text: 1-based first line")] = 1,
        limit: Annotated[int, Field(description="Text: number of lines")] = 2000,
        pages: Annotated[str | None, Field(description="PDF/PowerPoint: pages such as '1-3,5'")] = None,
        render_pages: Annotated[bool, Field(description="PDF: also return the pages as images")] = False,
        region: Annotated[
            list[int] | None,
            Field(description="Image: only [left, top, width, height] of it, in the file's pixels (zoom in)"),
        ] = None,
        tail: Annotated[int | None, Field(ge=1, description="Text: only the last N lines (logs)")] = None,
        max_chars: Annotated[
            int, Field(ge=1000, description="Text: stop adding lines past this many characters")
        ] = fs.READ_MAX_CHARS,
    ):
        """Read any file. Text: numbered lines (UTF-8/GBK/BOM detected) plus the file's sha256;
        also logs another program is still writing. Images: returned as a picture you can see.
        PDF, Word (.docx), PowerPoint (.pptx), Excel (.xlsx): extracted text; PDF pages without
        text (scans) come back as page images. Other binaries: use fs_send."""
        kind = media.kind_of(path)
        full = winenv.long_path(path)
        if kind in ("image", "document") and not os.path.isfile(full):
            return {"error": f"no such file: {path}"}
        try:
            if kind == "image":
                data, fmt, facts = await asyncio.to_thread(media.read_image, full, media.MAX_SIDE, region)
                facts["path"] = path
                return media_result(facts, Image(data=data, format=fmt))
            if kind == "document":
                result = await asyncio.to_thread(media.read_document, full, pages, render_pages)
                result["facts"]["path"] = path
                # explicit content blocks: a list of plain strings would be merged into one JSON string
                return media_result(
                    result["facts"],
                    _text(result["text"] or "(no text found)"),
                    *[Image(data=data, format=fmt) for data, fmt, _ in result["images"]],
                )
        except (media.MediaError, ImportError, OSError, ValueError) as exc:
            return {"error": str(exc)}
        if kind == "legacy_office":
            return {
                "error": f"{Path(path).suffix} is the old binary Office format; open and save it as "
                ".docx/.xlsx/.pptx (or convert with LibreOffice via run) to read it"
            }
        return await asyncio.to_thread(_fs, fs.fs_read, path, offset, limit, tail, max_chars)

    @mcp.tool
    async def fs_write(
        path: str,
        content: str,
        encoding: str = "utf-8",
        append: Annotated[bool, Field(description="Add to the end instead of replacing the file")] = False,
        expected_sha256: Annotated[
            str | None,
            Field(
                description="Only write if the file still has this sha256 (from fs_read); '' = must not exist"
            ),
        ] = None,
    ) -> dict:
        """Create or overwrite a file (parent folders are created). Replacing is atomic: a reader
        never sees half a file. Returns the new sha256."""
        return await asyncio.to_thread(
            _fs, fs.fs_write, path, content, encoding, True, append, expected_sha256
        )

    @mcp.tool
    async def fs_edit(
        path: str,
        old: Annotated[
            str | None, Field(description="Exact text to replace (must be unique unless replace_all)")
        ] = None,
        new: str | None = None,
        replace_all: bool = False,
        edits: Annotated[
            list[Edit] | None,
            Field(
                description="Several replacements at once, applied in order: all succeed or none is written"
            ),
        ] = None,
        expected_sha256: Annotated[
            str | None, Field(description="Only edit if the file still has this sha256 (from fs_read)")
        ] = None,
    ) -> dict:
        """Replace exact snippets in a file; keeps the file's encoding and CRLF/LF line endings."""
        ops = [e.model_dump() for e in edits] if edits else None
        return await asyncio.to_thread(_fs, fs.fs_edit, path, old, new, replace_all, ops, expected_sha256)

    @mcp.tool
    async def fs_list(
        path: str = ".",
        depth: int = 1,
        pattern: str | None = None,
        show_hidden: bool = False,
        sort: Annotated[
            str, Field(description="name | mtime (newest first) | size (largest first)")
        ] = "name",
        newer_than: Annotated[
            str | None, Field(description="Only entries changed since: '2h', '3d' or '2026-10-05 13:30'")
        ] = None,
        kind: Annotated[str, Field(description="all | file | dir")] = "all",
        limit: int = 500,
    ) -> dict:
        """List a directory (optionally recursive to `depth`, filtered by glob `pattern`), with sizes
        and modification times."""
        return await asyncio.to_thread(
            _fs, fs.fs_list, path, depth, pattern, show_hidden, limit, sort, newer_than, kind
        )

    @mcp.tool
    async def fs_find(
        name: Annotated[
            str, Field(description="Glob (*.py, report-*.xlsx) or, without wildcards, text the name contains")
        ],
        path: str = ".",
        max_depth: int | None = None,
        kind: Annotated[str, Field(description="all | file | dir")] = "all",
        newer_than: Annotated[
            str | None, Field(description="Only entries changed since: '2h', '3d' or '2026-10-05 13:30'")
        ] = None,
        max_results: int = 200,
    ) -> dict:
        """Find files and folders by name below `path` (case-insensitive). Answers within ~35 s,
        with what it found so far if the tree is huge (then narrow path or max_depth)."""
        return await asyncio.to_thread(_fs, fs.fs_find, name, path, max_depth, kind, newer_than, max_results)

    @mcp.tool
    async def fs_search(
        pattern: Annotated[str, Field(description="Regex (or text with literal=true)")],
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        max_results: int = 200,
        context: Annotated[int, Field(ge=0, le=20, description="Lines of context around each match")] = 0,
        files_only: Annotated[bool, Field(description="Only list the files that match")] = False,
        encoding: Annotated[
            str | None, Field(description="Force the files' encoding, e.g. gbk (default: auto)")
        ] = None,
    ) -> dict:
        """Search file contents (ripgrep when available); returns path:line:text. Answers within
        ~35 s, with the matches found so far on very large trees."""
        return await asyncio.to_thread(
            _fs,
            fs.fs_search,
            pattern,
            path,
            glob,
            ignore_case,
            literal,
            max_results,
            context,
            files_only,
            encoding,
        )

    @mcp.tool
    async def fs_stat(
        path: str,
        hash: Annotated[str | None, Field(description="Also return the file's sha256 | md5 | sha1")] = None,
    ) -> dict:
        """Existence, type, size and modification time of a path (and optionally its hash)."""
        return await asyncio.to_thread(_fs, fs.fs_stat, path, hash)

    @mcp.tool
    async def fs_op(
        action: Annotated[str, Field(description="copy | move | delete | mkdir")],
        path: str,
        to: Annotated[str | None, Field(description="For copy/move: destination file or folder")] = None,
        overwrite: bool = False,
        recursive: Annotated[bool, Field(description="For delete: also a folder that is not empty")] = False,
        permanent: Annotated[
            bool, Field(description="For delete: skip the Recycle Bin (default: it goes there)")
        ] = False,
    ) -> dict:
        """Copy, move, delete or create files and folders without a shell. Deleting moves to the
        Recycle Bin unless permanent=true; drive roots, the home folder and Windows are refused."""
        return await asyncio.to_thread(_fs, fs.fs_op, action, path, to, overwrite, recursive, permanent)

    @mcp.tool
    async def fs_apply_patch(
        root: Annotated[
            str, Field(description="Folder the patch paths are relative to (the repo/work tree)")
        ],
        patch: Annotated[str | None, Field(description="Unified diff text (diff -u or git diff)")] = None,
        patch_file: Annotated[
            str | None,
            Field(description="Or: a .diff/.patch file on this machine (e.g. sent with fs_upload_url)"),
        ] = None,
        strip: Annotated[
            int | None, Field(ge=0, le=5, description="Path components to strip (-p); default: worked out")
        ] = None,
        dry_run: Annotated[bool, Field(description="Only check that every hunk fits; write nothing")] = False,
        partial: Annotated[
            bool,
            Field(description="Write the files that apply even if others fail (default: all or nothing)"),
        ] = False,
    ) -> dict:
        """Apply a unified diff to files here, without git or patch. Keeps each file's encoding and
        CRLF/LF, finds hunks that moved, works out -p, creates/deletes/renames files, and writes
        nothing unless every hunk fits. The answer lists each file and hunk (line, offset, or why not)."""
        return await asyncio.to_thread(_fs, patching.apply, patch, root, patch_file, strip, dry_run, partial)

    @mcp.tool
    def history(
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        tool: Annotated[str | None, Field(description="Only calls of this tool, e.g. run")] = None,
    ) -> dict:
        """The most recent tool calls this machine handled (newest first): status, summary, exit code
        and run id. After a connection error, check here whether your call ran before resending it.
        A tool reached through call() is listed under its own name, with via="call"."""
        out = {"calls": recent.list(limit, tool), "winhand": __version__}
        if transfer.recent:
            out["transfers"] = list(transfer.recent)[::-1][:10]
        return out

    tools_desktop.register(mcp)

    mcp.sessions = sessions  # type: ignore[attr-defined]  # for tests and the relay client
    return mcp
