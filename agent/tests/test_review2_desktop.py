"""Desktop action aliases, program-only errors, loading hints and password-manager protection.

All windows, pictures and approval-dialog answers here are fake.
"""

from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from conftest import on_windows
from fastmcp import Client
from PIL import Image, ImageGrab

from winhand import approvals, desktop, ocr, secret_store, tool_hints
from winhand import tools_desktop as td
from winhand.approvals import App, NotOperable
from winhand.config import Config
from winhand.server import build_server


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(approvals, "_session", {})
    monkeypatch.setattr(approvals, "_protected_until", {})
    monkeypatch.setattr(approvals, "_denied", {})
    monkeypatch.setattr(approvals, "_pending", {})
    monkeypatch.setattr(approvals, "_opening", set())
    monkeypatch.setattr(approvals, "_file", {"stamp": None, "ask": True, "always": {}, "error": None})
    monkeypatch.setattr(approvals, "_listeners", [])
    monkeypatch.setattr(approvals, "_ask_person", Mock(side_effect=AssertionError("unexpected dialog")))


@pytest.mark.parametrize("action", ["tree", "read"])
async def test_ui_aliases_inspect_without_opening_a_window(monkeypatch, action):
    window = {"hwnd": 1, "title": "app", "process": "app.exe"}
    inspect = Mock(return_value={"window": "app", "elements": []})
    monkeypatch.setattr(desktop, "find_window", lambda _: window)
    monkeypatch.setattr(td, "_ui_inspect", inspect)
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("ui", {"window": "app", "action": action})
    assert result.structured_content == {"window": "app", "elements": []}
    inspect.assert_called_once_with(window, None, None, None, None)


async def test_window_read_lists_without_a_target(monkeypatch):
    windows = [{"hwnd": 1, "title": "app", "process": "app.exe"}]
    monkeypatch.setattr(desktop, "windows", lambda: windows)
    monkeypatch.setattr(desktop, "monitors", lambda: [])
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    async with Client(build_server(Config())) as client:
        result = await client.call_tool("window", {"action": "read"})
    assert result.structured_content == {"monitors": [], "windows": windows}


def test_missing_window_lists_only_ten_unique_exes(monkeypatch):
    windows = [
        {"hwnd": 1, "title": "Private browser tab", "process": "Chrome.exe"},
        {"hwnd": 2, "title": "Another private tab", "process": "chrome.exe"},
        *[{"hwnd": i + 3, "title": f"Secret title {i}", "process": f"app{i}.exe"} for i in range(12)],
    ]
    monkeypatch.setattr(desktop, "windows", lambda: windows)
    with pytest.raises(desktop.DesktopError) as error:
        desktop.find_window("missing")
    programs = ", ".join(["chrome.exe", *[f"app{i}.exe" for i in range(9)]])
    assert str(error.value) == f"no window matches 'missing'; visible programs: {programs}"
    assert all(w["title"] not in str(error.value) for w in windows)


def test_always_load_follows_usage():
    assert tool_hints.ALWAYS_LOAD == {
        "context",
        "fs_edit",
        "fs_find",
        "fs_list",
        "fs_read",
        "fs_search",
        "fs_write",
        "history",
        "job_start",
        "job_status",
        "job_stop",
        "run",
    }


def test_protected_exes_are_the_six_requested_names():
    assert approvals.PROTECTED_EXES == {
        "1password.exe",
        "bitwarden.exe",
        "keepass.exe",
        "keepassxc.exe",
        "dashlane.exe",
        "enpass.exe",
    }


