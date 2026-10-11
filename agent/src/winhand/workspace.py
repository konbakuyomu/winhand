"""context(): what this machine's owner wrote down for agents working in a folder.

- rules: the global rule files ([context] rules; by default ~/.codex/AGENTS.md and
  ~/.claude/CLAUDE.md when they exist) and every AGENTS.md from the project root down to the folder;
- skills: folders with a SKILL.md, from the user's skill folders ([context] skills) and the
  project's .agents/skills and .claude/skills; the project's win on a name clash;
- the guard rules in force there (guard.py) and the local MCP servers.

Files are read as they are now, so a rule edited a minute ago is what the next call returns.
Client-specific meaning (CLAUDE.md taking precedence, @imports ...) is not applied here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import fs, guard
from .config import Config

DEFAULT_RULES = ["~/.codex/AGENTS.md", "~/.claude/CLAUDE.md"]
DEFAULT_SKILLS = ["~/.agents/skills", "~/.claude/skills", "~/.codex/skills"]
PROJECT_SKILLS = [".agents/skills", ".claude/skills"]
RULE_FILE = "AGENTS.md"
BUDGET_CHARS = fs.READ_MAX_CHARS
DESCRIPTION_CHARS = 300


def _encoded(text: str) -> int:
    """Characters `text` takes inside a JSON string."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _setting(cfg: Config, key: str, default: list[str]) -> list[str]:
    value = (cfg.context or {}).get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


def project_root(start: str) -> str:
    """The nearest folder above `start` that holds .git (a folder, or a file in a worktree);
    `start` itself when there is none."""
    d = Path(start)
    while True:
        if (d / ".git").exists():
            return str(d)
        if d == d.parent:
            return start
        d = d.parent


def _chain(root: str, start: str) -> list[str]:
    """Folders from root down to start."""
    out = [start]
    d = Path(start)
    while str(d) != root and d != d.parent:
        d = d.parent
        out.append(str(d))
    return out[::-1] if out[-1] == root else [start]


def _frontmatter(text: str) -> dict[str, str]:
    """name/description from a SKILL.md header (the few YAML forms skills use)."""
    lines = text.lstrip("﻿").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        m = re.match(r"^(name|description)\s*:\s*(.*)$", lines[i])
        i += 1
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value in ("", "|", ">", "|-", ">-", "|+", ">+"):
            block = []
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                if lines[i].strip() == "---":
                    break
                block.append(lines[i].strip())
                i += 1
            value = " ".join(b for b in block if b)
        elif value[:1] == "'" and value.endswith("'") and len(value) > 1:
            value = value[1:-1].replace("''", "'")
        elif value[:1] == '"' and value.endswith('"') and len(value) > 1:
            try:
                value = json.loads(value)
            except ValueError:
                value = value[1:-1]
        out[key] = value
    return out


def _skills_in(folder: str, scope: str) -> list[dict]:
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return []
    out = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill_md = os.path.join(entry.path, "SKILL.md")
        if not os.path.isfile(skill_md):
            continue
        try:
            head, _ = fs.read_text(skill_md)
        except (OSError, ValueError, fs.FsError):
            head = ""
        meta = _frontmatter(head[:20_000])
        description = " ".join(meta.get("description", "").split())
        if len(description) > DESCRIPTION_CHARS:
            description = description[: DESCRIPTION_CHARS - 1] + "…"
        out.append(
            {
                "name": meta.get("name") or entry.name,
                "description": description,
                "path": skill_md,
                "scope": scope,
            }
        )
    return out


def skills(cfg: Config, root: str | None) -> list[dict]:
    found: dict[str, dict] = {}
    folders = [(os.path.join(root, p), "project") for p in PROJECT_SKILLS] if root else []
    folders += [(_expand(p), "user") for p in _setting(cfg, "skills", DEFAULT_SKILLS)]
    for folder, scope in folders:
        for skill in _skills_in(folder, scope):
            found.setdefault(skill["name"].lower(), skill)
    return sorted(found.values(), key=lambda s: (s["scope"] != "project", s["name"].lower()))


def _rule_files(cfg: Config, root: str | None, start: str | None) -> list[tuple[str, str]]:
    files = [(_expand(p), "global") for p in _setting(cfg, "rules", DEFAULT_RULES)]
    if root and start:
        files += [(os.path.join(d, RULE_FILE), "project") for d in _chain(root, start)]
    seen: set[str] = set()
    out = []
    for path, scope in files:
        key = os.path.normcase(path)
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            out.append((path, scope))
    return out


def context(cfg: Config, path: str | None = None) -> dict:
    start = root = None
    if path:
        start = guard._start_dir(_expand(path))
        root = project_root(start)
    out: dict = {}
    if path:
        out["path"] = _expand(path)
        out["project_root"] = root
    rules: list[dict] = []
    out["rules"] = rules
    out["skills"] = skills(cfg, root)
    out["skills_note"] = (
        "When a skill fits the task, read its SKILL.md with fs_read and follow it; run the scripts it "
        "names on this machine with run."
    )
    out["guards"] = guard.rules_for(start)
    out["local_mcp_servers"] = {
        "names": [s.name for s in cfg.servers if s.enabled],
        "note": "each is its own MCP endpoint at <relay>/mcp/<name>, added as a separate connector",
    }
    out["next"] = (
        "Follow these rules while you work here. Call context again for another folder, and after the "
        "conversation is compacted."
        if path
        else "Pass path=<the folder you work in> to also get that project's AGENTS.md, skills and guard rules."
    )

    # the budget is what the result will be once it is JSON: backslashes, quotes and line
    # breaks take two characters there
    budget = BUDGET_CHARS - len(json.dumps(out, ensure_ascii=False))
    for file, scope in _rule_files(cfg, root, start):
        try:
            text, _ = fs.read_text(file)
        except (OSError, ValueError, fs.FsError) as exc:
            rules.append({"path": file, "scope": scope, "error": str(exc)})
            continue
        lines = text.splitlines()
        item: dict = {"path": file, "scope": scope, "lines": len(lines)}
        if _encoded(text) + 200 <= budget:
            item["content"] = text
            budget -= _encoded(text) + 200
        else:
            shown, used = [], 0
            for line in lines:
                cost = _encoded(line) + 2
                if used + cost > budget - 400:
                    break
                shown.append(line)
                used += cost
            if shown:
                item["content"] = "\n".join(shown)
            item["truncated"] = (
                f"showing {len(shown)} of {len(lines)} lines; read the rest with "
                f"fs_read(path={file!r}, offset={len(shown) + 1})"
            )
            budget = max(0, budget - used - 400)
        rules.append(item)
    return out
