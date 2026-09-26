"""Byte pipes a session can run over.

All transports expose the same small interface, so everything above them
(buffering, screen, state inference, waiting, auto-replies) works identically
for a ConPTY console, a plain child process, a serial port or a TCP socket.
"""

from __future__ import annotations

import codecs
import os
import signal
import socket
import subprocess
import sys
import time
from abc import ABC, abstractmethod

from .. import winenv

IS_WINDOWS = sys.platform == "win32"


class TransportError(RuntimeError):
    pass


def _default_sigint() -> None:
    """Runs in the child before exec (POSIX). If winhand was started with SIGINT
    ignored (background jobs of non-interactive shells, CI runners, some service
    managers) children inherit that and Ctrl+C would silently do nothing."""
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class Transport(ABC):
    kind = "abstract"
    #: whether a virtual screen makes sense by default
    screen_default = False
    #: line ending appended by `submit`
    default_line_ending = "\n"

    @abstractmethod
    def read(self) -> str | None:
        """Block briefly for output. "" means nothing yet, None means closed for good."""

    @abstractmethod
    def write(self, data: str) -> None: ...

    @abstractmethod
    def alive(self) -> bool: ...

    def exit_code(self) -> int | None:
        return None

    def resize(self, cols: int, rows: int) -> None:  # noqa: B027 - optional hook
        pass

    def interrupt(self) -> str:
        """Deliver Ctrl+C; returns what actually happened."""
        self.write("\x03")
        return "sent Ctrl+C"

    @abstractmethod
    def close(self, force: bool = False) -> None: ...

    def describe(self) -> dict:
        return {"transport": self.kind}


class _Decoder:
    def __init__(self, encoding: str) -> None:
        self._dec = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.encoding = encoding

    def decode(self, data: bytes, final: bool = False) -> str:
        return self._dec.decode(data, final)


# --------------------------------------------------------------------------- pty


def _kill_tree(pid: int | None, force: bool = True) -> None:
    """End a process and all its descendants (a venv python.exe on Windows is only a
    launcher for the real interpreter, so killing the parent alone leaves it running)."""
    if not pid:
        return
    import psutil

    try:
        root = psutil.Process(pid)
        procs = [*root.children(recursive=True), root]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill() if force else p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=3)


class _ConPty:
    """Direct use of pywinpty's low-level ConPTY handle.

    pywinpty's `PtyProcess` wrapper relays output through a helper thread and a local
    socket; that thread quits silently on any exception and uses partial `send()`s, so
    under heavy output the session looked closed after ~1400 lines while the program
    sat blocked on a full console buffer. Reading the handle directly avoids all of it.
    """

    def __init__(self, argv: list[str], cwd: str | None, env: dict[str, str], cols: int, rows: int) -> None:
        import subprocess as sp

        from winpty import PTY, Backend  # type: ignore[import-not-found]

        self._pty = PTY(cols, rows, backend=Backend.ConPTY)
        block = "\0".join(f"{k}={v}" for k, v in env.items()) + "\0"
        cmdline = (" " + sp.list2cmdline(argv[1:])) if len(argv) > 1 else None
        if not self._pty.spawn(argv[0], cmdline=cmdline, cwd=cwd or os.getcwd(), env=block):
            raise TransportError(f"ConPTY refused to start {argv[0]!r}")
        self.pid = self._pty.pid

    def read(self) -> str | None:
        # A blocking read holds the GIL inside pywinpty, freezing every other thread
        # (the MCP event loop included) while the program is silent: poll instead.
        try:
            data = self._pty.read(blocking=False)
        except Exception:
            # raised once the console is gone; anything else is transient
            if not self._pty.isalive() or self._pty.iseof():
                return None
            data = ""
        if not data:
            time.sleep(0.01)
        return data

    def write(self, data: str) -> None:
        self._pty.write(data)

    def isalive(self) -> bool:
        return bool(self._pty.isalive())

    def exitstatus(self) -> int | None:
        return None if self._pty.isalive() else self._pty.get_exitstatus()

    def set_size(self, cols: int, rows: int) -> None:
        self._pty.set_size(cols, rows)

    def terminate(self, force: bool) -> None:
        _kill_tree(self.pid, force=True)
        try:
            self._pty.cancel_io()
        except Exception:
            pass


