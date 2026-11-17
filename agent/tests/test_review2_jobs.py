"""Command retention, concise job replies, kept jobs and load/process queries."""

from __future__ import annotations

import functools
import json
import os
import sys
import time
from datetime import datetime
from types import SimpleNamespace

import psutil
import pytest
from conftest import on_windows
from fastmcp.client import Client

from winhand import actions, config, jobs, jobwatch, proc, secret_store, server
from winhand.config import Config

WINDOWS = pytest.mark.skipif(not on_windows(), reason="background jobs are Windows-only")
SUMMARY_KEYS = {"id", "name", "state", "exit_code", "started", "finished", "seconds", "timed_out", "reason"}
STOP_KEYS = {"id", "state", "exit_code", "stopped", "tail"}
OUTPUT_CODE = (
    "import sys, time\n"
    "for i in range(30): print(f'line {i:02} ' + 'x' * 120, flush=True)\n"
    "print('error tail', file=sys.stderr, flush=True)\n"
    "time.sleep(60)\n"
)


@pytest.fixture(autouse=True)
def isolated_runs_and_reviver(monkeypatch):
    monkeypatch.setattr(proc, "_running", {})
    monkeypatch.setattr(proc, "_ended", {})
    yield
    for rid, job in list(proc._running.items()):
        if isinstance(job, proc._Running):
            proc.run_stop(rid)


@pytest.fixture
async def client():
    async with Client(server.build_server(Config())) as connected:
        yield connected


@pytest.fixture
def cleanup_jobs():
    yield
    root = config.home() / "jobs"
    if root.exists():
        for directory in root.iterdir():
            if jobs.status(directory.name, max_chars=0)["state"] == "running":
                jobs.stop(directory.name)


def _finished_job(job_id, name=None):
    directory = jobs._root() / job_id
    directory.mkdir()
    meta = {"id": job_id, "command": "test command", "started": "2026-11-17T10:00:00", "program": True}
    if name is not None:
        meta["name"] = name
    jobs._write_meta(directory, meta)
    (directory / "exit.json").write_text(
        json.dumps({"exit_code": 0, "finished": "2026-11-17T10:00:02", "seconds": 2}), encoding="utf-8"
    )
    (directory / "output.log").write_text("output\n", encoding="utf-8")
    (directory / "stderr.log").write_text("progress\n" * 1000, encoding="utf-8")
    return directory


def test_a_command_handed_back_still_running_outlives_25_others_for_an_hour():
    waited = proc.run(sys.executable, ["-c", "import time; time.sleep(1); print('late')"], reply_within_s=0.1)
    assert waited["still_running"] is True
    proc._running[waited["id"]].wait(10)
    ids = []
    for i in range(25):
        assert proc.run(sys.executable, ["-c", f"print({i})"])["exit_code"] == 0
        ids.append(next(reversed(proc._running)))
    assert proc.run_status(waited["id"])["output"].strip() == "late"
    assert list(proc._running) == [waited["id"], *ids[-20:]]
    proc._ended[waited["id"]] -= 3601
    proc._forget_finished()
    with pytest.raises(ValueError, match="keeps finished commands for an hour"):
        proc.run_status(waited["id"])
    assert list(proc._running) == ids[-20:]


@WINDOWS
async def test_list_keys_prefix_before_limit_and_files_without_id(client, tmp_path):
    for i in range(25):
        directory = _finished_job(f"saved-{i:02}", f"target-{i:02}")
        os.utime(directory, (1000 + i, 1000 + i))
    for i in range(25):
        directory = _finished_job(f"other-{i:02}", f"other-{i:02}")
        os.utime(directory, (2000 + i, 2000 + i))
    _finished_job("legacy")
    log = tmp_path / "extra.log"
    log.write_text("".join(f"pass {i}\n" for i in range(50)), encoding="utf-8")
    listed = (await client.call_tool("job_status", {"files": [str(log)]})).structured_content
    assert len(listed["jobs"]) == 20
    assert all(set(row) <= SUMMARY_KEYS for row in listed["jobs"])
    assert "name" not in next(row for row in listed["jobs"] if row["id"] == "legacy")
    filtered = (await client.call_tool("job_status", {"prefix": "target-"})).structured_content["jobs"]
    assert [row["id"] for row in filtered] == [f"saved-{i:02}" for i in range(24, 4, -1)]
    assert all(
        set(row) == {"id", "name", "state", "exit_code", "started", "finished", "seconds"} for row in filtered
    )
    by_id = (await client.call_tool("job_status", {"prefix": "saved-00"})).structured_content
    assert [row["id"] for row in by_id["jobs"]] == ["saved-00"]
    assert (await client.call_tool("job_status", {"prefix": "absent"})).structured_content["jobs"] == []
    single = (
        await client.call_tool("job_status", {"id": "saved-00", "files": [str(log)]})
    ).structured_content
    assert set(listed["files"][str(log)]) == set(single["files"][str(log)]) == {"size", "quiet_s", "tail"}
    assert (
        listed["files"][str(log)]["tail"]
        == single["files"][str(log)]["tail"]
        == "\n".join(f"pass {i}" for i in range(30, 50))
    )
    assert single["command"] == "test command" and "stderr" in single and "output" in single


