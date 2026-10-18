"""Skills and local MCP servers through winhand's own connection: skill, mcp_tools, mcp_call and the
tool-list cache behind the tools part of a cloud session's start."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap

import pytest
from fastmcp import Client
from mcp.types import ImageContent, TextContent

from winhand import local_mcp, mcp_bridge, session_bridge
from winhand.activity import ActivityHub
from winhand.config import Config, ServerEntry
from winhand.mcp_bridge import Services, compose_app
from winhand.relay_client import _free_port, serve_local
from winhand.server import build_server

from .test_mcp_bridge import fake_entry


def text_of(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


def write(path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "user"
    folder.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(folder))
    return folder


@pytest.fixture
async def services():
    """A fake stdio server and a disabled one, registered as the running agent's."""
    every = Services([fake_entry(), fake_entry("off", enabled=False)])
    local_mcp.active = every
    yield every
    await every.close()


@pytest.fixture
async def client(services):
    async with Client(build_server(Config())) as c:
        yield c


# ------------------------------------------------------------------ A2 skill(name)

OFFICE = """\
    ---
    name: officecli
    description: Edit Word and Excel files from the command line.
    ---
    # officecli
    Run scripts/office.py with the file to change.
    """


async def test_skill_returns_its_instructions_and_files(home):
    write(home / ".agents" / "skills" / "officecli" / "SKILL.md", OFFICE)
    write(home / ".agents" / "skills" / "officecli" / "scripts" / "office.py", "print('hi')\n")
    async with Client(build_server(Config())) as c:
        found = (await c.call_tool("skill", {"name": "OfficeCLI"})).structured_content
    assert "Run scripts/office.py" in found["content"]
    assert found["folder"] == str(home / ".agents" / "skills" / "officecli")
    assert found["description"] == "Edit Word and Excel files from the command line."
    assert {f["path"] for f in found["files"]} == {"SKILL.md", "scripts/office.py"}
    assert found["scope"] == "user" and "run" in found["note"]


