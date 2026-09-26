"""The MCP surface: a small set of flat tools over the session engine, files,
processes and mounted local MCP servers."""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from . import __version__, fs, proc, profiles
from .config import Config
from .config import load as load_config
from .gateway import mount_servers
from .proc import split_command
from .secret_prompt import PromptUnavailable, ask_secret
from .session import SessionManager, SessionSpec, TransportError, wait_for
from .session.session import TRANSPORTS
from .session.wait import MAX_WAIT_S

GUIDE = """\
winhand drives the Windows machine it runs on.

Quick rules
- One-shot command that finishes by itself -> `run` (no shell quoting issues when you pass `args`).
- Anything interactive, long-running or stateful (shells, REPLs, debuggers, gdbserver, installers,
  serial/telnet consoles, full-screen TUIs) -> a session:
    session_start -> session_send / session_wait -> session_read / session_screen -> session_stop
- Every session result carries `state` + `reason` + `next`:
    running | awaiting_input | needs_user | idle | blocked | exited
  `needs_user` means a password/one-time code/browser sign-in: never invent it. Give URLs to the user,
  or call session_prompt_user so they type a secret into a masked dialog on the machine.
- session_wait ORs its conditions across several sessions at once (e.g. gdb prompt OR serial "PANIC").
  A timeout is not a failure: call it again to keep waiting; already-returned output is not repeated.
- Full-screen programs (menuconfig, htop, vim, setup wizards): after keys, session_wait(screen_stable_ms=300)
  then session_screen; `highlighted` shows the selected row.
- Profiles (profile_list) know prompts, exit commands and quirks of common tools: gdb, pyocd-gdbserver,
  pyocd-commander, openocd, jlink, rtthread-msh, serial, telnet, menuconfig, python, shell.
- Files: fs_read (numbered) / fs_edit (exact replace) / fs_write / fs_search / fs_list / fs_stat.
- Local MCP servers configured in ~/.winhand/config.toml appear as <name>_<tool>.
"""

Ids = Annotated[list[str] | str, Field(description="Session id, or a list of ids to watch together")]


def _spec_from_args(transport: str, command: list[str] | str | None, **kw: Any) -> SessionSpec:
    if transport not in TRANSPORTS:
        raise ValueError(f"transport must be one of {TRANSPORTS}")
    argv = split_command(command) if isinstance(command, str) else list(command or [])
    return SessionSpec(transport=transport, argv=argv, **{k: v for k, v in kw.items() if v is not None})


