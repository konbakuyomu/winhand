"""One interactive thing (process, port or socket) plus everything we know about it."""

from __future__ import annotations

import datetime as _dt
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import winenv
from . import state as _state
from .buffer import OutputBuffer
from .keys import encode_key
from .screen import VirtualScreen
from .text import SecretFilter, clean, clip, extract_urls
from .transports import (
    PipeTransport,
    PtyTransport,
    SerialTransport,
    TcpTransport,
    Transport,
    TransportError,
)

TRANSPORTS = ("pty", "pipe", "serial", "tcp")


@dataclass
class AutoReply:
    """Answer a recurring prompt automatically, e.g. a pager or "Continue? [y/N]"."""

    pattern: str
    send: str = ""
    keys: list[str] = field(default_factory=list)
    submit: bool = True
    max_times: int = 1000
    fired: int = 0

    def __post_init__(self) -> None:
        self.regex = re.compile(self.pattern, re.MULTILINE)


@dataclass
class SessionSpec:
    transport: str = "pty"
    argv: list[str] = field(default_factory=list)
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    name: str | None = None
    profile: str | None = None
    description: str | None = None
    # serial
    port: str | None = None
    baudrate: int = 115200
    serial_options: dict = field(default_factory=dict)
    # tcp
    host: str | None = None
    tcp_port: int | None = None
    telnet: bool = True
    # behaviour
    encoding: str = "utf-8"
    cols: int = 120
    rows: int = 40
    screen: bool | None = None
    line_ending: str | None = None
    prompts: list[str] = field(default_factory=list)
    replace_prompts: bool = False
    needs_user: list[str] = field(default_factory=list)
    confirm: list[str] = field(default_factory=list)
    quiet_ms: int | None = None
    blocked_after_s: float | None = None
    heuristic_prompts: bool | None = None
    exit_command: str | None = None
    autoreply: list[dict] = field(default_factory=list)
    ready: dict = field(default_factory=dict)


def _log_dir() -> Path:
    base = Path(os.environ.get("WINHAND_HOME") or Path.home() / ".winhand")
    return base / "logs" / _dt.date.today().isoformat()