async def test_a_skill_the_read_rules_protect_is_not_read(home, winhand_home):
    write(home / ".agents" / "skills" / "officecli" / "SKILL.md", OFFICE)
    write(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "read"
        path = ["**/officecli/**"]
        decision = "deny"
        reason = "private"
        """,
    )
    async with Client(build_server(Config())) as c:
        found = (await c.call_tool("skill", {"name": "officecli"})).structured_content
    assert found["content"] is None and "guards.toml" in found["refused"] and "private" in found["refused"]


async def test_an_unknown_skill_names_the_close_ones(home):
    write(home / ".agents" / "skills" / "officecli" / "SKILL.md", OFFICE)
    async with Client(build_server(Config())) as c:
        missing = await c.call_tool("skill", {"name": "officecl"}, raise_on_error=False)
    assert missing.is_error and "officecli" in text_of(missing)


async def test_a_projects_skill_comes_first(home, tmp_path):
    write(home / ".agents" / "skills" / "officecli" / "SKILL.md", OFFICE)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    write(
        project / ".claude" / "skills" / "officecli" / "SKILL.md", "---\nname: officecli\n---\nPROJECT COPY\n"
    )
    async with Client(build_server(Config())) as c:
        found = (await c.call_tool("skill", {"name": "officecli", "path": str(project)})).structured_content
    assert found["scope"] == "project" and "PROJECT COPY" in found["content"]


# ------------------------------------------------------------------ A3 the tools part


def test_the_tools_part_comes_from_the_cache_and_starts_nothing(services):
    fake = services.services["fake"].entry
    local_mcp.remember(
        fake,
        [{"name": "echo", "description": "Say it back.\nMore lines.", "inputSchema": {"type": "object"}}],
        "A fake server for the tests.",
    )
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert "## fake\nA fake server for the tests.\n- echo: Say it back." in text
    assert "More lines" not in text and "## off" not in text  # one line per tool; disabled ones left out
    assert "mcp_call(server, tool, arguments)" in text
    assert services.services["fake"].process is None  # nothing was started


def test_a_server_never_listed_or_changed_since_says_so(services, winhand_home):
    changed = ServerEntry(**{**fake_entry().__dict__, "args": ["-u", "other.py"]})
    local_mcp.remember(changed, [{"name": "echo"}])  # listed under another configuration
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert '## fake\n(not listed yet: mcp_tools("fake") starts it' in text


def test_a_broken_cache_is_no_cache(services, winhand_home):
    write(winhand_home / "cache" / "mcp-tools.json", "{not json")
    assert local_mcp.cached(services.services["fake"].entry) is None
    assert "not listed yet" in local_mcp.session_text(session_bridge.BUDGET_CHARS)
    write(winhand_home / "cache" / "mcp-tools.json", json.dumps({"fake": {"fingerprint": 1, "tools": 2}}))
    assert local_mcp.cached(services.services["fake"].entry) is None


def test_many_servers_fit_the_budget(winhand_home, monkeypatch):
    entries = [fake_entry(f"server-{i:02}") for i in range(40)]
    every = Services(entries)
    monkeypatch.setattr(local_mcp, "active", every)
    tools = [{"name": f"tool_{j}", "description": "x" * 140} for j in range(15)]
    for entry in entries:
        local_mcp.remember(entry, tools)
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert session_bridge._size(text) <= session_bridge.BUDGET_CHARS
    assert "## server-00\n- tool_0: " in text  # the first ones in full
    assert "tool_0, tool_1" in text or "left out for lack of room" in text  # the rest by name, or counted


# ------------------------------------------------------------------ A4 what fills the cache


async def test_a_client_listing_through_the_gateway_fills_the_cache(winhand_home):
    port = _free_port()
    every = Services([fake_entry()], ActivityHub())
    server, task = await serve_local(compose_app(build_server(Config()).http_app(), every), port)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp/fake") as c:
            await c.list_tools()
        for _ in range(100):  # written by a thread of its own
            item = local_mcp.cached(every.services["fake"].entry)
            if item:
                break
            await asyncio.sleep(0.05)
        assert item and "echo" in {t["name"] for t in item["tools"]}
        assert item["instructions"] == "A fake server for the tests."
        echo = next(t for t in item["tools"] if t["name"] == "echo")
        assert "text" in echo["inputSchema"]["properties"]
    finally:
        await every.close()
        server.should_exit = True
        await task


async def test_a_server_reached_over_http_is_listed_and_called(winhand_home):
    """winhand's own endpoint, set up as a local HTTP server, stands in for one."""
    port = _free_port()
    every = Services([ServerEntry(name="self", url=f"http://127.0.0.1:{port}/mcp")])
    server, task = await serve_local(compose_app(build_server(Config()).http_app(), every), port)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            listed = (await c.call_tool("mcp_tools", {"server": "self"})).structured_content
            names = {t["name"] for t in listed["tools"]}
            assert {"run", "mcp_call", "skill"} <= names
            run = next(t for t in listed["tools"] if t["name"] == "run")
            assert "command" in run["inputSchema"]["properties"]
            assert "winhand drives the Windows machine" in listed["instructions"]
            profiles = await c.call_tool("mcp_call", {"server": "self", "tool": "profile_list"})
            assert not profiles.is_error and "gdb" in text_of(profiles)
        assert local_mcp.cached(every.services["self"].entry)["tools"]
    finally:
        await every.close()
        server.should_exit = True
        await task


async def test_the_test_button_fills_the_cache(winhand_home):
    entry = fake_entry()
    assert (await mcp_bridge.probe(entry))["ok"]
    tools = {t["name"]: t for t in local_mcp.cached(entry)["tools"]}
    assert tools["echo"]["inputSchema"]["properties"]["text"]["type"] == "string"
    assert local_mcp.cached(entry)["instructions"] == "A fake server for the tests."


# ------------------------------------------------------------------ A5 mcp_tools / mcp_call


async def test_mcp_tools_lists_a_server_and_remembers_it(client, services):
    before = (await client.call_tool("mcp_tools", {})).structured_content
    assert [s["server"] for s in before["servers"]] == ["fake"] and before["servers"][0]["tools"] is None
    listed = (await client.call_tool("mcp_tools", {"server": "fake"})).structured_content
    echo = next(t for t in listed["tools"] if t["name"] == "echo")
    assert (
        echo["description"] == "Say it back."
        and echo["inputSchema"]["properties"]["text"]["type"] == "string"
    )
    assert listed["instructions"] == "A fake server for the tests."
    after = (await client.call_tool("mcp_tools", {})).structured_content["servers"][0]
    assert after["state"] == "running" and {"name": "echo", "description": "Say it back."} in after["tools"]


async def test_mcp_call_returns_what_the_server_gave(client, services):
    echo = await client.call_tool(
        "mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "你好"}}
    )
    assert text_of(echo) == "echo: 你好"
    picture = await client.call_tool("mcp_call", {"server": "fake", "tool": "picture"})
    assert isinstance(picture.content[0], ImageContent) and picture.content[0].mime_type == "image/png"
    failed = await client.call_tool(
        "mcp_call", {"server": "fake", "tool": "fail", "arguments": {"why": "no luck"}}, raise_on_error=False
    )
    assert failed.is_error and "no luck" in text_of(failed)


async def test_mcp_call_says_clearly_what_is_wrong(client, services):
    nobody = await client.call_tool("mcp_call", {"server": "nope", "tool": "echo"}, raise_on_error=False)
    assert nobody.is_error and "nope" in text_of(nobody) and "enabled: fake" in text_of(nobody)
    off = await client.call_tool("mcp_call", {"server": "off", "tool": "echo"}, raise_on_error=False)
    assert off.is_error and "enabled: fake" in text_of(off)
    no_tool = await client.call_tool("mcp_call", {"server": "fake", "tool": "nope"}, raise_on_error=False)
    assert no_tool.is_error and "nope" in text_of(no_tool)
    nothing = await client.call_tool("mcp_call", {}, raise_on_error=False)
    assert nothing.is_error and "give server and tool" in text_of(nothing)
    lost = await client.call_tool("mcp_call", {"call_id": "m999"}, raise_on_error=False)
    assert lost.is_error and "m999" in text_of(lost)


async def test_a_long_call_comes_back_with_an_id_to_wait_on(client, services, monkeypatch):
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 0.05)
    first = (
        await client.call_tool("mcp_call", {"server": "fake", "tool": "slow", "arguments": {"steps": 30}})
    ).structured_content
    assert first["still_running"] and first["call_id"]
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 30)
    done = await client.call_tool("mcp_call", {"call_id": first["call_id"]})
    assert text_of(done) == "done 30"
    again = await client.call_tool("mcp_call", {"call_id": first["call_id"]}, raise_on_error=False)
    assert again.is_error  # handed out once


