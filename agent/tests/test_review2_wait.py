"""Persistent wait links and relay-only wake commands, without external requests."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastmcp.client import Client
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from winhand import config, jobs, proc, tools_desktop, transfer, waiting
from winhand.config import Config
from winhand.server import build_server

WAKE_TEMPLATE = """u='<url>'; while :; do r=$(curl -sS -m 75 "$u" 2>/dev/null); case "$r" in *'"done":true'*|*expired*|*unknown*) echo "$r"; break;; '') sleep 15;; *) sleep 5;; esac; done"""
WAKE_NOTE = (
    "Run `wake` in the background (Bash run_in_background, timeout 7200000 ms). "
    "It ends when the condition holds and you are woken with its final status; "
    "if it is stopped at 2 h, run it again."
)


@pytest.fixture(autouse=True)
def isolated_links(monkeypatch):
    monkeypatch.setattr(transfer, "_wait_links", {})
    monkeypatch.setattr(transfer, "_wait_done", {})
    monkeypatch.setattr(transfer, "_tickets", {})
    monkeypatch.setattr(transfer, "_public_base", "https://relay.example")
    monkeypatch.setattr(transfer, "WAIT_HOLD_S", 0.02)


@pytest.fixture
def http():
    app = Starlette(routes=[Route("/transfer/{token}", transfer.endpoint, methods=["GET", "PUT", "POST"])])
    with TestClient(app) as client:
        yield client


def _local(link):
    return "/transfer/" + link["url"].rsplit("/", 1)[1]


def _condition(reply):
    url = reply["wake"].split("'", 2)[1]
    assert reply["wake"] == WAKE_TEMPLATE.replace("<url>", url)
    assert reply["wake_note"] == WAKE_NOTE
    return transfer._wait_links[transfer._key(url.rsplit("/", 1)[1])]["cond"]


def test_issue_wait_stores_only_hash_condition_and_times_and_exact_wake():
    link = transfer.issue_wait({"online": True})
    assert set(link) == {"url", "expires", "wake"}
    token = link["url"].rsplit("/", 1)[1]
    assert len(token) == transfer.TOKEN_CHARS
    assert link["wake"] == WAKE_TEMPLATE.replace("<url>", link["url"]) and "\n" not in link["wake"]
    expiry = datetime.fromisoformat(link["expires"])
    assert expiry.tzinfo is not None and abs(expiry.timestamp() - time.time() - 43200) < 2
    raw = (config.home() / "wait-links.json").read_text(encoding="utf-8")
    stored = json.loads(raw)
    assert token not in raw and link["url"] not in raw
    assert list(stored) == [transfer._key(token)]
    ticket = next(iter(stored.values()))
    assert set(ticket) == {"cond", "issued", "expires"} and ticket["cond"] == {"online": True}
    assert ticket["expires"] - ticket["issued"] == 43200


def test_pending_hold_checks_each_second_and_returns_compact_status(http, monkeypatch):
    clock, sleeps, calls = [0.0], [], []
    issued = time.time()

    async def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    def status(job_id, since):
        calls.append((job_id, since))
        return {"state": "running", "output": "private output", "stderr": "private stderr", "cursor": 50}

    monkeypatch.setattr(
        transfer, "time", SimpleNamespace(time=lambda: issued + clock[0], monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(transfer, "asyncio", SimpleNamespace(sleep=sleep, to_thread=asyncio.to_thread))
    monkeypatch.setattr(transfer, "WAIT_HOLD_S", 2.5)
    monkeypatch.setattr(jobs, "status", status)
    response = http.get(_local(transfer.issue_wait({"job": "build-1"})))
    assert response.status_code == 200 and response.text == '{"done":false,"state":"running"}'
    assert sleeps == [1, 1, 0.5] and calls == [("build-1", 0)] * 4


@pytest.mark.parametrize("job_id", ["build-1", "run-123"])
def test_job_finish_is_reusable_and_does_not_return_output(http, monkeypatch, job_id):
    current = {"state": "running", "output": "private output", "stderr": "private stderr", "cursor": 14}
    calls = []

    def status(rid, since):
        calls.append((rid, since))
        return dict(current)

    monkeypatch.setattr(jobs, "status", status)
    monkeypatch.setattr(proc, "run_status", status)
    link = transfer.issue_wait({"job": job_id})
    assert http.get(_local(link)).json() == {"done": False, "state": "running"}
    current.update(state="finished", exit_code=7, reason="not returned", timed_out=True)
    finished = http.get(_local(link))
    assert finished.status_code == 200 and finished.text == '{"done":true,"state":"finished","exit_code":7}'
    before = len(calls)
    current["state"] = "running"
    assert http.get(_local(link)).content == finished.content and len(calls) == before


@pytest.mark.parametrize("job_id", ["build-1", "run-123"])
def test_patterns_use_new_output_cursor_and_multiline_regex(http, monkeypatch, job_id):
    output = ["old\nready\n"]
    cursor = len(output[0])

    def status(rid, since):
        assert rid == job_id and since == cursor
        return {"state": "running", "output": output[0][since:], "cursor": len(output[0]), "stderr_size": 0}

    monkeypatch.setattr(jobs, "status", status)
    monkeypatch.setattr(proc, "run_status", status)
    link = transfer.issue_wait({"job": job_id, "patterns": ["^ready$", "^other$"], "cursor": cursor})
    assert http.get(_local(link)).json() == {"done": False, "state": "running"}
    output[0] += "next\nready\n"
    response = http.get(_local(link))
    assert response.text == '{"done":true,"state":"running","matched":"^ready$"}'


def test_patterns_match_new_stderr_during_hold_and_finish_without_a_match(http, monkeypatch):
    calls = []

    def status(job_id, since):
        calls.append(True)
        stderr = "old\n" if len(calls) == 1 else "old\nready\n"
        return {"state": "running", "output": "", "stderr": stderr, "stderr_size": len(stderr)}

    monkeypatch.setattr(jobs, "status", status)
    link = transfer.issue_wait({"job": "build-1", "patterns": ["^ready$"], "cursor": 0})
    assert http.get(_local(link)).json() == {"done": True, "state": "running", "matched": "^ready$"}
    monkeypatch.setattr(jobs, "status", lambda *a: {"state": "vanished", "output": "no match"})
    gone = transfer.issue_wait({"job": "build-2", "patterns": ["^ready$"], "cursor": 0})
    assert http.get(_local(gone)).json() == {"done": True, "state": "vanished"}


def test_file_appearing_and_later_gets_remain_done(http, tmp_path):
    path = tmp_path / "appeared.txt"
    link = transfer.issue_wait({"file": str(path)})
    assert http.get(_local(link)).json() == {"done": False}
    path.write_text("private contents", encoding="utf-8")
    assert http.get(_local(link)).text == '{"done":true}'
    path.unlink()
    assert http.get(_local(link)).text == '{"done":true}'


def test_existing_file_waits_for_modification_after_issue(http, tmp_path):
    path = tmp_path / "existing.txt"
    path.write_text("old", encoding="utf-8")
    old = time.time() - 60
    os.utime(path, (old, old))
    link = transfer.issue_wait({"file": str(path)})
    assert http.get(_local(link)).json() == {"done": False}
    later = time.time() + 2
    os.utime(path, (later, later))
    assert http.get(_local(link)).json() == {"done": True}


def test_online_and_wait_links_reject_put_and_post_without_consuming(http):
    link = transfer.issue_wait({"online": True})
    assert http.put(_local(link), content=b"unused").status_code == 405
    assert http.post(_local(link), content=b"unused").status_code == 405
    assert http.get(_local(link)).text == '{"done":true}'
    assert http.get(_local(link)).text == '{"done":true}'


def test_expired_is_410_even_after_done_and_unknown_is_404(http, monkeypatch):
    link = transfer.issue_wait({"online": True}, ttl_s=1)
    assert http.get(_local(link)).json() == {"done": True}
    later = time.time() + 2
    monkeypatch.setattr(transfer, "time", SimpleNamespace(time=lambda: later, monotonic=time.monotonic))
    response = http.get(_local(link))
    assert response.status_code == 410 and response.text == "expired wait link"
    unknown = http.get("/transfer/" + "x" * transfer.TOKEN_CHARS)
    assert unknown.status_code == 404 and "unknown" in unknown.text


def test_wait_links_outlive_a_restart_and_long_expired_ones_are_dropped(http, monkeypatch):
    live = transfer.issue_wait({"online": True}, ttl_s=120)
    expired = transfer.issue_wait({"online": True}, ttl_s=-1)
    monkeypatch.setattr(transfer, "_wait_links", None)  # as after a restart: read again from disk
    monkeypatch.setattr(transfer, "_wait_done", {})
    assert http.get(_local(live)).json() == {"done": True}
    assert http.get(_local(expired)).status_code == 410  # expired less than a day ago: still known
    later = time.time() + transfer.WAIT_FORGET_S + 10
    monkeypatch.setattr(transfer, "time", SimpleNamespace(time=lambda: later, monotonic=time.monotonic))
    transfer.issue_wait({"online": True}, ttl_s=120)  # writing drops what expired over a day ago
    assert transfer._key(expired["url"].rsplit("/", 1)[1]) not in transfer._waits()
    assert transfer._key(live["url"].rsplit("/", 1)[1]) in transfer._waits()


@pytest.mark.parametrize("condition", [{"window": "app"}, {"job": "build", "cursor": 0}, {"online": False}])
def test_only_the_four_condition_shapes_are_accepted(condition):
    with pytest.raises(transfer.TransferError):
        transfer.issue_wait(condition)


def test_invalid_regex_is_exposed():
    with pytest.raises(re.error):
        transfer.issue_wait({"job": "build-1", "patterns": ["("], "cursor": 0})


@pytest.mark.parametrize("relayed", [False, True])
async def test_tools_only_issue_wake_for_relay_calls_and_use_the_reply_cursor(monkeypatch, tmp_path, relayed):
    monkeypatch.setattr(waiting, "relayed", lambda: relayed)
    monkeypatch.setattr(jobs, "start", lambda *a, **kw: {"id": "build-1", "state": "running", "cursor": 3})
    monkeypatch.setattr(jobs, "list_jobs", lambda **kw: [])
    monkeypatch.setattr(proc, "run", lambda *a, **kw: {"id": "run-123", "still_running": True, "cursor": 4})
    monkeypatch.setattr(tools_desktop, "_activity", lambda job_id: {})
    monkeypatch.setattr(
        tools_desktop,
        "_job_fetch",
        lambda job_id, since, limit: {"id": job_id, "state": "running", "cursor": 17, "output": ""},
    )
    first, second = tmp_path / "first.log", tmp_path / "second.log"
    first.write_text("first\n", encoding="utf-8")
    second.write_text("second\n", encoding="utf-8")
    async with Client(build_server(Config())) as client:
        replies = [
            (await client.call_tool("job_start", {"command": "test-program", "args": []})).structured_content,
            (await client.call_tool("run", {"command": "test-program", "args": []})).structured_content,
            (await client.call_tool("job_status", {"id": "build-1", "wake": True})).structured_content,
            (
                await client.call_tool(
                    "job_status", {"id": "build-1", "patterns": ["^ready$"], "since": 5, "wake": True}
                )
            ).structured_content,
            (
                await client.call_tool("job_status", {"files": [str(first), str(second)], "wake": True})
            ).structured_content,
            (await client.call_tool("job_status", {"wake": True})).structured_content,
            (
                await client.call_tool(
                    "job_status", {"id": ["build-1", "build-2"], "patterns": ["^ready$"], "wake": True}
                )
            ).structured_content,
        ]
        normal = (await client.call_tool("job_status", {"id": "build-1"})).structured_content
        monkeypatch.setattr(
            tools_desktop, "_job_fetch", lambda *a: {"state": "finished", "exit_code": 0, "cursor": 19}
        )
        waited = (
            await client.call_tool(
                "job_status", {"id": "build-1", "patterns": ["^ready$"], "wait_s": 1, "wake": True}
            )
        ).structured_content
        monkeypatch.setattr(proc, "run", lambda *a, **kw: {"exit_code": 0, "stdout": "done", "stderr": ""})
        completed = (
            await client.call_tool("run", {"command": "test-program", "args": []})
        ).structured_content
    assert "wake" not in normal and "wake_note" not in normal
    assert "wake" not in completed and "wake_note" not in completed
    if relayed:
        assert [_condition(reply) for reply in replies] == [
            {"job": "build-1"},
            {"job": "run-123"},
            {"job": "build-1"},
            {"job": "build-1", "patterns": ["^ready$"], "cursor": 17},
            {"file": str(first)},
            {"online": True},
            {"job": "build-1", "patterns": ["^ready$"], "cursor": 17},
        ]
        assert _condition(waited) == {"job": "build-1", "patterns": ["^ready$"], "cursor": 19}
        assert len(transfer._wait_links) == 8
    else:
        assert all("wake" not in reply and "wake_note" not in reply for reply in [*replies, waited])
        assert not transfer._wait_links and not (config.home() / "wait-links.json").exists()
