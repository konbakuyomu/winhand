"""context(): what this machine's owner wrote down for agents working in a folder.

Rules are read the way the tools that own them read them ([context] formats picks which):
- codex: $CODEX_HOME/AGENTS.override.md or AGENTS.md, then one AGENTS.override.md or AGENTS.md per
  folder from the project root down to the folder (as codex-rs/core/src/agents_md.rs does);
- claude-code: the managed and user CLAUDE.md and ~/.claude/rules, then every CLAUDE.md,
  .claude/CLAUDE.md and CLAUDE.local.md from the filesystem root down, the project's .claude/rules
  (a rule with `paths` only when the folder matches), each with its @imports up to four hops; with
  only this format on, AGENTS.md stands in where there is no CLAUDE file, as in Claude Code.
A file reached twice (CLAUDE.md saying @AGENTS.md) is returned once.

Every file is checked against the owner's guard rules for reading first, and an @import from a
project file to a file outside the project is followed only when Claude Code approved external
imports for that project, or [context] external_imports is true: a cloned repository must not
be able to hand ~/.ssh to the model.

Skills are folders with a SKILL.md, from the user's skill folders ([context] skills) and the
project's .agents/skills and .claude/skills; the project's win on a name clash.

context(repo="owner/name") finds the local checkout of a repository from Codex's and Claude
Code's project lists and the folders in [context] repo_roots.

Files are read as they are now, so a rule edited a minute ago is what the next call returns.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

from . import config, fs, guard, redact
from .actions import Action
from .config import Config

DEFAULT_SKILLS = ["~/.agents/skills", "~/.claude/skills", "~/.codex/skills"]
PROJECT_SKILLS = [".agents/skills", ".claude/skills"]
FORMATS = ("codex", "claude-code")
CODEX_FILES = ("AGENTS.override.md", "AGENTS.md")
CLAUDE_FILES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md")
MAX_IMPORT_HOPS = 4
MAX_RULE_FILES = 500  # per rules folder
MAX_LISTED = 30  # imports and skipped imports listed per file
NOT_APPLIED_CHARS = 8_000
OMITTED_CHARS = 2_500
MAX_FILE_BYTES = 2_000_000
BUDGET_CHARS = fs.READ_MAX_CHARS
SKILLS_BUDGET_CHARS = 20_000
SKILLS_FULL_CHARS = 16_000  # full entries; names of the rest fill up to SKILLS_BUDGET_CHARS
DESCRIPTION_CHARS = 300


def _encoded(text: str) -> int:
    """Characters `text` takes inside a JSON string."""
    return len(json.dumps(text, ensure_ascii=False)) - 2


def _json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _setting(cfg: Config, key: str, default: list[str]) -> list[str]:
    value = (cfg.context or {}).get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


def _formats(cfg: Config) -> list[str]:
    wanted = [f.strip().lower() for f in _setting(cfg, "formats", list(FORMATS))]
    return [f for f in dict.fromkeys(wanted) if f in FORMATS]


def _key(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


# ------------------------------------------------------------------ where the tools keep things


def codex_home() -> str:
    return _expand(os.environ.get("CODEX_HOME") or "~/.codex")


def claude_home() -> str:
    return _expand(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def claude_state_file() -> str:
    """Claude Code's ~/.claude.json: per-project state, including the approval of external imports."""
    folder = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.join(_expand(folder), ".claude.json") if folder else _expand("~/.claude.json")


def managed_claude_md() -> str:
    if os.name == "nt":
        return os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "ClaudeCode", "CLAUDE.md")
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode/CLAUDE.md"
    return "/etc/claude-code/CLAUDE.md"


_json_cache: dict[str, tuple[float, dict]] = {}


def _claude_projects() -> dict:
    """The `projects` map of ~/.claude.json (keyed by folder), {} when it cannot be read."""
    path = claude_state_file()
    try:
        stamp = os.stat(path).st_mtime
        cached = _json_cache.get(path)
        if cached is None or cached[0] != stamp:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            cached = (stamp, data if isinstance(data, dict) else {})
            _json_cache.clear()
            _json_cache[path] = cached
    except (OSError, ValueError):
        return {}
    projects = cached[1].get("projects")
    return projects if isinstance(projects, dict) else {}