async def test_a_call_past_its_timeout_is_given_up_and_the_server_still_serves(client, services):
    late = await client.call_tool(
        "mcp_call",
        {"server": "fake", "tool": "slow", "arguments": {"steps": 100}, "timeout_s": 0.3},
        raise_on_error=False,
    )
    assert late.is_error and "did not answer tools/call within 0.3 s" in text_of(late)
    echo = await client.call_tool("mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "x"}})
    assert text_of(echo) == "echo: x"


async def test_a_slow_start_is_waited_on_across_calls(client, services, monkeypatch):
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 0.0)
    first = (await client.call_tool("mcp_tools", {"server": "fake"})).structured_content
    assert first["still_running"] and "again" in first["next"]
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 30)
    listed = (await client.call_tool("mcp_tools", {"server": "fake"})).structured_content
    assert "echo" in {t["name"] for t in listed["tools"]}


async def test_without_the_agent_winhand_starts_its_own(winhand_home, monkeypatch):
    """stdio mode: the servers come from the configuration file."""
    from winhand import config

    monkeypatch.setattr(config, "load", lambda: Config(servers=[fake_entry()]))
    async with Client(build_server(Config())) as c:
        echo = await c.call_tool("mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "y"}})
    assert text_of(echo) == "echo: y"
    own = local_mcp._own
    await own.close()


# ------------------------------------------------------------------ A6 the guard


async def test_a_guard_rule_on_mcp_refuses_before_anything_starts(client, services, winhand_home):
    write(
        winhand_home / "guards.toml",
        """\
        [[guard]]
        on = "mcp"
        match = '^fake\\.echo '
        decision = "deny"
        reason = "not that one"
        """,
    )
    refused = await client.call_tool(
        "mcp_call", {"server": "fake", "tool": "echo", "arguments": {"text": "x"}}, raise_on_error=False
    )
    assert (
        refused.is_error and "not that one" in text_of(refused) and "guards.toml rule 1" in text_of(refused)
    )
    assert services.services["fake"].process is None
    allowed = await client.call_tool("mcp_call", {"server": "fake", "tool": "pid"})
    assert not allowed.is_error


async def test_waiting_on_an_earlier_call_is_not_judged_again(services, winhand_home, monkeypatch):
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 0.05)
    async with Client(build_server(Config())) as c:
        first = (
            await c.call_tool("mcp_call", {"server": "fake", "tool": "slow", "arguments": {"steps": 20}})
        ).structured_content
        write(winhand_home / "guards.toml", '[[guard]]\non = "mcp"\ndecision = "deny"\n')
        monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 30)
        done = await c.call_tool("mcp_call", {"call_id": first["call_id"]})
        assert text_of(done) == "done 20"
        refused = await c.call_tool("mcp_call", {"server": "fake", "tool": "pid"}, raise_on_error=False)
        assert refused.is_error


