"""The visible desktop: screenshots, windows, mouse and keyboard, clipboard (Windows).

Everything works in physical pixels (the process is made per-monitor DPI aware), so a
screenshot, a window rectangle and a click all use the same coordinates. Input defaults to
the coordinate space of the latest screenshot, which may have been scaled down to fit a
vision model: the conversion back to the screen happens here, not in the model's head.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import uuid
from ctypes import wintypes

from . import media

IS_WINDOWS = sys.platform == "win32"


class DesktopError(RuntimeError):
    pass


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise DesktopError("desktop tools are only available on Windows")


_dpi_done = False


def _dpi_aware() -> None:
    global _dpi_done
    if _dpi_done or not IS_WINDOWS:
        return
    _dpi_done = True
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass


if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", wintypes.DWORD),
            ("szDevice", wintypes.WCHAR * 32),
        ]

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
    )
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageTimeoutW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, ctypes.c_void_p, wintypes.UINT, wintypes.UINT,
        ctypes.POINTER(ctypes.c_size_t),
    ]  # fmt: skip
    user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
    user32.RealGetWindowClassW.argtypes = [wintypes.HWND, wintypes.LPWSTR, wintypes.UINT]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowEnabled.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT
    ]  # fmt: skip
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    user32.EnumDisplayMonitors.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(RECT),
        MONITORENUMPROC,
        wintypes.LPARAM,
    ]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT
    ]  # fmt: skip
    dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]


# ---------------------------------------------------------------- monitors


def monitors() -> list[dict]:
    _require_windows()
    _dpi_aware()
    found: list[dict] = []

    def callback(handle, _dc, _rect, _data):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        user32.GetMonitorInfoW(handle, ctypes.byref(info))
        r = info.rcMonitor
        found.append(
            {
                "index": len(found),
                "left": r.left,
                "top": r.top,
                "width": r.right - r.left,
                "height": r.bottom - r.top,
                "primary": bool(info.dwFlags & 1),
                "device": info.szDevice,
            }
        )
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(callback), 0)
    return found


def _virtual_screen() -> tuple[int, int, int, int]:
    return (
        user32.GetSystemMetrics(76),  # SM_XVIRTUALSCREEN
        user32.GetSystemMetrics(77),
        user32.GetSystemMetrics(78),  # SM_CXVIRTUALSCREEN
        user32.GetSystemMetrics(79),
    )


# ----------------------------------------------------------------- windows


def _title(hwnd) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def _class(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _rect(hwnd) -> dict:
    r = RECT()
    # extended frame bounds: the visible window without the invisible resize border
    if dwmapi.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return {"left": r.left, "top": r.top, "width": r.right - r.left, "height": r.bottom - r.top}


def _cloaked(hwnd) -> bool:
    value = wintypes.DWORD()
    return dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value)) == 0 and bool(
        value.value
    )


def windows() -> list[dict]:
    """Top-level windows a person can see or restore from the taskbar, front to back."""
    _require_windows()
    _dpi_aware()
    import psutil

    foreground = user32.GetForegroundWindow()
    out: list[dict] = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or _cloaked(hwnd):
            return True
        title = _title(hwnd)
        ex_style = user32.GetWindowLongW(hwnd, -20)
        if not title or ex_style & 0x00000080:  # WS_EX_TOOLWINDOW
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = psutil.Process(pid.value).name()
        except psutil.Error:
            process = ""
        out.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "process": process,
                "pid": pid.value,
                "class": _class(hwnd),
                **_rect(hwnd),
                "minimized": bool(user32.IsIconic(hwnd)),
                "maximized": bool(user32.IsZoomed(hwnd)),
                "foreground": hwnd == foreground,
            }
        )
        return True

    user32.EnumWindows(WNDENUMPROC(callback), 0)
    return out


def find_window(selector: str | int) -> dict:
    """A window by handle, title substring or process name ("notepad.exe"); the frontmost wins."""
    candidates = windows()
    if isinstance(selector, int) or (isinstance(selector, str) and selector.isdigit()):
        for w in candidates:
            if w["hwnd"] == int(selector):
                return w
        raise DesktopError(f"no visible window with handle {selector}")
    needle = str(selector).lower()
    for w in candidates:
        if needle == w["process"].lower() or needle == w["process"].lower().removesuffix(".exe"):
            return w
    for w in candidates:
        if needle in w["title"].lower():
            return w
    titles = ", ".join(f"{w['title'][:40]!r}" for w in candidates[:15])
    raise DesktopError(f"no window matches {selector!r}; visible: {titles}")


def _force_foreground(hwnd) -> bool:
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() == hwnd:
        return True
    # Windows only lets the process that owns the foreground hand it over. Sharing the input
    # queue of the current foreground thread (and the target's) makes this thread count as it.
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    me = kernel32.GetCurrentThreadId()
    foreground = user32.GetForegroundWindow()
    threads = {
        t
        for t in (
            user32.GetWindowThreadProcessId(foreground, None) if foreground else 0,
            user32.GetWindowThreadProcessId(hwnd, None),
        )
        if t and t != me
    }
    for thread in threads:
        user32.AttachThreadInput(me, thread, True)
    try:
        user32.keybd_event(0x12, 0, 0, 0)  # a tapped Alt also counts as recent input
        user32.keybd_event(0x12, 0, 2, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        for thread in threads:
            user32.AttachThreadInput(me, thread, False)
    time.sleep(0.15)
    if user32.GetForegroundWindow() != hwnd:
        user32.SwitchToThisWindow(hwnd, True)
        time.sleep(0.2)
    return user32.GetForegroundWindow() == hwnd


def window_action(
    selector: str | int,
    action: str,
    left: int | None = None,
    top: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict:
    _require_windows()
    w = find_window(selector)
    hwnd = wintypes.HWND(w["hwnd"])
    if action == "focus":
        ok = _force_foreground(hwnd)
        if not ok:
            raise DesktopError(
                "Windows refused to bring the window forward (a full-screen app or a UAC prompt?)"
            )
    elif action == "minimize":
        user32.ShowWindow(hwnd, 6)
    elif action == "maximize":
        user32.ShowWindow(hwnd, 3)
    elif action == "restore":
        user32.ShowWindow(hwnd, 9)
    elif action == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the app may ask to save first
    elif action == "move":
        rect = w
        user32.ShowWindow(hwnd, 9)
        user32.SetWindowPos(
            hwnd,
            None,
            rect["left"] if left is None else left,
            rect["top"] if top is None else top,
            rect["width"] if width is None else width,
            rect["height"] if height is None else height,
            0x0004 | 0x0010,  # SWP_NOZORDER | SWP_NOACTIVATE
        )
    else:
        raise DesktopError("action must be one of focus, minimize, maximize, restore, close, move")
    time.sleep(0.2)
    try:
        return {"action": action, "window": find_window(w["hwnd"])}
    except DesktopError:
        return {"action": action, "window": None, "note": "the window is gone"}


# --------------------------------------------------------- classic controls
#
# Win32, WinForms, VCL (Delphi), MFC and installer (Inno/NSIS) controls are real child windows.
# The managed UI Automation client sees many of them only as nameless "Pane"s, so they are read
# and driven here with the standard control messages, which also works while the window is in
# the background.

_REAL_TYPES = {
    "EDIT": "Edit",
    "RICHEDIT20W": "Edit",
    "RICHEDIT50W": "Edit",
    "COMBOBOX": "ComboBox",
    "LISTBOX": "List",
    "STATIC": "Text",
    "SYSLISTVIEW32": "List",
    "SYSTREEVIEW32": "Tree",
    "MSCTLS_TRACKBAR32": "Slider",
    "MSCTLS_PROGRESS32": "ProgressBar",
    "SYSTABCONTROL32": "Tab",
}


def _real_class(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.RealGetWindowClassW(hwnd, buffer, 256)  # the system class a control is built on
    return buffer.value


def _send_timeout(hwnd, message: int, wparam: int = 0, lparam=None) -> int | None:
    result = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, message, wparam, lparam, 0x0002, 3000, ctypes.byref(result))
    return result.value if ok else None  # SMTO_ABORTIFHUNG


def _control_text(hwnd) -> str | None:
    length = _send_timeout(hwnd, 0x000E)  # WM_GETTEXTLENGTH
    if length is None:
        return None
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(min(length, 65535) + 1)
    if _send_timeout(hwnd, 0x000D, len(buffer), buffer) is None:  # WM_GETTEXT
        return None
    return buffer.value


if IS_WINDOWS:

    class VARIANT(ctypes.Structure):
        _fields_ = [("vt", ctypes.c_ushort), ("pad", ctypes.c_ushort * 3), ("value", ctypes.c_longlong),
                    ("extra", ctypes.c_void_p)]  # fmt: skip

    _IID_IACCESSIBLE = (ctypes.c_byte * 16).from_buffer_copy(
        uuid.UUID("618736e0-3c3d-11cf-810c-00aa00389b71").bytes_le
    )


def _msaa_role_state(hwnd) -> tuple[int, int] | None:
    """Role and state from the control's accessibility object (owner-drawn buttons, as WinForms
    and Delphi draw them, only tell there whether they are a checkbox and whether it is checked)."""
    ole32, oleacc = ctypes.windll.ole32, ctypes.windll.oleacc
    initialized = ole32.CoInitializeEx(None, 2) in (0, 1)
    pointer = ctypes.c_void_p()
    try:
        if oleacc.AccessibleObjectFromWindow(
            hwnd, ctypes.c_long(-4).value & 0xFFFFFFFF, ctypes.byref(_IID_IACCESSIBLE), ctypes.byref(pointer)
        ) or not pointer.value:  # fmt: skip
            return None
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        getter = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, VARIANT, ctypes.POINTER(VARIANT))
        values = []
        for slot in (13, 14):  # IAccessible::get_accRole, get_accState
            out = VARIANT()
            status = getter(table[slot])(pointer, VARIANT(vt=3), ctypes.byref(out))  # CHILDID_SELF
            values.append(out.value & 0xFFFFFFFF if status == 0 and out.vt == 3 else None)
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(pointer)  # Release
        return (values[0], values[1]) if all(value is not None for value in values) else None
    except OSError:
        return None
    finally:
        if initialized:
            ole32.CoUninitialize()


def _control_type(hwnd) -> str | None:
    real = _real_class(hwnd).upper()
    if real == "BUTTON":
        kind = user32.GetWindowLongW(hwnd, -16) & 0x0F
        if kind == 0x0B:  # owner-drawn: ask its accessibility object what it is
            role = (_msaa_role_state(hwnd) or (0, 0))[0]
            return {0x2C: "CheckBox", 0x2D: "RadioButton"}.get(role, "Button")
        if kind in (2, 3, 5, 6):
            return "CheckBox"
        if kind in (4, 9):
            return "RadioButton"
        return "Group" if kind == 7 else "Button"
    return _REAL_TYPES.get(real)


def _toggle_state(hwnd) -> str | None:
    if user32.GetWindowLongW(hwnd, -16) & 0x0F == 0x0B:
        role_state = _msaa_role_state(hwnd)
        if role_state is None:
            return None
        state = role_state[1]
        return "On" if state & 0x10 else "Indeterminate" if state & 0x20 else "Off"
    return {0: "Off", 1: "On", 2: "Indeterminate"}.get(_send_timeout(hwnd, 0x00F0))  # BM_GETCHECK


def native_controls(top: int) -> list[dict]:
    """The visible classic controls inside a window, in tab order."""
    _require_windows()
    _dpi_aware()
    handles: list[int] = []
    user32.EnumChildWindows(wintypes.HWND(top), WNDENUMPROC(lambda h, _: handles.append(h) or True), 0)
    out = []
    for handle in handles:
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindowVisible(hwnd):
            continue
        kind = _control_type(hwnd)
        if kind is None:
            continue
        r = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        control = {
            "hwnd": int(handle),
            "type": kind,
            "name": _title(hwnd) if kind != "Edit" else "",
            "class": _class(hwnd),
            "enabled": bool(user32.IsWindowEnabled(hwnd)),
            "rect": [r.left, r.top, r.right - r.left, r.bottom - r.top],
        }
        if kind in ("Edit", "ComboBox"):
            secret = kind == "Edit" and user32.GetWindowLongW(hwnd, -16) & 0x20  # ES_PASSWORD
            text = "(hidden)" if secret else _control_text(hwnd)
            control["value"] = text[:2000] if text is not None else None
        if kind in ("CheckBox", "RadioButton"):
            control["toggle"] = _toggle_state(hwnd)
        out.append(control)
    return out


def native_act(control: dict, verb: str, value: str | None = None) -> str | None:
    """Operate a classic control; None when this kind of control cannot do `verb` this way."""
    hwnd = wintypes.HWND(control["hwnd"])
    if not user32.IsWindow(hwnd):
        raise DesktopError("the control is gone")
    if not user32.IsWindowEnabled(hwnd):
        raise DesktopError(f"the control is disabled: {control.get('name') or control['type']}")
    kind = control["type"]
    if verb in ("invoke", "toggle", "select") and kind in ("Button", "CheckBox", "RadioButton"):
        # posted: a button that opens a modal dialog must not block this call
        user32.PostMessageW(hwnd, 0x00F5, 0, 0)  # BM_CLICK
        time.sleep(0.15)
        return {"Button": "invoked", "CheckBox": "toggled", "RadioButton": "selected"}[kind]
    if verb == "set_value" and kind in ("Edit", "ComboBox"):
        if _send_timeout(hwnd, 0x000C, 0, ctypes.c_wchar_p(value or "")) is None:  # WM_SETTEXT
            raise DesktopError("the window did not respond")
        return "value set"
    return None


# ------------------------------------------------------------- screenshots

_last_shot: dict | None = None  # origin + scale of the latest screenshot, for input coordinates
_lock = threading.Lock()


def _print_window(hwnd, width: int, height: int):
    from PIL import Image

    screen_dc = user32.GetDC(None)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):  # PW_RENDERFULLCONTENT
            return None
        header = (ctypes.c_uint32 * 10)(40, width, (-height) & 0xFFFFFFFF, 1 | (32 << 16), 0, 0, 0, 0, 0, 0)
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, header, 0):
            return None
        return Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    finally:
        gdi32.SelectObject(memory_dc, old)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


def screenshot(
    window: str | int | None = None,
    monitor: int | None = None,
    region: list[int] | None = None,
    max_side: int = media.MAX_SIDE,
) -> tuple[bytes, str, dict]:
    """Capture the whole desktop, one monitor, a region [left, top, width, height] or a window
    (even when covered by others). Returns the encoded image and how its pixels map to the screen."""
    global _last_shot
    _require_windows()
    _dpi_aware()
    from PIL import ImageGrab

    target: dict = {}
    image = None
    if window is not None:
        w = find_window(window)
        if w["minimized"]:
            raise DesktopError("the window is minimized; restore it first (window action=restore)")
        target = {"window": {k: w[k] for k in ("hwnd", "title", "process")}}
        left, top, width, height = w["left"], w["top"], w["width"], w["height"]
        full = RECT()
        user32.GetWindowRect(wintypes.HWND(w["hwnd"]), ctypes.byref(full))
        image = _print_window(wintypes.HWND(w["hwnd"]), full.right - full.left, full.bottom - full.top)
        if image is not None:
            # trim the invisible resize border so the picture matches the visible frame
            image = image.crop(
                (left - full.left, top - full.top, left - full.left + width, top - full.top + height)
            )
            if not image.getbbox():  # all black: GPU-drawn content PrintWindow cannot copy
                image = None
    elif region is not None:
        if len(region) != 4:
            raise DesktopError("region is [left, top, width, height] in screen pixels")
        left, top, width, height = region
    elif monitor is not None:
        screens = monitors()
        if not 0 <= monitor < len(screens):
            raise DesktopError(f"monitor must be 0..{len(screens) - 1}")
        m = screens[monitor]
        left, top, width, height = m["left"], m["top"], m["width"], m["height"]
        target = {"monitor": monitor}
    else:
        left, top, width, height = _virtual_screen()
        target = {"desktop": True}
    if image is None:
        image = ImageGrab.grab(bbox=(left, top, left + width, top + height), all_screens=True)
    data, fmt, facts = media.encode_image(image, max_side)
    saved = _keep_original(image)
    if saved:
        facts["original_file"] = saved
    facts.update(target)
    facts.update({"screen_left": left, "screen_top": top})
    with _lock:
        _last_shot = {"left": left, "top": top, "scale": facts["scale"]}
    facts["coordinates"] = (
        "input(x, y) uses this image's pixels; they map to the screen as "
        f"screen = ({left}, {top}) + pixel / {facts['scale']}"
    )
    return data, fmt, facts


SHOTS_KEPT = 30


def _keep_original(image) -> str | None:
    """Save the full-resolution capture (the returned image may be scaled down) so details can
    be read later with fs_read on a region or handed over with fs_send. Keeps the latest few."""
    from .config import home

    try:
        folder = home() / "shots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"shot-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
        image.save(path, "PNG", compress_level=1)
        for old in sorted(folder.glob("shot-*.png"))[:-SHOTS_KEPT]:
            old.unlink(missing_ok=True)
        return str(path)
    except OSError:
        return None


def to_screen(x: float, y: float, space: str = "screenshot") -> tuple[int, int]:
    if space == "screen":
        return round(x), round(y)
    with _lock:
        shot = _last_shot
    if shot is None:
        raise DesktopError("no screenshot yet: take one first, or pass coordinates='screen'")
    return round(shot["left"] + x / shot["scale"]), round(shot["top"] + y / shot["scale"])


# ------------------------------------------------------------------- input

if IS_WINDOWS:

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]


def _send(*events) -> None:
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise DesktopError(
            "Windows blocked the input: the screen may be locked, a UAC prompt may be showing, "
            "or the target window runs as administrator"
        )


def _mouse(flags: int, x: int = 0, y: int = 0, data: int = 0):
    event = INPUT(type=0)
    event.u.mi = MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0)
    return event


def _move_event(x: int, y: int):
    vx, vy, vw, vh = _virtual_screen()
    nx = round((x - vx) * 65535 / max(vw - 1, 1))
    ny = round((y - vy) * 65535 / max(vh - 1, 1))
    return _mouse(0x0001 | 0x8000 | 0x4000, nx, ny)  # MOVE | ABSOLUTE | VIRTUALDESK


_BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}

_VK = {
    "ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "capslock": 0x14, "printscreen": 0x2C, "menu": 0x5D, "apps": 0x5D,
    "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD, "playpause": 0xB3,
}  # fmt: skip
_EXTENDED = {0x2E, 0x2D, 0x24, 0x23, 0x21, 0x22, 0x26, 0x28, 0x25, 0x27, 0x5B, 0x5D}


def _vk(name: str) -> int:
    key = name.strip().lower()
    if key in _VK:
        return _VK[key]
    if len(key) >= 2 and key[0] == "f" and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return 0x70 + int(key[1:]) - 1
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    punctuation = {";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
                   "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}  # fmt: skip
    if key in punctuation:
        return punctuation[key]
    raise DesktopError(f"unknown key {name!r} (examples: ctrl+c, alt+tab, win+r, enter, f5, pagedown)")


def _key(vk: int, up: bool = False):
    event = INPUT(type=1)
    event.u.ki = KEYBDINPUT(vk, 0, (0x0002 if up else 0) | (0x0001 if vk in _EXTENDED else 0), 0, 0)
    return event


def press(combo: str) -> None:
    """'ctrl+shift+esc', 'alt+f4', 'enter' ..."""
    codes = [_vk(part) for part in combo.split("+") if part.strip()]
    if not codes:
        raise DesktopError("empty key combination")
    _send(*[_key(c) for c in codes], *[_key(c, up=True) for c in reversed(codes)])


def type_text(text: str) -> None:
    """Type any text (Chinese included) as Unicode keystrokes, independent of the keyboard layout."""
    events = []
    for char in text:
        if char == "\n":
            events += [_key(0x0D), _key(0x0D, up=True)]
            continue
        if char == "\r":
            continue
        units = char.encode("utf-16-le")
        for i in range(0, len(units), 2):
            code = int.from_bytes(units[i : i + 2], "little")
            down = INPUT(type=1)
            down.u.ki = KEYBDINPUT(0, code, 0x0004, 0, 0)  # KEYEVENTF_UNICODE
            up = INPUT(type=1)
            up.u.ki = KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, 0)
            events += [down, up]
    for start in range(0, len(events), 200):  # keep bursts small so apps keep up
        _send(*events[start : start + 200])
        time.sleep(0.01)


def input_action(
    action: str,
    x: float | None = None,
    y: float | None = None,
    to_x: float | None = None,
    to_y: float | None = None,
    button: str = "left",
    text: str | None = None,
    keys: str | None = None,
    amount: int = 3,
    coordinates: str = "screenshot",
) -> dict:
    _require_windows()
    _dpi_aware()
    done: dict = {"action": action}
    point = None
    if x is not None and y is not None:
        point = to_screen(x, y, coordinates)
        done["screen"] = list(point)
    if action in ("click", "double_click", "right_click", "move", "mouse_down", "mouse_up"):
        if action == "right_click":
            button = "right"
        if button not in _BUTTONS:
            raise DesktopError("button is left, right or middle")
        down, up = _BUTTONS[button]
        events = [_move_event(*point)] if point else []
        if action == "click" or action == "right_click":
            events += [_mouse(down), _mouse(up)]
        elif action == "double_click":
            events += [_mouse(down), _mouse(up), _mouse(down), _mouse(up)]
        elif action == "mouse_down":
            events += [_mouse(down)]
        elif action == "mouse_up":
            events += [_mouse(up)]
        if not events:
            raise DesktopError("give x and y")
        _send(*events)
    elif action == "drag":
        if point is None or to_x is None or to_y is None:
            raise DesktopError("drag needs x, y, to_x, to_y")
        end = to_screen(to_x, to_y, coordinates)
        down, up = _BUTTONS.get(button, _BUTTONS["left"])
        _send(_move_event(*point), _mouse(down))
        for step in range(1, 11):  # a few intermediate points so apps see a real drag
            time.sleep(0.02)
            _send(
                _move_event(
                    point[0] + (end[0] - point[0]) * step // 10, point[1] + (end[1] - point[1]) * step // 10
                )
            )
        _send(_mouse(up))
        done["to_screen"] = list(end)
    elif action in ("scroll", "hscroll"):
        events = [_move_event(*point)] if point else []
        flag = 0x0800 if action == "scroll" else 0x01000  # WHEEL / HWHEEL
        events.append(_mouse(flag, data=int(amount) * 120))
        _send(*events)
    elif action == "type":
        if not text:
            raise DesktopError("type needs text")
        if point:
            _send(_move_event(*point), _mouse(0x0002), _mouse(0x0004))
            time.sleep(0.1)
        type_text(text)
    elif action == "keys":
        if not keys:
            raise DesktopError("keys needs a combination such as ctrl+s or a sequence like 'tab tab enter'")
        for combo in keys.split():
            press(combo)
            time.sleep(0.05)
    else:
        raise DesktopError(
            "action is one of click, double_click, right_click, move, drag, scroll, hscroll, "
            "mouse_down, mouse_up, type, keys"
        )
    time.sleep(0.15)
    foreground = user32.GetForegroundWindow()
    done["foreground"] = _title(foreground) if foreground else ""
    return done


# --------------------------------------------------------------- clipboard


def clipboard_get(max_side: int = media.MAX_SIDE) -> dict:
    """{'kind': 'text'|'image'|'files'|'empty', ...}; images come back encoded."""
    _require_windows()
    from PIL import Image, ImageGrab

    try:
        grabbed = ImageGrab.grabclipboard()
    except OSError:
        grabbed = None
    if isinstance(grabbed, Image.Image):
        data, fmt, facts = media.encode_image(grabbed, max_side)
        return {"kind": "image", "image": (data, fmt), **facts}
    if isinstance(grabbed, list) and grabbed:
        return {"kind": "files", "files": grabbed}
    text = _clipboard_text()
    if text is None:
        return {"kind": "empty"}
    return {"kind": "text", "text": text}


def _open_clipboard() -> None:
    for _ in range(20):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise DesktopError("the clipboard is busy (another program holds it)")


def _clipboard_text() -> str | None:
    user32.GetClipboardData.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    _open_clipboard()
    try:
        handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def _set_clipboard(format_id: int, payload: bytes) -> None:
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    handle = kernel32.GlobalAlloc(0x0002, len(payload))  # GMEM_MOVEABLE
    pointer = kernel32.GlobalLock(handle)
    ctypes.memmove(pointer, payload, len(payload))
    kernel32.GlobalUnlock(handle)
    _open_clipboard()
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(format_id, handle):
            raise DesktopError("could not place data on the clipboard")
    finally:
        user32.CloseClipboard()


def clipboard_set(text: str | None = None, image_path: str | None = None) -> dict:
    _require_windows()
    if image_path:
        from PIL import Image

        with Image.open(image_path) as image:
            buffer = __import__("io").BytesIO()
            image.convert("RGB").save(buffer, "BMP")
        _set_clipboard(8, buffer.getvalue()[14:])  # CF_DIB: a BMP without its file header
        return {"kind": "image", "from": image_path}
    if text is None:
        raise DesktopError("give text or image_path")
    _set_clipboard(13, (text + "\0").encode("utf-16-le"))
    return {"kind": "text", "chars": len(text)}
