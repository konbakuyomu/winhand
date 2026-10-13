"""Windows' own UI Automation client (IUIAutomation), used in process through comtypes.

The managed System.Windows.Automation client that PowerShell offers sees the menus, trees, lists
and toolbars of classic Win32 programs as bare panes; the native client sees their items and can
operate them, and needs no PowerShell start per call. Every call runs on one worker thread in the
multithreaded COM apartment. A program that stops answering costs that thread, not winhand: the
call fails after TIMEOUT_S and the next one gets a fresh thread.
"""

from __future__ import annotations

import queue
import sys
import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeout

from . import desktop

TIMEOUT_S = 20.0
MAX_ELEMENTS = 600
# kept even without a name: what a person could operate
ACTIONABLE = {
    "Button", "SplitButton", "Edit", "CheckBox", "RadioButton", "ComboBox", "ListItem", "DataItem",
    "MenuItem", "TabItem", "TreeItem", "Hyperlink", "Slider", "Document",
}  # fmt: skip
PATTERNS = ("Invoke", "Toggle", "SelectionItem", "ExpandCollapse", "Value", "ScrollItem")
TOGGLE = {0: "Off", 1: "On", 2: "Indeterminate"}
EXPANDED = {0: "Collapsed", 1: "Expanded", 2: "PartiallyExpanded", 3: "LeafNode"}


class Unavailable(desktop.DesktopError):
    """The native client cannot be used on this machine; the PowerShell script takes over."""


class NoAnswer(desktop.DesktopError):
    """The program behind the window did not answer in time."""


# ------------------------------------------------------------------ worker


class _Worker:
    def __init__(self) -> None:
        self.jobs: queue.SimpleQueue = queue.SimpleQueue()
        # a daemon thread: one stuck in an unresponsive program must not hold up winhand's exit
        threading.Thread(target=self._loop, name="winhand-uia", daemon=True).start()

    def _loop(self) -> None:
        try:
            _enter_apartment()
            broken = None
        except Exception as exc:  # noqa: BLE001 - reported with every call instead
            broken = Unavailable(f"COM could not start: {type(exc).__name__}: {exc}")
        while True:
            fn, args, future = self.jobs.get()
            if not future.set_running_or_notify_cancel():
                continue
            if broken is not None:
                future.set_exception(broken)
                continue
            try:
                future.set_result(fn(*args))
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)

    def submit(self, fn, *args) -> Future:
        future: Future = Future()
        self.jobs.put((fn, args, future))
        return future


def _enter_apartment() -> None:
    if getattr(sys, "coinit_flags", None) is None:
        sys.coinit_flags = 0  # comtypes initializes the thread that imports it: make that the MTA
    import comtypes

    comtypes.CoInitializeEx(0)  # COINIT_MULTITHREADED; S_FALSE when the import already did it


_lock = threading.Lock()
_worker: _Worker | None = None
_client = None  # (generated UIAutomationClient module, IUIAutomation)


def call(fn, *args, timeout: float = TIMEOUT_S):
    """Run fn(*args) on the UI Automation thread."""
    global _worker
    if not desktop.IS_WINDOWS:
        raise Unavailable("UI Automation is only available on Windows")
    with _lock:
        if _worker is None:
            _worker = _Worker()
        worker = _worker
    future = worker.submit(fn, *args)
    try:
        return future.result(timeout=timeout)
    except desktop.DesktopError:
        raise
    except FutureTimeout:
        future.cancel()  # if it has not started, it never will
        with _lock:
            if _worker is worker:
                _worker = None  # that thread still waits on the program; the next call gets a new one
        raise NoAnswer(NO_ANSWER) from None
    except Exception as exc:  # noqa: BLE001 - COM errors: the control went away, is disabled ...
        raise explain(exc) from exc


