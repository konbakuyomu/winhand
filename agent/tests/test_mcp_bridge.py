"""Local MCP servers offered one by one at /mcp/<name> (what the relay forwards to)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import ImageContent

from winhand import mcp_bridge
from winhand.activity import ActivityHub
from winhand.config import Config, ServerEntry
from winhand.mcp_bridge import Services, compose_app, probe
from winhand.relay_client import _free_port, serve_local
from winhand.server import build_server

FAKE = str(Path(__file__).parent / "fake_mcp_server.py")


def fake_entry(name: str = "fake", **kw) -> ServerEntry:
    return ServerEntry(name=name, command=sys.executable, args=["-u", FAKE], startup_timeout_s=30, **kw)


@pytest.fixture
async def site():
    """winhand's app on a local port with one fake stdio server and one HTTP pass-through."""
    port = _free_port()
    hub = ActivityHub()
    services = Services(
        [
            fake_entry(),
            fake_entry("off", enabled=False),
            ServerEntry(name="self", url=f"http://127.0.0.1:{port}/mcp"),  # winhand's own endpoint, proxied
        ],
        hub,
    )
    server = await serve_local(compose_app(build_server(Config()).http_app(), services), port)
    yield f"http://127.0.0.1:{port}", services, hub
    await services.close()
    server.should_exit = True
    await asyncio.sleep(0.1)


async def test_a_local_server_is_its_own_endpoint(site):
    base, services, hub = site
    async with Client(f"{base}/mcp/fake") as client:
        assert client.initialize_result.server_info.name == "fake-local"
        names = {t.name for t in await client.list_tools()}
        assert names == {
            "echo",
            "picture",
            "slow",
            "roots",
            "ask",
            "crash",
            "freeze",
            "pid",
        }  # not renamed, nothing else mixed in
        result = await client.call_tool("echo", {"text": "你好"})
        assert result.content[0].text == "echo: 你好"
        picture = await client.call_tool("picture", {})
        assert isinstance(picture.content[0], ImageContent) and picture.content[0].mime_type == "image/png"
    status = services.services["fake"].status()
    assert status["state"] == "running" and status["server"]["name"] == "fake-local" and status["calls"] == 2
    done = [e for e in hub.snapshot() if e.get("server") == "fake" and e["status"] == "ok"]
    assert [e["title"] for e in done] == ["fake · echo", "fake · picture"]
    assert done[0]["summary"] == "echo: 你好" and done[1]["summary"] == "[image]"


async def test_clients_share_one_process_and_their_calls_do_not_mix(site):
    base, services, _ = site
    async with Client(f"{base}/mcp/fake") as a, Client(f"{base}/mcp/fake") as b:
        pid_a = (await a.call_tool("pid", {})).data
        pid_b = (await b.call_tool("pid", {})).data
        assert pid_a == pid_b
        answers = await asyncio.gather(
            *(c.call_tool("echo", {"text": f"{i}"}) for i, c in enumerate([a, b] * 5))
        )
        assert [r.content[0].text for r in answers] == [f"echo: {i}" for i in range(10)]
        assert services.services["fake"].status()["sessions"] == 2
    assert services.services["fake"].status()["sessions"] == 0  # clients end their sessions on exit


async def test_progress_and_server_requests_reach_the_client(site):
    base, _, _ = site
    seen = []

    async def on_progress(progress, total, message):
        seen.append((progress, total))

    async def on_elicit(message, response_type, params, context):
        return response_type(value="小明")

    async with Client(
        f"{base}/mcp/fake",
        progress_handler=on_progress,
        roots=["file:///C:/work"],
        elicitation_handler=on_elicit,
    ) as client:
        assert (await client.call_tool("slow", {"steps": 3})).data == "done 3"
        assert seen == [(1, 3), (2, 3), (3, 3)]
        assert (await client.call_tool("roots", {})).data == "file:///C:/work"
        assert (await client.call_tool("ask", {})).data == "accept: 小明"


async def test_a_crashed_server_is_restarted_on_the_next_call(site):
    base, services, _ = site
    async with Client(f"{base}/mcp/fake") as client:
        first = (await client.call_tool("pid", {})).data
        with pytest.raises(MCPError) as crashed:
            await client.call_tool("crash", {})
        assert "fake exited (code 3)" in str(crashed.value)
        assert "about to crash" in str(crashed.value)  # its last words help the model
        second = (await client.call_tool("pid", {})).data  # same client session, new process
        assert second != first
    assert services.services["fake"].status()["state"] == "running"


