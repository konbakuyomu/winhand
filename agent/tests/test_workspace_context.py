"""context(): the owner's rules and skills for a folder, and the instructions clients see."""

from __future__ import annotations

import textwrap

from fastmcp.client import Client

from winhand import config, workspace
from winhand.config import Config, entry_from_dict
from winhand.server import build_server


def skill(folder, name: str, header: str) -> None:
    (folder / name).mkdir(parents=True, exist_ok=True)
    (folder / name / "SKILL.md").write_text(f"---\n{header}\n---\n\n# {name}\n", encoding="utf-8")


def tree(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("root rules", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "AGENTS.md").write_text("sub rules", encoding="utf-8")
    skill(root / ".agents" / "skills", "demo", "name: demo\ndescription: the project's demo")
    user = tmp_path / "user-skills"
    skill(user, "demo", "name: demo\ndescription: the user's demo")
    skill(user, "other", "name: other\ndescription: 'it''s another'")
    (user / ".hidden").mkdir()
    (user / "loose.zip").write_text("zip")
    global_rules = tmp_path / "global.md"
    global_rules.write_text("global rules", encoding="utf-8")
    (root / ".winhand").mkdir()
    (root / ".winhand" / "guards.toml").write_text(
        '[[guard]]\non = "write"\ndecision = "note"\nreason = "project note"\n', encoding="utf-8"
    )
    cfg = Config(
        servers=[entry_from_dict("pyocd-debug", {"command": "uv"})],
        context={"rules": [str(global_rules), str(tmp_path / "missing.md")], "skills": [str(user)]},
    )
    return root, cfg


# ------------------------------------------------------------------ A10


async def test_context_returns_rules_skills_guards_and_servers(tmp_path):
    root, cfg = tree(tmp_path)
    async with Client(build_server(cfg)) as client:
        out = (await client.call_tool("context", {"path": str(root / "sub" / "x.c")})).structured_content
        assert out["project_root"] == str(root)
        assert [(r["scope"], r["content"]) for r in out["rules"]] == [
            ("global", "global rules"),
            ("project", "root rules"),
            ("project", "sub rules"),
        ]
        skills = {s["name"]: s for s in out["skills"]}
        assert set(skills) == {"demo", "other"}
        assert skills["demo"]["scope"] == "project" and skills["demo"]["description"] == "the project's demo"
        assert skills["other"]["description"] == "it's another"
        assert skills["demo"]["path"].endswith("SKILL.md")
        assert any(r["reason"] == "project note" for r in out["guards"]["rules"])
        assert out["local_mcp_servers"]["names"] == ["pyocd-debug"]

        bare = (await client.call_tool("context", {})).structured_content
        assert [r["content"] for r in bare["rules"]] == ["global rules"]
        assert {s["scope"] for s in bare["skills"]} == {"user"} and "path=" in bare["next"]
        assert all(r.get("reason") != "project note" for r in bare["guards"]["rules"])


def test_defaults_read_the_usual_files_when_they_exist(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "AGENTS.md").write_text("codex global", encoding="utf-8")
    skill(
        home / ".codex" / "skills",
        "eide-builder",
        "name: eide-builder\ndescription: >\n  builds\n  EIDE projects",
    )
    skill(home / ".agents" / "skills", "comet", 'name: comet\ndescription: "Comet \\u5165\\u53e3"')
    out = workspace.context(Config())
    assert [r["content"] for r in out["rules"]] == ["codex global"]
    found = {s["name"]: s["description"] for s in out["skills"]}
    assert found == {"comet": "Comet 入口", "eide-builder": "builds EIDE projects"}
    none = workspace.context(Config(context={"rules": [], "skills": []}))
    assert none["rules"] == [] and none["skills"] == []


def test_project_root_without_git_is_the_folder_itself(tmp_path):
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "AGENTS.md").write_text("notes rules", encoding="utf-8")
    out = workspace.context(Config(context={"rules": [], "skills": []}), str(tmp_path / "notes" / "new.md"))
    assert out["project_root"] == str(tmp_path / "notes")
    assert [r["content"] for r in out["rules"]] == ["notes rules"]


# ------------------------------------------------------------------ A11


async def test_context_is_masked_and_cut_to_budget(tmp_path, winhand_home):
    config.save(Config(redact={"values": ["s3cret-value-123"]}))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / "AGENTS.md").write_text("token is s3cret-value-123\n", encoding="utf-8")
    big = "\n".join(f"line {i} " + "x" * 80 for i in range(1500))
    (root / "sub").mkdir()
    (root / "sub" / "AGENTS.md").write_text(big, encoding="utf-8")
    cfg = Config(context={"rules": [], "skills": []})
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool("context", {"path": str(root / "sub")})
    out = result.structured_content
    first, second = out["rules"]
    assert "s3cret-value-123" not in first["content"] and "redacted" in first["content"]
    assert "fs_read(" in second["truncated"] and "offset=" in second["truncated"]
    assert second["lines"] == 1500 and len(second["content"]) < len(big)
    assert sum(len(b.text) for b in result.content) < workspace.BUDGET_CHARS + 5_000


# ------------------------------------------------------------------ A12


def test_context_settings_survive_saving_the_config(winhand_home):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        textwrap.dedent(
            """\
            [context]
            rules = ['~/.codex/AGENTS.md']
            skills = ['D:\\Skills']
            stdio_hint = true
            """
        ),
        encoding="utf-8",
    )
    cfg = config.load()
    cfg.relay_url = "wss://relay.example/agent"
    config.save(cfg)
    again = config.load()
    assert again.relay_url == "wss://relay.example/agent"
    assert again.context == {"rules": ["~/.codex/AGENTS.md"], "skills": ["D:\\Skills"], "stdio_hint": True}


# ------------------------------------------------------------------ A1, A14


async def test_instructions_fit_and_help_has_the_rest():
    async with Client(build_server(Config())) as client:
        text = client.instructions or ""
        guide = (await client.call_tool("help", {})).data
    assert len(text) <= 4096 and "context(" in text and "help()" in text
    for part in (
        "screenshot",
        "job_start",
        "sys_info(checks=true)",
        "call(tool, arguments)",
        "<relay>/mcp/<name>",
    ):
        assert part in guide, part
    assert "context(path=" in guide


async def test_stdio_clients_get_the_context_hint_only_when_asked():
    async def instructions(cfg, **kw):
        async with Client(build_server(cfg, **kw)) as client:
            return client.instructions or ""

    plain = await instructions(Config(), local_stdio=True)
    asked = await instructions(Config(context={"stdio_hint": True}), local_stdio=True)
    as_text = await instructions(Config(context={"stdio_hint": "false"}), local_stdio=True)
    remote = await instructions(Config())
    assert "context(" not in plain and "context(" in asked and "context(" in remote
    assert "context(" not in as_text, 'stdio_hint = "false" means false'
    assert all(len(t) <= 4096 for t in (plain, asked, remote))


def test_the_budget_counts_what_json_makes_of_the_text(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text(
        "\n".join('path C:\\a\\b\\c "q"' for _ in range(6000)), encoding="utf-8"
    )
    out = workspace.context(Config(context={"rules": [], "skills": []}), str(tmp_path))
    size = len(__import__("json").dumps(out, ensure_ascii=False))
    assert "truncated" in out["rules"][0] and size <= workspace.BUDGET_CHARS + 1_000