@pytest.mark.parametrize("exe", sorted(approvals.PROTECTED_EXES))
@pytest.mark.parametrize("answer", ["session", "always"])
def test_password_manager_answers_expire_and_never_write(monkeypatch, exe, answer):
    now = [100.0]
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    person = Mock(return_value=answer)
    app = App(exe.upper())
    approvals.check(app, "点击", person)
    assert approvals.status(exe) == "session"
    assert "这是密码管理器，只放开 10 分钟" in person.call_args.args[2]
    now[0] = 699.999
    approvals.check(app, "点击", person)
    assert person.call_count == 1
    now[0] = 700.0
    assert approvals.status(exe) == "ask"
    approvals.check(app, "点击", person)
    assert person.call_count == 2
    assert not approvals.path().exists()
    approvals.forget_session()
    assert approvals.status(exe) == "ask"


def test_protected_program_still_asks_with_switch_off_and_old_permanent_entry(monkeypatch):
    approvals.path().parent.mkdir(parents=True, exist_ok=True)
    original = 'ask = false\n[always]\n"1password.exe" = "old approval"\n'
    approvals.path().write_text(original, encoding="utf-8")
    app = App("1password.exe", "1Password", pid=77)
    monkeypatch.setattr(desktop, "_root", lambda hwnd: 1)
    monkeypatch.setattr(desktop, "refuse_own", lambda hwnd: {"process": app.exe, "pid": app.pid})
    monkeypatch.setattr(desktop, "is_elevated", lambda pid: False)
    monkeypatch.setattr(desktop, "app_of", lambda hwnd: app)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    person = Mock(return_value="always")
    monkeypatch.setattr(approvals, "_ask_person", person)
    assert approvals.enabled() is False
    assert desktop._approval_of(1, app.pid, app.exe, "App") == "ask"
    desktop.require_operable(1, "点击")
    assert person.call_count == 1
    assert approvals.status(app.exe) == "session"
    assert approvals.path().read_text(encoding="utf-8") == original


@pytest.fixture
def protected_window(monkeypatch):
    window = {
        "hwnd": 1,
        "title": "Private vault",
        "process": "1Password.exe",
        "pid": 77,
        "left": 102,
        "top": 52,
        "width": 4,
        "height": 3,
        "minimized": False,
    }
    monkeypatch.setattr(desktop, "windows", lambda: [window])
    monkeypatch.setattr(desktop, "app_of", lambda hwnd: App("1password.exe", "1Password", pid=77))
    return window


def test_mask_blacks_only_the_window_rectangle(protected_window):
    image = Image.new("RGB", (10, 8), "white")
    assert desktop._mask_protected(image, 100, 50) == ["1Password"]
    for x in range(10):
        for y in range(8):
            assert image.getpixel((x, y)) == ((0, 0, 0) if 2 <= x < 6 and 2 <= y < 5 else (255, 255, 255))


def test_mask_clips_to_capture_and_expires_with_approval(monkeypatch, protected_window):
    now = [100.0]
    monkeypatch.setattr(approvals.time, "monotonic", lambda: now[0])
    approvals.allow_session("1password.exe")
    image = Image.new("RGB", (3, 3), "white")
    assert desktop._mask_protected(image, 104, 53) == []
    assert image.getextrema() == ((255, 255),) * 3
    now[0] = 700.0
    assert desktop._mask_protected(image, 104, 53) == ["1Password"]
    assert image.getpixel((0, 0)) == (0, 0, 0)
    assert image.getpixel((1, 1)) == (0, 0, 0)
    assert image.getpixel((2, 1)) == (255, 255, 255)
    assert image.getpixel((1, 2)) == (255, 255, 255)


@pytest.mark.parametrize("change", [{"minimized": True}, {"left": 200}, {"process": "app.exe"}])
def test_mask_ignores_windows_outside_the_visible_protected_area(protected_window, change):
    protected_window.update(change)
    image = Image.new("RGB", (10, 8), "white")
    assert desktop._mask_protected(image, 100, 50) == []
    assert image.getextrema() == ((255, 255),) * 3