NO_ANSWER = (
    "the program did not answer UI Automation in time (it may be busy, hung, or showing a dialog "
    "it opened); an action may still have taken effect: look before repeating it"
)
# UI Automation's own error codes share their numbers with unrelated COM errors, so Windows' message
# texts for them are misleading ("An event was unable to invoke any of the subscribers")
_HRESULTS = {
    0x80040200: "the control is disabled right now",
    0x80040201: "the control is gone (the window changed); inspect it again",
    0x80040202: "the control has no point that can be clicked",
    0x80131509: "the control refused this operation in its current state",
    0x80070005: "access denied (is the program running as administrator?)",
}


def explain(exc: Exception) -> desktop.DesktopError:
    """A COM failure from UI Automation as an error a person can read."""
    code = getattr(exc, "hresult", None)
    if code is None and exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]
    if code is None:
        code = getattr(exc, "winerror", None)
    code = code & 0xFFFFFFFF if isinstance(code, int) else None
    if code == 0x80131505:  # UIA_E_TIMEOUT: UI Automation's own time limit
        return NoAnswer(NO_ANSWER)
    if code in _HRESULTS:
        return desktop.DesktopError(f"UI Automation: {_HRESULTS[code]}")
    return desktop.DesktopError(f"UI Automation failed: {type(exc).__name__}: {exc}")


def _automation():
    global _client
    if _client is not None:
        return _client
    try:
        import comtypes.client

        try:
            from comtypes.gen import UIAutomationClient as module  # generated at build time
        except ImportError:
            module = comtypes.client.GetModule("UIAutomationCore.dll")
        try:
            automation = comtypes.client.CreateObject(module.CUIAutomation8, interface=module.IUIAutomation2)
            automation.ConnectionTimeout = 5000
            automation.TransactionTimeout = 10000
        except Exception:  # noqa: BLE001 - older Windows: no CUIAutomation8 / IUIAutomation2
            automation = comtypes.client.CreateObject(module.CUIAutomation, interface=module.IUIAutomation)
    except Exception as exc:  # noqa: BLE001 - comtypes missing, type library or COM failure
        raise Unavailable(f"{type(exc).__name__}: {exc}") from exc
    _client = (module, automation)
    return _client


def status() -> str:
    """'native' when the in-process client works, else 'powershell: <why>'."""
    try:
        call(_automation, timeout=60)
        return "native"
    except desktop.DesktopError as exc:
        return f"powershell: {exc}"


# ----------------------------------------------------------------- reading


def _property_ids(m) -> dict[str, int]:
    return {
        "name": m.UIA_NamePropertyId,
        "automation_id": m.UIA_AutomationIdPropertyId,
        "type": m.UIA_ControlTypePropertyId,
        "class": m.UIA_ClassNamePropertyId,
        "hwnd": m.UIA_NativeWindowHandlePropertyId,
        "enabled": m.UIA_IsEnabledPropertyId,
        "focused": m.UIA_HasKeyboardFocusPropertyId,
        "rect": m.UIA_BoundingRectanglePropertyId,
        "offscreen": m.UIA_IsOffscreenPropertyId,
        "password": m.UIA_IsPasswordPropertyId,
        "runtime_id": m.UIA_RuntimeIdPropertyId,
        "Invoke": m.UIA_IsInvokePatternAvailablePropertyId,
        "Toggle": m.UIA_IsTogglePatternAvailablePropertyId,
        "SelectionItem": m.UIA_IsSelectionItemPatternAvailablePropertyId,
        "ExpandCollapse": m.UIA_IsExpandCollapsePatternAvailablePropertyId,
        "Value": m.UIA_IsValuePatternAvailablePropertyId,
        "ScrollItem": m.UIA_IsScrollItemPatternAvailablePropertyId,
        "value": m.UIA_ValueValuePropertyId,
        "toggle": m.UIA_ToggleToggleStatePropertyId,
        "selected": m.UIA_SelectionItemIsSelectedPropertyId,
        "expanded": m.UIA_ExpandCollapseExpandCollapseStatePropertyId,
    }


