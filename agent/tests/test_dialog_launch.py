"""Dialogs (approval, guard confirmation, masked input, file pick) run in a child process. Found in
the 0.6.0 real-machine acceptance: the desktop app's backend reads its requests from stdin in a
thread, and a Python child that inherited that pipe blocked at startup until the read ended, so no
dialog ever appeared, and the AI was told nobody had answered."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from winhand import secret_prompt

REAL_POPEN = subprocess.Popen


def fake_dialog(monkeypatch, code: str, seen: dict | None = None) -> list[subprocess.Popen]:
    """Run `code` in place of the dialog process; returns the processes started."""
    started = []

    def popen(argv, **kwargs):
        if seen is not None:
            seen.update(kwargs)
        child = REAL_POPEN([sys.executable, "-c", code], **kwargs)
        started.append(child)
        return child

    monkeypatch.setattr(secret_prompt.subprocess, "Popen", popen)
    return started


def test_the_dialog_child_never_gets_this_process_stdin(monkeypatch):
    seen = {}
    fake_dialog(monkeypatch, 'print(\'{"value": "deny"}\')', seen)
    assert secret_prompt._run_dialog("{}", timeout_s=20) == "deny"
    assert seen["stdin"] is subprocess.DEVNULL


def test_a_dialog_that_never_shows_is_reported_at_once(monkeypatch):
    # its process runs, but no window of it appears: say so, instead of waiting out the timeout
    started = fake_dialog(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setattr(secret_prompt, "_window_visible", lambda pid: False)
    monkeypatch.setattr(secret_prompt, "SHOW_S", 1.0)
    began = time.monotonic()
    with pytest.raises(secret_prompt.DialogNotShown, match="did not appear on the screen"):
        secret_prompt._run_dialog("{}", timeout_s=120)
    assert time.monotonic() - began < 10
    assert started[0].poll() is not None  # the stuck process was ended


def test_a_dialog_on_the_screen_is_waited_for(monkeypatch):
    answer = json.dumps({"value": "session"})
    fake_dialog(monkeypatch, f"import time; time.sleep(2); print({answer!r})")
    monkeypatch.setattr(secret_prompt, "_window_visible", lambda pid: True)
    monkeypatch.setattr(secret_prompt, "SHOW_S", 1.0)
    assert secret_prompt._run_dialog("{}", timeout_s=30) == "session"  # answered after SHOW_S


def test_a_dialog_on_the_screen_nobody_answers_times_out(monkeypatch):
    started = fake_dialog(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setattr(secret_prompt, "_window_visible", lambda pid: True)
    with pytest.raises(secret_prompt.PromptUnavailable, match="nobody answered") as unanswered:
        secret_prompt._run_dialog("{}", timeout_s=1.5)
    assert not isinstance(unanswered.value, secret_prompt.DialogNotShown)
    assert started[0].poll() is not None


def test_a_dialog_that_fails_after_much_output_reports_its_error(monkeypatch):
    # its output is read while waiting for the window: a full pipe must not hold it until SHOW_S
    code = "import sys; sys.stderr.write('x' * 300000 + '\\n_tkinter.TclError: no display'); sys.exit(1)"
    fake_dialog(monkeypatch, code)
    monkeypatch.setattr(secret_prompt, "_window_visible", lambda pid: False)
    monkeypatch.setattr(secret_prompt, "SHOW_S", 5.0)
    with pytest.raises(secret_prompt.PromptUnavailable, match="cannot open a dialog.*no display") as failed:
        secret_prompt._run_dialog("{}", timeout_s=30)
    assert not isinstance(failed.value, secret_prompt.DialogNotShown)


def test_nobody_is_asked_while_the_screen_is_locked(monkeypatch):
    started = fake_dialog(monkeypatch, 'print(\'{"value": "deny"}\')')
    monkeypatch.setattr(secret_prompt, "_screen_state", lambda: "locked")
    with pytest.raises(secret_prompt.DialogNotShown, match="screen at the machine is locked"):
        secret_prompt._run_dialog("{}", timeout_s=30)
    assert not started  # no dialog process at all


def test_a_dialog_opens_while_another_thread_reads_stdin():
    # a process like the desktop backend: one thread waits for requests on stdin, another asks
    script = textwrap.dedent(
        """
        import json, sys, threading, time
        from winhand import secret_prompt
        threading.Thread(target=lambda: sys.stdin.buffer.read(1), daemon=True).start()
        time.sleep(0.5)
        answer = json.dumps({"value": "deny"})
        secret_prompt._dialog_command = lambda payload: [sys.executable, "-c", f"print({answer!r})"]
        began = time.monotonic()
        try:
            value = secret_prompt._run_dialog("{}", timeout_s=20)
        except secret_prompt.PromptUnavailable as exc:
            value = f"unavailable: {exc}"
        print(json.dumps({"value": value, "seconds": time.monotonic() - began}), flush=True)
        """
    )
    backend = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    watchdog = threading.Timer(60, backend.kill)
    watchdog.start()
    try:
        line = backend.stdout.readline()  # stdin stays open (and read) until the answer is printed
    finally:
        watchdog.cancel()
        backend.stdin.close()
        err = backend.stderr.read()
        backend.wait(timeout=30)
    result = json.loads(line.decode().strip() or "{}")
    assert result.get("value") == "deny", (result, err.decode(errors="replace")[-2000:])
    assert result["seconds"] < 15


@pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("tkinter") is None,
    reason="the real dialog on a Windows desktop",
)
def test_the_real_approval_dialog_is_seen_on_the_screen(monkeypatch):
    # the real dialog's process (behind a venv's python.exe, a child of the one started) must count
    # as shown: it stays up well past SHOW_S, and closing its window answers "deny"
    from winhand import desktop

    monkeypatch.setattr(secret_prompt, "SHOW_S", 2.0)
    result = {}

    def ask():
        try:
            result["value"] = secret_prompt.ask_approval(
                "测试程序", "winhand-test.exe", "测试窗口", "点击", timeout_s=60
            )
        except secret_prompt.PromptUnavailable as exc:
            result["value"] = f"unavailable: {exc}"

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    began, seen, closed = time.monotonic(), None, False
    while worker.is_alive() and time.monotonic() - began < 40:
        dialogs = [w for w in desktop.windows() if w["title"].startswith("WinHand：AI 想操作一个程序")]
        if dialogs and seen is None:
            seen = time.monotonic()
        if dialogs and time.monotonic() - seen >= 4:  # twice SHOW_S: a dialog taken for unseen is gone
            for window in dialogs:
                desktop.user32.PostMessageW(window["hwnd"], 0x0010, 0, 0)  # WM_CLOSE
            closed = True
        time.sleep(0.2)
    worker.join(30)
    assert closed and result.get("value") == "deny", result
