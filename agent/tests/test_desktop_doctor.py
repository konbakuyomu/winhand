"""winhand 0.4.3: picking the right window, waiting on the desktop, full-screen sessions,
local MCP call time limits and the machine health check."""

from __future__ import annotations

import sys
import time

import pytest
from conftest import wait_until_exited
from fastmcp import Client

from winhand import config, desktop, proc, tools_desktop
from winhand.config import Config, ServerEntry
from winhand.mcp_bridge import Services, compose_app
from winhand.relay_client import _free_port, serve_local
from winhand.server import build_server
from winhand.session import SessionSpec

from .test_mcp_bridge import fake_entry


def win(title, process="app.exe", owned=False, cls="AppWindow", w=800, h=600):
    return {"title": title, "process": process, "owned": owned, "class": cls, "width": w, "height": h}


# ------------------------------------------------------------------ windows


def test_process_name_prefers_the_main_window_over_its_popups():
    windows = [
        win("Tooltip text", cls="tooltips_class32", w=120, h=30),
        win("Options", owned=True),
        win("Sparkle"),
        win("Sparkle - other", process="other.exe"),
    ]
    assert desktop.rank_windows(windows, "app.exe")["title"] == "Sparkle"
    assert desktop.rank_windows(windows, "app")["title"] == "Sparkle"


def test_title_prefers_exact_then_prefix_then_part_and_the_frontmost():
    windows = [win("Untitled - Notepad"), win("Notepad"), win("Notepad++ settings"), win("Notepad")]
    assert desktop.rank_windows(windows, "notepad") is windows[1]
    assert desktop.rank_windows(windows, "notepad+")["title"] == "Notepad++ settings"
    assert desktop.rank_windows(windows, "untitled")["title"] == "Untitled - Notepad"
    assert desktop.rank_windows(windows, "nothing") is None


def test_wait_window_until_it_appears_or_times_out(monkeypatch):
    monkeypatch.setattr(tools_desktop, "WAIT_POLL_S", 0.05)
    appears_at = time.monotonic() + 0.3

    def find(target):
        if time.monotonic() < appears_at:
            raise desktop.DesktopError("no window")
        return {"title": "Setup - done"}

    out = tools_desktop.wait_window("Setup", "exists", 5, find=find)
    assert out["matched"] and out["window"]["title"] == "Setup - done" and out["waited_s"] >= 0.2
    gone = tools_desktop.wait_window("Setup", "gone", 0.2, find=find)
    assert not gone["matched"] and "call again" in gone["note"]
    with pytest.raises(ValueError, match="exists or gone"):
        tools_desktop.wait_window("x", "maybe", 1, find=find)


def test_wait_control_until_enabled(monkeypatch):
    monkeypatch.setattr(tools_desktop, "WAIT_POLL_S", 0.05)
    monkeypatch.setattr(desktop, "find_window", lambda selector: {"hwnd": 1, "title": "Installer"})
    calls = []

    def inspect(hwnd):
        calls.append(hwnd)
        return [{"name": "Next >", "type": "Button", "enabled": len(calls) >= 3}]

    out = tools_desktop.wait_control("Installer", "Next*", None, "Button", "enabled", 5, inspect=inspect)
    assert out["matched"] and out["element"]["enabled"] and len(calls) == 3
    value = tools_desktop.wait_control("Installer", "Next*", None, None, "value:Next", 1, inspect=inspect)
    assert value["matched"]
    with pytest.raises(ValueError, match="needs name"):
        tools_desktop.wait_control("Installer", None, None, None, "exists", 1, inspect=inspect)
    with pytest.raises(ValueError, match="state is"):
        tools_desktop.wait_control("Installer", "x", None, None, "blue", 1, inspect=inspect)


# ------------------------------------------------------------------ sessions


async def test_full_screen_output_is_recognised(manager):
    code = (
        "import sys; sys.stdout.write('\\x1b[1;1Ha\\x1b[2;1Hb\\x1b[3;1Hc\\x1b[4;1Hd\\n'); sys.stdout.flush()"
    )
    s = manager.create(SessionSpec(transport="pty", argv=[sys.executable, "-c", code]))
    await wait_until_exited(manager, s, total_s=30)
    assert s.redraws_screen(0)
    plain = manager.create(SessionSpec(transport="pipe", argv=[sys.executable, "-c", "print('hi')"]))
    await wait_until_exited(manager, plain, total_s=30)
    assert not plain.redraws_screen(0)


# ------------------------------------------------------------------ local MCP call limits


async def test_a_local_call_past_its_time_limit_is_cancelled():
    port = _free_port()
    services = Services([fake_entry("timed", call_timeout_s=1)])
    server, server_task = await serve_local(compose_app(build_server(Config()).http_app(), services), port)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp/timed") as client:
            began = time.monotonic()
            with pytest.raises(Exception, match="did not finish this call within 1 s"):
                await client.call_tool("slow", {"steps": 400})
            assert time.monotonic() - began < 8
            assert (await client.call_tool("echo", {"text": "still fine"})).content[
                0
            ].text == "echo: still fine"
    finally:
        await services.close()
        server.should_exit = True
        await server_task


def test_call_timeout_is_kept_in_the_config(winhand_home):
    config.save(Config(servers=[ServerEntry(name="pyocd", command="uv", call_timeout_s=90)]))
    assert config.load().servers[0].call_timeout_s == 90


# ------------------------------------------------------------------ health check


async def test_sys_info_checks():
    names = {c["check"] for c in proc.doctor()}
    assert {"ssh", "ripgrep", "non_ascii_home"} <= names
    if sys.platform == "win32":
        assert {"pwsh", "code_page", "redirection_guard", "long_paths", "git_bash", "shims"} <= names
    async with Client(build_server(Config())) as client:
        info = (await client.call_tool("sys_info", {"checks": True})).structured_content
        assert all({"check", "ok", "detail"} <= set(c) for c in info["checks"])
        plain = (await client.call_tool("sys_info", {})).structured_content
        assert "checks" not in plain