@pytest.mark.parametrize("background", [False, pytest.param(True, marks=WINDOWS)])
async def test_job_stop_returns_only_summary_and_last_20_lines(client, cleanup_jobs, background):
    if background:
        started = (
            await client.call_tool(
                "job_start", {"command": sys.executable, "args": ["-u", "-c", OUTPUT_CODE]}
            )
        ).structured_content
    else:
        started = proc.run(sys.executable, ["-u", "-c", OUTPUT_CODE], timeout_s=90, reply_within_s=0.2)
    ready = (
        await client.call_tool("job_status", {"id": started["id"], "wait_s": 20, "patterns": ["line 29"]})
    ).structured_content
    assert ready["state"] == "running" and "line 29" in ready["output"]
    stopped = (await client.call_tool("job_stop", {"id": started["id"]})).structured_content
    assert set(stopped) == STOP_KEYS
    assert stopped["id"] == started["id"] and stopped["state"] == "finished" and stopped["stopped"] is True
    assert isinstance(stopped["exit_code"], int)
    expected = [f"line {i:02} " + "x" * 120 for i in range(30)] + ["error tail"]
    assert stopped["tail"] == "\n".join(expected[-20:])[-2000:]
    assert len(stopped["tail"]) == 2000 and len(stopped["tail"].splitlines()) <= 20
    assert set((await client.call_tool("job_stop", {"id": started["id"]})).structured_content) == STOP_KEYS


@WINDOWS
async def test_stdin_file_reads_utf8_and_tool_passes_quiet_parameter(client, cleanup_jobs, tmp_path):
    source = tmp_path / "stdin.txt"
    source.write_text("hello 中文\nsecond line", encoding="utf-8-sig")  # a BOM is not part of stdin
    started = (
        await client.call_tool(
            "job_start",
            {
                "command": sys.executable,
                "args": ["-c", "import sys; print(sys.stdin.read().upper())"],
                "stdin_file": str(source),
                "quiet_ok_s": 900,
            },
        )
    ).structured_content
    result = (await client.call_tool("job_status", {"id": started["id"], "wait_s": 20})).structured_content
    assert result["state"] == "finished" and result["exit_code"] == 0
    assert result["output"].splitlines() == ["HELLO 中文", "SECOND LINE"]
    meta = jobs._read_json(jobs._root() / started["id"] / "job.json")
    assert meta["quiet_ok_s"] == 900 and meta["keep"] is False


@WINDOWS
@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"stdin_file": "unused"}, "give its arguments as args"),
        ({"args": [], "stdin": "", "stdin_file": "unused"}, "give stdin or stdin_file, not both"),
        ({"args": [], "stdin": "", "keep": True}, "keep cannot be used with stdin or stdin_file"),
        ({"args": [], "stdin_file": "unused", "keep": True}, "keep cannot be used with stdin or stdin_file"),
        ({"args": [], "keep": True, "elevated": True}, "keep cannot be used with elevated"),
    ],
)
def test_stdin_file_and_keep_reject_invalid_combinations(arguments, message):
    with pytest.raises(jobs.JobError, match=message):
        jobs.start(sys.executable, **arguments)


