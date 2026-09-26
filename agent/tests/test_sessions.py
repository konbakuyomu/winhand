from __future__ import annotations

import asyncio
import socket
import sys
import threading

import pytest

from winhand.session import SessionSpec, TransportError, wait_for


async def ready(manager, session):
    result = await wait_for(manager, [session.id], timeout_s=15)
    assert result["hit"]["condition"] == "state", result
    return result


async def cmd(manager, session, text, **wait_kw):
    since = session.read_cursor
    session.send(text, submit=True)
    wait_kw.setdefault("timeout_s", 15)
    return await wait_for(manager, [session.id], since={session.id: since}, **wait_kw)


async def test_prompt_roundtrip(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    r = await cmd(manager, s, "echo 你好 world")
    snap = r["sessions"][s.id]
    assert "你好 world" in snap["output"]
    assert snap["state"] == "awaiting_input" and snap["kind"] == "prompt"


async def test_secret_prompt_needs_user_and_is_answerable(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    r = await cmd(manager, s, "secret")
    assert r["hit"]["condition"] == "needs_user" and r["hit"]["kind"] == "secret"
    r = await cmd(manager, s, "hunter2")
    assert "got 7 chars" in r["sessions"][s.id]["output"]


async def test_browser_auth_surfaces_url(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    r = await cmd(manager, s, "auth")
    snap = r["sessions"][s.id]
    assert r["hit"]["condition"] == "needs_user" and snap["kind"] == "browser_auth"
    assert snap["urls"] == ["https://example.com/auth/cli/abc123"]
    r = await cmd(manager, s, "")
    assert "authenticated" in r["sessions"][s.id]["output"]
    assert r["sessions"][s.id]["state"] == "awaiting_input"


async def test_confirm_and_pager_are_awaiting_input(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    r = await cmd(manager, s, "confirm")
    assert r["sessions"][s.id]["kind"] == "confirm"
    r = await cmd(manager, s, "y")
    assert "confirmed" in r["sessions"][s.id]["output"]
    r = await cmd(manager, s, "more")
    assert r["sessions"][s.id]["kind"] == "pager"


async def test_silent_program_is_idle_then_pattern_wait_catches_output(manager, fake_spec):
    s = manager.create(fake_spec(quiet_ms=300))
    await ready(manager, s)
    s.send("sleep 3", submit=True)
    r = await wait_for(manager, [s.id], timeout_s=1.5)
    assert r["timed_out"] and r["sessions"][s.id]["state"] in ("idle", "running")
    r = await wait_for(manager, [s.id], patterns=[r"^woke$"], timeout_s=10)
    assert r["hit"]["condition"] == "pattern" and r["hit"]["match"] == "woke"


async def test_wait_across_sessions_reports_which_fired(manager, fake_spec):
    a = manager.create(fake_spec(name="alpha"))
    b = manager.create(fake_spec(name="bravo"))
    await ready(manager, a)
    await ready(manager, b)
    a.send("sleep 20", submit=True)
    b.send("sleep 1", submit=True)
    r = await wait_for(manager, [a.id, b.id], patterns=[r"^woke$"], timeout_s=15)
    assert r["hit"]["session"] == b.id
    assert set(r["sessions"]) == {a.id, b.id}


async def test_exit_always_ends_wait(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    s.send("exit 3", submit=True)
    r = await wait_for(manager, [s.id], patterns=["never-printed"], timeout_s=15)
    assert r["hit"]["condition"] == "exited"
    assert r["sessions"][s.id]["state"] == "exited"


async def test_autoreply_answers_pager(manager, fake_spec):
    s = manager.create(fake_spec(autoreply=[{"pattern": "--More--", "send": ""}]))
    await ready(manager, s)
    r = await cmd(manager, s, "more", patterns=[r"^line 3$"])
    assert r["hit"]["condition"] == "pattern"
    assert any(e["event"] == "autoreply" for e in s.events)


async def test_large_output_is_clipped_but_pageable(manager, fake_spec):
    s = manager.create(fake_spec())
    await ready(manager, s)
    since = s.read_cursor
    s.send("spam 3000", submit=True)
    r = await wait_for(
        manager, [s.id], patterns=[r"^row 2999$"], timeout_s=20, since={s.id: since}, output_limit=2000
    )
    snap = r["sessions"][s.id]
    assert snap["omitted"] > 0 and "row 2999" in snap["output"]
    page = s.read(since=since, limit=500)
    assert page["more"] and page["output"].count("row") >= 10


@pytest.mark.skipif(sys.platform == "win32", reason="raw-mode fake TUI uses termios; ConPTY covered manually")
async def test_tui_screen_and_arrow_keys(manager, fake_spec):
    s = manager.create(fake_spec(cols=60, rows=15))
    await ready(manager, s)
    s.send("tui", submit=True)
    r = await wait_for(manager, [s.id], screen_stable_ms=300, timeout_s=10, include_screen=True)
    screen = r["sessions"][s.id]["screen"]
    assert screen["highlighted"][0]["text"].endswith("Alpha")
    s.send(keys=["Down", "Down"])
    r = await wait_for(manager, [s.id], screen_stable_ms=300, timeout_s=10)
    assert r["sessions"][s.id]["screen"]["highlighted"][0]["text"].endswith("Charlie")
    r = await cmd(manager, s, "", patterns=["selected Charlie"])
    assert r["hit"]["condition"] == "pattern"


async def test_pipe_transport_and_interrupt(manager, fake_spec):
    s = manager.create(
        SessionSpec(
            transport="pipe",
            argv=[
                sys.executable,
                "-u",
                "-c",
                "import time\nprint('ready> ', end='', flush=True)\n"
                "try:\n  input(); time.sleep(30)\nexcept KeyboardInterrupt:\n  print('interrupted', flush=True)",
            ],
            prompts=[r"^ready> ?$"],
        )
    )
    r = await wait_for(manager, [s.id], timeout_s=10)
    assert r["sessions"][s.id]["state"] == "awaiting_input"
    s.send("go", submit=True)
    # Interrupt the running command, not the instant input() returns: CPython drops a
    # SIGINT that lands exactly as readline() hands back its line (~1 in 300 on CI).
    await asyncio.sleep(0.5)
    s.send(keys=["Ctrl+C"])
    r = await wait_for(manager, [s.id], patterns=["interrupted"], timeout_s=10)
    if sys.platform == "win32":
        # console-less pipe children cannot get Ctrl+C on Windows: the tree is ended instead
        assert r["hit"]["condition"] == "exited"
        assert any(e["event"] == "interrupt" and "pty" in e["result"] for e in s.events)
    else:
        diag = {
            "hit": r.get("hit"),
            "snapshot": r["sessions"][s.id],
            "events": s.events,
            "alive": s.alive,
            "exit": s.transport.exit_code(),
        }
        assert r.get("hit", {}).get("condition") in ("pattern", "exited"), diag
        assert "interrupted" in r["sessions"][s.id]["output"], diag


async def test_serial_loopback(manager):
    s = manager.create(SessionSpec(transport="serial", port="loop://", baudrate=115200, line_ending="\r\n"))
    s.send("hello-serial", submit=True)
    r = await wait_for(manager, [s.id], patterns=["hello-serial"], timeout_s=5)
    assert r["hit"]["condition"] == "pattern"


async def test_tcp_console_with_telnet_negotiation(manager):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def serve():
        conn, _ = server.accept()
        conn.sendall(bytes([255, 251, 1, 255, 251, 3]) + b"Welcome\r\ndev> ")
        buf = b""
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                conn.sendall(line.strip().upper() + b"\r\ndev> ")

    threading.Thread(target=serve, daemon=True).start()
    s = manager.create(SessionSpec(transport="tcp", host="127.0.0.1", tcp_port=port, prompts=[r"^dev> ?$"]))
    r = await wait_for(manager, [s.id], timeout_s=5)
    assert r["sessions"][s.id]["state"] == "awaiting_input"
    assert "\xff" not in r["sessions"][s.id]["output"] and "Welcome" in r["sessions"][s.id]["output"]
    r = await cmd(manager, s, "status", timeout_s=5)
    assert "STATUS" in r["sessions"][s.id]["output"]
    server.close()


async def test_bad_command_and_missing_cwd_raise_clear_errors(manager):
    with pytest.raises(TransportError, match="cannot start"):
        manager.create(SessionSpec(transport="pipe", argv=["definitely-not-a-program-xyz"]))
    with pytest.raises(TransportError, match="working directory"):
        manager.create(SessionSpec(transport="pty", argv=[sys.executable], cwd="/no/such/dir"))


async def test_quiet_after_output_waits_for_slow_starters(manager):
    code = "import time\ntime.sleep(2)\nprint('slow> ', end='', flush=True)\ninput()"
    s = manager.create(
        SessionSpec(transport="pty", argv=[sys.executable, "-u", "-c", code], prompts=[r"^slow> ?$"])
    )
    r = await wait_for(
        manager,
        [s.id],
        states=["awaiting_input"],
        quiet_ms=500,
        quiet_after_output=True,
        timeout_s=15,
        since={s.id: 0},
    )
    assert r["hit"]["condition"] == "state", r["hit"]
    assert r["sessions"][s.id]["state"] == "awaiting_input"


async def test_exited_only_after_all_output_is_read(manager):
    code = "import sys\nfor i in range(20000): print(f'line {i} 中文')"
    s = manager.create(SessionSpec(transport="pty", argv=[sys.executable, "-u", "-c", code]))
    r = await wait_for(manager, [s.id], states=["exited"], timeout_s=60, since={s.id: 0})
    assert r["hit"]["condition"] == "exited"
    text = s.read(since=0, limit=10_000_000)["output"]
    assert "line 19999 中文" in text, text[-200:]