async def test_a_busy_server_that_still_answers_pings_is_left_alone(site, monkeypatch):
    base, services, _ = site
    monkeypatch.setattr(mcp_bridge, "CHECK_AFTER_S", 0.3)
    async with Client(f"{base}/mcp/fake") as client:
        first = (await client.call_tool("pid", {})).data
        long_call = asyncio.create_task(client.call_tool("slow", {"steps": 30}))  # ~1.5 s, loop stays free
        await asyncio.sleep(0.6)
        assert (await client.call_tool("echo", {"text": "hi"})).content[0].text == "echo: hi"
        assert (await long_call).data == "done 30"
        assert (await client.call_tool("pid", {})).data == first
    assert services.services["fake"].status()["state"] == "running"


async def test_a_stuck_server_is_reported_then_restarted(site, monkeypatch):
    base, services, _ = site
    monkeypatch.setattr(mcp_bridge, "CHECK_AFTER_S", 0.3)
    monkeypatch.setattr(mcp_bridge, "PING_TIMEOUT_S", 0.5)
    monkeypatch.setattr(mcp_bridge, "RESTART_AFTER_S", 2.5)
    service = services.services["fake"]
    async with Client(f"{base}/mcp/fake", timeout=15) as client:
        first = (await client.call_tool("pid", {})).data
        with pytest.raises(MCPError):  # the client gives up; the server stays frozen
            await client.call_tool("freeze", {"seconds": 30}, timeout=1)
        await asyncio.sleep(0.2)
        with pytest.raises(MCPError) as refused:  # answered at once instead of hanging
            await client.call_tool("echo", {"text": "x"})
        assert "fake is not responding" in str(refused.value)
        assert service.status()["state"] == "unresponsive"
        while service.stuck_for() < 2.6:
            await asyncio.sleep(0.2)
    async with Client(f"{base}/mcp/fake", timeout=15) as client:
        assert (await client.call_tool("pid", {})).data != first  # restarted on its own
        assert (await client.call_tool("echo", {"text": "ok"})).content[0].text == "echo: ok"
    assert service.status()["state"] == "running"


async def test_a_request_that_finds_the_server_gone_is_sent_to_a_new_one(site):
    base, services, _ = site
    service = services.services["fake"]
    async with Client(f"{base}/mcp/fake") as client:
        first = (await client.call_tool("pid", {})).data
        dead = service.process

        def broken(message):
            raise BrokenPipeError(32, "Broken pipe")

        dead.send = broken  # the process died and nobody noticed yet
        assert (await client.call_tool("echo", {"text": "again"})).content[0].text == "echo: again"
        assert service.process is not dead
        assert (await client.call_tool("pid", {})).data != first


async def test_unknown_and_disabled_servers_are_explained(site):
    base, _, _ = site
    async with httpx.AsyncClient() as http:
        for name in ("nope", "off"):
            r = await http.post(f"{base}/mcp/{name}", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert r.status_code == 404 and "enabled: fake, self" in r.json()["error"]["message"]
    async with Client(f"{base}/mcp") as winhand:  # winhand's own tools are unaffected
        assert "session_start" in {t.name for t in await winhand.list_tools()}


async def test_a_local_http_server_is_passed_through(site):
    base, _, _ = site
    async with Client(f"{base}/mcp/self") as client:
        assert client.initialize_result.server_info.name == "winhand"
        assert "fs_read" in {t.name for t in await client.list_tools()}


async def test_a_server_that_cannot_start_says_why(site):
    base, services, _ = site
    services.configure(
        [
            *[s.entry for s in services.services.values()],
            ServerEntry(name="broken", command="no-such-program-xyz"),
        ]
    )
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{base}/mcp/broken",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            },
            headers={"accept": "application/json, text/event-stream"},
        )
    assert "cannot start 'no-such-program-xyz'" in r.json()["error"]["message"]
    assert services.services["broken"].status()["state"] == "error"


async def test_probe_lists_what_a_configuration_offers():
    result = await probe(fake_entry())
    assert result["ok"] and result["server"] == {"name": "fake-local", "version": "1.2.3"}
    assert {t["name"] for t in result["tools"]} >= {"echo", "slow"}
    failed = await probe(
        ServerEntry(
            name="bad",
            command=sys.executable,
            args=["-c", "import sys; print('boom', file=sys.stderr)"],
            startup_timeout_s=5,
        )
    )
    assert not failed["ok"] and failed["error"] and "boom" in "\n".join(failed.get("stderr_tail", []))