def build_server(cfg: Config | None = None, manager: SessionManager | None = None) -> FastMCP:
    cfg = cfg if cfg is not None else load_config()
    sessions = manager or SessionManager()
    mcp = FastMCP("winhand", instructions=GUIDE, version=__version__)
    mounted = mount_servers(mcp, cfg.servers)

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
    def help() -> str:
        """How to use winhand well (read once at the start)."""
        extra = ""
        if mounted:
            names = ", ".join(f"{m['name']}_*" for m in mounted if m.get("enabled"))
            extra = f"\nMounted local MCP servers: {names}\n"
        return GUIDE + extra

    @mcp.tool
    async def sys_info() -> dict:
        """Machine facts: OS, shell, code page, available dev tools, serial ports, mounted MCP servers."""
        info = await asyncio.to_thread(proc.sys_info)
        info["winhand"] = __version__
        info["gateway"] = mounted
        return info

    # ------------------------------------------------------------ processes

    @mcp.tool
    async def run(
        command: Annotated[
            str, Field(description="Program name (with `args`) or a full shell command line (without `args`)")
        ],
        args: Annotated[
            list[str] | None, Field(description="Arguments passed verbatim, no shell involved")
        ] = None,
        cwd: str | None = None,
        timeout_s: Annotated[float, Field(description="Kill the process tree after this many seconds")] = 120,
        env: dict[str, str] | None = None,
        stdin: Annotated[str | None, Field(description="Text piped to stdin")] = None,
    ) -> dict:
        """Run a command to completion and return exit code, stdout and stderr.
        Use a session instead for anything interactive or that must keep running."""
        return await asyncio.to_thread(proc.run, command, args, cwd, timeout_s, env, stdin)

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
        cols: int | None = None,
        rows: int | None = None,
        startup_wait_s: Annotated[
            float, Field(description="How long to wait for the program to settle")
        ] = 15,
    ) -> dict:
        """Start an interactive session (process in a real terminal, plain pipes, serial port or TCP socket),
        wait until it settles and return its first output and state."""
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
    ) -> dict:
        """Type into a session and return what it printed in response, with the resulting state."""
        try:
            session = sessions.get(id)
            since = session.read_cursor
            await asyncio.to_thread(session.send, text, keys, submit)
        except (KeyError, TransportError, ValueError) as exc:
            return {"error": str(exc)}
        if wait_s <= 0:
            return session.snapshot(since=since)
        result = await wait_for(
            sessions,
            [id],
            states=["awaiting_input", "needs_user", "exited"],
            quiet_ms=1200,
            quiet_after_output=True,
            timeout_s=wait_s,
            since={id: since},
            progress=await _progress(ctx),
            include_screen=False,
        )
        snap = result["sessions"][id]
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
            float, Field(description="Max wait for this call (capped at 50s; call again to keep waiting)")
        ] = 30,
        include_screen: bool = False,
    ) -> dict:
        """Wait until something happens in one or more sessions. Conditions are OR-ed across all sessions;
        with no condition it waits for a prompt, a request for the user, or exit. `needs_user` and exit
        always end the wait. Returns which session/condition fired plus each session's new output and state."""
        id_list = [ids] if isinstance(ids, str) else list(ids)
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
                progress=await _progress(ctx),
            )
        except (KeyError, ValueError) as exc:
            return {"error": str(exc)}

    @mcp.tool
    def session_read(
        id: str,
        since: Annotated[
            int | None, Field(description="Cursor to read from (default: everything unread)")
        ] = None,
        max_chars: int = 20000,
    ) -> dict:
        """Page through a session's output (escape codes removed). Returns a cursor for the next call."""
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
        """Open a masked input dialog on the machine's desktop; what the person types goes straight into the
        session and is never returned to you. Use for passwords/OTPs a session asks for."""
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
        await asyncio.to_thread(session.send, value, None, submit)
        result = await wait_for(
            sessions, [id], quiet_ms=1500, quiet_after_output=True, timeout_s=10, since={id: since}
        )
        return {"id": id, "sent": True, **result["sessions"][id]}

    # ----------------------------------------------------------------- files

    def _fs(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (fs.FsError, OSError, UnicodeError) as exc:
            return {"error": str(exc)}

    @mcp.tool
    async def fs_read(
        path: str, offset: Annotated[int, Field(description="1-based first line")] = 1, limit: int = 2000
    ) -> dict:
        """Read a text file with line numbers (UTF-8/GBK/BOM detected)."""
        return await asyncio.to_thread(_fs, fs.fs_read, path, offset, limit)

    @mcp.tool
    async def fs_write(path: str, content: str, encoding: str = "utf-8") -> dict:
        """Create or overwrite a file (parent folders are created)."""
        return await asyncio.to_thread(_fs, fs.fs_write, path, content, encoding)

    @mcp.tool
    async def fs_edit(
        path: str,
        old: Annotated[str, Field(description="Exact text to replace (must be unique unless replace_all)")],
        new: str,
        replace_all: bool = False,
    ) -> dict:
        """Replace an exact snippet in a file; keeps the file's encoding and CRLF/LF line endings."""
        return await asyncio.to_thread(_fs, fs.fs_edit, path, old, new, replace_all)

    @mcp.tool
    async def fs_list(
        path: str = ".", depth: int = 1, pattern: str | None = None, show_hidden: bool = False
    ) -> dict:
        """List a directory (optionally recursive to `depth`, filtered by glob `pattern`)."""
        return await asyncio.to_thread(_fs, fs.fs_list, path, depth, pattern, show_hidden)

    @mcp.tool
    async def fs_search(
        pattern: Annotated[str, Field(description="Regex (or text with literal=true)")],
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = False,
        literal: bool = False,
        max_results: int = 200,
    ) -> dict:
        """Search file contents (ripgrep when available); returns path:line:text."""
        return await asyncio.to_thread(
            _fs, fs.fs_search, pattern, path, glob, ignore_case, literal, max_results
        )

    @mcp.tool
    async def fs_stat(path: str) -> dict:
        """Existence, type, size and modification time of a path."""
        return await asyncio.to_thread(_fs, fs.fs_stat, path)

    mcp.sessions = sessions  # type: ignore[attr-defined]  # for tests and the relay client
    return mcp
