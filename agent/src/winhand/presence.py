"""Give the desktop back to its owner while an AI operates it.

From the 2026-11-01 review (and Windows-MCP, which does the same): an AI's clicks and keys land in
the middle of whatever the person at the machine is doing. Now:

- A desktop action (input, a ui action, an ocr click, a window action) waits while the person has
  used the mouse or keyboard in the last `owner_idle_s` seconds, for at most WAIT_S, and then answers
  that the person is using the machine. Input winhand sent itself does not count, nor does the
  person answering one of winhand's own dialogs (an approval, a secret, a file picker).
- Ctrl+Alt+Shift+Backspace stops the AI's desktop actions: they are refused for STOP_S after it,
  and the answer says the person stopped them. The keys are registered when winhand starts (live test
  2026-11-01: registered only at the first desktop action, a press right after an update was missed),
  and the press is kept in ~/.winhand/desktop-stopped so every winhand process on the machine stops,
  not only the one holding the keys.
- While an AI operates the desktop a thin frame lines the screens, and goes BORDER_LINGER_S after the
  last action. Screenshots do not show it (it is excluded from capture); without that exclusion
  (Windows before 10 2004) there is no frame at all.

`[desktop]` in config.toml: yield_to_owner = true, owner_idle_s = 5, stop_keys = true, border = true.
The frame windows use DefWindowProcW itself as their window procedure: no Python code runs inside
Windows' window machinery.
"""

from __future__ import annotations

import contextlib
import ctypes
import sys
import threading
import time
from collections.abc import Iterator
from time import time as _wall  # the stop file holds wall time: other processes read it

IDLE_S = 5.0  # the owner counts as using the machine for this long after their last input
WAIT_S = 10.0  # a desktop action waits at most this long for them (an approval may still take ~40 s)
STOP_S = 60.0  # desktop actions are refused this long after the stop keys
OURS_MS = 500  # input that arrives this soon after winhand sent some is winhand's own
BORDER_LINGER_S = 2.0
BORDER_PX = 4
BORDER_RGB = (255, 120, 0)
STOP_KEYS = "Ctrl+Alt+Shift+Backspace"
POLL_S = 0.25

_lock = threading.Lock()
_ours: int | None = None  # tick when winhand last sent input
_dialogs: list[tuple[int, int]] = []  # (opened, closed) ticks of winhand's own dialogs lately
_stopped: float | None = None  # time.monotonic() of the stop keys
_active = 0  # desktop actions running now
_quiet_since = 0.0
_local = threading.local()


def settings() -> dict:
    """[desktop] in config.toml, read once per action."""
    from . import config

    raw: dict = {}
    with contextlib.suppress(Exception):
        value = config.load().raw.get("desktop")
        raw = value if isinstance(value, dict) else {}

    def flag(name: str) -> bool:
        return str(raw.get(name, True)).strip().lower() not in ("false", "0", "no", "off")

    try:
        idle = max(0.0, min(float(raw.get("owner_idle_s", IDLE_S)), 60.0))
    except (TypeError, ValueError):
        idle = IDLE_S
    return {
        "yield": flag("yield_to_owner"),
        "idle_s": idle,
        "stop_keys": flag("stop_keys"),
        "border": flag("border"),
    }


# ------------------------------------------------------------------ input times


