"""skills.py: where skills are found (project chain, user folders, Claude Code and Codex plugins), what
they are called, which project is trusted, and how they are searched and shown."""

from __future__ import annotations

import json
import os
import sys

import pytest
from fastmcp import Client

from winhand import skills, workspace
from winhand.config import Config
from winhand.server import build_server


def write(path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def skill(folder, name: str, description: str = "d", extra: str = ""):
    return write(
        folder / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\n"
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "user"
    folder.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(folder))
    return folder


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


def ids(index) -> list[str]:
    return [s.id for s in index.skills]


# ------------------------------------------------------------------ the project chain


def test_every_folder_up_to_the_root_and_each_tools_folder(home, repo):
    sub = repo / "packages" / "cli"
    skill(repo / ".agents" / "skills", "root-agents")
    skill(repo / ".codex" / "skills", "openspec-flow")
    skill(repo / ".claude" / "skills", "root-claude")
    skill(repo / ".pi" / "skills", "root-pi")
    skill(sub / ".agents" / "skills", "near")
    skill(sub / ".claude" / "skills", "nested-claude")
    index = skills.build(Config(context={"skills": []}), str(sub), str(repo), with_plugins=False)
    assert ids(index) == [
        "project:packages/cli/.agents/skills/near",
        "project:packages/cli/.claude/skills/nested-claude",
        "project:.agents/skills/root-agents",
        "project:.claude/skills/root-claude",
        "project:.codex/skills/openspec-flow",
    ]  # nearest first; .pi only where the work starts
    at_root = skills.build(Config(context={"skills": []}), str(repo), str(repo), with_plugins=False)
    assert "project:.pi/skills/root-pi" in ids(
        at_root
    ) and "project:packages/cli/.agents/skills/near" not in ids(at_root)


def test_without_git_only_the_folder_itself(home, tmp_path):
    above = tmp_path / "plain"
    here = above / "inner"
    skill(above / ".agents" / "skills", "above")
    skill(here / ".agents" / "skills", "here")
    root = workspace.project_root(str(here))
    index = skills.build(Config(context={"skills": []}), str(here), root, with_plugins=False)
    assert ids(index) == ["project:.agents/skills/here"]


def test_codex_folders_are_walked_deep_claude_folders_one_level(home):
    codex = home / ".codex" / "skills"
    skill(codex, "officecli")
    skill(codex / "officecli", "officecli-docx")  # a skill inside a skill: Codex counts both
    skill(codex / ".system", "hidden")
    skill(codex / "synced", "skipped")
    claude = home / ".claude" / "skills"
    skill(claude, "top")
    skill(claude / "group", "deeper")  # Claude Code reads only the folders directly inside
    index = skills.build(Config(), None, None, with_plugins=False)
    assert ids(index) == [
        "user:~/.claude/skills/top",
        "user:~/.codex/skills/officecli",
        "user:~/.codex/skills/officecli/officecli-docx",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need extra rights on Windows")
def test_a_file_reached_twice_is_one_skill_with_both_ids(home, repo):
    real = skill(repo / ".agents" / "skills", "dev-board").parent
    (home / ".claude" / "skills").mkdir(parents=True)
    os.symlink(real, home / ".claude" / "skills" / "dev-board")
    index = skills.build(Config(), str(repo), str(repo), with_plugins=False)
    (only,) = index.skills
    assert only.id == "project:.agents/skills/dev-board" and only.also == ["user:~/.claude/skills/dev-board"]


def test_a_name_resolves_to_the_nearest_trusted_skill(home, repo):
    skill(home / ".agents" / "skills", "close-out", "the user's")
    skill(repo / ".agents" / "skills", "close-out", "the project's")
    untrusted = skills.build(Config(), str(repo), str(repo), with_plugins=False)
    assert skills.find(untrusted, "close-out").layer == "user"  # a cloned repository does not stand in
    assert skills.find(untrusted, "project:.agents/skills/close-out").layer == "project"
    trusted = skills.build(
        Config(context={"trusted_roots": [str(repo)]}), str(repo), str(repo), with_plugins=False
    )
    assert skills.find(trusted, "close-out").layer == "project"


# ------------------------------------------------------------------ trust


def test_project_skills_modes_and_an_explicit_untrusted(home, repo):
    skill(repo / ".agents" / "skills", "own")
    base = {"skills": []}
    assert ids(skills.build(Config(context=base), str(repo), str(repo))) == ["project:.agents/skills/own"]
    assert ids(skills.build(Config(context={**base, "project_skills": "off"}), str(repo), str(repo))) == []
    assert (
        ids(skills.build(Config(context={**base, "project_skills": "trusted"}), str(repo), str(repo))) == []
    )
    trusted = {**base, "project_skills": "trusted", "trusted_roots": [str(repo.parent)]}
    assert ids(skills.build(Config(context=trusted), str(repo), str(repo))) == ["project:.agents/skills/own"]
    write(home / ".codex" / "config.toml", f'[projects.{json.dumps(str(repo))}]\ntrust_level = "untrusted"\n')
    denied = {**base, "trusted_roots": [str(repo.parent)]}
    assert ids(skills.build(Config(context=denied), str(repo), str(repo))) == []  # the owner said no


# ------------------------------------------------------------------ plugins


def claude_plugin(home, market, name, version, enabled=True, **manifest):
    base = home / ".claude" / "plugins" / "cache" / market / name / version
    if manifest:
        write(base / ".claude-plugin" / "plugin.json", json.dumps({"name": name, **manifest}))
    installed = home / ".claude" / "plugins" / "installed_plugins.json"
    data = json.loads(installed.read_text()) if installed.exists() else {"version": 2, "plugins": {}}
    data["plugins"][f"{name}@{market}"] = [{"scope": "user", "installPath": str(base), "version": version}]
    write(installed, json.dumps(data))
    settings = home / ".claude" / "settings.json"
    current = json.loads(settings.read_text()) if settings.exists() else {}
    current.setdefault("enabledPlugins", {})[f"{name}@{market}"] = enabled
    write(settings, json.dumps(current))
    return base


def test_claude_plugins_enabled_for_the_project(home, repo):
    figma = claude_plugin(home, "official", "figma", "2.2.111", description="x")
    skill(figma / "skills", "figma-use")
    skill(figma / "skills-figquery", "not-declared")
    off = claude_plugin(home, "official", "off", "1.0.0", enabled=False)
    skill(off / "skills", "never")
    ce = claude_plugin(home, "ce-market", "context-engineering", "6dbe1a1")
    write(
        ce / ".claude-plugin" / "marketplace.json",
        json.dumps(
            {"plugins": [{"name": "context-engineering", "source": "./", "skills": ["./extra/tool-design"]}]}
        ),
    )
    skill(ce / "extra", "tool-design")
    index = skills.build(Config(context={"skills": []}), None, None)
    assert ids(index) == [
        "plugin:figma@official/figma-use",
        "plugin:context-engineering@ce-market/extra/tool-design",
    ]
    assert index.skills[0].name == "figma:figma-use" and skills.find(index, "figma-use") is index.skills[0]
    write(repo / ".claude" / "settings.local.json", json.dumps({"enabledPlugins": {"figma@official": False}}))
    in_repo = skills.build(Config(context={"skills": []}), str(repo), str(repo))
    assert "plugin:figma@official/figma-use" not in ids(in_repo)


def test_codex_plugins_from_config_or_the_owners_account(home):
    cache = home / ".codex" / "plugins" / "cache"
    for market, name, versions in (
        ("bundled", "documents", ["26.1111.1", "26.1119.2"]),
        ("remote", "figma", ["20.0.0"]),
        ("mattpocock", "skills-pack", ["1.3.2"]),
        ("codestable", "codestable", ["2.0.2"]),
        ("local-dev", "mine", ["1.0.0", "local"]),
    ):
        for version in versions:
            skill(cache / market / name / version / "skills", f"{name}-{version}")
    write(cache / "remote" / "figma" / ".codex-remote-plugin-install.json", "{}")
    write(
        home / ".codex" / "config.toml",
        '[plugins."documents@bundled"]\nenabled = true\n'
        '[plugins."skills-pack@mattpocock"]\nenabled = false\n'
        '[plugins."mine@local-dev"]\nenabled = true\n',
    )
    found = ids(skills.build(Config(context={"skills": []}), None, None))
    assert found == [
        "plugin:documents@bundled/documents-26.1119.2",  # the highest version
        "plugin:mine@local-dev/mine-local",  # local first
        "plugin:figma@remote/figma-20.0.0",  # installed from the account, not turned off
    ]  # codestable sits in the cache but nothing turns it on; skills-pack is turned off


# ------------------------------------------------------------------ host switches, areas, the map


def test_what_claude_code_or_codex_turned_off_is_marked_not_hidden(home):
    skill(home / ".agents" / "skills", "legacy", "Fill PDF forms")
    skill(home / ".agents" / "skills", "forms", "Fill PDF forms")
    write(home / ".claude" / "settings.json", json.dumps({"skillOverrides": {"legacy": "off"}}))
    write(home / ".codex" / "config.toml", '[[skills.config]]\nname = "legacy"\nenabled = false\n')
    index = skills.build(Config(), None, None)
    legacy = skills.find(index, "legacy")
    assert legacy.host_off == ["claude", "codex"]
    found = skills.search(Config(), index, ["pdf"])["results"]
    assert [r["name"] for r in found] == ["forms", "legacy"] and found[1]["host_off"] == ["claude", "codex"]


def test_the_map_has_pins_areas_and_projects(home, repo):
    skill(home / ".agents" / "skills", "kicad-local", "Edit KiCad schematics and PCB")
    skill(home / ".agents" / "skills", "officecli", "Edit docx, xlsx and pptx files")
    skill(home / ".agents" / "skills", "x", "")
    skill(repo / ".agents" / "skills", "build")
    write(home / ".codex" / "config.toml", f'[projects.{json.dumps(str(repo))}]\ntrust_level = "trusted"\n')
    cfg = Config(context={"pinned_skills": ["officecli", "missing"]})
    data = skills.skill_map(cfg, skills.build(cfg, None, None))
    assert [p["name"] for p in data["pinned"]] == ["officecli"]
    areas = {a["area"]: a for a in data["areas"]}
    assert areas["hardware"]["examples"] == ["kicad-local"] and areas["docs"]["count"] == 1
    assert data["unsorted"] == 1 and data["projects"] == [{"project": str(repo), "skills": 1}]
    fixed = Config(context={"skill_areas": {"kicad-local": ["ops"]}})
    assert skills.areas_of(fixed, skills.find(skills.build(fixed, None, None), "kicad-local")) == ["ops"]


# ------------------------------------------------------------------ search


def test_a_chinese_query_finds_an_english_skill(home):
    skill(home / ".agents" / "skills", "eide-builder", "Build and flash firmware for EIDE projects")
    skill(home / ".agents" / "skills", "officecli", "Edit Word and Excel files")
    found = skills.search(Config(), skills.build(Config(), None, None), ["烧录固件"])
    assert [r["name"] for r in found["results"]] == ["eide-builder"]
    assert found["results"][0]["load"] == {"name": "user:~/.agents/skills/eide-builder"}


def test_the_project_worked_in_takes_at_most_half(home, repo):
    for i in range(6):
        skill(repo / ".agents" / "skills", f"deploy-{i}", "deploy the service")
    for i in range(6):
        skill(home / ".agents" / "skills", f"ship-{i}", "deploy anything")
    cfg = Config(context={"trusted_roots": [str(repo)]})
    found = skills.search(cfg, skills.build(cfg, str(repo), str(repo)), ["deploy"], scope=str(repo), limit=6)
    names = [r["name"] for r in found["results"]]
    assert sum(n.startswith("deploy-") for n in names) == 3 and len(names) == 6


async def test_skill_searches_every_project_and_loads_by_id(home, repo, tmp_path):
    other = tmp_path / "other"
    (other / ".git").mkdir(parents=True)
    skill(other / ".codex" / "skills", "openspec-flow", "Plan changes with OpenSpec proposals")
    write(home / ".codex" / "config.toml", f'[projects.{json.dumps(str(other))}]\ntrust_level = "trusted"\n')
    async with Client(build_server(Config())) as c:
        found = (
            await c.call_tool("skill", {"query": ["openspec 提案"], "path": str(repo)})
        ).structured_content
        (hit,) = found["results"]
        assert hit["load"] == {"name": "project:.codex/skills/openspec-flow", "path": str(other)}
        assert hit["cross_project"] is True
        loaded = (
            await c.call_tool("skill", {"name": hit["load"]["name"], "path": hit["load"]["path"]})
        ).structured_content
        assert loaded["skill_md"] == str(other / ".codex" / "skills" / "openspec-flow" / "SKILL.md")
        no_path = await c.call_tool(
            "skill", {"name": "project:.codex/skills/openspec-flow"}, raise_on_error=False
        )
        assert no_path.is_error and "path=" in no_path.content[0].text
        neither = await c.call_tool("skill", {}, raise_on_error=False)
        assert neither.is_error
        area = (await c.call_tool("skill", {"area": "project"})).structured_content
        assert [r["name"] for r in area["results"]] == ["openspec-flow"]


async def test_a_subfolder_keeps_its_own_skills(home, repo):
    sub = repo / "apps" / "web"
    skill(sub / ".claude" / "skills", "deploy", "the web app's")
    cfg = Config(context={"trusted_roots": [str(repo)]})
    async with Client(build_server(cfg)) as c:
        loaded = (
            await c.call_tool("skill", {"name": "deploy", "path": str(sub / "src" / "x.ts")})
        ).structured_content
        out = (await c.call_tool("context", {"path": str(sub)})).structured_content
    assert loaded["id"] == "project:apps/web/.claude/skills/deploy" and loaded["project"] == str(repo)
    assert [s["id"] for s in out["skills"]] == ["project:apps/web/.claude/skills/deploy"]


def test_the_owners_report(home, winhand_home):
    skill(
        home / ".agents" / "skills",
        "smart-search-cli",
        "Search the web, fetch pages and check facts with sources",
    )
    skill(home / ".agents" / "skills", "tiny", "short")
    write(
        winhand_home / "activity" / "2026-11-22.jsonl",
        json.dumps({"title": "skill", "status": "ok", "args": {"name": "smart-search-cli"}}) + "\n",
    )
    data = skills.report(Config(context={"pinned_skills": ["smart-search-cli"]}))
    assert data["total"] == 2 and data["pinned"] == ["smart-search-cli"]
    assert data["skills"][0]["name"] == "smart-search-cli" and data["skills"][0]["days_used"] == 1
    assert data["vague"] == ["user:~/.agents/skills/tiny"]