@pytest.fixture
def fake_capture(monkeypatch, protected_window):
    normal = {
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

    monkeypatch.setattr(secret_store, "shown", lambda: False)
    monkeypatch.setattr(desktop, "_dpi_aware", lambda: None)
    monkeypatch.setattr(desktop, "desktop_state", lambda: "ok")
    monkeypatch.setattr(desktop, "_virtual_screen", lambda: (100, 50, 10, 8))
    monkeypatch.setattr(desktop, "monitors", lambda: [{"left": 100, "top": 50, "width": 10, "height": 8}])
    monkeypatch.setattr(desktop, "find_window", lambda _: normal)
    monkeypatch.setattr(desktop, "user32", SimpleNamespace(GetWindowRect=window_rect))
    monkeypatch.setattr(desktop, "_print_window", lambda *args: Image.new("RGB", (10, 8), "white"))
    monkeypatch.setattr(desktop, "_content_scale", lambda hwnd: 1.0)
    monkeypatch.setattr(desktop, "_seen", {})
    monkeypatch.setattr(desktop, "_last_shot", None)
    monkeypatch.setattr(desktop, "_window_scale", {})
    monkeypatch.setattr(
        ImageGrab,
        "grab",
        lambda bbox, all_screens: Image.new("RGB", (bbox[2] - bbox[0], bbox[3] - bbox[1]), "white"),
    )
    return protected_window


@pytest.mark.skipif(not on_windows(), reason="Windows capture, with fake windows and images")
@pytest.mark.parametrize("target", [{}, {"monitor": 0}, {"region": [100, 50, 10, 8]}, {"window": "app"}])
def test_screenshot_and_ocr_receive_masked_pixels_and_names(monkeypatch, fake_capture, target):
    data, _, facts = desktop.screenshot(**target)
    assert facts["masked"] == ["1Password"]
    with Image.open(io.BytesIO(data)) as picture:
        assert picture.getpixel((3, 3)) == (0, 0, 0)
        assert picture.getpixel((0, 0)) == (255, 255, 255)
    with Image.open(facts["original_file"]) as picture:
        assert picture.getpixel((3, 3)) == (0, 0, 0)

    def recognize(image, left, top, language):
        assert image.getpixel((3, 3)) == (0, 0, 0)
        assert image.getpixel((0, 0)) == (255, 255, 255)
        return [ocr.Pass("en-US", 1.0, [])], ["en-US"]

    monkeypatch.setattr(ocr, "recognize", recognize)
    assert ocr.read(**target)["masked"] == ["1Password"]


@pytest.mark.skipif(not on_windows(), reason="Windows capture, with fake windows and images")
@pytest.mark.parametrize("tool", [desktop.capture, desktop.screenshot, ocr.read])
@pytest.mark.parametrize("minimized", [False, True])
def test_password_window_refuses_before_grabbing_or_restoring(monkeypatch, fake_capture, tool, minimized):
    fake_capture["minimized"] = minimized
    monkeypatch.setattr(desktop, "find_window", lambda _: fake_capture)
    grab = Mock(side_effect=AssertionError("a protected window was captured"))
    monkeypatch.setattr(ImageGrab, "grab", grab)
    monkeypatch.setattr(desktop, "_print_window", grab)
    person = Mock(return_value="deny")
    monkeypatch.setattr(approvals, "_ask_person", person)
    with pytest.raises(NotOperable, match="这是密码管理器，要用请让电脑前的人放开一次"):
        tool(window="1password.exe")
    assert person.call_count == 1
    assert "这是密码管理器，只放开 10 分钟" in person.call_args.args[2]
    grab.assert_not_called()


@pytest.mark.skipif(not on_windows(), reason="Windows capture, with fake windows and images")
def test_password_window_can_be_captured_after_dialog_permission(monkeypatch, fake_capture):
    monkeypatch.setattr(desktop, "find_window", lambda _: fake_capture)
    person = Mock(return_value="always")
    monkeypatch.setattr(approvals, "_ask_person", person)
    image, place = desktop.capture(window="1password.exe")
    assert person.call_count == 1
    assert "masked" not in place["target"]
    assert image.getextrema() == ((255, 255),) * 3
    assert not approvals.path().exists()
