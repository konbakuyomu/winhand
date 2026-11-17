"""context() sends changed rule files and keeps the latest lines when a file is too long."""

from __future__ import annotations

import hashlib
import json

import pytest
from fastmcp.client import Client

from winhand import workspace
from winhand.config import Config
from winhand.server import build_server


@pytest.fixture
def context_tree(tmp_path, monkeypatch):
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(tmp_path / "home"))
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    claude = root / "CLAUDE.md"
    claude.write_text("# Rules\nfirst rule\nlast rule\n", encoding="utf-8", newline="")
    (root / "AGENTS.md").write_text("other rules\n", encoding="utf-8", newline="")
    cfg = Config(context={"rules": [str(claude)], "formats": ["codex"], "skills": []})
    return root, claude, cfg


async def test_unchanged_context_returns_unchanged(context_tree):
    root, _, cfg = context_tree
    async with Client(build_server(cfg)) as client:
        full = (await client.call_tool("context", {"path": str(root)})).structured_content
        same = (
            await client.call_tool("context", {"path": str(root), "since": full["digest"]})
        ).structured_content
    assert len(full["digest"]) == 16
    assert same["unchanged"] is True and same["digest"] == full["digest"]


async def test_appended_lines_are_sent_without_unchanged_file_contents(context_tree):
    root, claude, cfg = context_tree
    old = claude.read_text(encoding="utf-8")
    added = "new rule one\nnew rule two\nnew rule three\n"
    async with Client(build_server(cfg)) as client:
        full = (await client.call_tool("context", {"path": str(root)})).structured_content
        claude.write_text(old + added, encoding="utf-8", newline="")
        changed = (
            await client.call_tool("context", {"path": str(root), "since": full["digest"]})
        ).structured_content
    rule, other = changed["rules"]
    assert changed["digest"] != full["digest"]
    assert rule["appended_after_line"] == len(old.splitlines())
    assert rule["content"] == added and rule["lines"] == 6
    assert rule["sha"] == hashlib.sha256((old + added).encode("utf-8")).hexdigest()[:16]
    assert other == {key: full["rules"][1][key] for key in ("path", "format", "scope", "lines", "sha")} | {
        "unchanged": True
    }
    assert changed["digest"] == workspace.context(cfg, str(root))["digest"]


async def test_middle_edit_sends_the_whole_file(context_tree):
    root, claude, cfg = context_tree
    new = "# Rules\nchanged middle rule\nlast rule\n"
    async with Client(build_server(cfg)) as client:
        full = (await client.call_tool("context", {"path": str(root)})).structured_content
        claude.write_text(new, encoding="utf-8", newline="")
        changed = (
            await client.call_tool("context", {"path": str(root), "since": full["digest"]})
        ).structured_content
    rule, other = changed["rules"]
    assert rule["content"] == new and "appended_after_line" not in rule
    assert rule["sha"] == hashlib.sha256(new.encode("utf-8")).hexdigest()[:16]
    assert other["unchanged"] is True and "content" not in other


async def test_unknown_since_sends_whole_files(context_tree):
    root, _, cfg = context_tree
    async with Client(build_server(cfg)) as client:
        full = (await client.call_tool("context", {"path": str(root)})).structured_content
        unknown = (
            await client.call_tool("context", {"path": str(root), "since": "not-in-cache"})
        ).structured_content
    assert unknown == full
    assert all("content" in rule and len(rule["sha"]) == 16 for rule in unknown["rules"])


def test_long_rule_sends_heading_directory_and_tail(context_tree):
    root, claude, cfg = context_tree
    lines = [f"line {number} " + "x" * 80 for number in range(1, 3001)]
    for number in (1, 1200, 2400):
        lines[number - 1] = f"## Section {number}"
    claude.write_text("\n".join(lines), encoding="utf-8", newline="")
    full = workspace.context(cfg, str(root))
    rule = full["rules"][0]
    directory, tail = rule["content"].split("\n\n", 1)
    assert directory.splitlines() == [
        "Headings:",
        "1: ## Section 1",
        "1200: ## Section 1200",
        "2400: ## Section 2400",
    ]
    first = 3000 - len(tail.splitlines()) + 1
    assert tail.splitlines() == lines[first - 1 :]
    assert rule["content"].endswith(lines[-1])
    assert rule["truncated"] == (
        f"showing headings and lines {first}-3000 of 3000; read lines 1-{first - 1} with "
        f"fs_read(path={str(claude)!r}, offset=1, limit={first - 1})"
    )
    assert len(json.dumps(full, ensure_ascii=False)) <= workspace.BUDGET_CHARS
    lines[1198] = "changed outside the shown tail"
    claude.write_text("\n".join(lines), encoding="utf-8", newline="")
    assert workspace.context(cfg, str(root), since=full["digest"])["digest"] != full["digest"]
