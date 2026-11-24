"""Epic 004 ticket 3: while the AI works on window A, nothing of an unrelated window B reaches its
replies. B's title only shows when A owns B (A's dialog or popup), the steps' picture is of A or
A's dialog, a window's picture is only ever its own drawing (never the screen in its place), and
keys never go to whatever window happens to be in front.

The Windows parts are simulated here; test_windows_scenarios.py drives the real thing.
"""

from __future__ import annotations

import ctypes
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastmcp.client import Client
from PIL import Image, ImageGrab
from test_review_round4 import _control, _fake_window, _snapshot

from winhand import desktop, ocr, secret_store, uia
from winhand import tools_desktop as td
from winhand.config import Config
from winhand.server import build_server

MARK = "private-chat-7f3a"  # in the unrelated window's title: it must not show anywhere

PICTURE = (b"\x89PNG", "png", {"width": 1})


def _shown(out) -> str:
    return json.dumps(out, ensure_ascii=False)


def _opening(monkeypatch, window, *opened_windows, owned=()):
    """uia.act opens these windows; root_owner gives the window for those in `owned`."""
    opened = []
    monkeypatch.setattr(td.desktop, "windows", lambda: [window, *opened_windows] if opened else [window])
    monkeypatch.setattr(td.desktop, "root_owner", lambda hwnd: window["hwnd"] if hwnd in owned else hwnd)
    acted = []
    monkeypatch.setattr(
        uia,
        "act",
        lambda e, verb, value=None, timeout=0, hwnd=None: acted.append(verb) or opened.append(1) or "invoked",
    )
    monkeypatch.setattr(uia, "refresh", lambda e: None)
    return acted


# ------------------------------------------------------------------ new windows


async def test_only_the_windows_own_dialogs_are_described(monkeypatch):
    window = _fake_window(monkeypatch)
    dialog = {"hwnd": 22, "title": "确认", "process": "app.exe", "pid": 9, "class": "#32770"}
    second = {"hwnd": 33, "title": f"app - {MARK}", "process": "app.exe", "pid": 9, "class": ""}
    other = {"hwnd": 44, "title": MARK, "process": "chat.exe", "pid": 5, "class": ""}
    _opening(monkeypatch, window, dialog, second, other, owned={22})
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("删除")]))
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool("ui", {"window": "app", "action": "invoke", "name": "删除"})
        ).structured_content
    assert [w["hwnd"] for w in done["new_windows"]] == [22]
    # a second window of the same program may be the person's too: counted, not described
    assert done["other_windows"] == 2 and "may be the person's" in done["other_windows_note"]
    assert MARK not in _shown(done)


def test_an_unrelated_window_does_not_end_the_wait_for_the_dialog(monkeypatch):
    window = {"hwnd": 11, "title": "app", "process": "app.exe", "pid": 9}
    other = {"hwnd": 44, "title": MARK, "process": "chat.exe", "pid": 5}
    dialog = {"hwnd": 22, "title": "确认", "process": "app.exe", "pid": 9, "class": "#32770"}
    listings = iter([[window, other], [window, other], [window, other, dialog]])
    monkeypatch.setattr(td.desktop, "windows", lambda: next(listings))
    monkeypatch.setattr(td.desktop, "root_owner", lambda hwnd: 11 if hwnd == 22 else hwnd)
    monkeypatch.setattr(td.time, "sleep", lambda s: None)
    own, others = td._new_windows({11}, window, settle_s=5)
    assert [w["hwnd"] for w in own] == [22] and others == 1


# ------------------------------------------------------------------ steps


async def test_steps_stop_when_a_window_it_does_not_own_opens(monkeypatch):
    window = _fake_window(monkeypatch)
    other = {"hwnd": 44, "title": MARK, "process": "chat.exe", "pid": 5, "class": ""}
    acted = _opening(monkeypatch, window, other)
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("下一步"), _control("完成")]))
    shots = []
    monkeypatch.setattr(
        td.desktop, "screenshot", lambda target=None, *a, **k: shots.append(target) or PICTURE
    )
    steps = [{"action": "invoke", "name": "下一步"}, {"action": "invoke", "name": "完成"}]
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("ui", {"window": "app", "steps": steps})).structured_content
    assert out["stopped"].startswith("UNEXPECTED_DIALOG: during step 1") and acted == ["invoke"]
    assert [s["step"] for s in out["steps"]] == [1] and out["steps"][0]["other_windows"] == 1
    assert shots == [11]  # the picture is of the window, not of the one that opened
    assert MARK not in _shown(out)


async def test_the_last_step_only_counts_the_window_it_does_not_own(monkeypatch):
    window = _fake_window(monkeypatch)
    other = {"hwnd": 44, "title": MARK, "process": "chat.exe", "pid": 5, "class": ""}
    _opening(monkeypatch, window, other)
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("完成")]))
    shots = []
    monkeypatch.setattr(
        td.desktop, "screenshot", lambda target=None, *a, **k: shots.append(target) or PICTURE
    )
    steps = [{"action": "invoke", "name": "完成"}]
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("ui", {"window": "app", "steps": steps})).structured_content
    assert "stopped" not in out and out["done"] == 1 and out["steps"][0]["other_windows"] == 1
    assert shots == [11] and MARK not in _shown(out)