def _tick() -> int:
    """Milliseconds on the clock GetLastInputInfo uses (32 bits, wraps after 49 days)."""
    if sys.platform == "win32":
        return ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def _last_input_tick() -> int | None:
    """When the latest mouse or keyboard input reached the system (anyone's), or None."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return None
    return info.dwTime & 0xFFFFFFFF


def _diff(a: int, b: int) -> int:
    """a - b in milliseconds, across the wrap of the 32-bit clock."""
    return ((a - b + 2**31) & 0xFFFFFFFF) - 2**31


def note_sent() -> None:
    """winhand just sent input (SendInput, a keybd_event): the next input seen is its own."""
    global _ours
    with _lock:
        _ours = _tick()


@contextlib.contextmanager
def dialog() -> Iterator[None]:
    """Around one of winhand's own dialogs: the person's clicks and typing in it are not them
    using the machine."""
    opened = _tick()
    try:
        yield
    finally:
        with _lock:
            _dialogs.append((opened, _tick()))
            del _dialogs[:-4]


def owner_input_ago(idle_s: float = IDLE_S) -> float | None:
    """Seconds since the person last used the mouse or keyboard, when that was less than idle_s
    ago and not winhand's own input or an answer to its dialogs; else None."""
    last = _last_input_tick()
    if last is None:
        return None
    ago = max(0, _diff(_tick(), last))
    if ago >= idle_s * 1000:
        return None
    with _lock:
        ours, dialogs = _ours, list(_dialogs)
    if ours is not None and -50 <= _diff(last, ours) <= OURS_MS:
        return None
    for opened, closed in dialogs:
        if _diff(last, opened) >= -50 and _diff(last, closed) <= OURS_MS:
            return None
    return ago / 1000


# ------------------------------------------------------------------ stop keys


def _stop_file():
    from . import config

    return config.home() / "desktop-stopped"


def stop_pressed() -> None:
    global _stopped
    _stopped = time.monotonic()
    with contextlib.suppress(OSError):  # other winhand processes on this machine stop too
        _stop_file().write_text(f"{_wall():.3f}\n", encoding="utf-8")
    _post(_WM_HIDE)


def stopped_left() -> float:
    """Seconds the stop keys still hold the AI off (0: not stopped): pressed here, or in another
    winhand process on this machine (the stop file)."""
    left = 0.0
    if _stopped is not None:
        left = STOP_S - (time.monotonic() - _stopped)
    with contextlib.suppress(OSError, ValueError):
        pressed = float(_stop_file().read_text(encoding="utf-8").strip() or 0)
        left = max(left, STOP_S - max(0.0, _wall() - pressed))  # a clock set back: still only STOP_S
    return max(0.0, min(left, STOP_S))


def resume() -> None:
    """Let desktop actions go on before STOP_S is over (the desktop app, tests)."""
    global _stopped
    _stopped = None
    with contextlib.suppress(OSError):
        _stop_file().write_text("0\n", encoding="utf-8")


# ------------------------------------------------------------------ before an action


def before(what: str) -> dict:
    """Wait for the owner to leave the mouse and keyboard, or refuse when they stopped the AI.
    Returns what the caller should mention (empty when nothing happened)."""
    from . import desktop

    left = stopped_left()
    if left:
        raise desktop.OwnerStopped(
            f"the person at this computer pressed {STOP_KEYS} to stop desktop actions; nothing was done "
            f"({what}). Ask them before going on (refused for {left:.0f} s more)"
        )
    conf = settings()
    if not conf["yield"]:
        return {}
    began = time.monotonic()
    while (ago := owner_input_ago(conf["idle_s"])) is not None:
        if stopped_left():
            return before(what)
        if time.monotonic() - began >= WAIT_S:
            raise desktop.OwnerBusy(
                f"the person at this computer is using the mouse or keyboard (last {ago:.0f} s ago); waited "
                f"{WAIT_S:.0f} s and did nothing ({what}). Try again later, or ask them to leave it to you"
            )
        time.sleep(POLL_S)
    waited = time.monotonic() - began
    return {"waited_for_owner_s": round(waited, 1)} if waited >= POLL_S else {}


@contextlib.contextmanager
def acting(what: str) -> Iterator[dict]:
    """Around a desktop action: wait for the owner (outermost call only), show the frame, and
    take it away a little after the last action."""
    global _active, _quiet_since
    depth = getattr(_local, "depth", 0)
    said = before(what) if depth == 0 else {}
    _local.depth = depth + 1
    with _lock:
        _active += 1
    if depth == 0:
        _show_border()
    try:
        yield said
    finally:
        _local.depth = depth
        with _lock:
            _active -= 1
            _quiet_since = time.monotonic()
        if depth == 0:
            timer = threading.Timer(BORDER_LINGER_S, _hide_if_quiet)
            timer.daemon = True
            timer.start()