def _codex_projects() -> list[str]:
    try:
        data = tomllib.loads(Path(codex_home(), "config.toml").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    projects = data.get("projects")
    return [str(k) for k in projects] if isinstance(projects, dict) else []


def _folder_key(path: str) -> str:
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _external_imports_approved(root: str) -> bool:
    want = _folder_key(root)
    for folder, entry in _claude_projects().items():
        if _folder_key(str(folder)) == want and isinstance(entry, dict):
            return entry.get("hasClaudeMdExternalIncludesApproved") is True
    return False


# ------------------------------------------------------------------ folders


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


def _levels(start: str) -> list[str]:
    """Folders from the filesystem root down to start."""
    out = []
    d = Path(start)
    while True:
        out.append(str(d))
        if d == d.parent:
            return out[::-1]
        d = d.parent


# ------------------------------------------------------------------ front matter


def _front_matter(text: str) -> list[str] | None:
    """The lines between a leading `---` and the next one; None without a complete header."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for end in range(1, len(lines)):
        if lines[end].strip() == "---":
            return lines[1:end]
    return None


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _strip_comment(value: str) -> str:
    """A YAML value without its trailing `# comment` (a # outside quotes, after a space)."""
    quote = ""
    for i, ch in enumerate(value):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or value[i - 1] in " \t"):
            return value[:i].rstrip()
    return value


def _split_top(value: str) -> list[str]:
    """Split on commas that are outside quotes and braces."""
    parts, current, depth, quote = [], [], 0, ""
    for ch in value:
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(ch)
    parts.append("".join(current).strip())
    return [p for p in parts if p]


def _frontmatter(text: str) -> dict[str, str]:
    """name/description from a SKILL.md header (the few YAML forms skills use)."""
    lines = text.lstrip("\ufeff").splitlines()
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


def rule_paths(text: str) -> list[str] | None:
    """The `paths` of a Claude Code rule file: a YAML list or a comma-separated string. None when
    there are none, or the header is not complete (Claude Code then loads the rule always)."""
    header = _front_matter(text)
    if header is None:
        return None
    for i, line in enumerate(header):
        m = re.match(r"^paths\s*:\s*(.*)$", line)
        if not m:
            continue
        value = _strip_comment(m.group(1)).strip()
        if not value:
            items = []
            for item in header[i + 1 :]:
                if not item.strip() or item.strip().startswith("#"):
                    continue
                bullet = re.match(r"^\s*-\s*(.*)$", item)
                if not bullet:
                    break
                items.append(_unquote(_strip_comment(bullet.group(1))))
            found = items
        else:
            if value.startswith("[") and value.endswith("]"):
                value = value[1:-1]
            found = [_unquote(p) for p in _split_top(value)]
        return [p for p in found if p] or None
    return None


def _braces(pattern: str, limit: int = 1000) -> list[str]:
    """`src/*.{c,h}` -> ['src/*.c', 'src/*.h']."""
    start = pattern.find("{")
    if start < 0:
        return [pattern]
    depth = 0
    for end in range(start, len(pattern)):
        if pattern[end] == "{":
            depth += 1
        elif pattern[end] == "}":
            depth -= 1
            if depth == 0:
                break
    else:
        return [pattern]
    out: list[str] = []
    for part in _split_top(pattern[start + 1 : end]) or [""]:
        out += _braces(pattern[:start] + part + pattern[end + 1 :], limit)
        if len(out) >= limit:
            break
    return out[:limit]


def rule_applies(paths: list[str], root: str, target: str | None) -> bool:
    """Whether a path-scoped rule is in force for `target` (a file or folder) in the project at root."""
    if not target or not guard._under(target, root):
        return False
    rel = os.path.relpath(target, root).replace("\\", "/")
    if rel == ".":
        return False
    for glob in paths:
        for pattern in _braces(glob.replace("\\", "/").removeprefix("./").lstrip("/")):
            try:
                if pattern and guard._PurePath(rel).full_match(pattern):
                    return True
            except ValueError:
                continue
    return False


# ------------------------------------------------------------------ @imports

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_IMPORT = re.compile(r"(?<!\S)@((?:\\ |\S)+)")
_TRAILING = ".,;:!?)]}\"'"


def import_refs(text: str) -> list[str]:
    """The @path references in a CLAUDE.md, skipping code blocks and code spans."""
    refs: list[str] = []
    fence = ""
    for line in text.splitlines():
        m = _FENCE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = ""
            continue
        if m:
            fence = m.group(1)
            continue
        line = _CODE_SPAN.sub(lambda s: " " * len(s.group(0)), line)
        for ref in _IMPORT.finditer(line):
            value = ref.group(1).replace("\\ ", " ")
            if value not in refs:
                refs.append(value)
    return refs


def _import_paths(ref: str, folder: str) -> list[str]:
    """The paths a reference may name, relative to the importing file's folder (no file system access)."""
    out = []
    for candidate in dict.fromkeys((ref, ref.rstrip(_TRAILING))):
        if not candidate:
            continue
        path = os.path.expanduser(candidate) if candidate.startswith("~") else candidate
        out.append(os.path.abspath(path if os.path.isabs(path) else os.path.join(folder, path)))
    return out


def resolve_import(ref: str, folder: str) -> str | None:
    """The file a reference names, None if there is none."""
    return next((p for p in _import_paths(ref, folder) if os.path.isfile(p)), None)


def _format_of(path: str) -> str:
    name = os.path.basename(path)
    if name in ("CLAUDE.md", "CLAUDE.local.md") or os.path.basename(os.path.dirname(path)) == "rules":
        return "claude-code"
    return "codex"


# ------------------------------------------------------------------ collecting the rules


_OUTSIDE = (
    "outside the project, and Claude Code has not approved external imports for this project "
    "([context] external_imports = true in winhand's config.toml allows them)"
)
_LINKED_OUT = (
    "not read: it is a link to something outside the project, and links out of a project are followed "
    "only when Claude Code approved external imports for it ([context] external_imports = true allows them)"
)


def _network(path: str) -> bool:
    """A UNC path: looking at it would open a network connection."""
    return path.startswith(("\\\\", "//"))


class _Rules:
    """The rule files for one context() call, in the order they are returned."""

    def __init__(self, cfg: Config, root: str | None, target: str | None) -> None:
        self.cfg = cfg
        self.root = root
        self.target = target
        self.items: list[dict] = []
        self.not_applied: list[dict] = []
        self.texts: dict[int, str] = {}  # id(item) -> text
        self.by_key: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.expanded: set[str] = set()  # files whose @imports were followed
        self.guard_cache: dict = {}
        self._approved: bool | None = None

    def refused(self, path: str) -> str | None:
        """Why the owner's guard rules keep `path` from being read, None when they do not."""
        verdict = guard.judge([Action(kind="read", tool="context", paths=(path,))], self.guard_cache)
        if verdict.decision not in ("deny", "ask"):
            return None
        if verdict.broken:
            broken = verdict.broken[0]
            return f"not read: the guard rule file {broken.path} cannot be used ({broken.error})"
        rule = (verdict.deny or verdict.ask)[0][0]
        how = "do not allow reading it" if verdict.deny else "ask the person before reading it"
        return f"not read: the owner's guard rules {how} ({guard._where(rule)})" + (
            f": {rule.reason}" if rule.reason else ""
        )

    def external_ok(self) -> bool:
        if self._approved is None:
            self._approved = (self.cfg.context or {}).get("external_imports") is True or bool(
                self.root and _external_imports_approved(self.root)
            )
        return self._approved

    def _links_out(self, path: str, home: str | None) -> bool:
        """A project file or folder that is really somewhere outside the folder it is found in."""
        if home is None or self.external_ok():
            return False
        return not guard._under(os.path.realpath(path), os.path.realpath(home))

    def first(self, paths: list[str], fmt: str, scope: str, home: str | None = None) -> None:
        for path in paths:
            if os.path.isfile(path):
                self.file(path, fmt, scope, home=home)
                return

    def file(self, path: str, fmt: str, scope: str, text: str | None = None, home: str | None = None) -> None:
        """A rule file found in a known place; `home` is the folder of a project file (links out of it
        need approval)."""
        if not os.path.isfile(path) or _key(path) in self.seen:
            return
        why = (_LINKED_OUT if self._links_out(path, home) else None) or self.refused(path)
        if why:
            self.seen.add(_key(path))
            self.items.append({"path": path, "format": fmt, "scope": scope, "skipped": why})
            return
        self._read(path, fmt, scope, None, 0, text)

    def folder(self, folder: str, scope: str, home: str | None = None) -> None:
        """A Claude Code rules folder: every .md below it; one with `paths` only when it applies."""
        if not os.path.isdir(folder):
            return
        if self._links_out(folder, home):
            self.items.append(
                {"path": folder, "format": "claude-code", "scope": scope, "skipped": _LINKED_OUT}
            )
            return
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            found += [os.path.join(dirpath, f) for f in filenames if f.lower().endswith(".md")]
            if len(found) > MAX_RULE_FILES:
                break
        found.sort(key=lambda p: os.path.relpath(p, folder).replace("\\", "/").lower())
        if len(found) > MAX_RULE_FILES:
            self.items.append(
                {
                    "path": folder,
                    "format": "claude-code",
                    "scope": scope,
                    "skipped": f"more than {MAX_RULE_FILES} rule files here; only the first {MAX_RULE_FILES} "
                    "(by path) are looked at",
                }
            )
        for path in found[:MAX_RULE_FILES]:
            if _key(path) in self.seen:
                continue
            why = (_LINKED_OUT if self._links_out(path, home) else None) or self.refused(path)
            text = None
            if not why:
                try:
                    text = _read_file(path)
                except (OSError, ValueError, fs.FsError):
                    text = None  # file() reports the error
            paths = rule_paths(text) if text is not None else None
            if paths and not (self.root and rule_applies(paths, self.root, self.target)):
                self.not_applied.append(
                    {"path": path, "paths": paths, "format": "claude-code", "scope": scope}
                )
                continue
            self.file(path, "claude-code", scope, text, home=home)

    def _read(self, path: str, fmt: str, scope: str, imported_by: str | None, hops: int, text=None) -> None:
        self.seen.add(_key(path))
        item: dict = {"path": path, "format": fmt, "scope": scope}
        if imported_by:
            item["imported_by"] = imported_by
        self.items.append(item)
        self.by_key[_key(path)] = item
        try:
            text = _read_file(path) if text is None else text
        except (OSError, ValueError, fs.FsError) as exc:
            item["error"] = str(exc)
            return
        self.texts[id(item)] = text
        if fmt == "claude-code":
            self._expand(item, text, scope, hops)

    def _expand(self, item: dict, text: str, scope: str, hops: int) -> None:
        """Follow the @imports of a file Claude Code reads, as Claude Code does."""
        path = item["path"]
        self.expanded.add(_key(path))
        imports: list[str] = []
        skipped: list[dict] = []
        for ref in import_refs(text):
            candidates = _import_paths(ref, os.path.dirname(path))
            outside = scope == "project" and not self.external_ok()
            if outside and any(_network(c) for c in candidates):  # never touch a share on its say-so
                skipped.append({"import": ref, "path": candidates[0], "reason": _OUTSIDE})
                continue
            target = next((c for c in candidates if os.path.isfile(c)), None)
            if target is None:
                continue  # not a file: an @mention, not an import
            if _key(target) not in {_key(i) for i in imports}:
                imports.append(target)
            if _key(target) in self.seen:
                # read already, perhaps as an AGENTS.md for Codex: Claude Code still follows its imports
                seen_item = self.by_key.get(_key(target))
                seen_text = self.texts.get(id(seen_item)) if seen_item else None
                if (
                    seen_text is not None
                    and _key(target) not in self.expanded
                    and hops + 1 <= MAX_IMPORT_HOPS
                ):
                    self._expand(seen_item, seen_text, seen_item["scope"], hops + 1)
                continue
            if hops + 1 > MAX_IMPORT_HOPS:
                why = (
                    f"more than {MAX_IMPORT_HOPS} imports deep, so Claude Code does not load it either; "
                    "read it with fs_read if it matters"
                )
            elif outside and not self._inside(target):
                why = _OUTSIDE
            else:
                why = self.refused(target)
            if why:
                skipped.append({"import": ref, "path": target, "reason": why})
                continue
            self._read(target, "claude-code", scope, path, hops + 1)
        if imports:
            item["imports"] = imports[:MAX_LISTED]
            if len(imports) > MAX_LISTED:
                item["imports_more"] = len(imports) - MAX_LISTED
        if skipped:
            item["imports_skipped"] = skipped[:MAX_LISTED]
            if len(skipped) > MAX_LISTED:
                item["imports_skipped_more"] = len(skipped) - MAX_LISTED

    def _inside(self, path: str) -> bool:
        return bool(self.root) and guard._under(os.path.realpath(path), os.path.realpath(self.root))


def _read_file(path: str) -> str:
    if os.path.getsize(path) > MAX_FILE_BYTES:
        raise fs.FsError(
            f"{path} is larger than {MAX_FILE_BYTES:,} bytes; read the part you need with fs_read"
        )
    return fs.read_text(path)[0]


def _user_claude_md() -> set[str]:
    """The user's own CLAUDE.md: global, never a folder's .claude/CLAUDE.md."""
    return {_key(os.path.join(claude_home(), "CLAUDE.md")), _key(_expand("~/.claude/CLAUDE.md"))}


def _collect(cfg: Config, root: str | None, start: str | None, target: str | None) -> _Rules:
    rules = _Rules(cfg, root, target)
    formats = _formats(cfg)
    if "rules" in (cfg.context or {}):  # the owner's own list replaces the formats' global files
        for path in _setting(cfg, "rules", []):
            path = _expand(path)
            rules.file(path, _format_of(path), "global")
    else:
        for fmt in formats:
            if fmt == "codex":
                rules.first([os.path.join(codex_home(), n) for n in CODEX_FILES], "codex", "global")
            else:
                rules.file(managed_claude_md(), "claude-code", "managed")
                rules.file(os.path.join(claude_home(), "CLAUDE.md"), "claude-code", "global")
                rules.folder(os.path.join(claude_home(), "rules"), "global")
    if root is None or start is None:
        return rules
    project = {os.path.normcase(d) for d in _chain(root, start)}
    levels = _levels(start)
    user_files = _user_claude_md()

    def claude_files(folder: str) -> list[str]:
        paths = [os.path.join(folder, n) for n in CLAUDE_FILES]
        return [p for p in paths if _key(p) not in user_files]

    # with Claude Code's reading alone, AGENTS.md stands in when no CLAUDE file is in sight
    agents_for_claude = formats == ["claude-code"] and not any(
        os.path.isfile(p) for d in levels for p in claude_files(d)
    )
    for folder in levels:
        in_project = os.path.normcase(folder) in project
        for fmt in formats:
            if fmt == "codex":
                if in_project:
                    rules.first(
                        [os.path.join(folder, n) for n in CODEX_FILES], "codex", "project", home=folder
                    )
                continue
            for path in claude_files(folder):
                rules.file(path, "claude-code", "project", home=folder)
            if agents_for_claude and in_project:
                rules.file(os.path.join(folder, "AGENTS.md"), "claude-code", "project", home=folder)
            if os.path.normcase(folder) == os.path.normcase(root):
                rules.folder(os.path.join(root, ".claude", "rules"), "project", home=root)
    return rules


# ------------------------------------------------------------------ skills


def _skills_in(folder: str, scope: str, refused=None) -> list[dict]:
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return []
    out = []
    for entry in entries:
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        skill_md = os.path.join(entry.path, "SKILL.md")
        if not os.path.isfile(skill_md) or (refused and refused(skill_md)):
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


def skill_folders(cfg: Config, root: str | None) -> list[tuple[str, str]]:
    folders = [(os.path.join(root, p), "project") for p in PROJECT_SKILLS] if root else []
    return folders + [(_expand(p), "user") for p in _setting(cfg, "skills", DEFAULT_SKILLS)]


def skills(cfg: Config, root: str | None, refused=None) -> list[dict]:
    found: dict[str, dict] = {}
    for folder, scope in skill_folders(cfg, root):
        for skill in _skills_in(folder, scope, refused):
            found.setdefault(skill["name"].lower(), skill)
    return sorted(found.values(), key=lambda s: (s["scope"] != "project", s["name"].lower()))


# ------------------------------------------------------------------ repo -> local checkout


def repo_key(text: str) -> str | None:
    """'owner/name' (lower case) of a repository name or any git remote URL."""
    value = text.strip().rstrip("/")
    value = value.removesuffix(".git").rstrip("/")
    # git@github.com:owner/name, or a host alias from ~/.ssh/config: github-work:owner/name
    scp = re.match(r"^(?:[\w.+-]+@)?[\w.-]{2,}:(?!//|\\)(.+)$", value)
    if scp:
        value = scp.group(1)
    else:
        value = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://[^/]*", "", value)
    parts = [p for p in re.split(r"[/\\]", value) if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[-2:]).lower()


def _git_dirs(root: str) -> tuple[str | None, str | None]:
    """(git dir, common git dir) of a checkout; a worktree's .git file points at both."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot, dot
    try:
        first = Path(dot).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, None
    if not first.startswith("gitdir:"):
        return None, None
    gitdir = first[len("gitdir:") :].strip()
    gitdir = gitdir if os.path.isabs(gitdir) else os.path.normpath(os.path.join(root, gitdir))
    common = gitdir
    try:
        rel = Path(gitdir, "commondir").read_text(encoding="utf-8").strip()
        common = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(gitdir, rel))
    except OSError:
        pass
    return gitdir, common


def _remote_urls(common: str) -> list[str]:
    try:
        text = Path(common, "config").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    urls, in_remote = [], False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_remote = line.lower().startswith("[remote ")
            continue
        m = re.match(r"^url\s*=\s*(.+)$", line, re.IGNORECASE)
        if in_remote and m:
            urls.append(_unquote(m.group(1)))
    return urls


def _activity(gitdir: str, common: str) -> float:
    times = []
    for path in (
        os.path.join(gitdir, "index"),
        os.path.join(gitdir, "HEAD"),
        os.path.join(common, "FETCH_HEAD"),
    ):
        try:
            times.append(os.path.getmtime(path))
        except OSError:
            continue
    return max(times, default=0.0)


def _under_roots(cfg: Config) -> list[str]:
    out = []
    for folder in _setting(cfg, "repo_roots", []):
        folder = _expand(folder)
        out.append(folder)
        try:
            out += [e.path for e in sorted(os.scandir(folder), key=lambda e: e.name.lower()) if e.is_dir()]
        except OSError:
            continue
    return out


def find_repo(cfg: Config, repo: str) -> dict:
    """The local checkout of `repo` (owner/name or a remote URL); ValueError when there is none."""
    want = repo_key(repo)
    if not want:
        raise ValueError(f"repo should be owner/name or a git remote URL, not {repo!r}")
    sources = [
        ("codex", _codex_projects()),
        ("claude-code", [str(k) for k in _claude_projects()]),
        ("repo_roots", _under_roots(cfg)),
    ]
    found: dict[str, tuple[str, str, float]] = {}
    checked: set[str] = set()
    for source, folders in sources:
        for folder in folders:
            folder = folder[4:] if folder.startswith("\\\\?\\") else folder
            if not os.path.isdir(folder):
                continue
            root = project_root(os.path.abspath(folder))
            key = os.path.normcase(os.path.realpath(root))
            if key in checked:
                continue
            checked.add(key)
            gitdir, common = _git_dirs(root)
            if not gitdir or not common:
                continue
            if want in {repo_key(url) for url in _remote_urls(common)}:
                found[key] = (root, source, _activity(gitdir, common))
    if not found:
        counts = ", ".join(f"{len(folders)} from {name}" for name, folders in sources)
        raise ValueError(
            f"no checkout of {repo} found on this machine (folders looked at: {counts}; Codex's list is "
            f"[projects] in {os.path.join(codex_home(), 'config.toml')}, Claude Code's is projects in "
            f"{claude_state_file()}). Add the folder that holds your clones to [context] repo_roots in "
            f"{config.config_path()} (for example repo_roots = ['D:\\code']), or call context with path= instead."
        )
    ordered = sorted(found.values(), key=lambda f: f[2], reverse=True)
    return {
        "name": repo,
        "checkout": ordered[0][0],
        "source": ordered[0][1],
        "other_checkouts": [f[0] for f in ordered[1:]],
    }


# ------------------------------------------------------------------ the result


def _truncated(path: str, shown: int, total: int) -> str:
    return f"showing {shown} of {total} lines; read the rest with fs_read(path={path!r}, offset={shown + 1})"


def context(cfg: Config, path: str | None = None, repo: str | None = None) -> dict:
    repo_info = find_repo(cfg, repo) if repo else None
    if repo_info:
        if not path:
            path = repo_info["checkout"]
        else:
            path = os.path.expandvars(os.path.expanduser(path))
            if not os.path.isabs(path):
                path = os.path.join(repo_info["checkout"], path)
    start = root = target = None
    if path:
        target = _expand(path)
        start = guard._start_dir(target)
        root = project_root(start)
    rules = _collect(cfg, root, start, target)

    redactor = redact.current()
    masked = 0

    def mask(value):
        nonlocal masked
        if redactor.empty:
            return value
        value, n = redactor.data(value)
        masked += n
        return value

    out: dict = {}
    if path:
        out["path"] = target
        out["project_root"] = root
    if repo_info:
        out["repo"] = repo_info
    out["formats"] = _formats(cfg)
    out["rules"] = []
    if rules.not_applied:
        listed, used = [], 0
        for entry in rules.not_applied:
            cost = _json_len(entry) + 1
            if used + cost > NOT_APPLIED_CHARS:
                break
            listed.append(entry)
            used += cost
        out["rules_not_applied"] = listed
        out["rules_not_applied_note"] = (
            "these Claude Code rules apply only to the paths they list: call context(path=<the file>) "
            "before working on a matching file"
        ) + (
            f"; {len(rules.not_applied) - len(listed)} more are not listed"
            if len(listed) < len(rules.not_applied)
            else ""
        )

    every_skill = mask(skills(cfg, root, rules.refused))
    shown, used = [], 2
    for skill in every_skill:
        cost = _json_len(skill) + 1
        if used + cost > SKILLS_FULL_CHARS:
            break
        shown.append(skill)
        used += cost
    out["skills"] = shown
    rest = every_skill[len(shown) :]
    if rest:
        names = []
        for skill in rest:
            cost = _encoded(skill["name"]) + 3
            if used + cost > SKILLS_BUDGET_CHARS:
                break
            names.append(skill["name"])
            used += cost
        out["skills_more"] = {
            "count": len(rest),
            "names": names,
            "note": (
                f"{len(rest)} more skills only by name"
                + (f" ({len(rest) - len(names)} not even named)" if len(names) < len(rest) else "")
                + ": each is <folder>/<name>/SKILL.md in one of these folders; fs_list or fs_find them"
            ),
            "folders": mask([f for f, _ in skill_folders(cfg, root) if os.path.isdir(f)]),
        }
    out["skills_note"] = (
        "When a skill fits the task, read its SKILL.md with fs_read and follow it; run the scripts it "
        "names on this machine with run."
    )
    out["guards"] = mask(guard.rules_for(start))
    out["local_mcp_servers"] = {
        "names": mask([s.name for s in cfg.servers if s.enabled]),
        "note": "each is its own MCP endpoint at <relay>/mcp/<name>, added as a separate connector",
    }
    out["next"] = (
        "Follow these rules while you work here. Call context again for another folder, and after the "
        "conversation is compacted."
        if path
        else "Pass path=<the folder you work in>, or repo=<owner/name> when you only know the repository, "
        "to also get that project's rules, skills and guard rules."
    )
    for key in ("path", "project_root", "repo", "rules_not_applied"):
        if key in out:
            out[key] = mask(out[key])

    # the budget is what the masked result will be once it is JSON: backslashes, quotes and line
    # breaks take two characters there; 300 more for the redaction count and note, and room to
    # name the files that do not fit at all
    budget = BUDGET_CHARS - _json_len(out) - 300 - OMITTED_CHARS
    omitted: list[str] = []
    for item in rules.items:
        text = rules.texts.get(id(item))
        item = mask(item)
        if text is None:
            cost = _json_len(item) + 1
            if cost > budget:
                omitted.append(item["path"])
                continue
            out["rules"].append(item)
            budget -= cost
            continue
        text = mask(text)
        lines = text.splitlines()
        item["lines"] = len(lines)
        cost = _json_len(item) + len(', "content": ""') + 1
        if cost + _encoded(text) <= budget:
            item["content"] = text
            budget -= cost + _encoded(text)
            out["rules"].append(item)
            continue
        cost += _encoded(_truncated(item["path"], len(lines), len(lines))) + len(', "truncated": ""')
        if cost > budget:
            omitted.append(item["path"])
            continue
        shown_lines, spent = [], 0
        for line in lines:
            line_cost = _encoded(line) + 2
            if cost + spent + line_cost > budget:
                break
            shown_lines.append(line)
            spent += line_cost
        if shown_lines:
            item["content"] = "\n".join(shown_lines)
        item["truncated"] = _truncated(item["path"], len(shown_lines), len(lines))
        budget -= cost + spent
        out["rules"].append(item)
    if omitted:
        names, used = [], 0
        for name in omitted:
            if used + _encoded(name) + 3 > OMITTED_CHARS - 300:
                break
            names.append(name)
            used += _encoded(name) + 3
        out["rules_omitted"] = {
            "count": len(omitted),
            "paths": names,
            "note": "these rule files did not fit in this answer; read them with fs_read"
            + (f" ({len(omitted) - len(names)} not even named)" if len(names) < len(omitted) else ""),
        }
    if masked:
        out["redacted"] = masked
        out["redaction_note"] = redact.NOTE
    return out
