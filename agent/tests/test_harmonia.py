"""winhand 0.7.0 with Harmonia: keys synced from the owner's phone are used without the AI seeing
them. A fake `harmonia` command line stands in for the real one (harmonia-vault/harmonia)."""

from __future__ import annotations

import json
import sys

import pytest
from fastmcp import Client

from winhand import guard, harmonia, redact, secret_store, workspace
from winhand.actions import Action, describe
from winhand.config import Config
from winhand.server import build_server

CF = "cf-token-0123456789abcdef"
TEST_SHARED = "test-shared-value-111"
PROD_SHARED = "prod-shared-value-222"


class FakeHarmonia:
    """What the harmonia command line answers: `export --env <id> --format json` and `var set`."""

    def __init__(self, home, envs):
        self.home, self.envs, self.calls = home, envs, []
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.json").write_text(json.dumps({"deviceId": "dev-1"}), encoding="utf-8")
        (home / "keys.json").write_text(
            json.dumps({"signSeed": "SEED-SECRET", "boxSeed": "BOX"}), encoding="utf-8"
        )
        self.sync()

    def sync(self):
        """What a sync leaves in cache.json: names and ids in the clear, values encrypted."""
        cache = {
            "seq": len(self.calls),
            "environments": [{"id": f"id-{n}", "name": n, "active": True} for n in self.envs],
            "variables": {f"id-{n}": {k: {"value": "<sealed>"} for k in v} for n, v in self.envs.items()},
        }
        cache["seq"] = sum(len(v) for v in self.envs.values())  # changes only with the data
        (self.home / "cache.json").write_text(json.dumps(cache, sort_keys=True), encoding="utf-8")

    def __call__(self, args, stdin=None, timeout=0):
        self.calls.append(list(args))
        if args[:2] == ["export", "--env"]:
            name = args[2].removeprefix("id-")
            if name not in self.envs:
                return 125, "", f"harmonia：找不到环境“{name}”"
            return 0, json.dumps(self.envs[name]), ""
        if args[:2] == ["var", "set"]:
            self.envs.setdefault(args[2], {})[args[3]] = (stdin or "").splitlines()[0].strip()
            self.sync()
            return 0, f"已写入 {args[3]}，回读校验一致。\n注意：环境“{args[2]}”在这台电脑上没有启用。", ""
        return 2, "", "unknown"


@pytest.fixture
def fake(tmp_path, monkeypatch):
    home = tmp_path / "harmonia-home"
    monkeypatch.setenv("HARMONIA_HOME", str(home))
    monkeypatch.setattr(harmonia, "executable", lambda: "harmonia")
    cli = FakeHarmonia(
        home,
        {
            "Test": {"CLOUDFLARE_API_TOKEN": CF, "SHARED": TEST_SHARED, "FLAG": "1"},
            "Prod": {"SHARED": PROD_SHARED},
        },
    )
    monkeypatch.setattr(harmonia, "_cli", cli)
    redact.reset()
    yield cli
    redact.reset()


def _printing(*values):
    return {"command": sys.executable, "args": ["-c", f"print({list(values)!r})"]}


async def test_every_value_harmonia_gives_this_machine_is_masked(fake):
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("run", _printing(CF, TEST_SHARED, PROD_SHARED))).structured_content
    text = json.dumps(out, ensure_ascii=False)
    assert CF not in text and TEST_SHARED not in text and PROD_SHARED not in text, text
    # labelled with the variable's name; a name shared by two environments: both values masked
    assert (
        "«redacted:harmonia:CLOUDFLARE_API_TOKEN»" in text and text.count("«redacted:harmonia:SHARED»") == 2
    )
    exports = [c for c in fake.calls if c[0] == "export"]
    assert sorted(c[2] for c in exports) == ["id-Prod", "id-Test"]  # one export per environment
    assert (
        harmonia.masks()["harmonia:CLOUDFLARE_API_TOKEN"] == [CF] and "harmonia:FLAG" not in harmonia.masks()
    )


async def test_a_key_added_on_the_phone_is_masked_in_the_next_result(fake):
    async with Client(build_server(Config())) as client:
        await client.call_tool("run", _printing("warm up"))
        reads = len(fake.calls)
        # a sync that changed nothing rewrites the cache with the same content: nothing is read again
        fake.sync()
        await client.call_tool("run", _printing("again"))
        assert len(fake.calls) == reads
        # the phone adds a key; `harmonia exec` syncs it here, and the very result showing it is masked
        fake.envs["Prod"]["GITHUB_TOKEN"] = "gh-new-token-987654321"
        fake.sync()
        out = (await client.call_tool("run", _printing("gh-new-token-987654321"))).structured_content
    assert "gh-new-token-987654321" not in json.dumps(out) and "harmonia:GITHUB_TOKEN" in json.dumps(out)


async def test_without_a_connected_harmonia_nothing_is_read(fake, monkeypatch):
    (fake.home / "config.json").write_text("{}", encoding="utf-8")  # installed, not approved yet
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("run", _printing(CF))).structured_content
    assert CF in json.dumps(out) and fake.calls == []


