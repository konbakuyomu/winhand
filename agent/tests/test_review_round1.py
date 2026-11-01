"""0.7.4: what the 2026-11-01 usage review found (docs/reviews/2026-11-01-usage-review.md).

Repeated polls carry only what is new, the instructions name remote_run / fs_send / commit
messages on stdin, run ids go on across restarts and are waitable from the start, and a few
arguments models send stop failing.
"""

from __future__ import annotations

import asyncio
import sys

from fastmcp.client import Client

from winhand import config, fs, proc, secret_store, server, tools_desktop
from winhand.config import Config
from winhand.server import build_server

CODE_WITH_STDERR = (
    "import sys, time\n"
    "print('out1', flush=True)\n"
    "sys.stderr.write('err1\\n'); sys.stderr.flush()\n"
    "time.sleep(1.5)\n"
    "sys.stderr.write('err2\\n'); sys.stderr.flush()\n"
    "time.sleep(30)\n"
)


# ------------------------------------------------------------------ polling


async def test_repeat_polls_leave_out_what_the_caller_already_has():
    res = proc.run(sys.executable, ["-c", CODE_WITH_STDERR], timeout_s=60, reply_within_s=0.5)
    assert res["still_running"]
    async with Client(build_server(Config())) as client:

        async def status(**args):
            return (await client.call_tool("job_status", {"id": res["id"], **args})).structured_content

        first = await status(since=res["cursor"])
        assert "command" in first and "err1" in first["stderr"]  # first answer at this cursor: all
        await asyncio.sleep(2)
        again = await status(since=first["cursor"])
        assert again["state"] == "running" and "command" not in again
        assert "err2" in again["stderr"] and "err1" not in again["stderr"]  # only what is new
        quiet = await status(since=again["cursor"])
        assert "stderr" not in quiet and "command" not in quiet and quiet["output"] == ""
        tail = await status(since=again["cursor"], tail=500)
        assert "err1" in tail["stderr"]  # an explicit tail still shows the end of stderr
        await client.call_tool("job_stop", {"id": res["id"]})
        done = await status(since=again["cursor"])
        assert done["state"] == "finished" and "command" in done  # an ended job answers in full


def test_job_ids_as_models_write_them():
    assert tools_desktop._job_ids('["run-1", "run-2"]') == ["run-1", "run-2"]
    assert tools_desktop._job_ids("build-rc1-1029, full-tests-1029") == ["build-rc1-1029", "full-tests-1029"]
    assert tools_desktop._job_ids("t25-chain-1101010603") == "t25-chain-1101010603"
    assert tools_desktop._job_ids(["a", "b"]) == ["a", "b"]


# ------------------------------------------------------------------ instructions


def test_instructions_name_remote_run_fs_send_and_commit_messages_on_stdin(monkeypatch):
    names = [
        "CLOUDFLARE_API_TOKEN", "CPAMP_ADMIN_KEY", "DEEPPCB_API_KEY", "DEEPSEEK_API_TOKEN",
        "DEVBOARD_NOTES_TOKEN", "GH_TOKEN", "JINA_API_KEY", "TENCENT_SecretId", "TENCENT_SecretKey",
        "TYPESAFE_API_KEY", "XUI_PASSWORD", "XUI_USERNAME",
    ]  # fmt: skip
    monkeypatch.setattr(secret_store, "stored_names", lambda: names)
    text = server.instructions(Config())
    assert len(text) <= server.INSTRUCTIONS_MAX
    assert "remote_run(host, script)" in text and "fs_send(path)" in text and "'-F', '-'], stdin=" in text
    # the owner's twelve stored names all fit, so the model does not ask for a key it already has
    assert all(name in text for name in names) and "more (context()" not in text


# ------------------------------------------------------------------ run ids


def test_run_ids_go_on_across_restarts(monkeypatch):
    (config.home()).mkdir(parents=True, exist_ok=True)
    (config.home() / "run-counter").write_text("100", encoding="ascii")
    monkeypatch.setattr(proc, "_first_ids", {})  # a fresh winhand process
    assert proc._next_run_id() == "run-101"
    monkeypatch.setattr(proc, "_first_ids", {})  # winhand restarted
    assert proc._next_run_id() == "run-102"
    try:
        proc.run_status("run-57")
    except ValueError as exc:
        assert "before winhand last restarted" in str(exc) and "run-102" in str(exc)
    else:
        raise AssertionError("an id from before the restart must not be found")


async def test_a_run_is_in_history_and_waitable_from_the_start():
    async with Client(build_server(Config())) as client:
        done = (
            await client.call_tool("run", {"command": sys.executable, "args": ["-c", "print('hi')"]})
        ).structured_content
        assert done["exit_code"] == 0
        calls = (await client.call_tool("history", {"limit": 5})).structured_content["calls"]
        entry = next(c for c in calls if c["tool"] == "run")
        status = (await client.call_tool("job_status", {"id": entry["run_id"]})).structured_content
        assert status["state"] == "finished" and status["exit_code"] == 0 and "hi" in status["output"]


async def test_history_shows_the_run_id_while_the_command_is_still_in_its_first_answer():
    async with Client(build_server(Config())) as client:
        call = asyncio.create_task(
            client.call_tool("run", {"command": sys.executable, "args": ["-c", "import time; time.sleep(2)"]})
        )
        await asyncio.sleep(0.8)
        calls = (await client.call_tool("history", {"limit": 5})).structured_content["calls"]
        entry = next(c for c in calls if c["tool"] == "run")
        assert entry["status"] == "running" and entry["run_id"].startswith("run-")
        await call


# ------------------------------------------------------------------ small arguments


def test_fs_search_says_when_the_path_does_not_exist(tmp_path):
    try:
        fs.fs_search("x", path=str(tmp_path / "gone"))
    except fs.FsError as exc:
        assert "no such file or folder" in str(exc)
    else:
        raise AssertionError("a missing path must be an error that says so")


def test_fs_edit_tolerates_a_repeated_edit_and_skips_ones_that_change_nothing(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    out = fs.fs_edit(
        str(path),
        old="one",
        new="ONE",
        edits=[{"old": "one", "new": "ONE"}, {"old": "two", "new": "two"}, {"old": "three", "new": "3"}],
    )
    assert path.read_text(encoding="utf-8") == "ONE\ntwo\n3\n"
    assert out["replacements"] == 2 and "edit 2" in out["skipped"]
    unchanged = fs.fs_edit(str(path), old="ONE", new="ONE")
    assert unchanged["replacements"] == 0 and "not written" in unchanged["note"]
    try:
        fs.fs_edit(str(path), old="x", new="y", edits=[{"old": "ONE", "new": "1"}])
    except fs.FsError as exc:
        assert "either old/new or edits" in str(exc)
    else:
        raise AssertionError("old/new that differ from every edit stay ambiguous")


def test_run_accepts_the_interpreter_named_as_command_and_shell():
    out = proc.run(sys.executable, script="print('same interpreter')", shell="python")
    assert out.get("exit_code") == 0 and "same interpreter" in out["stdout"]
    other = proc.run(sys.executable, script="print(1)", shell="pwsh")
    assert "not both" in other["error"]


async def test_area_is_taken_as_region():
    async with Client(build_server(Config())) as client:
        result = await client.call_tool(
            "screenshot", {"area": [0, 0, 10, 10], "max_side": 64}, raise_on_error=False
        )
        text = " ".join(getattr(block, "text", "") for block in result.content)
        assert "not a parameter" not in text  # renamed to region and passed on (Linux: no desktop)
