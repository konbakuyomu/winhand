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


# ------------------------------------------------------------------ the app's 密钥 section


def test_the_person_manages_the_store_in_the_app(winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    store = winhand_home / "secrets.env"
    store.write_text(f"# kept as written\nGH_TOKEN={TOKEN}\nXUI_PASSWORD=pw-123456\n", encoding="utf-8")
    told = []

    def listener():
        told.append(1)

    secret_store.listeners.append(listener)
    try:
        assert secret_store.summary()["items"] == [
            {"name": "GH_TOKEN", "chars": len(TOKEN)}, {"name": "XUI_PASSWORD", "chars": 9},
        ]  # fmt: skip
        assert secret_store.put("CLOUDFLARE_API_TOKEN", "  cf-new-value-123  \n") == "CLOUDFLARE_API_TOKEN"
        # renamed in place, the value kept (left empty); then a new value
        secret_store.put("GITHUB_TOKEN", "", original="GH_TOKEN")
        secret_store.put("XUI_PASSWORD", "pw-changed-789", original="XUI_PASSWORD")
        assert store.read_text(encoding="utf-8") == (
            f"# kept as written\nGITHUB_TOKEN={TOKEN}\nXUI_PASSWORD=pw-changed-789\nCLOUDFLARE_API_TOKEN=cf-new-value-123\n"
        )
        secret_store.remove("XUI_PASSWORD")
        assert "XUI_PASSWORD" not in store.read_text(encoding="utf-8") and len(told) == 4
        for bad, why in (
            (lambda: secret_store.put("9X", "v"), "不能以数字开头"),
            (lambda: secret_store.put("GITHUB_TOKEN", "v"), "已经有叫 GITHUB_TOKEN"),
            (lambda: secret_store.put("NEW", ""), "请填写值"),
            (lambda: secret_store.put("NEW", "a\nb"), "一行"),
            (lambda: secret_store.put("NEW", "", original="GONE"), "没有叫 GONE"),
            (lambda: secret_store.remove("GONE"), "没有叫 GONE"),
        ):
            with pytest.raises(secret_store.SecretError, match=why):
                bad()
    finally:
        secret_store.listeners.remove(listener)


async def test_a_value_set_in_the_app_is_masked_at_once(client, winhand_home):
    secret_store.put("PANEL_PASSWORD", "panel-pass-424242")
    ran = (
        await client.call_tool("run", {**_echo("PANEL_PASSWORD"), "secrets": ["PANEL_PASSWORD"]})
    ).structured_content
    assert ran["stdout"].startswith("17 «redacted:") and "panel-pass-424242" not in json.dumps(ran)


def test_no_screenshot_while_the_app_shows_a_value(winhand_home):
    from winhand import desktop

    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text(f"GH_TOKEN={TOKEN}\n", encoding="utf-8")
    try:
        assert secret_store.reveal("GH_TOKEN", show=False) == TOKEN and not secret_store.shown()  # copied
        assert secret_store.reveal("GH_TOKEN") == TOKEN and secret_store.shown()
        with pytest.raises(desktop.DesktopError, match="looking at a stored secret"):
            desktop.capture()
        secret_store.hide()
        assert not secret_store.shown()
    finally:
        secret_store.hide()


# ------------------------------------------------------------------ 0.7.2: known without context()


async def test_a_connecting_agent_is_told_the_stored_names(winhand_home):
    from winhand import server

    redact.reset()
    mcp = build_server(Config())
    async with Client(mcp) as c:
        assert "Stored here: none yet." in c.instructions
    secret_store.put("GH_TOKEN", TOKEN)  # the app or secret_prompt adds one: the next client knows
    secret_store.put("CLOUDFLARE_API_TOKEN", TOKEN + "x")
    async with Client(mcp) as c:
        text = c.instructions
    assert "Stored here: CLOUDFLARE_API_TOKEN, GH_TOKEN." in text and TOKEN not in text
    assert len(text) <= server.INSTRUCTIONS_MAX and text.endswith(server.GUIDE_CONTEXT)
    redact.reset()


def test_many_names_still_fit_in_what_claude_code_keeps(winhand_home):
    from winhand import server

    winhand_home.mkdir(parents=True, exist_ok=True)
    names = [f"SOME_RATHER_LONG_SERVICE_NAME_{i:02d}_API_TOKEN" for i in range(40)]
    (winhand_home / "secrets.env").write_text(
        "".join(f"{n}=v{i}\n" for i, n in enumerate(names)), encoding="utf-8"
    )
    text = server.instructions(Config())
    assert len(text) <= server.INSTRUCTIONS_MAX and text.endswith(server.GUIDE_CONTEXT)
    flat = " ".join(text.split())  # the line is wrapped
    assert names[0] in flat and "37 more (context() lists them)" in flat and names[-1] not in flat


async def test_asked_again_for_a_stored_name_the_person_can_just_cancel(monkeypatch, winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "secrets.env").write_text(f"GH_TOKEN={TOKEN}\n", encoding="utf-8")
    asked = []
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: asked.append(message))
    async with Client(build_server(Config())) as c:
        out = (
            await c.call_tool("secret_prompt", {"message": "GitHub token", "key": "GH_TOKEN"})
        ).structured_content
    assert asked[0].startswith("GH_TOKEN 已经存在这台电脑上了") and asked[0].endswith("GitHub token")
    assert (
        out["saved"] is False and out["already_stored"] == "GH_TOKEN" and "secrets=['GH_TOKEN']" in out["use"]
    )
    assert (winhand_home / "secrets.env").read_text(encoding="utf-8") == f"GH_TOKEN={TOKEN}\n"  # unchanged
