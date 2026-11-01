"""0.8.0: long jobs without babysitting (docs/reviews/2026-11-01-usage-review.md, round 2).

A client that takes progress notifications waits in one call until a job ends, each caller with
its own heartbeat; tools carry read-only / idempotent / always-load hints; the PC stays awake
while commands and jobs run; jobs gain a time limit, a process-tree view and a stall signal.
"""

from __future__ import annotations

import asyncio
import sys
import time
from types import SimpleNamespace

from fastmcp.client import Client

from winhand import proc, waiting
from winhand.config import Config
from winhand.server import build_server


def fake_ctx(meta):
    return SimpleNamespace(request_context=SimpleNamespace(_srctx=SimpleNamespace(meta=meta)))


# ------------------------------------------------------------------ long waits


def test_only_a_client_that_takes_progress_gets_long_waits():
    with_progress, without = fake_ctx({"progress_token": 7}), fake_ctx({})
    assert waiting.takes_progress(with_progress) and not waiting.takes_progress(without)
    assert not waiting.takes_progress(None)
    assert (
        waiting.limit(with_progress, 600) == 600
        and waiting.limit(with_progress, 99_999) == waiting.LONG_WAIT_S
    )
    assert waiting.limit(without, 600) == waiting.SHORT_WAIT_S
    assert waiting.capped_note(without, 600) and waiting.capped_note(with_progress, 600) is None


async def test_one_call_waits_past_the_old_limit_and_beats_meanwhile(monkeypatch):
    monkeypatch.setattr(waiting, "SHORT_WAIT_S", 1.0)
    monkeypatch.setattr(waiting, "HEARTBEAT_S", 0.3)
    beats: list[float] = []

    async def on_progress(progress, total, message):
        beats.append(progress)

    res = proc.run(
        sys.executable,
        ["-c", "import time; time.sleep(2.5); print('done')"],
        timeout_s=30,
        reply_within_s=0.2,
    )
    async with Client(build_server(Config()), progress_handler=on_progress) as client:
        began = time.monotonic()
        out = (await client.call_tool("job_status", {"id": res["id"], "wait_s": 20})).structured_content
    assert out["state"] == "finished" and "done" in out["output"]
    assert 2.0 < time.monotonic() - began < 15  # past the 1 s short limit, ended with the job
    assert beats and beats[0] == 0.0 and len(beats) >= 4  # one at once, then every beat


async def test_callers_sharing_one_wait_each_get_their_own_beat(monkeypatch):
    monkeypatch.setattr(waiting, "HEARTBEAT_S", 0.3)
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(1.5)"], timeout_s=30, reply_within_s=0.2)
    beats: dict[str, int] = {"a": 0, "b": 0}

    def counter(name):
        async def on_progress(progress, total, message):
            beats[name] += 1

        return on_progress

    server = build_server(Config())
    async with (
        Client(server, progress_handler=counter("a")) as a,
        Client(server, progress_handler=counter("b")) as b,
    ):
        args = {"id": res["id"], "wait_s": 20}
        first, second = await asyncio.gather(a.call_tool("job_status", args), b.call_tool("job_status", args))
    assert first.structured_content["state"] == second.structured_content["state"] == "finished"
    assert beats["a"] >= 2 and beats["b"] >= 2  # the wait was shared, the beats were not


# ------------------------------------------------------------------ tool hints


async def test_tools_carry_read_only_idempotent_and_always_load_hints():
    from winhand import tool_hints

    async with Client(build_server(Config())) as client:
        tools = {t.name: t for t in await client.list_tools()}
    assert set(tool_hints.READ_ONLY | tool_hints.IDEMPOTENT | tool_hints.ALWAYS_LOAD) <= set(tools)
    assert tools["fs_read"].annotations.read_only_hint is True
    assert tools["job_status"].annotations.read_only_hint is True
    assert (
        tools["job_stop"].annotations.idempotent_hint is True
        and not tools["job_stop"].annotations.read_only_hint
    )
    # tools that can change the machine get no read-only hint, even when some actions only read
    for name in ("run", "fs_write", "window", "ocr", "clipboard", "mcp_tools"):
        assert not (tools[name].annotations and tools[name].annotations.read_only_hint), name
    assert (tools["run"].meta or {}).get("anthropic/alwaysLoad") is True
    assert (tools["remote_run"].meta or {}).get("anthropic/alwaysLoad") is True
    assert not (tools["ui"].meta or {}).get("anthropic/alwaysLoad")


# ------------------------------------------------------------------ keep awake


def test_the_keeper_holds_while_anything_runs_and_lets_go_after(monkeypatch):
    from winhand import power

    calls: list[int] = []
    monkeypatch.setattr(power, "supported", lambda: True)
    monkeypatch.setattr(power, "_set_state", calls.append)
    keeper = power.Keeper()
    assert keeper.hold("run-1") and keeper.hold("run-2")
    _until(lambda: keeper.awake)
    keeper.release("run-1")
    assert keeper.awake  # run-2 still runs
    keeper.release("run-2")
    _until(lambda: not keeper.awake)
    assert calls[0] == power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED and calls[-1] == power.ES_CONTINUOUS


