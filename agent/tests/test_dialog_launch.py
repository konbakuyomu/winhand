"""Dialogs (approval, guard confirmation, masked input, file pick) run in a child process. Found in
the 0.6.0 real-machine acceptance: the desktop app's backend reads its requests from stdin in a
thread, and a Python child that inherited that pipe blocked at startup until the read ended, so no
dialog ever appeared."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading

from winhand import secret_prompt


def test_the_dialog_child_never_gets_this_process_stdin(monkeypatch):
    seen = {}

    def run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=b'{"value": "deny"}', stderr=b"")

    monkeypatch.setattr(secret_prompt.subprocess, "run", run)
    assert secret_prompt._run_dialog("{}", timeout_s=5) == "deny"
    assert seen["stdin"] is subprocess.DEVNULL


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