def test_guide_mentions_the_new_tools():
    from winhand import server

    assert len(server.GUIDE + server.GUIDE_CONTEXT) <= 4096
    for name in ("skill(name)", "mcp_tools()", "mcp_call(server, tool, arguments)"):
        assert name in server.GUIDE and name in server.GUIDE_FULL


async def test_the_tasks_left_behind_do_not_leak(services):
    """A call nobody comes back for is dropped after a while."""
    local_mcp._calls["m1"] = (asyncio.ensure_future(asyncio.sleep(0)), 0.0)
    await asyncio.sleep(0.01)
    local_mcp._forget_old()
    assert "m1" not in local_mcp._calls


# ------------------------------------------------------------------ round 1 of the verification


@pytest.mark.parametrize(
    "tools",
    [
        [1, 2],
        ["echo"],
        [{"description": "x"}],
        [{"name": "x", "description": 3}],
        [{"name": "x", "inputSchema": []}],
    ],
)
async def test_a_cache_entry_of_the_wrong_shape_is_no_cache(client, services, winhand_home, tools):
    fake = services.services["fake"].entry
    entry = {"fingerprint": local_mcp.fingerprint(fake), "tools": tools}
    write(winhand_home / "cache" / "mcp-tools.json", json.dumps({"fake": entry}))
    assert local_mcp.cached(fake) is None
    assert "## fake\n(not listed yet" in local_mcp.session_text(session_bridge.BUDGET_CHARS)
    listed = (await client.call_tool("mcp_tools", {})).structured_content
    assert listed["servers"][0]["tools"] is None


def test_the_tools_part_holds_its_budget_after_masking(services, monkeypatch):
    """Each line fits before masking; masked, every secret grows from 7 to 16 characters."""
    from winhand import redact

    monkeypatch.setattr(redact, "current", lambda: redact.Redactor(values={"value": ["appleng"]}))
    tools = [{"name": f"t{i:02}", "description": "appleng " * 18} for i in range(50)]
    local_mcp.remember(services.services["fake"].entry, tools)
    text = local_mcp.session_text(session_bridge.BUDGET_CHARS)
    assert "appleng" not in text and session_bridge._size(text) <= session_bridge.BUDGET_CHARS
    assert "- t49: «redacted" in text  # every tool still listed with its line


def test_a_secret_in_a_long_skill_description_is_counted(home):
    from winhand import redact, workspace

    write(
        home / ".agents" / "skills" / "long" / "SKILL.md",
        f"---\nname: long\ndescription: {'x' * 295} appleng-secret-value {'y' * 40}\n---\n",
    )
    redact.remember("appleng-secret-value")
    try:
        out = workspace.context(Config())
    finally:
        redact.reset()
    (skill,) = [s for s in out["skills"] if s["name"] == "long"]
    assert "appleng" not in skill["description"] and out["redacted"] >= 1 and out["redaction_note"]