async def test_masking_harmonia_can_be_turned_off(fake, winhand_home):
    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text("[redact]\nharmonia = false\n", encoding="utf-8")
    async with Client(build_server(Config())) as client:
        out = (await client.call_tool("run", _printing(CF))).structured_content
    assert CF in json.dumps(out) and fake.calls == []


def test_harmonias_files_are_off_limits(fake):
    keys = str(fake.home / "keys.json")
    for action in (
        Action("read", "fs_read", (keys,)),
        Action("delete", "fs_op", (str(fake.home),)),
        Action("command", "run", text=f"Get-Content {fake.home}\\keys.json".replace("/", "\\")),
        Action("command", "run", text="type %USERPROFILE%\\.harmonia\\keys.json"),
        Action("command", "run", text="harmonia manager"),
    ):
        verdict = guard.judge([action])
        assert verdict.decision == "deny", action
        assert all(rule.file == guard.BUILTIN for rule, _ in verdict.deny)
    # printing keys or changing the setup: the person decides
    for line in ("harmonia export --all --format dotenv", "harmonia var list --show", "harmonia var rm Prod SHARED",
                 r"C:\Users\me\AppData\Local\Programs\Harmonia\harmonia.exe logout --yes"):  # fmt: skip
        assert guard.judge([Action("command", "run", text=line)]).decision == "ask", line
    # using the keys is what Harmonia is for
    for line in ("harmonia exec --env Prod -- npx wrangler deploy", "harmonia status", "harmonia env list",
                 "harmonia var list", "harmonia skill"):  # fmt: skip
        assert guard.judge([Action("command", "run", text=line)]).decision is None, line
    # storing a key through secret_prompt is judged as the command winhand runs for it
    (stored,) = describe("secret_prompt", {"message": "x", "harmonia": "Prod/CLOUDFLARE_API_TOKEN"})
    assert stored.kind == "command" and stored.text == "harmonia var set Prod CLOUDFLARE_API_TOKEN"
    # a line typed with editing keys is not refused for naming nothing: these rules only ever name Harmonia
    assert guard.judge([Action("input", "session_send", text="", opaque=True)]).decision is None


async def test_a_search_never_returns_harmonias_device_keys(fake):
    (fake.home.parent / "notes.txt").write_text("my SEED-SECRET notes\n", encoding="utf-8")
    async with Client(build_server(Config())) as client:
        found = (
            await client.call_tool("fs_search", {"pattern": "SEED-SECRET", "path": str(fake.home.parent)})
        ).structured_content
        refused = (
            (await client.call_tool("fs_read", {"path": str(fake.home / "keys.json")}, raise_on_error=False))
            .content[0]
            .text
        )
    assert [m.split(":")[0] for m in found["matches"]] == ["notes.txt"] and "keys.json" in found[
        "guard_hidden"
    ]
    assert "refused" in refused and "harmonia exec" in refused


async def test_secret_prompt_stores_a_new_key_in_harmonia(fake, monkeypatch):
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: "  cf-fresh-token-555666777  ")
    async with Client(build_server(Config())) as client:
        saved = (
            await client.call_tool(
                "secret_prompt", {"message": "Cloudflare API token", "harmonia": "Prod/CLOUDFLARE_API_TOKEN"}
            )
        ).structured_content
        shown = (await client.call_tool("run", _printing("cf-fresh-token-555666777"))).structured_content
        bad = (
            await client.call_tool("secret_prompt", {"message": "x", "harmonia": "no-slash"})
        ).structured_content
    assert (
        saved["saved"] is True
        and saved["harmonia"] == "Prod/CLOUDFLARE_API_TOKEN"
        and "没有启用" in saved["notes"][0]
    )
    assert ["var", "set", "Prod", "CLOUDFLARE_API_TOKEN"] in fake.calls
    assert fake.envs["Prod"]["CLOUDFLARE_API_TOKEN"] == "cf-fresh-token-555666777"  # given on stdin, trimmed
    assert "cf-fresh-token-555666777" not in json.dumps(saved) + json.dumps(shown)
    assert "environment>/<VARIABLE_NAME>" in bad["error"]


async def test_context_tells_the_ai_how_to_use_the_keys_unseen(fake, tmp_path):
    out = workspace.context(Config(context={"skills": []}), str(tmp_path))
    keys = out["harmonia"]
    assert keys["connected"] is True and sorted(keys["environments"]) == ["Prod", "Test"]
    assert "'exec', '--env'" in keys["use"] and "secret_prompt" in keys["use"]
    assert any(r["file"] == guard.BUILTIN for r in out["guards"]["rules"])


async def test_without_harmonia_context_says_nothing_about_it(tmp_path):
    out = workspace.context(Config(context={"skills": []}), str(tmp_path))
    assert "harmonia" not in out and not any(r["file"] == guard.BUILTIN for r in out["guards"]["rules"])


def test_one_masked_value_inside_another_is_never_cut_in_two():
    r = redact.Redactor({"value": ["abc123"], "harmonia:LONG": ["xxabc123yy"]})
    assert r.text("see xxabc123yy and abc123") == ("see «redacted:harmonia:LONG» and «redacted:value»", 2)
