"""A model that writes a parameter's name with nothing after it breaks the whole call before it reaches
winhand ("could not be parsed as JSON"). Seen with 0.7.2: fs_write's expected_sha256 said "'' = must not
exist", so a model creating new files sent exactly that, four calls in a row (the raw input ended in
`"expected_sha256": }`). Nothing in a tool's schema may ask for an empty value; create-only is a boolean."""

from __future__ import annotations

import re

from fastmcp import Client

from winhand.config import Config
from winhand.server import build_server

# nullable parameters whose description may mention an empty string: the words are about the `new` of an
# item inside `edits` (a plain string there), not about this parameter
MAY_MENTION_EMPTY = {("fs_edit", "new")}


async def _tools() -> dict:
    async with Client(build_server(Config())) as client:
        return {t.name: t for t in await client.list_tools()}


async def test_no_nullable_parameter_asks_for_an_empty_value():
    asked = []
    for name, tool in (await _tools()).items():
        for param, schema in tool.input_schema["properties"].items():
            nullable = any(branch.get("type") == "null" for branch in schema.get("anyOf", []))
            text = schema.get("description") or ""
            if (
                nullable
                and (name, param) not in MAY_MENTION_EMPTY
                and re.search(r"''|\"\"|\bempty\b|\bblank\b", text, re.IGNORECASE)
            ):
                asked.append(f"{name}.{param}: {text}")
    assert not asked, "a nullable parameter must not ask for an empty value:\n" + "\n".join(asked)


async def test_the_hash_is_a_plain_string_and_create_only_is_a_switch():
    tools = await _tools()
    for name in ("fs_write", "fs_edit"):
        sha = tools[name].input_schema["properties"]["expected_sha256"]
        assert sha["type"] == "string" and "anyOf" not in sha and sha["default"] == ""
        assert "must not exist" not in sha["description"]
    overwrite = tools["fs_write"].input_schema["properties"]["overwrite"]
    assert overwrite["type"] == "boolean" and overwrite["default"] is True


async def test_fs_write_creates_only_when_asked_and_an_empty_hash_is_no_check(tmp_path):
    target = tmp_path / "docs" / "agents" / "issue-tracker.md"
    async with Client(build_server(Config())) as client:

        async def write(content, **more):
            args = {"path": str(target), "content": content, **more}
            return (await client.call_tool("fs_write", args)).structured_content

        assert (await write("one\n", overwrite=False))["bytes"] == 4  # new file, parent folders made
        refused = await write("two\n", overwrite=False)
        assert "already exists" in refused["error"] and target.read_text() == "one\n"
        # what a model sends for "not given": empty or null; neither is a check
        for value in ("", None):
            assert "error" not in await write("x\n", expected_sha256=value)
        # a real hash is still held against the file
        stale = await write("y\n", expected_sha256="0" * 64)
        assert "changed since you read it" in stale["error"] and target.read_text() == "x\n"


async def test_fs_edit_treats_an_empty_hash_as_no_check(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("alpha beta\n", encoding="utf-8")
    async with Client(build_server(Config())) as client:

        async def edit(**args):
            return (await client.call_tool("fs_edit", {"path": str(target), **args})).structured_content

        done = await edit(old="alpha", new="gamma", expected_sha256="")
        assert done["replacements"] == 1 and target.read_text() == "gamma beta\n"
        stale = await edit(old="gamma", new="delta", expected_sha256="0" * 64)
        assert "changed since you read it" in stale["error"] and target.read_text() == "gamma beta\n"
        # deleting text goes through edits: its `new` is a plain string, so an empty one is fine
        gone = await edit(edits=[{"old": " beta", "new": ""}])
        assert gone["replacements"] == 1 and target.read_text() == "gamma\n"