def test_stdin_file_is_a_separate_read_action(tmp_path):
    source = tmp_path / "brief.txt"
    source.write_text("file contents stay out of command text", encoding="utf-8")
    command, read = actions.describe(
        "job_start", {"command": "python", "args": ["-"], "stdin_file": str(source)}
    )
    assert command.kind == "command" and command.text == "python -"
    assert read.kind == "read" and read.paths == (str(source),)


@WINDOWS
async def test_kept_job_is_revived_once_and_manual_stop_prevents_revival(
    client, cleanup_jobs, tmp_path, monkeypatch
):
    earlier = _finished_job("earlier").name
    monkeypatch.setattr(secret_store, "load", lambda names: {"WH_REVIEW2": "ready"})
    code = "import os,time; print(os.environ['WH_REVIEW2'], os.getcwd(), flush=True); time.sleep(60)"
    started = (
        await client.call_tool(
            "job_start",
            {
                "command": sys.executable,
                "args": ["-u", "-c", code],
                "cwd": str(tmp_path),
                "shell": "pwsh",
                "name": "keep-worker",
                "secrets": ["WH_REVIEW2"],
                "timeout_s": 60,
                "after": [earlier],
                "keep_awake": False,
                "keep": True,
                "quiet_ok_s": 900,
            },
        )
    ).structured_content
    ready = (
        await client.call_tool("job_status", {"id": started["id"], "wait_s": 20, "patterns": ["ready"]})
    ).structured_content
    assert ready["state"] == "running" and "ready" in ready["output"]
    old_directory = jobs._root() / started["id"]
    original = jobs._read_json(old_directory / "job.json")
    parent = psutil.Process(original["pid"])
    tree = parent.children(recursive=True) + [parent]
    for process in reversed(tree):
        process.kill()
    _, alive = psutil.wait_procs(tree, timeout=5)
    assert not alive and jobs.status(started["id"])["state"] == "vanished"
    assert original["secrets"] == ["WH_REVIEW2"] and "env" not in original
    assert "ready" not in (old_directory / "job.json").read_text(encoding="utf-8")  # names, not values
    time.sleep(1.1)  # job ids count seconds: the restart gets its own
    rows = jobs.revive_kept()
    assert rows == [{"name": "keep-worker", "from": started["id"], "id": rows[0]["id"]}]
    revived = [rows[0]["id"]]
    assert revived[0] != started["id"]
    new_meta = jobs._read_json(jobs._root() / revived[0] / "job.json")
    assert jobs._read_json(old_directory / "job.json")["revived_as"] == revived[0]
    assert new_meta["revived_from"] == started["id"] and "after" not in new_meta
    for key in (
        "start_command",
        "command",
        "args",
        "cwd",
        "shell",
        "name",
        "secrets",
        "timeout_s",
        "keep",
        "quiet_ok_s",
        "keep_awake",
        "elevated",
    ):
        assert new_meta[key] == original[key]
    running = (
        await client.call_tool("job_status", {"id": revived[0], "wait_s": 20, "patterns": ["ready"]})
    ).structured_content
    assert running["state"] == "running" and "ready" in running["output"]
    assert jobs.revive_kept() == []
    stopped = (await client.call_tool("job_stop", {"id": revived[0]})).structured_content
    assert set(stopped) == STOP_KEYS and stopped["stopped"] is True
    assert jobs._read_json(jobs._root() / revived[0] / "job.json")["stopped"] is True
    assert jobs.revive_kept() == []


@WINDOWS
def test_revival_ignores_finished_stopped_revived_and_duplicate_running_jobs(monkeypatch):
    states = {}
    for job_id, extra, state in [
        ("ordinary", {"keep": False}, "vanished"),
        ("finished", {}, "finished"),
        ("stopped", {"stopped": False}, "vanished"),
        ("revived", {"revived_as": ""}, "vanished"),
        ("failed", {"revive_error": "program not found"}, "vanished"),
        ("old-duplicate", {"name": "same"}, "vanished"),
        ("live-duplicate", {"name": "same"}, "running"),
    ]:
        directory = jobs._root() / job_id
        directory.mkdir()
        jobs._write_meta(directory, {"keep": True, "name": job_id, **extra})
        states[job_id] = state
    monkeypatch.setattr(jobs, "status", lambda job_id, **kw: {"state": states[job_id]})

    def unexpected_start(*args, **kwargs):
        pytest.fail("ineligible jobs must not restart")

    monkeypatch.setattr(jobs, "start", unexpected_start)
    assert jobs.revive_kept() == []