def type_names(m) -> dict[int, str]:
    return {
        getattr(m, n): n[len("UIA_") : -len("ControlTypeId")]
        for n in dir(m)
        if n.startswith("UIA_") and n.endswith("ControlTypeId")
    }


def _rect(value) -> list[int] | None:
    try:
        left, top, width, height = (round(float(v)) for v in value)
    except (TypeError, ValueError):
        return None
    return [left, top, width, height] if width > 0 and height > 0 else None


def describe(raw: dict, types: dict[int, str], keep: bool = False) -> dict | None:
    """One element as ui lists it, from its cached properties; None when it is not worth listing
    (the same choice as the PowerShell script made) unless `keep`."""
    kind = types.get(raw.get("type"), "") if isinstance(raw.get("type"), int) else ""
    name = raw.get("name") or ""
    if not isinstance(name, str):
        name = ""
    automation_id = raw.get("automation_id") if isinstance(raw.get("automation_id"), str) else ""
    hwnd = raw.get("hwnd") if isinstance(raw.get("hwnd"), int) else 0
    if not keep and raw.get("offscreen") is True and not name:
        return None
    if not keep and not (name or automation_id or hwnd or kind in ACTIONABLE):
        return None
    patterns = [p for p in PATTERNS if raw.get(p) is True]
    runtime_id = raw.get("runtime_id")
    item = {
        "type": kind,
        "name": name,
        "automation_id": automation_id,
        "class": raw.get("class") if isinstance(raw.get("class"), str) else "",
        "hwnd": hwnd,
        "enabled": raw.get("enabled") is True,
        "focused": raw.get("focused") is True,
        "_runtime_id": [int(v) for v in runtime_id] if isinstance(runtime_id, (tuple, list)) else [],
        "rect": _rect(raw.get("rect")),
        "patterns": patterns,
    }
    if raw.get("offscreen") is True:
        item["offscreen"] = True
    if "Value" in patterns:
        value = raw.get("value")
        if raw.get("password") is True:
            item["value"] = "(hidden)"
        else:
            item["value"] = value[:2000] if isinstance(value, str) else None
    if "Toggle" in patterns:
        item["toggle"] = TOGGLE.get(raw.get("toggle"))
    if "SelectionItem" in patterns:
        item["selected"] = raw.get("selected") is True
    if "ExpandCollapse" in patterns:
        item["expanded"] = EXPANDED.get(raw.get("expanded"))
    return item


def _cached(element, property_id):
    try:
        return element.GetCachedPropertyValue(property_id)
    except Exception:  # noqa: BLE001 - one unreadable property leaves the rest
        return None


_tables: dict = {}


def _request():
    """The cache request for every property ui shows, the property ids and the type names."""
    module, automation = _automation()
    if "ids" not in _tables:
        _tables["ids"] = _property_ids(module)
        _tables["types"] = type_names(module)
    request = automation.CreateCacheRequest()
    for property_id in _tables["ids"].values():
        request.AddProperty(property_id)
    return request, _tables["ids"], _tables["types"]


def _snapshot(hwnd: int, popups: list[int]) -> dict:
    module, automation = _automation()
    request, ids, types = _request()
    root = automation.ElementFromHandle(hwnd)
    try:
        title = root.CurrentName or ""
    except Exception:  # noqa: BLE001
        title = ""
    seen_windows: set[int] = set()
    elements: list[dict] = []
    total = 0

    def collect(top) -> None:
        nonlocal total
        found = top.FindAllBuildCache(module.TreeScope_Descendants, automation.ControlViewCondition, request)
        for i in range(found.Length):
            element = found.GetElement(i)
            raw = {key: _cached(element, property_id) for key, property_id in ids.items()}
            if isinstance(raw.get("hwnd"), int) and raw["hwnd"]:
                seen_windows.add(raw["hwnd"])
            item = describe(raw, types)
            if item is None:
                continue
            total += 1
            if len(elements) < MAX_ELEMENTS:
                item["index"] = len(elements)
                item["_element"] = element
                elements.append(item)

    collect(root)
    for popup in popups:  # an open menu that UI Automation did not hang under the window
        if popup not in seen_windows:
            try:
                collect(automation.ElementFromHandle(popup))
            except Exception:  # noqa: BLE001 - the menu closed meanwhile
                pass
    return {"window": title, "count": total, "elements": elements}