class Session:
    def __init__(self, sid: str, spec: SessionSpec) -> None:
        self.id = sid
        self.spec = spec
        self.name = spec.name or sid
        self.created_at = time.time()
        self._started = time.monotonic()
        self.transport = self._open_transport(spec)
        use_screen = spec.screen if spec.screen is not None else self.transport.screen_default
        self.screen = VirtualScreen(spec.cols, spec.rows) if use_screen else None
        self.buffer = OutputBuffer(log_path=_log_dir() / f"{sid}.log")
        self.line_ending = (
            spec.line_ending if spec.line_ending is not None else self.transport.default_line_ending
        )
        self.detectors = _state.Detectors.build(
            prompts=spec.prompts,
            needs_user=spec.needs_user,
            confirm=spec.confirm,
            replace_prompts=spec.replace_prompts,
            quiet_ms=spec.quiet_ms,
            blocked_after_s=spec.blocked_after_s,
            heuristic_prompts=spec.heuristic_prompts,
        )
        self.autoreplies = [AutoReply(**rule) for rule in spec.autoreply]
        self.last_output_at: float | None = None
        self.last_input_at: float | None = None
        self.last_input_cursor = 0
        self.read_cursor = 0
        self.events: list[dict] = []
        self._autoreply_from = 0
        self._ended = threading.Event()
        self._stopping = threading.Event()
        self._write_lock = threading.Lock()
        self._output_lock = threading.RLock()
        self._secret_filter = SecretFilter()
        self._reader = threading.Thread(target=self._pump, name=f"winhand-{sid}", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _open_transport(spec: SessionSpec) -> Transport:
        kind = spec.transport
        if kind in ("pty", "pipe"):
            if not spec.argv:
                spec.argv = winenv.default_shell(spec.cwd)
            env = winenv.build_env(spec.env, terminal=kind == "pty")
            cwd = os.path.expanduser(spec.cwd) if spec.cwd else None
            if cwd and not os.path.isdir(cwd):
                raise TransportError(f"working directory does not exist: {cwd}")
            if kind == "pty":
                return PtyTransport(spec.argv, cwd, env, spec.cols, spec.rows, spec.encoding)
            return PipeTransport(spec.argv, cwd, env, spec.encoding)
        if kind == "serial":
            if not spec.port:
                raise TransportError("serial sessions need `port` (e.g. COM3 or /dev/ttyUSB0)")
            return SerialTransport(spec.port, spec.baudrate, spec.encoding, **spec.serial_options)
        if kind == "tcp":
            if not spec.host or not spec.tcp_port:
                raise TransportError("tcp sessions need `host` and `tcp_port`")
            return TcpTransport(spec.host, int(spec.tcp_port), spec.encoding, spec.telnet)
        raise TransportError(f"unknown transport {kind!r}; choose one of {', '.join(TRANSPORTS)}")

    # --------------------------------------------------------------- reading

    def _pump(self) -> None:
        while True:
            try:
                chunk = self.transport.read()
            except Exception as exc:  # never let the reader die silently
                self._event("reader_error", error=str(exc))
                chunk = None
            if chunk is None:
                break
            if chunk:
                self._on_output(chunk)
            elif not self.transport.alive():
                # ConPTY keeps buffered output after the process exits; stopping at the
                # first empty read would drop the tail (often the error you need).
                self._drain()
                break
        with self._output_lock:
            self._append_output(self._secret_filter.finish())
        self._ended.set()
        self._event("ended", exit_code=self.transport.exit_code())

    def _drain(self, quiet_s: float = 2.0, cap_s: float = 30.0) -> None:
        deadline = time.monotonic() + cap_s
        last_data = time.monotonic()
        while time.monotonic() < deadline:
            # a deliberate stop only needs what is already buffered
            limit = 0.2 if self._stopping.is_set() else quiet_s
            if time.monotonic() - last_data >= limit:
                break
            try:
                chunk = self.transport.read()
            except Exception:
                return
            if chunk is None:
                return
            if chunk:
                self._on_output(chunk)
                last_data = time.monotonic()
            else:
                time.sleep(0.02)

    def _on_output(self, chunk: str) -> None:
        with self._output_lock:
            self.last_output_at = time.monotonic()
            self._append_output(self._secret_filter.feed(chunk))

    def _append_output(self, chunk: str) -> None:
        if not chunk:
            return
        self.buffer.append(chunk)
        if self.screen is not None:
            self.screen.feed(chunk)
        if self.autoreplies:
            self._run_autoreplies()

    def _run_autoreplies(self) -> None:
        window = self.buffer.read(max(self._autoreply_from, self.buffer.end - 4000))
        text = clean(window.text)
        last_line = self.last_line()
        if _state._first(self.detectors.secret, last_line):
            return  # never auto-answer a password prompt
        for rule in self.autoreplies:
            if rule.fired >= rule.max_times:
                continue
            match = rule.regex.search(text)
            if match:
                rule.fired += 1
                self._autoreply_from = window.end
                payload = rule.send + "".join(encode_key(k) for k in rule.keys)
                if rule.submit:
                    payload += self.line_ending
                self._write(payload)
                self._event(
                    "autoreply", pattern=rule.pattern, matched=match.group(0)[:200], sent=repr(payload)
                )
                return

    def _event(self, kind: str, **data) -> None:
        self.events.append({"t": round(time.time(), 3), "event": kind, **data})
        del self.events[:-50]

    # --------------------------------------------------------------- writing

    def _write(self, data: str) -> None:
        with self._write_lock:
            # Record before writing: a fast child may answer while transport.write is still returning.
            previous = self.last_input_at, self.last_input_cursor
            self.last_input_at = time.monotonic()
            self.last_input_cursor = self.buffer.end
            try:
                self.transport.write(data)
            except Exception:
                self.last_input_at, self.last_input_cursor = previous
                raise

    def send(
        self,
        text: str | None = None,
        keys: list[str] | None = None,
        submit: bool = False,
        key_delay_ms: int = 30,
    ) -> None:
        if not self.alive:
            raise TransportError(f"session {self.id} has ended; start a new one")
        if text:
            self._write(text)
        for i, name in enumerate(keys or []):
            encoded = encode_key(name)
            if encoded == "\x03" and self.transport.kind == "pipe":
                # a pipe has no terminal to turn ^C into SIGINT
                self._event("interrupt", result=self.transport.interrupt())
                self.last_input_cursor = self.buffer.end
            else:
                self._write(encoded)
            if key_delay_ms and i < len(keys or []) - 1:
                time.sleep(key_delay_ms / 1000)
        if submit:
            self._write(self.line_ending)

    def send_secret(self, text: str, submit: bool = True) -> None:
        """Register the secret before writing, including echoes arriving on the reader thread."""
        with self._output_lock:
            self._secret_filter.add(text)
        self.send(text, submit=submit)

    def resize(self, cols: int, rows: int) -> None:
        self.transport.resize(cols, rows)
        if self.screen is not None:
            self.screen.resize(cols, rows)

    # ----------------------------------------------------------------- state

    @property
    def alive(self) -> bool:
        # Ended only once the reader has consumed everything: the process may be gone
        # while the terminal still holds its last (often most important) output.
        return not self._ended.is_set()

    def last_line(self) -> str:
        if self.screen is not None:
            line = self.screen.cursor_line()
            if line.strip():
                return line
        tail = clean(self.buffer.tail(2000))
        return tail.split("\n")[-1] if tail else ""

    def idle_seconds(self) -> float:
        refs = [t for t in (self.last_output_at, self.last_input_at) if t is not None]
        return time.monotonic() - (max(refs) if refs else self._started)

    def awaiting_response(self) -> bool:
        if self.last_input_at is None:
            return False
        if self.last_output_at is None or self.last_output_at < self.last_input_at:
            return True
        # ANSI-only output or a withheld secret prefix does not make the old prompt new again.
        return not clean(self.buffer.read(max(self.last_input_cursor, self.buffer.end - 8000)).text).strip()

    def state(self) -> dict:
        since = self.buffer.read(max(self.last_input_cursor, self.buffer.end - 8000))
        return _state.infer(
            alive=self.alive,
            exit_code=self.transport.exit_code(),
            last_line=self.last_line(),
            since_input=clean(since.text),
            idle_s=self.idle_seconds(),
            det=self.detectors,
            awaiting_response=self.awaiting_response(),
        )

    # ---------------------------------------------------------------- output

    def read(self, since: int | None = None, limit: int = 20000) -> dict:
        """Page forward through output; advances the session's read cursor."""
        start = self.read_cursor if since is None else since
        chunk = self.buffer.read(start, limit)
        self.read_cursor = max(self.read_cursor, chunk.end)
        more = chunk.end < self.buffer.end
        out = {"output": clean(chunk.text), "cursor": chunk.end, "more": more}
        if chunk.dropped:
            out["dropped"] = chunk.dropped
            out["log_file"] = str(self.buffer.log_path)
        if more:
            out["hint"] = f"more output available: session_read(since={chunk.end})"
        return out

    def collect(self, since: int | None = None, limit: int = 8000) -> dict:
        """Everything new since `since` (default: unread), keeping the most recent part."""
        start = self.read_cursor if since is None else since
        chunk = self.buffer.read(start)
        text, omitted = clip(clean(chunk.text), limit, keep="both")
        self.read_cursor = max(self.read_cursor, chunk.end)
        out = {"output": text, "cursor": chunk.end}
        if omitted or chunk.dropped:
            out["omitted"] = omitted + chunk.dropped
            out["hint"] = f"full text: session_read(since={chunk.start}) or the log file"
            out["log_file"] = str(self.buffer.log_path)
        return out

    def snapshot(self, *, since: int | None = None, limit: int = 8000, with_screen: bool = False) -> dict:
        out = {"id": self.id, "name": self.name, **self.state()}
        new = self.collect(since, limit)
        out.update(new)
        urls = extract_urls(new["output"])
        if urls:
            out["urls"] = urls
        if with_screen and self.screen is not None:
            out["screen"] = self.screen.snapshot()
        if self.events:
            out["recent_events"] = self.events[-5:]
        out["next"] = _next_hint(out)
        return out

    def summary(self) -> dict:
        st = self.state()
        return {
            "id": self.id,
            "name": self.name,
            "profile": self.spec.profile,
            **self.transport.describe(),
            "state": st["state"],
            "reason": st["reason"],
            "idle_ms": st["idle_ms"],
            "unread_chars": max(0, self.buffer.end - self.read_cursor),
            "screen": self.screen is not None,
            "log_file": str(self.buffer.log_path),
        }

    # ------------------------------------------------------------------ stop

    def stop(self, force: bool = False, grace_s: float = 3.0) -> dict:
        self._stopping.set()
        if self.alive and not force and self.spec.exit_command:
            try:
                self.send(self.spec.exit_command, submit=True)
                self._ended.wait(grace_s)
            except TransportError:
                pass
        if self.alive:
            self.transport.close(force=force)
            self._ended.wait(2.0)
        if self.alive:
            self.transport.close(force=True)
            self._ended.wait(3.0)
        if not self.alive:
            self.transport.close(force=True)  # release fds/handles of ended sessions too
        self.buffer.close()
        return {
            "id": self.id,
            "stopped": not self.alive,
            "exit_code": self.transport.exit_code(),
            "log_file": str(self.buffer.log_path),
        }


def _next_hint(snap: dict) -> str:
    state = snap.get("state")
    kind = snap.get("kind")
    if state == "needs_user":
        if kind == "browser_auth":
            return "Give the URL(s) to the user and wait for them to finish; then session_wait again."
        return (
            "Ask the user to type it: session_prompt_user opens a masked dialog on the machine; "
            "never guess or invent secrets."
        )
    if state == "awaiting_input":
        if kind == "pager":
            return "Send keys=['Space'] for more or text='q' to leave the pager."
        if kind == "confirm":
            return "Answer with session_send(text='y' or 'n', submit=True) if you are sure; ask the user if unsure."
        return "Ready for the next command: session_send(text=..., submit=True)."
    if state == "running":
        return "Still producing output: session_wait(quiet_ms=...) or wait for a pattern."
    if state in ("idle", "blocked"):
        return (
            "Quiet without a prompt. If it waits for an external event (breakpoint, device, network) keep "
            "waiting with session_wait; if it is a full-screen UI, look at session_screen."
        )
    if state == "exited":
        return "Session ended; read the final output above or start a new session."
    return ""
