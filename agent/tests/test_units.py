from __future__ import annotations

import time

import pytest

from winhand.session import state as st
from winhand.session.buffer import OutputBuffer
from winhand.session.keys import encode_key
from winhand.session.screen import VirtualScreen
from winhand.session.text import clean, clip, extract_urls, strip_ansi

# ------------------------------------------------------------------ text


def test_clean_strips_escapes_and_resolves_carriage_returns():
    raw = "\x1b[32mok\x1b[0m\r\n10%\r50%\r100%\n\x1b]0;title\x07done\x07"
    assert clean(raw) == "ok\n100%\ndone"


def test_strip_ansi_handles_cursor_moves():
    assert strip_ansi("\x1b[2J\x1b[H\x1b[?25lhi") == "hi"


def test_extract_urls_dedupes_and_trims_punctuation():
    text = "open https://a.com/x?y=1. then https://a.com/x?y=1 and \x1b[4mhttp://b.org/p\x1b[0m"
    assert extract_urls(text) == ["https://a.com/x?y=1", "http://b.org/p"]


def test_clip_keeps_head_and_tail():
    text, omitted = clip("a" * 50 + "b" * 50, 30, keep="both")
    assert omitted == 70
    assert text.startswith("a") and text.endswith("b") and "omitted" in text


# ------------------------------------------------------------------ keys


@pytest.mark.parametrize(
    ("name", "seq"),
    [
        ("Enter", "\r"),
        ("down", "\x1b[B"),
        ("F5", "\x1b[15~"),
        ("Ctrl+C", "\x03"),
        ("^d", "\x04"),
        ("ctrl-]", "\x1d"),
        ("Alt+x", "\x1bx"),
        ("PageUp", "\x1b[5~"),
        ("shift+tab", "\x1b[Z"),
        ("q", "q"),
    ],
)
def test_encode_key(name, seq):
    assert encode_key(name) == seq


def test_encode_key_rejects_unknown():
    with pytest.raises(ValueError, match="unknown key"):
        encode_key("Hyper+Q")


# ---------------------------------------------------------------- buffer


def test_buffer_cursors_never_repeat_or_skip():
    buf = OutputBuffer()
    buf.append("hello ")
    first = buf.read(0)
    buf.append("world")
    second = buf.read(first.end)
    assert first.text == "hello " and second.text == "world" and second.end == 11


def test_buffer_reports_dropped_output(tmp_path):
    buf = OutputBuffer(capacity=100, log_path=tmp_path / "x.log")
    for i in range(50):
        buf.append(f"{i:04d}\n")
    chunk = buf.read(0)
    assert chunk.dropped > 0 and chunk.start == chunk.dropped
    buf.close()
    assert (tmp_path / "x.log").read_text().count("\n") == 50  # full stream kept on disk


# ---------------------------------------------------------------- screen


def test_screen_reports_cursor_and_highlight():
    screen = VirtualScreen(40, 10)
    screen.feed("\x1b[2J\x1b[HMenu\r\n  one\r\n\x1b[7m  two selected   \x1b[0m\r\n  three")
    snap = screen.snapshot()
    assert snap["lines"][0] == "Menu"
    assert snap["highlighted"] == [{"row": 2, "text": "two selected"}]
    assert snap["cursor"]["row"] == 3


# ----------------------------------------------------------------- state


def _infer(last_line="", since="", idle=1.0, alive=True, awaiting=False, **kw):
    det = st.Detectors.build(**kw)
    return st.infer(
        alive=alive,
        exit_code=0,
        last_line=last_line,
        since_input=since,
        idle_s=idle,
        det=det,
        awaiting_response=awaiting,
    )


@pytest.mark.parametrize(
    ("line", "state", "kind"),
    [
        ("Password: ", "needs_user", "secret"),
        ("Enter one-time password: ", "needs_user", "secret"),
        ("请输入密码：", "needs_user", "secret"),
        ("Continue? [y/N] ", "awaiting_input", "confirm"),
        ("--More--", "awaiting_input", "pager"),
        ("(gdb) ", "awaiting_input", "prompt"),
        ("msh />", "awaiting_input", "prompt"),
        ("PS C:\\Users\\me> ", "awaiting_input", "prompt"),
        ("C:\\Dev\\项目> ", "awaiting_input", "prompt"),
        ("Select an option:", "awaiting_input", "prompt_guess"),
    ],
)
def test_infer_prompts(line, state, kind):
    result = _infer(last_line=line, since=line)
    assert (result["state"], result.get("kind")) == (state, kind)


def test_browser_auth_only_while_it_is_the_latest_output():
    waiting = "Authenticate your account at:\nhttps://x.com/auth/1\n"
    assert _infer(since=waiting)["state"] == "needs_user"
    moved_on = waiting + "Press ENTER to open in the browser...\n+ pkg@1.0.0\nlots\nmore\n"
    assert _infer(since=moved_on, last_line="")["state"] == "idle"


def test_infer_running_idle_blocked_exited():
    assert _infer(last_line="building...", idle=0.1)["state"] == "running"
    assert _infer(last_line="", idle=5)["state"] == "idle"
    assert _infer(last_line="", idle=500, blocked_after_s=60)["state"] == "blocked"
    assert _infer(alive=False)["state"] == "exited"


def test_old_prompt_does_not_count_until_program_answers():
    assert _infer(last_line=">>> ", idle=0.2, awaiting=True)["state"] == "running"
    assert _infer(last_line=">>> ", idle=5, awaiting=True)["state"] == "idle"


def test_custom_prompt_and_disabled_heuristics():
    assert _infer(last_line="MYDEV# ", prompts=[r"^MYDEV# $"])["kind"] == "prompt"
    assert _infer(last_line="Loading:", heuristic_prompts=False)["state"] == "idle"


def test_screen_batches_parsing_but_never_shows_stale_content():
    from winhand.session.screen import VirtualScreen

    screen = VirtualScreen(20, 5)
    for ch in "hello":
        screen.feed(ch)  # tiny pieces are batched, not parsed one by one
    assert screen.cursor_line() == "hello"
    before = screen.last_change
    time.sleep(0.01)
    screen.feed(" world")
    assert screen.last_change > before  # reading stability flushes pending output first
    assert screen.snapshot()["lines"] == ["hello world"]


def test_build_env_fills_variables_a_thin_parent_left_out(monkeypatch):
    from winhand import winenv

    monkeypatch.setattr(winenv, "IS_WINDOWS", True)
    monkeypatch.setattr(
        winenv,
        "user_default_environment",
        lambda: {"ProgramFiles(x86)": r"C:\Program Files (x86)", "Path": r"C:\default", "HOME_ONLY": "x"},
    )
    monkeypatch.setattr(winenv.os, "environ", {"PATH": r"C:\mine", "SYSTEMROOT": r"C:\Windows"})
    env = winenv.build_env()
    assert env["ProgramFiles(x86)"] == r"C:\Program Files (x86)"  # missing: filled in
    assert env["PATH"] == r"C:\mine" and "Path" not in env  # present (any case): never overridden
    assert env["HOME_ONLY"] == "x"