async def test_mcp_tools_returns_what_it_just_listed_even_if_the_cache_cannot_be_written(
    client, services, monkeypatch
):
    local_mcp.remember(services.services["fake"].entry, [{"name": "stale_tool"}])

    def refuse(*args):
        raise PermissionError("in use")

    monkeypatch.setattr(local_mcp.os, "replace", refuse)
    listed = (await client.call_tool("mcp_tools", {"server": "fake"})).structured_content
    assert "echo" in {t["name"] for t in listed["tools"]} and "stale_tool" not in str(listed)


async def test_a_server_stuck_on_mcp_call_is_restarted(client, services, monkeypatch):
    monkeypatch.setattr(mcp_bridge, "CHECK_AFTER_S", 0.1)
    monkeypatch.setattr(mcp_bridge, "PING_TIMEOUT_S", 0.3)
    monkeypatch.setattr(mcp_bridge, "RESTART_AFTER_S", 0.2)
    first = (await client.call_tool("mcp_call", {"server": "fake", "tool": "pid"})).structured_content
    stuck = await client.call_tool(
        "mcp_call",
        {"server": "fake", "tool": "freeze", "arguments": {"seconds": 20}, "timeout_s": 0.3},
        raise_on_error=False,
    )
    assert stuck.is_error and services.services["fake"].unanswered  # left for the health check
    await asyncio.sleep(0.3)
    again = (await client.call_tool("mcp_call", {"server": "fake", "tool": "pid"})).structured_content
    assert again["result"] != first["result"]  # a new process answered


async def test_a_refused_skill_keeps_its_description_to_itself(home, winhand_home):
    write(
        home / ".agents" / "skills" / "officecli" / "SKILL.md",
        "---\nname: officecli\ndescription: TOP SECRET DESCRIPTION\n---\n",
    )
    write(
        winhand_home / "guards.toml",
        '[[guard]]\non = "read"\npath = ["**/officecli/**"]\ndecision = "deny"\n',
    )
    async with Client(build_server(Config())) as c:
        found = (await c.call_tool("skill", {"name": "officecli"})).structured_content
    assert found["content"] is None and "description" not in found and "SECRET" not in json.dumps(found)


async def test_a_skill_with_enormous_lines_is_shown_in_part(home):
    from winhand import fs

    skills = home / ".agents" / "skills"
    write(skills / "cr" / "SKILL.md", "---\rname: cr\r---\r" + "y" * (fs.READ_MAX_CHARS + 10))
    write(skills / "one" / "SKILL.md", "z" * (fs.READ_MAX_CHARS + 10))
    async with Client(build_server(Config())) as c:
        cr = (await c.call_tool("skill", {"name": "cr"})).structured_content
        one = (await c.call_tool("skill", {"name": "one"})).structured_content
    assert (
        cr["content"] == "---\rname: cr\r---\r" and "offset=4" in cr["truncated"]
    )  # lines as fs_read counts them
    assert one["content"] == "z" * fs.READ_MAX_CHARS and "first line" in one["truncated"]


async def test_a_chinese_skill_is_cut_at_what_one_result_holds(home):
    from winhand import fs
    from winhand.session.text import width

    skills = home / ".agents" / "skills"
    write(skills / "zh" / "SKILL.md", "---\nname: zh\n---\n" + "按这个步骤操作，先确认再执行。\n" * 2000)
    async with Client(build_server(Config())) as c:
        zh = (await c.call_tool("skill", {"name": "zh"})).structured_content
    # 32,000 characters of Chinese is about as many tokens: more than a client takes in one result
    assert width(zh["content"]) <= fs.READ_MAX_CHARS and "offset=" in zh["truncated"]


async def test_history_knows_the_id_of_a_call_still_running(client, services, monkeypatch):
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 0.05)
    first = (
        await client.call_tool("mcp_call", {"server": "fake", "tool": "slow", "arguments": {"steps": 20}})
    ).structured_content
    calls = (await client.call_tool("history", {})).structured_content
    entry = next(e for e in calls["calls"] if e["tool"] == "mcp_call")
    assert entry["status"] == "still_running" and entry["run_id"] == first["call_id"]
    assert first["call_id"] in entry["summary"]
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 30)
    await client.call_tool("mcp_call", {"call_id": first["call_id"]})