@WINDOWS
def test_quiet_ok_raises_stall_threshold_but_cannot_lower_it(monkeypatch):
    directory = _finished_job("quiet")
    meta = jobs._read_json(directory / "job.json")
    meta["quiet_ok_s"] = 900
    jobs._write_meta(directory, meta)
    process = SimpleNamespace(pid=123, memory_info=lambda: SimpleNamespace(rss=1024))
    monkeypatch.setattr(jobwatch, "tree", lambda *args: [process])
    monkeypatch.setattr(jobwatch, "_cpu", lambda processes: 0)
    monkeypatch.setattr(jobwatch, "CPU_SAMPLE_S", 0.001)
    monkeypatch.setattr(jobwatch, "windows_of", lambda pids: [])
    monkeypatch.setattr(jobwatch, "prompts_since", lambda *args: [])
    monkeypatch.setattr(jobwatch, "secure_screen", lambda: False)
    assert jobwatch.snapshot(123, None, 600)["stalled"] is True
    assert "stalled" not in jobwatch.snapshot(123, None, 600, key="quiet")
    assert jobwatch.snapshot(123, None, 901, key="quiet")["stalled"] is True
    meta["quiet_ok_s"] = 1
    jobs._write_meta(directory, meta)
    assert "stalled" not in jobwatch.snapshot(123, None, jobwatch.STALL_AFTER_S - 1, key="quiet")


@WINDOWS
async def test_timed_out_job_has_reason_in_status_and_list(client, cleanup_jobs):
    started = jobs.start("Start-Sleep -Seconds 30", name="timeout", timeout_s=1, keep_awake=False)
    finished = (await client.call_tool("job_status", {"id": started["id"], "wait_s": 20})).structured_content
    assert finished["state"] == "finished" and finished["timed_out"] is True
    assert finished["reason"] == "timeout_s reached (1 s)"
    listed = (await client.call_tool("job_status", {"prefix": "timeout"})).structured_content
    assert listed["jobs"][0]["timed_out"] is True
    assert listed["jobs"][0]["reason"] == finished["reason"]
    assert set(listed["jobs"][0]) <= SUMMARY_KEYS
    assert "now" in finished and "now" in listed


async def test_now_on_completed_running_waited_multi_and_list_replies(client, monkeypatch):
    completed = (
        await client.call_tool("run", {"command": sys.executable, "args": ["-c", "print('done')"]})
    ).structured_content
    assert completed["exit_code"] == 0
    first_id = next(reversed(proc._running))
    monkeypatch.setattr(proc, "run", functools.partial(proc.run, reply_within_s=0.1))
    running = (
        await client.call_tool(
            "run", {"command": sys.executable, "args": ["-c", "import time; time.sleep(30)"]}
        )
    ).structured_content
    assert running["still_running"] is True
    single = (await client.call_tool("job_status", {"id": running["id"]})).structured_content
    waited = (await client.call_tool("job_status", {"id": first_id, "wait_s": 1})).structured_content
    multi = (await client.call_tool("job_status", {"id": [first_id, running["id"]]})).structured_content
    listed = (await client.call_tool("job_status", {})).structured_content
    assert set(waited) == set(proc.run_status(first_id)) | {"now"}
    for reply in (completed, running, single, waited, multi, listed):
        stamp = datetime.fromisoformat(reply["now"])
        assert stamp.tzinfo is not None and stamp.isoformat(timespec="seconds") == reply["now"]
        assert abs((datetime.now().astimezone() - stamp).total_seconds()) < 10


