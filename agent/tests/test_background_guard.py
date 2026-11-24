from __future__ import annotations

import threading
import time

import pytest

from winhand import desktop, uia


@pytest.mark.parametrize(
    "class_name,kind",
    [
        ("ApplicationFrameWindow", "xaml"),
        ("Windows.UI.Core.CoreWindow", "xaml"),
        ("WinUIDesktopWin32WindowClass", "xaml"),
        ("Chrome_WidgetWin_1", "chromium"),
        ("HwndWrapper[app;;123]", "wpf"),
        ("Edit", "win32"),
    ],
)
def test_host_kind(class_name, kind):
    assert desktop.host_kind(class_name) == kind


class FakeUser32:
    def __init__(self):
        self.class_name, self.style, self.disabled = "Edit", 0x100, False
        self.calls = []

    def GetAncestor(self, hwnd, flag):
        assert (hwnd, flag) == (11, 2)
        return 22

    def GetClassNameW(self, hwnd, buffer, size):
        assert hwnd == 22
        buffer.value = self.class_name
        return len(self.class_name)

    def GetWindowLongPtrW(self, hwnd, index):
        assert (hwnd, index) == (22, desktop.GWL_EXSTYLE)
        self.calls.append(("read", self.style))
        return self.style

    def SetWindowLongPtrW(self, hwnd, index, style):
        assert (hwnd, index) == (22, desktop.GWL_EXSTYLE)
        self.calls.append(("style", style))
        self.style = style

    def EnableWindow(self, hwnd, enabled):
        assert hwnd == 22
        self.calls.append(("enabled", enabled))
        previous, self.disabled = self.disabled, not enabled
        return previous


@pytest.fixture
def user32(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(desktop, "user32", fake, raising=False)
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "_on_current_desktop", lambda hwnd: hwnd == 22)
    return fake


@pytest.mark.parametrize(
    "class_name,disable",
    [("Edit", False), ("ApplicationFrameWindow", True), ("Chrome_WidgetWin_1", True)],
)
def test_guard_restores_only_its_changes(user32, class_name, disable):
    user32.class_name = class_name
    guard = desktop.BackgroundGuard(11)
    guard.apply()
    assert guard.host == desktop.host_kind(class_name)
    assert user32.style == 0x100 | desktop.WS_EX_NOACTIVATE
    assert user32.disabled is disable
    assert ("read", user32.style) in user32.calls  # read-back after adding the bit
    user32.style |= 0x200
    guard.release()
    assert user32.style == 0x300 and not user32.disabled
    enabled = [call for call in user32.calls if call[0] == "enabled"]
    assert enabled == ([("enabled", False), ("enabled", True)] if disable else [])
    calls = user32.calls[:]
    guard.release()
    assert user32.calls == calls


def test_already_disabled_window_stays_disabled(user32):
    user32.class_name, user32.disabled = "ApplicationFrameWindow", True
    with desktop.background(11):
        assert user32.disabled
    assert user32.disabled
    assert [c for c in user32.calls if c[0] == "enabled"] == [("enabled", False), ("enabled", False)]


def test_existing_noactivate_is_not_removed(user32):
    user32.style |= desktop.WS_EX_NOACTIVATE
    with desktop.background(11):
        pass
    assert user32.style == 0x100 | desktop.WS_EX_NOACTIVATE
    assert not any(c[0] == "style" for c in user32.calls)


@pytest.fixture
def recorded_guard(monkeypatch):
    calls, released = [], threading.Event()

    class Guard:
        def __init__(self, hwnd):
            assert hwnd == 11

        def apply(self):
            calls.append("apply")

        def release(self):
            calls.append("release")
            released.set()

    monkeypatch.setattr(desktop, "BackgroundGuard", Guard)
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(uia, "_enter_apartment", lambda: None)
    monkeypatch.setattr(uia, "_worker", None)
    return calls, released


@pytest.mark.parametrize("fails", [False, True])
def test_act_releases_after_return_or_error(monkeypatch, recorded_guard, fails):
    def act(*args):
        if fails:
            raise desktop.DesktopError("action failed")
        return "invoked"

    monkeypatch.setattr(uia, "_act", act)
    if fails:
        with pytest.raises(desktop.DesktopError, match="action failed"):
            uia.act(object(), "invoke", hwnd=11)
    else:
        assert uia.act(object(), "invoke", hwnd=11) == "invoked"
    assert recorded_guard[0] == ["apply", "release"]


def test_act_keeps_guard_until_timed_out_call_finishes(monkeypatch, recorded_guard):
    monkeypatch.setattr(uia, "_act", lambda *args: time.sleep(1))
    with pytest.raises(uia.NoAnswer) as caught:
        uia.act(object(), "invoke", timeout=0.2, hwnd=11)
    calls, released = recorded_guard
    assert calls == ["apply"] and not caught.value.future.done()
    caught.value.future.result(timeout=2)
    assert released.wait(1)
    assert calls == ["apply", "release"]