def test_keep_awake_can_be_turned_off(monkeypatch, winhand_home):
    from winhand import power

    monkeypatch.setattr(power, "supported", lambda: True)
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text("[power]\nkeep_awake = false\n", encoding="utf-8")
    assert not power.enabled() and not power.Keeper().hold("run-1")


def test_a_run_holds_off_sleep_until_its_program_ends(monkeypatch):
    from winhand import power

    monkeypatch.setattr(power, "supported", lambda: True)
    monkeypatch.setattr(power, "_set_state", lambda flags: None)
    keeper = power.Keeper()
    monkeypatch.setattr(power, "keeper", keeper)
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(1.5)"], timeout_s=30, reply_within_s=0.3)
    assert res["still_running"] and keeper.holders() == 1
    proc.run_status(res["id"])
    _until(lambda: proc._running[res["id"]].proc.poll() is not None)
    proc._running[res["id"]].wait(5)
    assert keeper.holders() == 0


def _until(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < end, "condition never became true"
        time.sleep(0.02)


# ------------------------------------------------------------------ jobs and their activity


def test_job_wrappers_hold_off_sleep_wait_for_earlier_jobs_and_set_a_time_limit(tmp_path):
    from winhand import jobs

    earlier = tmp_path / "build-1101"
    earlier.mkdir()
    text = jobs._prologue(tmp_path, True, 90, [earlier])
    assert "SetThreadExecutionState" in text and "waiting for build-1101 to end" in text
    assert "'-Seconds', 90" in text and (tmp_path / "watchdog.ps1").exists()
    assert "taskkill.exe /PID $Target /T /F" in (tmp_path / "watchdog.ps1").read_text(encoding="utf-8-sig")
    assert jobs._prologue(tmp_path, False, None, []) == ""


def test_activity_shows_cpu_quiet_time_and_a_stall():
    from winhand import jobwatch

    busy = proc.run(sys.executable, ["-c", "while True: pass"], timeout_s=30, reply_within_s=0.3)
    idle = proc.run(sys.executable, ["-c", "import time; time.sleep(30)"], timeout_s=30, reply_within_s=0.3)
    try:
        pid, _, _ = proc.watch_info(busy["id"])
        working = jobwatch.snapshot(pid, None, quiet_s=0)
        assert working["processes"] >= 1 and working["cpu_busy"] > 0.3 and "stalled" not in working
        pid, _, _ = proc.watch_info(idle["id"])
        stuck = jobwatch.snapshot(pid, None, quiet_s=jobwatch.STALL_AFTER_S + 1)
        assert stuck["stalled"] and "stuck" in stuck["stalled_note"]
    finally:
        proc.run_stop(busy["id"])
        proc.run_stop(idle["id"])


async def test_a_long_wait_ends_when_the_job_goes_quiet_without_cpu(monkeypatch):
    from winhand import jobwatch

    # quiet for longer than the setup takes even on a busy machine: a job already stalled when the
    # wait first looks is no news, and the wait would sit out its whole time
    monkeypatch.setattr(jobwatch, "STALL_AFTER_S", 2.0)
    monkeypatch.setattr(jobwatch, "CHECK_EVERY_S", 0.5)
    code = "import time\nprint('working', flush=True)\ntime.sleep(60)\n"
    async with Client(build_server(Config())) as client:
        res = proc.run(sys.executable, ["-c", code], timeout_s=90, reply_within_s=0.3)
        try:
            began = time.monotonic()
            out = (
                await client.call_tool("job_status", {"id": res["id"], "since": res["cursor"], "wait_s": 30})
            ).structured_content
        finally:
            proc.run_stop(res["id"])
    assert out["matched"] == "stalled" and out["activity"]["stalled"]
    assert time.monotonic() - began < 15  # did not sit out the whole wait


async def test_job_status_shows_the_end_of_other_files(tmp_path):
    log = tmp_path / "router.log"
    log.write_text("".join(f"pass {i}\n" for i in range(50)), encoding="utf-8")
    res = proc.run(sys.executable, ["-c", "import time; time.sleep(5)"], timeout_s=30, reply_within_s=0.3)
    try:
        async with Client(build_server(Config())) as client:
            out = (
                await client.call_tool(
                    "job_status", {"id": res["id"], "files": [str(log), str(tmp_path / "x")]}
                )
            ).structured_content
        assert (
            out["files"][str(log)]["tail"].endswith("pass 49")
            and "pass 29" not in out["files"][str(log)]["tail"]
        )
        assert "error" in out["files"][str(tmp_path / "x")]
        assert out["activity"]["processes"] >= 1
    finally:
        proc.run_stop(res["id"])


def test_reading_files_through_job_status_is_judged_as_a_read():
    from winhand.actions import describe

    (action,) = describe("job_status", {"id": "run-1", "files": ["C:/secrets/.env"]})
    assert action.kind == "read" and action.paths