@WINDOWS
async def test_sys_info_load_keys_precision_sampling_and_all_running_jobs(client, monkeypatch):
    for i in range(22):
        directory = jobs._root() / f"running-{i}"
        directory.mkdir()
        jobs._write_meta(directory, {"pid": i + 1})
    finished = jobs._root() / "finished"
    finished.mkdir()
    jobs._write_meta(finished, {"pid": 100})
    (finished / "exit.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(jobs, "_alive", lambda meta: True)
    monkeypatch.setitem(proc._running, "run-live", SimpleNamespace(proc=SimpleNamespace(poll=lambda: None)))
    monkeypatch.setitem(proc._running, "run-done", SimpleNamespace(proc=SimpleNamespace(poll=lambda: 0)))
    monkeypatch.setattr(
        proc.psutil, "virtual_memory", lambda: SimpleNamespace(total=16 * 2**30, available=6.26 * 2**30)
    )
    intervals = []
    monkeypatch.setattr(proc.psutil, "cpu_percent", lambda interval: intervals.append(interval) or 12.34)
    plain = (await client.call_tool("sys_info", {})).structured_content
    loaded = (await client.call_tool("sys_info", {"load": True})).structured_content
    assert "load" not in plain
    assert loaded["load"] == {
        "memory_free_gb": 6.3,
        "memory_total_gb": 16.0,
        "cpu_percent": 12.3,
        "jobs_running": 22,
        "runs_running": 1,
    }
    assert intervals == [0.5]


async def test_tray_upkeep_puts_restarted_and_failed_kept_jobs_on_the_timeline(
    winhand_home, monkeypatch, caplog
):
    from winhand import desktop_backend

    backend = desktop_backend.Backend(lambda _line: None)
    calls = []

    def revive():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("revival test error")  # logged; the loop goes on
        backend.done.set()
        return [{"name": "w", "from": "w-1", "id": "w-2"}, {"name": "x", "from": "x-1", "error": "gone"}]

    monkeypatch.setattr(jobs, "revive_kept", revive)
    monkeypatch.setattr(desktop_backend, "UPKEEP_EVERY_S", 0.01)
    monkeypatch.setattr(desktop_backend, "SCRATCH_FIRST_S", 3600)  # not this test's business
    try:
        await backend.upkeep()
        entries = [
            (e["title"], e["status"], e["summary"]) for e in backend.hub.snapshot() if e["kind"] == "job"
        ]
        assert entries == [
            ("常驻任务 w 已重新启动", "ok", "w-1 -> w-2"),
            ("常驻任务 x 没能重新启动", "error", "gone"),
        ]
        assert len(calls) == 2 and "revival test error" in caplog.text
    finally:
        await backend.close()


async def test_tray_upkeep_records_an_approved_scratch_cleanup(winhand_home, monkeypatch):
    from winhand import desktop_backend, scratch_cleanup

    backend = desktop_backend.Backend(lambda _line: None)

    def ask():
        backend.done.set()
        return {
            "approved": True,
            "moved": ["C:/s/old-a", "C:/s/old-b"],
            "failed": [{"path": "C:/s/c", "error": "busy"}],
        }

    monkeypatch.setattr(jobs, "revive_kept", lambda: [])
    monkeypatch.setattr(scratch_cleanup, "ask_and_clean", ask)
    monkeypatch.setattr(desktop_backend, "SCRATCH_FIRST_S", 0)
    try:
        await backend.upkeep()
        entries = [
            (e["title"], e["status"], e["summary"]) for e in backend.hub.snapshot() if e["kind"] == "cleanup"
        ]
        assert entries == [("暂存目录：2 个文件夹移到了回收站，1 个没移成", "error", "old-a; old-b; busy")]
    finally:
        await backend.close()


async def test_tray_upkeep_waits_an_hour_after_the_scratch_question_fails(winhand_home, monkeypatch, caplog):
    from winhand import desktop_backend, scratch_cleanup

    backend = desktop_backend.Backend(lambda _line: None)
    rounds, asked = [], []

    def revive():
        rounds.append(True)
        if len(rounds) == 3:
            backend.done.set()
        return []

    def ask():
        asked.append(True)
        raise RuntimeError("cannot open a dialog")

    monkeypatch.setattr(jobs, "revive_kept", revive)
    monkeypatch.setattr(scratch_cleanup, "ask_and_clean", ask)
    monkeypatch.setattr(desktop_backend, "SCRATCH_FIRST_S", 0)
    monkeypatch.setattr(desktop_backend, "UPKEEP_EVERY_S", 0.01)
    try:
        await backend.upkeep()
        assert len(rounds) == 3 and len(asked) == 1  # not again each minute
        assert "could not ask about the scratch folders" in caplog.text
    finally:
        await backend.close()
