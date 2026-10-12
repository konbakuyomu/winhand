"""context() reading rules the way Codex and Claude Code do, finding checkouts by repository name,
and staying inside its budget."""

from __future__ import annotations

import json
import os

import pytest
from fastmcp.client import Client

from winhand import config, guard, workspace
from winhand.config import Config
from winhand.server import build_server


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home folder of our own: the real ~/.claude and ~/.codex stay out of the tests."""
    folder = tmp_path / "home-dir"
    folder.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(folder))
    monkeypatch.setattr(workspace, "managed_claude_md", lambda: str(tmp_path / "managed" / "CLAUDE.md"))
    return folder


def write(path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")  # the same bytes on Windows
    return path


def project(tmp_path, name: str = "repo"):
    root = tmp_path / "work" / name
    (root / ".git").mkdir(parents=True)
    return root


def ctx(path=None, **settings):
    return workspace.context(Config(context={"skills": [], **settings}), str(path) if path else None)


def skill(folder, name: str, description: str) -> None:
    write(folder / name / "SKILL.md", f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n")


# ------------------------------------------------------------------ A1


def test_claude_code_reads_from_the_filesystem_root_down(tmp_path, home):
    root = project(tmp_path)
    write(tmp_path / "managed" / "CLAUDE.md", "managed")
    write(home / ".claude" / "CLAUDE.md", "user")
    write(home / ".claude" / "rules" / "style.md", "style")
    write(tmp_path / "work" / "CLAUDE.md", "parent")
    write(root / "CLAUDE.md", "root")
    write(root / ".claude" / "CLAUDE.md", "root dot")
    write(root / "CLAUDE.local.md", "root local")
    write(root / ".claude" / "rules" / "general.md", "general")
    write(root / "sub" / "CLAUDE.md", "sub")
    write(root / "sub" / "deeper" / "CLAUDE.md", "below the folder: not loaded")
    out = ctx(root / "sub", formats=["claude-code"])
    assert [(r["content"], r["scope"]) for r in out["rules"]] == [
        ("managed", "managed"),
        ("user", "global"),
        ("style", "global"),
        ("parent", "project"),
        ("root", "project"),
        ("root dot", "project"),
        ("root local", "project"),
        ("general", "project"),
        ("sub", "project"),
    ]
    assert {r["format"] for r in out["rules"]} == {"claude-code"}
    assert out["formats"] == ["claude-code"]


def test_claude_config_dir_moves_the_user_files(tmp_path, home, monkeypatch):
    other = tmp_path / "claude-config"
    write(other / "CLAUDE.md", "moved")
    write(home / ".claude" / "CLAUDE.md", "not this one")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(other))
    assert [r["content"] for r in ctx(formats=["claude-code"])["rules"]] == ["moved"]


# ------------------------------------------------------------------ A2


def test_imports_follow_claude_codes_rules(tmp_path, home):
    root = project(tmp_path)
    write(
        root / "CLAUDE.md",
        "see @docs/style.md and mail me@example.com\n"
        "inline `@docs/hidden.md` is code\n"
        "```\n@docs/hidden.md\n```\n"
        "@a.md\n@not-a-file\n",
    )
    write(root / "docs" / "hidden.md", "hidden")
    write(root / "docs" / "style.md", "style @h2.md")
    write(root / "docs" / "h2.md", "@h3.md")
    write(root / "docs" / "h3.md", "@h4.md")
    write(root / "docs" / "h4.md", "@h5.md.")  # a full stop after the name still finds it
    write(root / "docs" / "h5.md", "five hops")
    write(root / "a.md", "a @b.md")
    write(root / "b.md", "b @a.md")
    out = ctx(root, formats=["claude-code"])
    rules = out["rules"]
    names = [os.path.relpath(r["path"], root).replace("\\", "/") for r in rules]
    assert names == ["CLAUDE.md", "docs/style.md", "docs/h2.md", "docs/h3.md", "docs/h4.md", "a.md", "b.md"]
    by = {n: r for n, r in zip(names, rules, strict=True)}
    assert by["docs/style.md"]["imported_by"] == str(root / "CLAUDE.md")
    assert by["CLAUDE.md"]["imports"] == [str(root / "docs" / "style.md"), str(root / "a.md")]
    (skipped,) = by["docs/h4.md"]["imports_skipped"]
    assert skipped["path"] == str(root / "docs" / "h5.md") and "4 imports deep" in skipped["reason"]
    assert by["b.md"]["imports"] == [str(root / "a.md")] and "imports_skipped" not in by["b.md"]


def test_import_paths_with_escaped_spaces_and_home(tmp_path, home):
    write(home / ".claude" / "CLAUDE.md", r"@my\ notes.md and @~/extra.md")
    write(home / ".claude" / "my notes.md", "spaced")
    write(home / "extra.md", "from home")
    out = ctx(formats=["claude-code"])
    assert [r["content"] for r in out["rules"][1:]] == ["spaced", "from home"]
    assert all(r["scope"] == "global" for r in out["rules"])


# ------------------------------------------------------------------ A3


def test_rules_with_paths_apply_only_to_matching_files(tmp_path, home):
    root = project(tmp_path)
    rules = root / ".claude" / "rules"
    write(rules / "c.md", '---\npaths: ["src/**/*.{c,h}"]\n---\nc rule')
    write(rules / "sub" / "lib.md", "---\npaths: src/**, lib/*.c\n---\nlib rule")
    write(rules / "all.md", "all rule")
    write(rules / "list.md", "---\npaths:\n  - 'docs/**'\n---\nlist rule")
    write(rules / "broken.md", "---\npaths: src/**\nno closing line")
    write(home / ".claude" / "rules" / "py.md", "---\npaths: '**/*.py'\n---\npy rule")

    def applied(path):
        out = ctx(path, formats=["claude-code"])
        return [os.path.basename(r["path"]) for r in out["rules"]], out.get("rules_not_applied", [])

    shown, skipped = applied(root / "src" / "a" / "x.h")
    assert shown == ["all.md", "broken.md", "c.md", "lib.md"]
    assert {os.path.basename(s["path"]): s["paths"] for s in skipped} == {
        "py.md": ["**/*.py"],
        "list.md": ["docs/**"],
    }
    shown, skipped = applied(root / "docs" / "a.md")
    assert shown == ["all.md", "broken.md", "list.md"]
    assert {os.path.basename(s["path"]) for s in skipped} == {"py.md", "c.md", "lib.md"}
    assert applied(root / "tools" / "x.py")[0] == ["py.md", "all.md", "broken.md"]
    assert "c rule" not in json.dumps(ctx(root / "docs", formats=["claude-code"]))


def test_rule_globs():
    assert workspace.rule_paths("---\npaths: [a/*.c, 'b/{x,y}/**']\n---\n") == ["a/*.c", "b/{x,y}/**"]
    assert workspace._braces("src/**/*.{c,h}") == ["src/**/*.c", "src/**/*.h"]
    assert workspace.rule_applies(["*.md"], "/p", "/p/README.md")
    assert not workspace.rule_applies(["*.md"], "/p", "/p/docs/x.md")


# ------------------------------------------------------------------ A4, A9


def test_codex_override_wins_in_each_folder(tmp_path, home, monkeypatch):
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    write(codex / "AGENTS.override.md", "global override")
    write(codex / "AGENTS.md", "global")
    root = project(tmp_path)
    write(tmp_path / "work" / "AGENTS.md", "above the project: Codex stops at the root")
    write(root / "AGENTS.override.md", "root override")
    write(root / "AGENTS.md", "root")
    write(root / "sub" / "AGENTS.md", "sub")
    write(root / "CLAUDE.md", "claude")
    write(home / ".claude" / "CLAUDE.md", "claude user")
    out = ctx(root / "sub", formats=["codex"])
    assert [(r["content"], r["scope"], r["format"]) for r in out["rules"]] == [
        ("global override", "global", "codex"),
        ("root override", "project", "codex"),
        ("sub", "project", "codex"),
    ]


# ------------------------------------------------------------------ A5


def test_both_formats_and_claude_codes_agents_md_fallback(tmp_path, home):
    root = project(tmp_path)
    write(root / "CLAUDE.md", "@AGENTS.md\nclaude extra")
    write(root / "AGENTS.md", "shared")
    both = ctx(root)
    assert [(r["content"], r["format"]) for r in both["rules"]] == [
        ("shared", "codex"),
        ("@AGENTS.md\nclaude extra", "claude-code"),
    ]
    assert both["rules"][1]["imports"] == [str(root / "AGENTS.md")]

    claude = ctx(root, formats=["claude-code"])
    assert [(os.path.basename(r["path"]), r.get("imported_by")) for r in claude["rules"]] == [
        ("CLAUDE.md", None),
        ("AGENTS.md", str(root / "CLAUDE.md")),
    ]
    only_agents = project(tmp_path, "other")
    write(only_agents / "AGENTS.md", "agents only")
    write(only_agents / "AGENTS.override.md", "Claude Code does not read overrides")
    out = ctx(only_agents, formats=["claude-code"])
    assert [(r["content"], r["format"]) for r in out["rules"]] == [("agents only", "claude-code")]


# ------------------------------------------------------------------ A6


def checkout(folder, url: str, when: float) -> None:
    write(
        folder / ".git" / "config",
        f'[core]\n\tbare = false\n[remote "origin"]\n\turl = {url}\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
        '[submodule "lib"]\n\turl = https://github.com/other/thing\n',
    )
    head = write(folder / ".git" / "HEAD", "ref: refs/heads/main\n")
    os.utime(head, (when, when))


def test_repo_finds_the_local_checkout(tmp_path, home, monkeypatch):
    a = tmp_path / "a" / "winhand"
    checkout(a, "https://github.com/konbakuyomu/winhand.git", 2_000_000_000)
    write(a / "AGENTS.md", "a rules")
    (a / "sub").mkdir()
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    write(codex / "config.toml", f'[projects.{json.dumps(str(a / "sub"))}]\ntrust_level = "trusted"\n')

    clones = tmp_path / "clones"
    old = clones / "winhand-old"
    checkout(old, "git@github.com:Konbakuyomu/winhand", 1_000_000_000)
    worktree = clones / "wt"
    gitdir = a / ".git" / "worktrees" / "wt"
    write(worktree / ".git", f"gitdir: {gitdir}\n")
    write(gitdir / "commondir", "../..\n")
    head = write(gitdir / "HEAD", "ref: refs/heads/topic\n")
    os.utime(head, (1_500_000_000, 1_500_000_000))
    via_claude = tmp_path / "c" / "winhand"
    checkout(via_claude, "ssh://git@github.com/konbakuyomu/winhand.git", 1_200_000_000)
    write(home / ".claude.json", json.dumps({"projects": {str(via_claude).replace("\\", "/"): {}}}))

    cfg = Config(context={"skills": [], "repo_roots": [str(clones)]})
    out = workspace.context(cfg, repo="Konbakuyomu/Winhand")
    assert out["path"] == str(a) and out["project_root"] == str(a)
    assert out["repo"] == {
        "name": "Konbakuyomu/Winhand",
        "checkout": str(a),
        "source": "codex",
        "other_checkouts": [str(worktree), str(via_claude), str(old)],
    }
    assert [r["content"] for r in out["rules"]] == ["a rules"]
    assert workspace.context(cfg, path="sub", repo="https://github.com/konbakuyomu/winhand")["path"] == str(
        a / "sub"
    )
    with pytest.raises(ValueError, match=r"repo_roots"):
        workspace.context(cfg, repo="nobody/none")
    with pytest.raises(ValueError, match="no checkout"):
        workspace.context(cfg, repo="other/thing")  # a submodule's URL is not the checkout's remote


async def test_repo_not_found_is_a_tool_error(home):
    async with Client(build_server(Config(context={"skills": []}))) as client:
        result = await client.call_tool("context", {"repo": "nobody/none"}, raise_on_error=False)
    assert result.is_error and "repo_roots" in result.content[0].text


def test_repo_keys():
    for url in (
        "konbakuyomu/winhand",
        "https://github.com/Konbakuyomu/winhand.git",
        "https://github.com/konbakuyomu/winhand/",
        "git@github.com:konbakuyomu/winhand.git",
        "ssh://git@github.com:22/konbakuyomu/winhand",
    ):
        assert workspace.repo_key(url) == "konbakuyomu/winhand", url
    assert workspace.repo_key("winhand") is None


# ------------------------------------------------------------------ A7, A8


async def test_many_skills_leave_the_rules_whole(tmp_path, home):
    root = project(tmp_path)
    text = "\n".join(f"rule line {i} " + "y" * 60 for i in range(270))
    write(root / "AGENTS.md", text)
    folder = tmp_path / "skills"
    for i in range(300):
        skill(folder, f"skill-{i:03d}", "d" * 320)
    cfg = Config(context={"rules": [], "skills": [str(folder)]})
    out = workspace.context(cfg, str(root))
    assert len(json.dumps(out, ensure_ascii=False)) <= workspace.BUDGET_CHARS
    assert out["rules"][0]["content"] == text and "truncated" not in out["rules"][0]
    assert len(json.dumps(out["skills"], ensure_ascii=False)) <= workspace.SKILLS_BUDGET_CHARS
    more = out["skills_more"]
    assert len(out["skills"]) + more["count"] == 300 and more["names"][0] == f"skill-{len(out['skills']):03d}"
    assert more["folders"] == [str(folder)]
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool("context", {"path": str(root)})
    assert sum(len(b.text) for b in result.content) <= workspace.BUDGET_CHARS


async def test_masked_text_still_fits(tmp_path, home):
    config.save(Config(redact={"values": ["abc123"]}))
    root = project(tmp_path)
    write(root / "AGENTS.md", "\n".join("key abc123 " * 3 + "z" * 22 for _ in range(1000)))
    async with Client(build_server(Config(context={"rules": [], "skills": []}))) as client:
        result = await client.call_tool("context", {"path": str(root)})
    out = result.structured_content
    assert "abc123" not in json.dumps(out) and out["redacted"] >= 1000
    assert "«redacted:" in out["rules"][0]["content"] and "fs_read(" in out["rules"][0]["truncated"]
    assert len(json.dumps(out, ensure_ascii=False)) <= workspace.BUDGET_CHARS
    assert sum(len(b.text) for b in result.content) <= workspace.BUDGET_CHARS


# ------------------------------------------------------------------ A10


def test_imports_out_of_the_project_need_claude_codes_approval(tmp_path, home, winhand_home):
    root = project(tmp_path)
    write(root / "CLAUDE.md", "@../outside/secret.md\n@~/.ssh/id_rsa\n@.env\n@docs/ok.md\n")
    write(tmp_path / "work" / "outside" / "secret.md", "outside secret")
    write(home / ".ssh" / "id_rsa", "PRIVATE KEY")
    write(root / ".env", "TOKEN=1")
    write(root / "docs" / "ok.md", "ok")
    write(
        guard.global_path(),
        '[[guard]]\non = "read"\npath = ["**/.env"]\ndecision = "deny"\nreason = "no env files"\n',
    )

    def read():
        out = ctx(root, formats=["claude-code"])
        first = out["rules"][0]
        skipped = {os.path.basename(s["path"]): s["reason"] for s in first.get("imports_skipped", [])}
        return [r.get("content") for r in out["rules"][1:]], skipped

    contents, skipped = read()
    assert contents == ["ok"]
    assert "outside the project" in skipped["secret.md"] and "outside the project" in skipped["id_rsa"]
    assert "guard" in skipped[".env"] and "no env files" in skipped[".env"]

    write(
        home / ".claude.json",
        json.dumps(
            {"projects": {str(root).replace("\\", "/"): {"hasClaudeMdExternalIncludesApproved": True}}}
        ),
    )
    contents, skipped = read()
    assert contents == ["outside secret", "PRIVATE KEY", "ok"] and list(skipped) == [".env"]

    (home / ".claude.json").unlink()
    out = ctx(root, formats=["claude-code"], external_imports=True)
    assert [r.get("content") for r in out["rules"][1:]] == ["outside secret", "PRIVATE KEY", "ok"]


def test_guarded_rule_files_and_skills_are_not_read(tmp_path, home, winhand_home):
    root = project(tmp_path)
    write(root / "CLAUDE.local.md", "private notes")
    write(root / "AGENTS.md", "shared")
    skill(root / ".agents" / "skills", "secret-tool", "should not be listed")
    skill(root / ".agents" / "skills", "fine", "listed")
    write(
        guard.global_path(),
        '[[guard]]\non = "read"\npath = ["**/CLAUDE.local.md", "**/secret-tool/**"]\ndecision = "ask"\n',
    )
    out = workspace.context(Config(context={"skills": []}), str(root))
    local = next(r for r in out["rules"] if r["path"].endswith("CLAUDE.local.md"))
    assert "content" not in local and "ask the person" in local["skipped"]
    assert [s["name"] for s in out["skills"]] == ["fine"]


# ------------------------------------------------------------------ A12 (new keys)


def test_new_context_settings_survive_saving(winhand_home):
    settings = {
        "formats": ["claude-code"],
        "repo_roots": ["D:\\code"],
        "external_imports": True,
        "rules": [],
    }
    config.save(Config(context=dict(settings)))
    assert config.load().context == settings


def test_a_long_rule_in_a_folder_with_braces_is_cut_cleanly(tmp_path, home):
    root = project(tmp_path, "odd {name}")
    write(root / "AGENTS.md", "\n".join("w" * 100 for _ in range(1000)))
    (rule,) = ctx(root, rules=[])["rules"]
    assert rule["truncated"].startswith("showing ") and "{name}" in rule["truncated"]


# ------------------------------------------------------------------ round 2: what the Verifier found


def symlink(link, target) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=os.path.isdir(target))
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not allowed here")


def test_many_files_stay_inside_the_budget(tmp_path, home):
    root = project(tmp_path)
    for i in range(200):
        write(root / "src" / "deep" / "folder" / f"file_{i:03d}.py", "\n".join(f"x = {n}" for n in range(50)))
    write(root / "CLAUDE.md", "\n".join(f"@src/deep/folder/file_{i:03d}.py" for i in range(200)))
    for i in range(500):
        write(root / ".claude" / "rules" / f"scoped_{i:03d}.md", f"---\npaths: lib/{i}/**\n---\nrule {i}")
    out = ctx(root, formats=["claude-code"])
    assert len(json.dumps(out, ensure_ascii=False)) <= workspace.BUDGET_CHARS
    claude = out["rules"][0]
    assert (
        len(claude["imports"]) == workspace.MAX_LISTED
        and claude["imports_more"] == 200 - workspace.MAX_LISTED
    )
    omitted = out["rules_omitted"]
    assert omitted["count"] + len(out["rules"]) == 201 and omitted["paths"]
    assert len(out["rules_not_applied"]) < 500 and "more are not listed" in out["rules_not_applied_note"]


def test_links_out_of_the_project_need_approval(tmp_path, home):
    root = project(tmp_path)
    secret = write(home / ".ssh" / "id_rsa", "PRIVATE KEY")
    other = write(home / "private.md", "PRIVATE NOTES")
    notes = home / "notes"
    write(notes / "n.md", "my notes")
    symlink(root / "CLAUDE.md", secret)
    symlink(root / "sub" / "AGENTS.md", other)
    symlink(root / ".claude" / "rules", notes)
    out = ctx(root / "sub")
    assert not any(s in json.dumps(out) for s in ("PRIVATE KEY", "PRIVATE NOTES", "my notes"))
    skipped = {os.path.relpath(r["path"], root): r["skipped"] for r in out["rules"]}
    assert set(skipped) == {"CLAUDE.md", os.path.join("sub", "AGENTS.md"), os.path.join(".claude", "rules")}
    assert all("outside the project" in why for why in skipped.values())

    approved = ctx(root / "sub", external_imports=True)
    assert [r.get("content") for r in approved["rules"]] == ["PRIVATE KEY", "my notes", "PRIVATE NOTES"]


def test_network_paths_are_not_touched(tmp_path, home, monkeypatch):
    root = project(tmp_path)
    write(root / "CLAUDE.md", "@//fileserver/share/rules.md")
    real_isfile = os.path.isfile

    def isfile(path):
        assert not str(path).replace("\\", "/").startswith("//"), f"looked at {path}"
        return real_isfile(path)

    monkeypatch.setattr(os.path, "isfile", isfile)
    out = ctx(root, formats=["claude-code"])
    (skipped,) = out["rules"][0]["imports_skipped"]
    assert "outside the project" in skipped["reason"]


def test_agents_md_fallback_ignores_the_users_own_claude_md(tmp_path, home, monkeypatch):
    write(home / ".claude" / "CLAUDE.md", "user")
    root = home / "code" / "repo"
    (root / ".git").mkdir(parents=True)
    write(root / "AGENTS.md", "agents")
    out = ctx(root, formats=["claude-code"])
    assert [(r["content"], r["scope"]) for r in out["rules"]] == [("user", "global"), ("agents", "project")]

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    out = ctx(root, formats=["claude-code"])
    assert [r["content"] for r in out["rules"]] == ["agents"], "~/.claude/CLAUDE.md is no folder's rule file"


def test_imports_of_agents_md_are_followed_with_both_formats(tmp_path, home):
    root = project(tmp_path)
    write(root / "CLAUDE.md", "@AGENTS.md @AGENTS.md @./AGENTS.md.")
    write(root / "AGENTS.md", "see @docs/arch.md")
    write(root / "docs" / "arch.md", "architecture")
    out = ctx(root)
    assert [(os.path.relpath(r["path"], root), r["format"]) for r in out["rules"]] == [
        ("AGENTS.md", "codex"),
        (os.path.join("docs", "arch.md"), "claude-code"),  # right after the file that imports it
        ("CLAUDE.md", "claude-code"),
    ]
    assert out["rules"][2]["imports"] == [str(root / "AGENTS.md")]


def test_repo_odds_and_ends(tmp_path, home, monkeypatch):
    assert workspace.repo_key("github-work:Org/Repo.git") == "org/repo"
    assert workspace.repo_key("C:\\code\\repo") == "code/repo"
    checkout(tmp_path / "clones" / "repo", "github-work:org/repo.git", 1_000_000_000)
    monkeypatch.setenv("MYDIR", str(tmp_path / "elsewhere"))
    cfg = Config(context={"skills": [], "repo_roots": [str(tmp_path / "clones")]})
    out = workspace.context(cfg, path="$MYDIR/x", repo="org/repo")
    assert out["path"] == str(tmp_path / "elsewhere" / "x")


def test_paths_with_comments():
    assert workspace.rule_paths("---\npaths: src/**  # only src\n---\n") == ["src/**"]
    assert workspace.rule_paths("---\npaths:\n  # sources\n  - 'src/**' # c\n\n  - lib/*\n---\n") == [
        "src/**",
        "lib/*",
    ]
    assert workspace.rule_paths("---\npaths: 'a#b/**'\n---\n") == ["a#b/**"]


def test_too_many_rule_files_are_named(tmp_path, home):
    root = project(tmp_path)
    for i in range(workspace.MAX_RULE_FILES + 5):
        write(root / ".claude" / "rules" / f"r{i:04d}.md", "")
    out = ctx(root, formats=["claude-code"])
    notes = [r for r in out["rules"] if r["path"] == str(root / ".claude" / "rules")]
    assert len(notes) == 1 and "more than" in notes[0]["skipped"]


# ------------------------------------------------------------------ round 3


def test_a_hostile_glob_is_quick(tmp_path, home):
    import time

    root = project(tmp_path)
    write(
        root / ".claude" / "rules" / "slow.md", "---\npaths: ['**/*a*a*a*a*a*a*a*a*a*a*a*a*a*a*a*b']\n---\nx"
    )
    write(root / ".claude" / "rules" / "deep.md", "---\npaths: '" + "{" * 3000 + "}" * 3000 + "'\n---\ny")
    began = time.monotonic()
    out = ctx(root / "src" / ("a" * 60), formats=["claude-code"])
    assert time.monotonic() - began < 5
    assert {os.path.basename(r["path"]) for r in out["rules_not_applied"]} == {"slow.md", "deep.md"}


def test_glob_match():
    g = workspace.glob_match
    assert g("src/a/x.h", "src/**/*.h") and g("src/x.h", "src/**/*.h") and not g("x.h", "src/**/*.h")
    assert g("README.md", "*.md") and not g("docs/x.md", "*.md") and g("docs/x.md", "**/*.md")
    assert g("a/b/c", "a/*/c") and not g("a/b/d/c", "a/*/c") and g("a/b/d/c", "a/**/c")
    assert g("file5.c", "file[0-9].c") and not g("fileA.c", "file[0-9].c") and g("x.c", "[!a]*.c")
    assert g("file1.c", "file?.c") and not g("file12.c", "file?.c")


def test_linked_rule_folders_follow_the_same_rules(tmp_path, home):
    root = project(tmp_path)
    write(root / "shared-rules" / "in.md", "inside the project")
    write(home / "team-rules" / "out.md", "outside the project")
    write(home / "dotfiles" / "mine.md", "user level")
    symlink(root / ".claude" / "rules" / "inside", root / "shared-rules")
    symlink(root / ".claude" / "rules" / "outside", home / "team-rules")
    symlink(home / ".claude" / "rules" / "dotfiles", home / "dotfiles")
    write(root / "docs" / "sub-rules.md", "moved to docs")
    symlink(root / "sub" / "CLAUDE.md", root / "docs" / "sub-rules.md")  # stays in the project

    out = ctx(root / "sub", formats=["claude-code"])
    contents = [r.get("content") for r in out["rules"]]
    assert "user level" in contents and "inside the project" in contents and "moved to docs" in contents
    assert "outside the project" not in contents
    (refused,) = [r for r in out["rules"] if "skipped" in r]
    assert refused["path"] == str(root / ".claude" / "rules" / "outside")

    approved = ctx(root / "sub", formats=["claude-code"], external_imports=True)
    assert "outside the project" in [r.get("content") for r in approved["rules"]]


def test_an_unapproved_import_of_a_global_file_does_not_open_its_imports(tmp_path, home, monkeypatch):
    codex = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex))
    write(codex / "AGENTS.md", "global, see @" + str(home / "private" / "notes.md"))
    write(home / "private" / "notes.md", "private notes")
    root = project(tmp_path)
    write(root / "CLAUDE.md", "@" + str(codex / "AGENTS.md"))
    out = ctx(root)
    assert "private notes" not in json.dumps(out)
    assert [r["path"] for r in out["rules"]] == [str(codex / "AGENTS.md"), str(root / "CLAUDE.md")]


def test_lists_stay_capped_after_masking(tmp_path, home, winhand_home):
    config.save(Config(redact={"values": ["ab"]}))
    root = project(tmp_path)
    write(root / "AGENTS.md", "the owner's rules")
    for i in range(400):
        write(root / ".claude" / "rules" / f"ab{i:03d}ab.md", f"---\npaths: ab/{i}/ab/**\n---\nab")
    rules = "".join(
        f'[[guard]]\non = "write"\npath = ["ab{i}/**"]\ndecision = "note"\nreason = "ab ab ab"\n\n'
        for i in range(1500)
    )
    write(root / ".winhand" / "guards.toml", rules)
    out = ctx(root)
    assert len(json.dumps(out, ensure_ascii=False)) <= workspace.BUDGET_CHARS
    assert out["rules"][0]["content"] == "the owner's rules"
    assert len(json.dumps(out["rules_not_applied"], ensure_ascii=False)) <= workspace.NOT_APPLIED_CHARS
    assert out["guards"]["rules_more"] > 0


def test_many_imports_are_quick(tmp_path, home):
    import time

    root = project(tmp_path)
    for i in range(1000):
        write(root / "r" / f"{i}.md", "")
    write(root / "CLAUDE.md", " ".join(f"@r/{i}.md" for i in range(1000)) + " @x" * 60_000)
    began = time.monotonic()
    ctx(root, formats=["claude-code"])
    assert time.monotonic() - began < 10


def test_many_checkouts_are_capped(tmp_path, home):
    for i in range(workspace.MAX_LISTED + 5):
        checkout(tmp_path / "clones" / f"c{i:02d}", "https://github.com/o/r.git", 1_000_000_000 + i)
    cfg = Config(context={"skills": [], "repo_roots": [str(tmp_path / "clones")]})
    repo = workspace.context(cfg, repo="o/r")["repo"]
    assert len(repo["other_checkouts"]) == workspace.MAX_LISTED and repo["other_checkouts_more"] == 4


async def test_file_names_that_are_not_utf8_do_not_break_the_answer(tmp_path, home):
    if os.name == "nt":
        pytest.skip("Windows file names are always Unicode")
    root = project(tmp_path)
    rules = root / ".claude" / "rules"
    rules.mkdir(parents=True)
    try:
        with open(os.path.join(os.fsencode(rules), b"bad-\xff.md"), "w") as fh:
            fh.write("odd name")
    except OSError:
        pytest.skip("this file system refuses such names")
    write(rules / "fine.md", "fine")
    async with Client(build_server(Config(context={"skills": [], "formats": ["claude-code"]}))) as client:
        result = await client.call_tool("context", {"path": str(root)})
    assert [r.get("content") for r in result.structured_content["rules"]] == ["fine"]


def test_network_paths_and_windows_link_targets():
    # Windows reports a local link target as \\?\C:\..., which must not count as a network share
    assert not workspace._network("\\\\?\\C:\\rules\\x.md") and not workspace._network("\\\\.\\C:\\x")
    assert workspace._network("\\\\?\\UNC\\server\\share\\x.md")
    assert workspace._network("\\\\server\\share\\x.md") and workspace._network("//server/share/x.md")
    assert not workspace._network("C:\\x") and not workspace._network("/home/x")