# ------------------------------------------------------------------ the frame and the stop keys

_WM_APP = 0x8000
_WM_SHOW = _WM_APP + 1
_WM_HIDE = _WM_APP + 2
_WM_REGISTER = _WM_APP + 3
_WM_HOTKEY = 0x0312
_thread: _Desk | None = None
_started = threading.Lock()


def _hide_if_quiet() -> None:
    with _lock:
        quiet = _active == 0 and time.monotonic() - _quiet_since >= BORDER_LINGER_S - 0.05
    if quiet:
        _post(_WM_HIDE)


def _show_border() -> None:
    if sys.platform != "win32":
        return
    conf = settings()
    if not (conf["border"] or conf["stop_keys"]):
        return
    desk = start()
    if desk is None:
        return
    if conf["stop_keys"] and not desk.hotkey:
        _post(_WM_REGISTER)  # another process held the keys at start; it may have let go
    if conf["border"]:
        _post(_WM_SHOW)


def _post(message: int) -> None:
    desk = _thread
    if desk is not None and desk.tid:
        ctypes.windll.user32.PostThreadMessageW(desk.tid, message, 0, 0)


def start() -> _Desk | None:
    """The thread that holds the stop keys and the frame, started once (Windows only)."""
    global _thread
    if sys.platform != "win32":
        return None
    with _started:
        if _thread is None:
            desk = _Desk()
            desk.start()
            desk.ready.wait(5)
            _thread = desk
    return _thread


def border_windows() -> list[int]:
    desk = _thread
    return list(desk.strips) if desk is not None else []