async def test_two_waits_on_one_call_get_one_result(services, monkeypatch):
    monkeypatch.setattr(local_mcp, "ANSWER_WITHIN_S", 0.01)
    result, key = await local_mcp.start_call("fake", "slow", {"steps": 10}, 30)
    assert result is None and key.startswith("m") and len(key) == 13
    outcomes = await asyncio.gather(
        local_mcp.wait_call(key, 30), local_mcp.wait_call(key, 30), return_exceptions=True
    )
    assert sum(isinstance(o, tuple) and o[0] is not None for o in outcomes) == 1
    assert sum(isinstance(o, ValueError) for o in outcomes) == 1


async def test_an_http_server_past_its_timeout_says_so(winhand_home):
    port = _free_port()
    every = Services([ServerEntry(name="self", url=f"http://127.0.0.1:{port}/mcp")])
    server, task = await serve_local(compose_app(build_server(Config()).http_app(), every), port)
    try:
        async with Client(f"http://127.0.0.1:{port}/mcp") as c:
            script = "import time; time.sleep(3)"
            late = await c.call_tool(
                "mcp_call",
                {
                    "server": "self",
                    "tool": "run",
                    "arguments": {"command": sys.executable, "args": ["-c", script]},
                    "timeout_s": 0.5,
                },
                raise_on_error=False,
            )
        assert late.is_error and "self did not answer tools/call within 0.5 s" in text_of(late)
    finally:
        await every.close()
        server.should_exit = True
        await task


# ------------------------------------------------------------------ round 3 of the verification


async def test_a_long_call_still_awaited_does_not_make_the_server_look_stuck(services, monkeypatch):
    monkeypatch.setattr(mcp_bridge, "CHECK_AFTER_S", 0.05)
    monkeypatch.setattr(mcp_bridge, "PING_TIMEOUT_S", 0.2)
    monkeypatch.setattr(mcp_bridge, "RESTART_AFTER_S", 0.1)
    fake = services.services["fake"]
    await fake.ensure_started()  # running already: the call itself is what is in flight
    result, key = await local_mcp.start_call("fake", "freeze", {"seconds": 1.5}, 20, wait_s=0.3)
    assert result is None
    pid = fake.process.pid
    assert not fake.unanswered  # its caller still waits within timeout_s
    assert await fake.check_health(before_call=True) is None
    done, _ = await local_mcp.wait_call(key, 20)
    assert done["content"][0]["text"] == "thawed" and fake.process.pid == pid  # not restarted


async def test_a_server_that_stopped_reading_is_judged_and_stopped_without_hanging(monkeypatch):
    import threading
    import time

    entry = ServerEntry(name="deaf", command=sys.executable, args=["-c", "import time; time.sleep(60)"])
    process = mcp_bridge.StdioProcess(entry, asyncio.get_running_loop(), lambda m: None, lambda c: None)
    blob = {"jsonrpc": "2.0", "method": "x", "params": {"blob": "x" * 4_000_000}}  # more than a pipe holds
    threading.Thread(target=mcp_bridge._quietly, args=(process.send, blob), daemon=True).start()
    for _ in range(100):
        if process.write_stuck_for() > 0.3:
            break
        await asyncio.sleep(0.05)
    assert process.write_stuck_for() > 0.3
    monkeypatch.setattr(mcp_bridge, "PING_TIMEOUT_S", 0.2)
    service = mcp_bridge.Service(entry)
    service.process = process
    before = threading.active_count()
    for _ in range(5):
        assert not await service.responds()  # no ping queued behind the blocked write
    assert threading.active_count() <= before
    started = time.monotonic()
    await asyncio.to_thread(process.stop)  # closing stdin would wait for the blocked write
    for _ in range(60):
        if not process.alive():
            break
        await asyncio.sleep(0.05)
    assert not process.alive() and time.monotonic() - started < 8


async def test_a_call_that_gives_up_while_its_server_is_stopped_is_forgotten(services):
    fake = services.services["fake"]
    await fake.ensure_started()
    task = asyncio.ensure_future(local_mcp.call_tool("fake", "freeze", {"seconds": 6}, 0.5))
    await asyncio.sleep(0.2)
    await fake.stop()  # the frozen server ignores EOF: stop waits, and the call times out meanwhile
    with pytest.raises(Exception):  # noqa: B017 - timed out or failed with the stop, either way
        await task
    assert not fake.unanswered  # nothing left to make the next start look stuck