class PtyTransport(Transport):
    """A real pseudo-terminal: ConPTY on Windows, openpty elsewhere.

    Programs see a genuine TTY, so they print prompts, colours, password
    requests and full-screen UIs exactly as they would for a person.
    """

    kind = "pty"
    screen_default = True
    default_line_ending = "\r"

    def __init__(
        self,
        argv: list[str],
        cwd: str | None,
        env: dict[str, str],
        cols: int,
        rows: int,
        encoding: str = "utf-8",
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        self._decoder = None if IS_WINDOWS else _Decoder(encoding)
        self._encoding = encoding
        exe = winenv.resolve_executable(argv[0], env)
        argv = [exe, *argv[1:]]
        try:
            if IS_WINDOWS:
                self._win: _ConPty | None = _ConPty(argv, cwd, env, cols, rows)
                self._proc = None
            else:
                from ptyprocess import PtyProcess

                self._win = None
                self._proc = PtyProcess.spawn(
                    argv, cwd=cwd, env=env, dimensions=(rows, cols), preexec_fn=_default_sigint
                )
        except TransportError:
            raise
        except Exception as exc:  # FileNotFoundError, OSError, winpty errors
            raise TransportError(f"cannot start {argv[0]!r}: {exc}") from exc

    @property
    def pid(self) -> int | None:
        return self._win.pid if self._win else getattr(self._proc, "pid", None)

    def read(self) -> str | None:
        if self._win:
            return self._win.read()
        try:
            data = self._proc.read(65536)
        except EOFError:
            return None
        except OSError:
            return None if not self.alive() else ""
        return self._decoder.decode(data)

    def write(self, data: str) -> None:
        if self._win:
            self._win.write(data)
        else:
            self._proc.write(data.encode(self._encoding, errors="replace"))

    def alive(self) -> bool:
        return self._win.isalive() if self._win else bool(self._proc.isalive())

    def exit_code(self) -> int | None:
        if self._win:
            return self._win.exitstatus()
        if self.alive():
            return None
        status = getattr(self._proc, "exitstatus", None)
        if status is None:
            sig = getattr(self._proc, "signalstatus", None)
            return -sig if sig else None
        return status

    def resize(self, cols: int, rows: int) -> None:
        if self._win:
            self._win.set_size(cols, rows)
        else:
            self._proc.setwinsize(rows, cols)

    def close(self, force: bool = False) -> None:
        if self._win:
            if self._win.isalive():
                self._win.terminate(force)
            return
        pid = self.pid
        try:
            if self.alive():
                self._proc.terminate(force=force)
                if self.alive():
                    self._proc.terminate(force=True)
        except Exception:
            pass
        _kill_tree(pid)  # grandchildren the shell left behind
        try:
            self._proc.close(force=True)
        except Exception:
            pass

    def describe(self) -> dict:
        return {"transport": self.kind, "argv": self.argv, "cwd": self.cwd, "pid": self.pid}


# -------------------------------------------------------------------------- pipe


class PipeTransport(Transport):
    """A child process on plain pipes (stdout and stderr merged).

    For programs that misbehave on a TTY or that only speak a line protocol.
    """

    kind = "pipe"

    def __init__(
        self, argv: list[str], cwd: str | None, env: dict[str, str], encoding: str = "utf-8"
    ) -> None:
        self.argv = argv
        self.cwd = cwd
        exe = winenv.resolve_executable(argv[0], env)
        kwargs: dict = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
            kwargs["preexec_fn"] = _default_sigint
        try:
            self._proc = subprocess.Popen(
                [exe, *argv[1:]],
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                **kwargs,
            )
        except OSError as exc:
            raise TransportError(f"cannot start {argv[0]!r}: {exc}") from exc
        self._decoder = _Decoder(encoding)
        self._encoding = encoding

    @property
    def pid(self) -> int:
        return self._proc.pid

    def read(self) -> str | None:
        assert self._proc.stdout is not None
        try:
            data = os.read(self._proc.stdout.fileno(), 65536)
        except OSError:
            data = b""
        if not data:
            self._proc.wait()
            tail = self._decoder.decode(b"", final=True)
            return tail or None
        return self._decoder.decode(data)

    def write(self, data: str) -> None:
        assert self._proc.stdin is not None
        try:
            self._proc.stdin.write(data.encode(self._encoding, errors="replace"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransportError(f"process no longer accepts input: {exc}") from exc

    def alive(self) -> bool:
        return self._proc.poll() is None

    def exit_code(self) -> int | None:
        return self._proc.poll()

    def interrupt(self) -> str:
        if IS_WINDOWS:
            # A console-less pipe child cannot receive console control events on
            # Windows, so the only reliable "stop" is ending its process tree.
            import psutil

            try:
                proc = psutil.Process(self._proc.pid)
                for p in [*proc.children(recursive=True), proc]:
                    p.kill()
            except psutil.Error:
                pass
            return "terminated the process tree (Windows pipes cannot receive Ctrl+C; use a pty session to interrupt)"
        os.killpg(self._proc.pid, signal.SIGINT)
        return "sent SIGINT"

    def close(self, force: bool = False) -> None:
        if self.alive():
            try:
                if force:
                    self._proc.kill()
                else:
                    self._proc.terminate()
                self._proc.wait(timeout=3)
            except Exception:
                self._proc.kill()
        for stream in (self._proc.stdin, self._proc.stdout):
            try:
                if stream:
                    stream.close()
            except Exception:
                pass

    def describe(self) -> dict:
        return {"transport": self.kind, "argv": self.argv, "cwd": self.cwd, "pid": self.pid}


# ------------------------------------------------------------------------ serial


class SerialTransport(Transport):
    """A serial port (COM3, /dev/ttyUSB0) or any pyserial URL (socket://, rfc2217://, loop://)."""

    kind = "serial"
    default_line_ending = "\r\n"

    def __init__(self, port: str, baudrate: int = 115200, encoding: str = "utf-8", **options) -> None:
        import serial

        self.port = port
        self.baudrate = baudrate
        try:
            self._ser = serial.serial_for_url(port, baudrate=baudrate, timeout=0.1, **options)
        except Exception as exc:
            raise TransportError(f"cannot open serial port {port!r}: {exc}") from exc
        self._decoder = _Decoder(encoding)
        self._encoding = encoding
        self._closed = False

    def read(self) -> str | None:
        if self._closed:
            return None
        try:
            waiting = self._ser.in_waiting
            data = self._ser.read(waiting or 1)
        except Exception:
            self._closed = True
            return None
        return self._decoder.decode(data) if data else ""

    def write(self, data: str) -> None:
        try:
            self._ser.write(data.encode(self._encoding, errors="replace"))
            self._ser.flush()
        except Exception as exc:
            raise TransportError(f"serial write failed: {exc}") from exc

    def alive(self) -> bool:
        return not self._closed and bool(self._ser.is_open)

    def close(self, force: bool = False) -> None:
        self._closed = True
        try:
            self._ser.close()
        except Exception:
            pass

    def describe(self) -> dict:
        return {"transport": self.kind, "port": self.port, "baudrate": self.baudrate}


# --------------------------------------------------------------------------- tcp

_IAC = 255


class TcpTransport(Transport):
    """A raw TCP stream: gdbserver/RTT/telnet ports, QEMU monitors, device consoles.

    Telnet negotiation bytes are stripped and refused so plain console servers
    that start with IAC options still read as text.
    """

    kind = "tcp"

    def __init__(
        self,
        host: str,
        port: int,
        encoding: str = "utf-8",
        telnet: bool = True,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        try:
            self._sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as exc:
            raise TransportError(f"cannot connect to {host}:{port}: {exc}") from exc
        self._sock.settimeout(0.2)
        self._decoder = _Decoder(encoding)
        self._encoding = encoding
        self._telnet = telnet
        self._closed = False

    def _strip_telnet(self, data: bytes) -> bytes:
        if not self._telnet or _IAC not in data:
            return data
        out = bytearray()
        i = 0
        while i < len(data):
            b = data[i]
            if b != _IAC:
                out.append(b)
                i += 1
                continue
            cmd = data[i + 1] if i + 1 < len(data) else None
            if cmd == _IAC:
                out.append(_IAC)
                i += 2
            elif cmd in (251, 252, 253, 254) and i + 2 < len(data):  # WILL WONT DO DONT
                opt = data[i + 2]
                reply = 254 if cmd in (251, 252) else 252  # DONT / WONT
                try:
                    self._sock.sendall(bytes([_IAC, reply, opt]))
                except OSError:
                    pass
                i += 3
            elif cmd == 250:  # subnegotiation until IAC SE
                end = data.find(bytes([_IAC, 240]), i + 2)
                i = len(data) if end < 0 else end + 2
            else:
                i += 2
        return bytes(out)

    def read(self) -> str | None:
        if self._closed:
            return None
        try:
            data = self._sock.recv(65536)
        except TimeoutError:
            return ""
        except OSError:
            self._closed = True
            return None
        if not data:
            self._closed = True
            return self._decoder.decode(b"", final=True) or None
        return self._decoder.decode(self._strip_telnet(data))

    def write(self, data: str) -> None:
        try:
            self._sock.sendall(data.encode(self._encoding, errors="replace"))
        except OSError as exc:
            raise TransportError(f"tcp write failed: {exc}") from exc

    def alive(self) -> bool:
        return not self._closed

    def close(self, force: bool = False) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass

    def describe(self) -> dict:
        return {"transport": self.kind, "host": self.host, "port": self.port}