class _Desk(threading.Thread):
    """Owns the stop keys' hotkey and the frame windows; everything happens in its message loop."""

    def __init__(self) -> None:
        super().__init__(name="winhand-presence", daemon=True)
        self.ready = threading.Event()
        self.tid = 0
        self.strips: list[int] = []
        self.rects: list[tuple[int, int, int, int]] = []
        self.hotkey = False
        self.border_ok = True  # false once the frame cannot be kept out of screenshots
        self.problem: str | None = None

    def run(self) -> None:
        from ctypes import wintypes

        # private copies: the argument types set below must not change anyone else's calls
        self.user32 = user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.kernel32 = kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            from . import desktop

            desktop._dpi_aware()
            self.tid = kernel32.GetCurrentThreadId()
            self._register()
        except Exception as exc:  # noqa: BLE001 - never take winhand down for this
            self.problem = f"{type(exc).__name__}: {exc}"
        finally:
            self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            try:
                if msg.message == _WM_HOTKEY:
                    stop_pressed()
                elif msg.message == _WM_SHOW:
                    self._show()
                elif msg.message == _WM_HIDE:
                    self._hide()
                elif msg.message == _WM_REGISTER:
                    self._register()
                else:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
            except Exception as exc:  # noqa: BLE001
                self.problem = f"{type(exc).__name__}: {exc}"
                self.border_ok = False

    def _register(self) -> None:
        """The stop keys, on this thread (a hotkey belongs to the thread that registers it). Only one
        process can hold them: the others still stop through the stop file."""
        if self.hotkey or not settings()["stop_keys"]:
            return
        mods = 0x0002 | 0x0001 | 0x0004 | 0x4000  # CONTROL | ALT | SHIFT | NOREPEAT
        self.hotkey = bool(self.user32.RegisterHotKey(None, 1, mods, 0x08))  # VK_BACK

    # -- the frame: four thin strips per monitor

    def _class(self) -> str:
        from ctypes import wintypes

        user32, gdi32, kernel32 = self.user32, self.gdi32, self.kernel32
        name = "WinhandBorder"
        if getattr(self, "_registered", False):
            return name
        lresult = ctypes.c_ssize_t
        wndproc = ctypes.WINFUNCTYPE(lresult, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
                ("hIconSm", wintypes.HICON),
            ]

        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
        address = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
        r, g, b = BORDER_RGB
        cls = WNDCLASSEXW()
        cls.cbSize = ctypes.sizeof(WNDCLASSEXW)
        cls.lpfnWndProc = wndproc(address)  # Windows' own procedure, not a Python callback
        cls.hInstance = self.instance = kernel32.GetModuleHandleW(None)
        cls.hbrBackground = gdi32.CreateSolidBrush(r | (g << 8) | (b << 16))
        cls.lpszClassName = name
        self._keep = cls  # the structure and its function pointer must outlive the class
        if (
            not user32.RegisterClassExW(ctypes.byref(cls)) and ctypes.get_last_error() != 1410
        ):  # already there
            raise OSError(f"RegisterClassExW failed ({ctypes.get_last_error()})")
        self._registered = True
        return name

    def _strip_rects(self) -> list[tuple[int, int, int, int]]:
        from . import desktop

        out = []
        for m in desktop.monitors():
            x, y, w, h, t = m["left"], m["top"], m["width"], m["height"], BORDER_PX
            out += [(x, y, w, t), (x, y + h - t, w, t), (x, y, t, h), (x + w - t, y, t, h)]
        return out

    def _make(self, rects) -> None:
        from ctypes import wintypes

        user32 = self.user32
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
        ]  # fmt: skip
        name = self._class()
        ex = (
            0x00000008 | 0x00000080 | 0x00080000 | 0x00000020 | 0x08000000
        )  # TOPMOST TOOL LAYERED TRANSPARENT NOACTIVATE
        for hwnd in self.strips:
            user32.DestroyWindow(wintypes.HWND(hwnd))
        self.strips = []
        for x, y, w, h in rects:
            hwnd = user32.CreateWindowExW(
                ex, name, "WinHand", 0x80000000, x, y, w, h, None, None, self.instance, None
            )
            if not hwnd:
                continue
            handle = wintypes.HWND(hwnd)
            user32.SetLayeredWindowAttributes(handle, 0, 230, 0x00000002)  # LWA_ALPHA
            if not user32.SetWindowDisplayAffinity(handle, 0x00000011):  # WDA_EXCLUDEFROMCAPTURE
                user32.DestroyWindow(handle)
                self.border_ok = False  # it would show in screenshots: no frame at all
                break
            self.strips.append(hwnd)
        if not self.border_ok:
            for hwnd in self.strips:
                user32.DestroyWindow(wintypes.HWND(hwnd))
            self.strips = []
        self.rects = list(rects)

    def _show(self) -> None:
        from ctypes import wintypes

        if not self.border_ok or stopped_left():
            return
        rects = self._strip_rects()
        if rects != self.rects:
            self._make(rects)
        user32 = self.user32
        for hwnd, (x, y, w, h) in zip(self.strips, self.rects, strict=False):
            # HWND_TOPMOST, SWP_NOACTIVATE | SWP_SHOWWINDOW
            user32.SetWindowPos(wintypes.HWND(hwnd), wintypes.HWND(-1), x, y, w, h, 0x0010 | 0x0040)

    def _hide(self) -> None:
        from ctypes import wintypes

        for hwnd in self.strips:
            self.user32.ShowWindow(wintypes.HWND(hwnd), 0)  # SW_HIDE


def status() -> dict:
    """For sys_info and tests: what this machine has of the above."""
    desk = _thread
    conf = settings()
    out = {
        "yield_to_owner": conf["yield"],
        "owner_idle_s": conf["idle_s"],
        "stop_keys": STOP_KEYS if conf["stop_keys"] else False,
        "border": conf["border"],
    }
    if desk is not None:
        out.update(hotkey_registered=desk.hotkey, border_works=desk.border_ok)
        if desk.problem:
            out["problem"] = desk.problem
    if stopped_left():
        out["stopped_for_s"] = round(stopped_left())
    return out
