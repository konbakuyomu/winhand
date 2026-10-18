"""winhand 0.7.0: a token is typed in once (secret_prompt(key=...)) and programs get it from then on
(run / job_start secrets=[...]), without the AI ever seeing it."""

from __future__ import annotations

import json
import sys

import pytest
from fastmcp import Client

from winhand import guard, redact, secret_store, workspace
from winhand.actions import Action, describe
from winhand.config import Config
from winhand.server import build_server

TOKEN = "cf-api-token-0123456789"


@pytest.fixture
async def client(monkeypatch, winhand_home):
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: TOKEN)
    redact.reset()
    async with Client(build_server(Config())) as c:
        yield c
    redact.reset()


def _echo(name):
    return {
        "command": sys.executable,
        "args": ["-c", f"import os; v = os.environ.get('{name}', ''); print(len(v), v)"],
    }


async def test_a_token_is_typed_once_and_programs_get_it(client, winhand_home):
    saved = (
        await client.call_tool(
            "secret_prompt", {"message": "Cloudflare API token", "key": "CLOUDFLARE_API_TOKEN"}
        )
    ).structured_content
    assert saved["saved"] is True and saved["path"].endswith("secrets.env") and TOKEN not in json.dumps(saved)
    assert (winhand_home / "secrets.env").read_text(encoding="utf-8") == f"CLOUDFLARE_API_TOKEN={TOKEN}\n"
    ran = (
        await client.call_tool("run", {**_echo("CLOUDFLARE_API_TOKEN"), "secrets": ["CLOUDFLARE_API_TOKEN"]})
    ).structured_content
    # the program had it (23 characters); what it printed of it is masked
    assert ran["stdout"].startswith(f"{len(TOKEN)} «redacted:") and TOKEN not in json.dumps(ran)
    # without secrets=[...] a program does not get it
    plain = (await client.call_tool("run", _echo("CLOUDFLARE_API_TOKEN"))).structured_content
    assert plain["stdout"].strip() == "0"


async def test_the_store_stays_masked_after_a_restart(client, winhand_home):
    (winhand_home).mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text(f"GH_TOKEN={TOKEN}\n", encoding="utf-8")
    redact.reset()  # what winhand remembered in memory is gone: the store itself is masked
    ran = (await client.call_tool("run", {**_echo("GH_TOKEN"), "secrets": ["GH_TOKEN"]})).structured_content
    assert TOKEN not in json.dumps(ran) and "«redacted:winhand»" in ran["stdout"]


async def test_a_name_not_stored_says_what_is_and_how_to_add_it(client, winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text(f"GH_TOKEN={TOKEN}\n", encoding="utf-8")
    missing = (
        await client.call_tool("run", {**_echo("X"), "secrets": ["CLOUDFLARE_API_TOKEN"]})
    ).structured_content
    assert (
        "it holds: GH_TOKEN" in missing["error"]
        and "secret_prompt(message, key='CLOUDFLARE_API_TOKEN')" in missing["error"]
    )
    job = (await client.call_tool("job_start", {"command": "x", "secrets": ["NOPE"]})).structured_content
    assert "not in this machine's secret store: NOPE" in job["error"]


def test_the_ai_never_reads_or_writes_the_store(winhand_home):
    store = str(winhand_home / "secrets.env")
    for action in (
        Action("read", "fs_read", (store,)),
        Action("write", "fs_write", (store,)),
        Action("delete", "fs_op", (store,)),
        Action("command", "run", text="Get-Content $env:USERPROFILE\\.winhand\\secrets.env"),
        Action("command", "run", text=f"cat {store}"),
    ):
        verdict = guard.judge([action])
        assert verdict.decision == "deny" and "secret_prompt" in verdict.deny[0][0].reason, action
    # the masked dialog is how a value gets in: nothing for a guard to refuse
    assert describe("secret_prompt", {"message": "x", "key": "GH_TOKEN"}) == []


def test_context_lists_the_names_never_the_values(winhand_home, tmp_path):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text(
        f"GH_TOKEN={TOKEN}\nCLOUDFLARE_API_TOKEN={TOKEN}x\n", encoding="utf-8"
    )
    out = workspace.context(Config(context={"skills": []}), str(tmp_path))
    assert (
        out["secrets"]["names"] == ["CLOUDFLARE_API_TOKEN", "GH_TOKEN"]
        and "secrets=['NAME']" in out["secrets"]["use"]
    )
    assert TOKEN not in json.dumps(out)
    assert any(r["file"] == guard.BUILTIN for r in out["guards"]["rules"])