async def test_the_steps_picture_follows_the_windows_own_dialog(monkeypatch):
    window = _fake_window(monkeypatch)
    dialog = {"hwnd": 22, "title": "确认", "process": "app.exe", "pid": 9, "class": "#32770"}
    _opening(monkeypatch, window, dialog, owned={22})
    monkeypatch.setattr(uia, "snapshot", _snapshot([_control("删除")]))
    shots = []
    monkeypatch.setattr(
        td.desktop, "screenshot", lambda target=None, *a, **k: shots.append(target) or PICTURE
    )
    async with Client(build_server(Config())) as client:
        out = (
            await client.call_tool("ui", {"window": "app", "steps": [{"action": "invoke", "name": "删除"}]})
        ).structured_content
    assert shots == [22] and out["steps"][0]["new_windows"][0]["title"] == "确认"


@pytest.mark.parametrize(
    "refusal",
    [desktop.DesktopError("Windows refused to bring the window forward"), desktop.OwnerBusy("在用电脑")],
)
async def test_keys_are_not_sent_when_the_window_cannot_come_forward(monkeypatch, refusal):
    _fake_window(monkeypatch)

    def focus(*args, **kwargs):
        raise refusal

    monkeypatch.setattr(td.desktop, "window_action", focus)
    sent = Mock()
    monkeypatch.setattr(td.desktop, "input_action", sent)
    monkeypatch.setattr(td.desktop, "screenshot", lambda *a, **k: PICTURE)
    steps = [{"action": "keys", "value": "ctrl+s"}, {"action": "type", "value": "hello"}]
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("ui", {"window": "app", "steps": steps})).structured_content
    sent.assert_not_called()  # before: the error was swallowed and the keys went to the window in front
    assert [s["step"] for s in out["steps"]] == [1] and "step 1 failed" in out["stopped"]
    error = out["steps"][0]["error"]
    if type(refusal) is desktop.DesktopError:
        assert error.startswith("blocked_before_input:") and "nothing was typed" in error
    else:
        assert error == "在用电脑"


@pytest.mark.parametrize(("in_front", "shown"), [(True, "app"), (False, "another window")])
async def test_after_keys_only_the_windows_own_title_is_named(monkeypatch, in_front, shown):
    _fake_window(monkeypatch)
    monkeypatch.setattr(td.desktop, "window_action", lambda *a, **k: {})
    title = "app" if in_front else MARK
    monkeypatch.setattr(td.desktop, "input_action", lambda action, **k: {"foreground": title})
    monkeypatch.setattr(td.desktop, "in_front", lambda hwnd: in_front)
    monkeypatch.setattr(td.desktop, "screenshot", lambda *a, **k: PICTURE)
    async with Client(build_server(Config())) as client:
        out = (
            await client.call_tool("ui", {"window": "app", "steps": [{"action": "keys", "value": "tab"}]})
        ).structured_content
    assert out["steps"][0]["foreground"] == shown and MARK not in _shown(out)


# ------------------------------------------------------------------ window pictures


class _Rect(ctypes.Structure):
    _fields_ = [(side, ctypes.c_long) for side in ("left", "top", "right", "bottom")]


@pytest.fixture
def screen(monkeypatch):
    """capture() with a fake window at (100, 50), 10 x 8; returns the screen grab, which records
    whether the screen was taken."""
    target = {
        "hwnd": 2,
        "title": "app",
        "process": "app.exe",
        "pid": 88,
        "left": 100,
        "top": 50,
        "width": 10,
        "height": 8,
        "minimized": False,
    }

    def window_rect(hwnd, rect):
        rect._obj.left, rect._obj.top, rect._obj.right, rect._obj.bottom = 100, 50, 110, 58
        return True

    monkeypatch.setattr(desktop, "_require_windows", lambda: None)
    monkeypatch.setattr(desktop, "_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(desktop, "find_window", lambda _: target)
    monkeypatch.setattr(desktop, "windows", lambda: [target])  # nothing to black out
    monkeypatch.setattr(desktop, "RECT", _Rect, raising=False)  # defined on Windows only
    monkeypatch.setattr(desktop, "user32", SimpleNamespace(GetWindowRect=window_rect), raising=False)
    monkeypatch.setattr(desktop, "_content_scale", lambda hwnd: 1.0)
    monkeypatch.setattr(desktop, "_virtual_screen", lambda: (100, 50, 10, 8))
    monkeypatch.setattr(secret_store, "shown", lambda: False)
    grab = Mock(
        side_effect=lambda bbox, all_screens: Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]))
    )
    monkeypatch.setattr(ImageGrab, "grab", grab)
    return grab


@pytest.mark.parametrize("drawn", [None, "black"])
async def test_a_window_picture_never_falls_back_to_the_screen(monkeypatch, screen, drawn):
    """PrintWindow failed, or drew only black: the screen there may show another window on top."""
    picture = None if drawn is None else Image.new("RGB", (10, 8), drawn)
    monkeypatch.setattr(desktop, "_print_window", lambda *args: picture)
    with pytest.raises(desktop.DesktopError, match="^CAPTURE_OCCLUDED: .*ui\\(window, action='inspect'\\)"):
        desktop.capture(window="app")
    with pytest.raises(desktop.DesktopError, match="^CAPTURE_OCCLUDED"):
        ocr.read(window="app")
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("screenshot", {"window": "app"})).structured_content
    assert out["error"].startswith("CAPTURE_OCCLUDED")
    screen.assert_not_called()


def test_a_window_picture_is_its_own_drawing_and_the_screen_stays_available(monkeypatch, screen):
    monkeypatch.setattr(desktop, "_print_window", lambda *args: Image.new("RGB", (10, 8), "white"))
    image, place = desktop.capture(window="app")
    assert image.getpixel((0, 0)) == (255, 255, 255) and place["target"]["window"]["hwnd"] == 2
    screen.assert_not_called()
    image, place = desktop.capture(region=[100, 50, 10, 8])  # the screen, asked for as the screen
    assert image.size == (10, 8) and screen.call_count == 1