def snapshot(hwnd: int, timeout: float = TIMEOUT_S) -> dict:
    """The window's controls, as the PowerShell script lists them, each with its live element."""
    return call(_snapshot, hwnd, desktop.menus_of(hwnd), timeout=timeout)


def _refresh(element) -> dict | None:
    request, ids, types = _request()
    try:
        fresh = element.BuildUpdatedCache(request)
    except Exception:  # noqa: BLE001 - the control (or its window) is gone
        return None
    item = describe({key: _cached(fresh, property_id) for key, property_id in ids.items()}, types, keep=True)
    item["_element"] = fresh
    return item


def refresh(element) -> dict | None:
    """The same control read again (its state after an action), None when it is gone. Reading the
    element itself is what identifies it: runtime ids of controls that Windows reaches through
    their old accessibility interface (classic menus, for one) change from one reading to the next."""
    return call(_refresh, element)


# ------------------------------------------------------------------ acting


def _pattern(element, pattern_id, interface):
    unknown = element.GetCurrentPattern(pattern_id)
    return unknown.QueryInterface(interface) if unknown else None


def _act(element, verb: str, value: str | None) -> str | None:
    m, _ = _automation()
    invoke = (m.UIA_InvokePatternId, m.IUIAutomationInvokePattern)
    toggle = (m.UIA_TogglePatternId, m.IUIAutomationTogglePattern)
    select = (m.UIA_SelectionItemPatternId, m.IUIAutomationSelectionItemPattern)
    expand = (m.UIA_ExpandCollapsePatternId, m.IUIAutomationExpandCollapsePattern)
    if verb == "invoke":
        for spec, method, done in (
            (invoke, "Invoke", "invoked"),
            (toggle, "Toggle", "toggled"),
            (select, "Select", "selected"),
            (expand, "Expand", "expanded"),
        ):
            pattern = _pattern(element, *spec)
            if pattern:
                getattr(pattern, method)()
                return done
        return None
    single = {
        "toggle": (toggle, "Toggle", "toggled"),
        "select": (select, "Select", "selected"),
        "expand": (expand, "Expand", "expanded"),
        "collapse": (expand, "Collapse", "collapsed"),
    }
    if verb in single:
        spec, method, done = single[verb]
        pattern = _pattern(element, *spec)
        if not pattern:
            return None
        getattr(pattern, method)()
        return done
    if verb == "set_value":
        pattern = _pattern(element, m.UIA_ValuePatternId, m.IUIAutomationValuePattern)
        if not pattern:
            return None
        pattern.SetValue(value or "")
        return "value set"
    if verb == "focus":
        element.SetFocus()
        return "focused"
    return None


def act(element, verb: str, value: str | None = None, timeout: float = TIMEOUT_S) -> str | None:
    """Operate the element through its UI Automation pattern; None when it offers none for verb."""
    return call(_act, element, verb, value, timeout=timeout)


def _scroll_into_view(element) -> list[int] | None:
    m, _ = _automation()
    pattern = _pattern(element, m.UIA_ScrollItemPatternId, m.IUIAutomationScrollItemPattern)
    if pattern:
        pattern.ScrollIntoView()
    return _rect(element.GetCurrentPropertyValue(m.UIA_BoundingRectanglePropertyId))


def scroll_into_view(element) -> list[int] | None:
    """Scroll an element into view where it can; its rectangle afterwards."""
    return call(_scroll_into_view, element)
