"""Epic 004 / Issue 002: a ui action that runs in the background (desktop.BackgroundGuard, nothing sent to
the foreground) goes on while the person uses the machine; foreground input still gives way."""

from __future__ import annotations

import contextlib
import types

import pytest
from fastmcp.client import Client

from winhand import desktop, presence
from winhand import tools_desktop as td
from winhand.config import Config
from winhand.server import build_server


@pytest.fixture
def clock(monkeypatch):
    """A fake clock for presence: GetTickCount, monotonic and sleep move together."""
    now = [1000.0]
    fake = types.SimpleNamespace(monotonic=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s))
    monkeypatch.setattr(presence, "time", fake)
    monkeypatch.setattr(presence, "_wall", lambda: now[0])
    monkeypatch.setattr(presence, "_tick", lambda: int(now[0] * 1000) & 0xFFFFFFFF)
    monkeypatch.setattr(presence, "_ours", None)
    monkeypatch.setattr(presence, "_dialogs", [])
    monkeypatch.setattr(presence, "_stopped", None)
    return now


def _person_typed(monkeypatch, clock, seconds_ago):
    tick = int((clock[0] - seconds_ago) * 1000)
    monkeypatch.setattr(presence, "_last_input_tick", lambda: tick)


def test_a_background_action_goes_on_while_the_person_types(monkeypatch, clock):
    _person_typed(monkeypatch, clock, 0.5)
    began = clock[0]
    with presence.acting("ui invoke", background=True) as said:
        assert said == {} and clock[0] == began
    with presence.acting("ui invoke") as said:  # a foreground one still waits
        assert said["waited_for_owner_s"] == pytest.approx(presence.IDLE_S - 0.5, abs=0.3)


def test_a_foreground_step_inside_a_background_action_waits_once(monkeypatch, clock):
    _person_typed(monkeypatch, clock, 0.5)
    with presence.acting("ui invoke", background=True) as outer:
        assert outer == {}
        with presence.acting("window focus") as focus:  # the real click a ui action falls back to
            assert focus["waited_for_owner_s"] == pytest.approx(presence.IDLE_S - 0.5, abs=0.3)
            _person_typed(monkeypatch, clock, 0.5)
            with presence.acting("input click") as click:
                assert click == {}  # this chain has waited already
        with presence.acting("window focus") as again:  # a second foreground step waits again
            assert "waited_for_owner_s" in again
    _person_typed(monkeypatch, clock, 0.5)
    with presence.acting("input click") as fresh:  # nesting state does not leak out
        assert "waited_for_owner_s" in fresh


def test_the_stop_keys_refuse_background_actions_too(monkeypatch, clock):
    monkeypatch.setattr(presence, "_last_input_tick", lambda: None)
    presence.stop_pressed()
    with pytest.raises(desktop.OwnerStopped, match="Ctrl\\+Alt\\+Shift\\+Backspace"):
        with presence.acting("ui invoke", background=True):
            pass
    presence.resume()


class _User32:
    def __init__(self, front, pids):
        self.front, self.pids = front, pids

    def GetForegroundWindow(self):
        return self.front

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self.pids.get(hwnd, 0)
        return 1


@pytest.mark.parametrize(
    "window,front,expected",
    [
        ({"class": "Edit", "pid": 200}, 5, True),  # the person works in another program
        ({"class": "Edit", "pid": 100}, 5, False),  # the person works in this one
        ({"class": "HwndWrapper[app;;1]", "pid": 200}, 5, False),  # WPF takes the focus itself
        ({"class": "ApplicationFrameWindow", "pid": 200}, 5, True),
        ({"class": "Edit", "pid": 200}, None, True),  # nothing in front
    ],
)
def test_background_needs_another_program_in_front_and_no_wpf(monkeypatch, window, front, expected):
    monkeypatch.setattr(desktop, "IS_WINDOWS", True)
    monkeypatch.setattr(desktop, "user32", _User32(front, {5: 100}), raising=False)
    assert desktop.background_ok(window) is expected


def test_never_background_off_windows(monkeypatch):
    monkeypatch.setattr(desktop, "IS_WINDOWS", False)
    assert desktop.background_ok({"class": "Edit", "pid": 200}) is False


async def test_the_ui_tool_asks_presence_for_a_background_action(monkeypatch):
    window = {"hwnd": 11, "title": "app", "process": "app.exe", "pid": 9, "class": "Edit"}
    monkeypatch.setattr(td.desktop, "find_window", lambda _: window)
    monkeypatch.setattr(td.desktop, "background_ok", lambda w: w is window)
    monkeypatch.setattr(td, "_ui_act", lambda w, action, **kw: {"done": "invoked"})
    asked = []

    @contextlib.contextmanager
    def acting(what, background=False):
        asked.append((what, background))
        yield {}

    monkeypatch.setattr(td.presence, "acting", acting)
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool("ui", {"window": "app", "action": "invoke", "name": "OK"})
        ).structured_content
    assert done["done"] == "invoked" and asked == [("ui invoke", True)]
