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

import difflib
import json
import os
import re
import stat
import sys
import time
import tomllib
from pathlib import Path

from . import config, fs, guard, redact
from .actions import Action
from .config import Config
from .session.text import fitting, width

DEFAULT_SKILLS = ["~/.agents/skills", "~/.claude/skills", "~/.codex/skills"]
PROJECT_SKILLS = [".agents/skills", ".claude/skills"]
FORMATS = ("codex", "claude-code")
CODEX_FILES = ("AGENTS.override.md", "AGENTS.md")
CLAUDE_FILES = ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md")
MAX_IMPORT_HOPS = 4
MAX_RULE_FILES = 500  # per rules folder
MAX_LISTED = 30  # imports and skipped imports listed per file
NOT_APPLIED_CHARS = 8_000
GUARDS_CHARS = 8_000
GUARD_ERRORS_CHARS = 3_000
ERROR_CHARS = 500
OMITTED_CHARS = 2_500
MAX_FILE_BYTES = 2_000_000
BUDGET_CHARS = 60_000  # the rules are the owner's and are kept whole up to this
SKILLS_BUDGET_CHARS = 20_000
SKILLS_FULL_CHARS = 16_000  # full entries; names of the rest fill up to SKILLS_BUDGET_CHARS
DESCRIPTION_CHARS = 300
SKILL_DESC_LIMIT = 1_536  # Claude Code's cut of description + when_to_use


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


def _split_top(value: str, escapes: bool = False) -> list[str]:
    """Split on commas that are outside quotes and braces (and, with escapes, not after a backslash)."""
    parts, current, depth, quote = [], [], 0, ""
    escaped = False
    for ch in value:
        if escaped:
            escaped = False
        elif escapes and ch == "\\":
            escaped = True
        elif quote:
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
    if escapes:  # a {} group: an empty alternative means "nothing here"
        return parts
    return [p for p in parts if p]


def _frontmatter(text: str) -> dict[str, str]:
    """name/description from a SKILL.md header (the few YAML forms skills use)."""
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    out: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        m = re.match(r"^(name|description|when_to_use|disable-model-invocation)\s*:\s*(.*)$", lines[i])
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


MAX_GLOB_CHARS = 1_000
MAX_RULE_PATTERNS = 1_000  # Claude Code's limit per rule file
GLOB_BUDGET = 20_000  # patterns tried in one context() call
RULES_TIME_S = 10.0  # reading rule files stops after this; the answer says so
CHECKOUTS_CHARS = 4_000


def _braces(pattern: str, limit: int = 1000, depth: int = 0) -> list[str]:
    """`src/*.{c,h}` -> ['src/*.c', 'src/*.h']; `\\{`, `\\}` and `\\,` are literal."""
    start = _unescaped(pattern, "{")
    if start < 0 or depth >= 16:
        return [pattern]
    level = 0
    i = start
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            level += 1
        elif ch == "}":
            level -= 1
            if level == 0:
                break
        i += 1
    else:
        return [pattern]
    end = i
    out: list[str] = []
    for part in _split_top(pattern[start + 1 : end], escapes=True) or [""]:
        out += _braces(pattern[:start] + part + pattern[end + 1 :], limit, depth + 1)
        if len(out) >= limit:
            break
    return out[:limit]


def _unescaped(text: str, char: str) -> int:
    i = 0
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == char:
            return i
        i += 1
    return -1


def _segment_tokens(segment: str) -> list:
    """`*`, `?`, `[a-z]` / `[!x]` and literal characters (`\\x` escapes one) of one path segment."""
    tokens: list = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            if not tokens or tokens[-1] != "*":
                tokens.append("*")
            i += 1
        elif ch == "?":
            tokens.append("?")
            i += 1
        elif ch == "[" and segment.find("]", i + 2) > 0:
            end = segment.find("]", i + 2)
            body = segment[i + 1 : end]
            negate = body[:1] in ("!", "^")
            tokens.append(("class", negate, body[1:] if negate else body))
            i = end + 1
        elif ch == "\\" and i + 1 < len(segment):
            tokens.append(("lit", segment[i + 1]))
            i += 2
        else:
            tokens.append(("lit", ch))
            i += 1
    return tokens


def _in_class(body: str, ch: str) -> bool:
    i = 0
    while i < len(body):
        if i + 2 < len(body) and body[i + 1] == "-":
            if body[i] <= ch <= body[i + 2]:
                return True
            i += 3
        else:
            if body[i] == ch:
                return True
            i += 1
    return False


def _one(token, ch: str) -> bool:
    if token == "?":
        return True
    if token[0] == "lit":
        return token[1] == ch
    return _in_class(token[2], ch) != token[1]


def _wild(items, tokens, one, star) -> bool:
    """Wildcard matching where `star` takes any run of items and every other token exactly one:
    greedy, going back only to the last star, so it takes len(items) * len(tokens) steps at most."""
    i = t = 0
    star_at, mark = -1, 0
    while i < len(items):
        if t < len(tokens) and tokens[t] != star and one(tokens[t], items[i]):
            i += 1
            t += 1
        elif t < len(tokens) and tokens[t] == star:
            star_at, mark = t, i
            t += 1
        elif star_at >= 0:
            t = star_at + 1
            mark += 1
            i = mark
        else:
            return False
    while t < len(tokens) and tokens[t] == star:
        t += 1
    return t == len(tokens)


def glob_match(rel: str, pattern: str) -> bool:
    """Whether a /-separated relative path matches a glob with `**`, `*`, `?` and `[...]`
    (case-insensitive on Windows). No regular expressions: a repository's pattern cannot make it slow."""
    if os.name == "nt":
        rel, pattern = rel.lower(), pattern.lower()
    segments: list = []
    for part in pattern.split("/"):
        if part == "**":
            if not segments or segments[-1] != "**":
                segments.append("**")
        else:
            segments.append(_segment_tokens(part))
    return _wild(rel.split("/"), segments, lambda tokens, seg: _wild(seg, tokens, _one, "*"), "**")


def rule_applies(
    paths: list[str], root: str, target: str | None, budget: list[int] | None = None, late=None
) -> bool | None:
    """Whether a path-scoped rule is in force for `target` (a file or folder) in the project at root.
    A rule's globs expand to at most MAX_RULE_PATTERNS patterns (as in Claude Code); `budget` is what
    is left for the whole call, and None comes back when it ran out before an answer."""
    if not target or not guard._under(target, root):
        return False
    rel = os.path.relpath(target, root).replace("\\", "/")
    if rel == ".":
        return False
    left = MAX_RULE_PATTERNS
    for glob in paths:
        if len(glob) > MAX_GLOB_CHARS:
            continue  # nobody writes such a path; do not spend time on it
        if late is not None and late():
            return None
        for pattern in _braces(glob.removeprefix("./").lstrip("/"), left):
            if late is not None and late():
                return None
            if budget is not None:
                if budget[0] <= 0:
                    return None
                budget[0] -= 1
            if pattern and glob_match(rel, pattern):
                return True
            left -= 1
            if left <= 0:
                return False
    return False


# ------------------------------------------------------------------ @imports

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_IMPORT = re.compile(r"(?<!\S)@((?:\\ |\S)+)")
_TRAILING = ".,;:!?)]}\"'"


def import_refs(text: str) -> list[str]:
    """The @path references in a CLAUDE.md, skipping code blocks and code spans."""
    refs: list[str] = []
    known: set[str] = set()
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
            if value not in known:
                known.add(value)
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
    """A UNC path: looking at it would open a network connection. Windows writes local link targets
    as \\\\?\\C:\\..., which is not one; \\\\?\\UNC\\server\\share is."""
    if path.startswith(("\\\\?\\", "\\\\.\\", "//?/", "//./")):
        # only a drive letter is surely local; UNC\\, GLOBALROOT\\Device\\Mup\\ ... may be a share
        return not re.match(r"[A-Za-z]:([\\\\/]|$)", path[4:])
    return path.startswith(("\\\\", "//"))


class _Rules:
    """The rule files for one context() call. Files imported by a file follow it (children)."""

    def __init__(self, cfg: Config, root: str | None, start: str | None, target: str | None) -> None:
        self.cfg = cfg
        self.root = root
        self.start = start
        self.target = target
        self.items: list[dict] = []
        self.children: dict[int, list[dict]] = {}
        self.not_applied: list[dict] = []
        self.texts: dict[int, str] = {}  # id(item) -> text
        self.by_key: dict[str, dict] = {}
        self.seen: set[str] = set()
        self.expanded: set[str] = set()  # files whose @imports were followed
        self.guard_cache: dict = {}
        self.glob_budget = [GLOB_BUDGET]
        self.deadline = time.monotonic() + RULES_TIME_S
        self.stopped = False
        self._approved: bool | None = None

    def late(self) -> bool:
        """Past the time limit: whatever a repository put in its files, context() answers."""
        if not self.stopped and time.monotonic() > self.deadline:
            self.stopped = True
        return self.stopped

    def ordered(self) -> list[dict]:
        out: list[dict] = []
        stack = list(reversed(self.items))
        while stack:
            item = stack.pop()
            out.append(item)
            stack.extend(reversed(self.children.get(id(item), [])))
        return out

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
            folders = _chain(self.root, self.start) if self.root and self.start else []
            self._approved = (self.cfg.context or {}).get(
                "external_imports"
            ) is True or _external_imports_approved(folders)
        return self._approved

    def _links_out(self, path: str, home: str | None) -> bool:
        """A project file or folder that is really somewhere outside `home` (the project root, or for a
        folder above the project, that folder)."""
        if home is None or self.external_ok():
            return False
        return not guard._under(os.path.realpath(path), os.path.realpath(home))

    def _network_link(self, path: str, home: str | None) -> bool:
        """A project file that is a link to a network share: refused without looking at the share."""
        if home is None or self.external_ok():
            return False
        try:
            target = os.readlink(path)
        except (OSError, ValueError):
            return False
        return _network(target)

    def _skip(self, path: str, fmt: str, scope: str, why: str, key: str | None = None) -> None:
        self.seen.add(key or _key(path))
        self.items.append({"path": path, "format": fmt, "scope": scope, "skipped": why})

    def first(self, paths: list[str], fmt: str, scope: str, home: str | None = None) -> None:
        for path in paths:
            if self._network_link(path, home) or os.path.isfile(path):
                self.file(path, fmt, scope, home=home)
                return

    def file(self, path: str, fmt: str, scope: str, text: str | None = None, home: str | None = None) -> None:
        """A rule file found in a known place; `home` limits where a project file may really be."""
        if self.late():
            return
        if self._network_link(path, home):
            key = os.path.normcase(os.path.abspath(path))
            if key not in self.seen:
                self._skip(path, fmt, scope, _LINKED_OUT, key)
            return
        if not os.path.isfile(path) or _key(path) in self.seen:
            return
        why = (_LINKED_OUT if self._links_out(path, home) else None) or self.refused(path)
        if why:
            self._skip(path, fmt, scope, why)
            return
        self._read(path, fmt, scope, None, 0, text)

    def _rule_files(self, folder: str, scope: str, home: str | None) -> tuple[list[str], bool]:
        """The .md files below a rules folder, following linked subfolders the way linked files are
        followed; (files, whether there were more than MAX_RULE_FILES)."""
        found: list[str] = []
        visited: set[str] = set()
        stack = [folder]
        while stack and not self.late():
            current = stack.pop()
            if _key(current) in visited:
                continue
            visited.add(_key(current))
            try:
                entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
            except OSError:
                continue
            subfolders = []
            for entry in entries:
                if entry.name.startswith(".") or not _plain_text(entry.name):
                    continue
                try:
                    if entry.is_symlink() and (
                        self._network_link(entry.path, home) or self._links_out(entry.path, home)
                    ):
                        self._skip(entry.path, "claude-code", scope, _LINKED_OUT)
                        continue
                    if entry.is_dir():
                        subfolders.append(entry.path)
                    elif entry.name.lower().endswith(".md") and entry.is_file():
                        found.append(entry.path)
                except OSError:
                    continue
                if len(found) > MAX_RULE_FILES:
                    return found, True
            stack.extend(reversed(subfolders))
        return found, False

    def folder(self, folder: str, scope: str, home: str | None = None) -> None:
        """A Claude Code rules folder: every .md below it; one with `paths` only when it applies."""
        if self._network_link(folder, home):
            self._skip(folder, "claude-code", scope, _LINKED_OUT, os.path.normcase(os.path.abspath(folder)))
            return
        if not os.path.isdir(folder):
            return
        if self._links_out(folder, home):
            self._skip(folder, "claude-code", scope, _LINKED_OUT)
            return
        found, more = self._rule_files(folder, scope, home)
        found.sort(key=lambda p: os.path.relpath(p, folder).replace("\\", "/").lower())
        if more:
            self.items.append(
                {
                    "path": folder,
                    "format": "claude-code",
                    "scope": scope,
                    "skipped": f"more than {MAX_RULE_FILES} rule files here; only {MAX_RULE_FILES} of them "
                    "are looked at",
                }
            )
        for path in found[:MAX_RULE_FILES]:
            if self.late():
                break
            if _key(path) in self.seen:
                continue
            why = self.refused(path)
            text = None
            if not why:
                try:
                    text = _read_file(path)
                except (OSError, ValueError, fs.FsError):
                    text = None  # file() reports the error
            paths = rule_paths(text) if text is not None else None
            applies = (
                rule_applies(paths, self.root, self.target, self.glob_budget, self.late)
                if paths and self.root
                else True
            )
            if paths and not applies:
                entry = {"path": path, "paths": paths, "format": "claude-code", "scope": scope}
                if applies is None:
                    entry["unchecked"] = (
                        "too many path patterns to check in one call; ask with path= that file"
                    )
                self.not_applied.append(entry)
                continue
            self.file(path, "claude-code", scope, text, home=home)

    def _read(
        self,
        path: str,
        fmt: str,
        scope: str,
        imported_by: dict | None,
        hops: int,
        text: str | None = None,
        check: str | None = None,
    ) -> None:
        """`scope` labels the file; `check` (default: scope) decides which of its imports are outside."""
        self.seen.add(_key(path))
        item: dict = {"path": path, "format": fmt, "scope": scope}
        if imported_by is None:
            self.items.append(item)
        else:
            item["imported_by"] = imported_by["path"]
            self.children.setdefault(id(imported_by), []).append(item)
        self.by_key[_key(path)] = item
        try:
            text = _read_file(path) if text is None else text
        except (OSError, ValueError, fs.FsError) as exc:
            item["error"] = str(exc)
            return
        self.texts[id(item)] = text
        if fmt == "claude-code":
            self._expand(item, text, check or scope, hops)

    def _expand(self, item: dict, text: str, scope: str, hops: int) -> None:
        """Follow the @imports of a file Claude Code reads, as Claude Code does."""
        path = item["path"]
        self.expanded.add(_key(path))
        imports: list[str] = []
        listed: set[str] = set()
        skipped: list[dict] = []
        outside = scope == "project" and not self.external_ok()
        for ref in import_refs(text):
            if self.late():
                break
            candidates = _import_paths(ref, os.path.dirname(path))
            if outside and any(_network(c) for c in candidates):  # never touch a share on its say-so
                skipped.append({"import": ref, "path": candidates[0], "reason": _OUTSIDE})
                continue
            target = next((c for c in candidates if os.path.isfile(c)), None)
            if target is None:
                continue  # not a file: an @mention, not an import
            key = _key(target)
            if key not in listed:
                listed.add(key)
                imports.append(target)
            why = None
            if hops + 1 > MAX_IMPORT_HOPS:
                why = (
                    f"more than {MAX_IMPORT_HOPS} imports deep, so Claude Code does not load it either; "
                    "read it with fs_read if it matters"
                )
            elif outside and not self._inside(target):
                why = _OUTSIDE
            if key in self.seen:
                # read already, perhaps as an AGENTS.md for Codex: Claude Code still follows its imports
                seen_item = self.by_key.get(key)
                seen_text = self.texts.get(id(seen_item)) if seen_item else None
                if why is None and seen_text is not None and key not in self.expanded:
                    self._expand(seen_item, seen_text, scope, hops + 1)  # judged as the referrer's
                continue
            why = why or self.refused(target)
            if why:
                skipped.append({"import": ref, "path": target, "reason": why})
                continue
            self._read(target, "claude-code", item["scope"], item, hops + 1, check=scope)
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


def _plain_text(path: str) -> bool:
    """A name that survives being sent as JSON (Linux allows bytes that are not UTF-8 in names)."""
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _external_imports_approved(folders: list[str]) -> bool:
    """Whether Claude Code approved external imports for the project (recorded under the project
    root, or the folder it was started in)."""
    wanted = {_folder_key(f) for f in folders}
    for folder, entry in _claude_projects().items():
        if _folder_key(str(folder)) in wanted and isinstance(entry, dict):
            if entry.get("hasClaudeMdExternalIncludesApproved") is True:
                return True
    return False


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
    rules = _Rules(cfg, root, start, target)
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
        return [p for p in paths if os.path.lexists(p) and _key(p) not in user_files]

    # with Claude Code's reading alone, AGENTS.md stands in when no CLAUDE file is in sight
    agents_for_claude = formats == ["claude-code"] and not any(
        os.path.isfile(p) for d in levels for p in claude_files(d)
    )
    for folder in levels:
        if rules.late():
            break
        in_project = os.path.normcase(folder) in project
        home = root if in_project else folder  # where a file found here may really be
        for fmt in formats:
            if fmt == "codex":
                if in_project:
                    rules.first([os.path.join(folder, n) for n in CODEX_FILES], "codex", "project", home=home)
                continue
            for path in claude_files(folder):
                rules.file(path, "claude-code", "project", home=home)
            if agents_for_claude and in_project:
                rules.file(os.path.join(folder, "AGENTS.md"), "claude-code", "project", home=home)
            if os.path.normcase(folder) == os.path.normcase(root):
                rules.folder(os.path.join(root, ".claude", "rules"), "project", home=root)
    return rules


# ------------------------------------------------------------------ skills


def _skills_in(
    folder: str, scope: str, refused=None, desc_chars: int = DESCRIPTION_CHARS, mask=None
) -> list[dict]:
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
    except OSError:
        return []
    out = []
    for entry in entries:
        if entry.name.startswith(".") or not _plain_text(entry.name) or not entry.is_dir():
            continue
        skill_md = os.path.join(entry.path, "SKILL.md")
        if not os.path.isfile(skill_md) or (refused and refused(skill_md)):
            continue
        try:
            head, _ = fs.read_text(skill_md)
        except (OSError, ValueError, fs.FsError):
            head = ""
        meta = _frontmatter(head[:20_000])
        # as Claude Code lists a skill: its description, then when_to_use, cut together
        description = " ".join(" ".join([meta.get("description", ""), meta.get("when_to_use", "")]).split())
        if len(description) > desc_chars:
            # masked before the cut: a secret cut in two would no longer be recognised
            description = (mask or redact.mask)(description)
            if len(description) > desc_chars:
                description = description[: desc_chars - 1] + "…"
        skill = {
            "name": meta.get("name") or entry.name,
            "description": description,
            "path": skill_md,
            "scope": scope,
        }
        if meta.get("disable-model-invocation", "").strip().lower() == "true":
            skill["model_invocable"] = False  # Claude Code leaves it out of the list the model sees
        out.append(skill)
    return out


def skill_folders(cfg: Config, root: str | None) -> list[tuple[str, str]]:
    folders = [(os.path.join(root, p), "project") for p in PROJECT_SKILLS] if root else []
    return folders + [(_expand(p), "user") for p in _setting(cfg, "skills", DEFAULT_SKILLS)]


def skills(
    cfg: Config, root: str | None, refused=None, desc_chars: int = DESCRIPTION_CHARS, mask=None
) -> list[dict]:
    """`mask`: masks a long description before it is cut (default: redact.mask)."""
    found: dict[str, dict] = {}
    for folder, scope in skill_folders(cfg, root):
        for skill in _skills_in(folder, scope, refused, desc_chars, mask):
            found.setdefault(skill["name"].lower(), skill)
    return sorted(found.values(), key=lambda s: (s["scope"] != "project", s["name"].lower()))


SKILL_FILES = 50  # files listed from a skill's folder


def _skill_files(folder: str) -> tuple[list[dict], bool]:
    """The files in a skill's folder (relative path and size), up to SKILL_FILES; whether more exist."""
    out: list[dict] = []
    for here, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in fs._SKIP_DIRS)
        for name in sorted(files):
            if len(out) >= SKILL_FILES:
                return out, True
            full = os.path.join(here, name)
            try:
                size = os.stat(full).st_size
            except OSError:
                continue
            out.append({"path": os.path.relpath(full, folder).replace(os.sep, "/"), "bytes": size})
    return out, False


def skill(cfg: Config, name: str, path: str | None = None, repo: str | None = None) -> dict:
    """One skill by name: its SKILL.md, its folder and the files in it (what Claude Code's Skill tool
    loads). A project's skills (path or repo) come before the user's."""
    root = None
    if repo:
        root = project_root(find_repo(cfg, repo)["checkout"])
    elif path:
        root = project_root(guard._start_dir(_expand(path)))
    rules = _Rules(cfg, root, root, root)
    every = skills(cfg, root, desc_chars=SKILL_DESC_LIMIT)
    want = name.strip().lower()
    found = next((s for s in every if s["name"].lower() == want), None) or next(
        (s for s in every if os.path.basename(os.path.dirname(s["path"])).lower() == want), None
    )
    if found is None:
        names = [s["name"] for s in every]
        close = difflib.get_close_matches(name, names, n=8, cutoff=0.5)
        close += [n for n in names if want and want in n.lower() and n not in close][: 8 - len(close)]
        raise ValueError(
            f"no skill named {name!r} here"
            + (f"; close: {', '.join(close)}" if close else "")
            + f" ({len(names)} skills; context() lists them all)"
        )
    folder = os.path.dirname(found["path"])
    out = {"name": found["name"], "scope": found["scope"], "folder": folder, "skill_md": found["path"]}
    why = rules.refused(found["path"])
    if why:  # its description comes from the same file: withheld too
        out["content"] = None
        out["refused"] = why
    else:
        if found.get("description"):
            out["description"] = found["description"]
        try:
            text, _ = fs.read_text(found["path"])
        except (OSError, ValueError, fs.FsError) as exc:
            raise ValueError(f"cannot read {found['path']}: {exc}") from None
        budget = fs.READ_MAX_CHARS  # what one result holds, as fs_read counts it (width)
        if width(text) > budget:
            lines = text.splitlines(keepends=True)
            kept, used = 0, 0
            while kept < len(lines) and used + width(lines[kept]) <= budget:
                used += width(lines[kept])
                kept += 1
            if kept:
                out["truncated"] = _truncated(found["path"], kept, len(lines))
                text = "".join(lines[:kept])
            else:
                shown = fitting(text, budget)
                out["truncated"] = (
                    f"showing the first {shown} characters of its first line, which is longer "
                    f"(the whole file: {found['path']})"
                )
                text = text[:shown]
        out["content"] = text
    out["files"], more = _skill_files(folder)
    if more:
        out["files_more"] = f"more than {SKILL_FILES} files; fs_list(path={folder!r}) lists the rest"
    out["note"] = (
        "Follow the instructions in content. Paths in it are relative to folder; scripts run on this "
        "machine (run / session_start with cwd=folder), read other files with fs_read."
    )
    redactor = redact.current()
    out = _sendable(out)
    return out if redactor.empty else redactor.data(out)[0]


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


def read_git_record(path: str, limit: int = 64 * 1024, follow: bool = False) -> str | None:
    """A small file git keeps (.git, commondir, gitdir, config), None unless it is a regular file
    of at most `limit` bytes; a link counts only with `follow`. A cloned or unpacked folder can
    hold a pipe or a device there, which must never block a read."""
    try:
        st = os.stat(path) if follow else os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            return None
        with open(path, "rb") as fh:
            return fh.read(limit).decode("utf-8", "replace").strip()
    except (OSError, ValueError):
        return None


def _git_dirs(root: str) -> tuple[str | None, str | None]:
    """(git dir, common git dir) of a checkout; a worktree's .git file points at both."""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return dot, dot
    first = read_git_record(dot, follow=True)
    if first is None:
        return None, None
    if not first.startswith("gitdir:"):
        return None, None
    gitdir = first[len("gitdir:") :].strip()
    gitdir = gitdir if os.path.isabs(gitdir) else os.path.normpath(os.path.join(root, gitdir))
    common = gitdir
    rel = read_git_record(os.path.join(gitdir, "commondir"), follow=True)
    if rel:
        common = rel if os.path.isabs(rel) else os.path.normpath(os.path.join(gitdir, rel))
    return gitdir, common


def _remote_urls(common: str) -> list[str]:
    text = read_git_record(os.path.join(common, "config"), limit=64_000_000, follow=True)
    if text is None:
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


# Clones live in nested folders (D:\\Dev\\20_个人项目\\winhand): repo_roots and the folders around the
# projects Codex and Claude Code know are searched a few levels down, within a time budget.
REPO_SCAN_DEPTH = 4
REPO_SCAN_BUDGET_S = 6.0
REPO_SCAN_MAX_DIRS = 40_000
NEARBY_LEVELS = 3  # how far above a known project its neighbourhood starts
_SCAN_TTL_S = 600.0
_SCAN_SKIP = {
    "node_modules", "__pycache__", ".venv", "venv", "site-packages", "$recycle.bin",
    "system volume information", "appdata", "windows", "program files", "program files (x86)",
    "programdata", "build", "dist", "target", "out", "bin", "obj",
}  # fmt: skip
_scans: dict[tuple[str, int], tuple[float, list[str]]] = {}


def _has_git(folder: str) -> bool:
    return os.path.lexists(os.path.join(folder, ".git"))


def git_roots_under(
    roots: list[str], depth: int = REPO_SCAN_DEPTH, budget_s: float = REPO_SCAN_BUDGET_S
) -> list[str]:
    """Checkouts (folders holding .git) at most `depth` levels below each root, shallowest first.
    Hidden and build folders, links and junctions are not entered, nor is a checkout's inside.
    Complete scans are remembered for ten minutes."""
    deadline = time.monotonic() + budget_s
    found: list[str] = []
    visited: set[str] = set()
    for root in roots:
        key = (os.path.normcase(os.path.abspath(root)), depth)
        cached = _scans.get(key)
        if cached and time.monotonic() - cached[0] < _SCAN_TTL_S:
            found += [f for f in cached[1] if f not in found]
            continue
        here: list[str] = []
        queue = [(root, 0)]
        complete = True
        while queue:
            if time.monotonic() > deadline or len(visited) > REPO_SCAN_MAX_DIRS:
                complete = False
                break
            folder, level = queue.pop(0)
            norm = os.path.normcase(folder)
            if norm in visited:
                continue
            visited.add(norm)
            if _has_git(folder):
                here.append(folder)
                continue
            if level >= depth:
                continue
            try:
                with os.scandir(folder) as entries:
                    subs = sorted(
                        (
                            e.path
                            for e in entries
                            if e.is_dir(follow_symlinks=False)
                            and not e.is_symlink()
                            and not (getattr(e, "is_junction", None) and e.is_junction())
                            and not e.name.startswith(".")
                            and e.name.lower() not in _SCAN_SKIP
                        ),
                        key=str.lower,
                    )
            except OSError:
                continue
            queue += [(sub, level + 1) for sub in subs]
        if complete:
            _scans[key] = (time.monotonic(), here)
        found += [f for f in here if f not in found]
    return found


def _nearby(known: list[str]) -> list[str]:
    """Folders around the projects Codex and Claude Code know: up to NEARBY_LEVELS above each one,
    never a drive root, the home folder or anything above it, or AppData."""
    home = os.path.normcase(os.path.abspath(os.path.expanduser("~")))
    appdata = os.path.join(home, "appdata")
    out: list[str] = []
    for level in range(1, NEARBY_LEVELS + 1):
        for folder in known:
            folder = folder[4:] if folder.startswith("\\\\?\\") else folder
            p = Path(os.path.abspath(folder))
            parents = list(p.parents)
            if len(parents) < level:
                continue
            up = parents[level - 1]
            norm = os.path.normcase(str(up))
            if (
                up.parent == up
                or norm == home
                or home.startswith(norm.rstrip("\\/") + os.sep)
                or norm == appdata
                or norm.startswith(appdata + os.sep)
                or str(up) in out
            ):
                continue
            out.append(str(up))
    return out


def _under_roots(cfg: Config) -> list[str]:
    roots = [_expand(folder) for folder in _setting(cfg, "repo_roots", [])]
    return git_roots_under([r for r in roots if os.path.isdir(r)]) if roots else []


def _with_nearby(sources: list, known: list[str], found: dict):
    """The sources in order, then, only while nothing was found, the neighbourhood of known projects."""
    yield from sources
    if not found:
        nearby = git_roots_under(_nearby(known))
        sources.append(("nearby", nearby))
        yield "nearby", nearby


def find_repo(cfg: Config, repo: str) -> dict:
    """The local checkout of `repo` (owner/name or a remote URL); ValueError when there is none."""
    if len(repo) > 500:
        raise ValueError("repo should be owner/name or a git remote URL; this is too long to be one")
    want = repo_key(repo)
    if not want:
        raise ValueError(f"repo should be owner/name or a git remote URL, not {repo!r}")
    known = _codex_projects() + [str(k) for k in _claude_projects()]
    sources = [
        ("codex", _codex_projects()),
        ("claude-code", [str(k) for k in _claude_projects()]),
        ("repo_roots", _under_roots(cfg)),
    ]
    found: dict[str, tuple[str, str, float]] = {}
    checked: set[str] = set()
    for source, folders in _with_nearby(sources, known, found):
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
            f"no checkout of {repo} found on this machine (folders looked at: {counts}, and the checkouts "
            f"up to {REPO_SCAN_DEPTH} levels below the folders around those projects; Codex's list is "
            f"[projects] in {os.path.join(codex_home(), 'config.toml')}, Claude Code's is projects in "
            f"{claude_state_file()}). Add the folder that holds your clones to [context] repo_roots in "
            f"{config.config_path()} (for example repo_roots = ['D:\\code']; searched {REPO_SCAN_DEPTH} levels "
            "down), or call context with path= instead."
        )
    ordered = sorted(found.values(), key=lambda f: f[2], reverse=True)
    others, used = [], 0
    for folder, _, _ in ordered[1 : 1 + MAX_LISTED]:
        used += _encoded(folder) + 4
        if used > CHECKOUTS_CHARS:
            break
        others.append(folder)
    more = {"other_checkouts_more": len(ordered) - 1 - len(others)} if len(ordered) - 1 > len(others) else {}
    return more | {
        "name": repo,
        "checkout": ordered[0][0],
        "source": ordered[0][1],
        "other_checkouts": others,
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
        value = _sendable(value)  # before measuring: a lossy name may grow
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
    if rules.stopped:
        out["incomplete"] = (
            f"stopped reading rule files after {RULES_TIME_S:.0f} s (very many or very large ones here); "
            "what is listed is what was read. Call context again with path= the file you work on, or read "
            "the rest with fs_read"
        )
    out["rules"] = []
    if rules.not_applied:
        listed, used = [], 0
        for entry in mask(rules.not_applied):
            cost = _json_len(entry) + 2
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
        unchecked = sum(1 for entry in rules.not_applied if "unchecked" in entry)
        if unchecked:
            out["rules_not_applied_note"] += f"; {unchecked} of them were not checked (too many patterns)"

    every_skill = mask(skills(cfg, root, rules.refused, mask=mask))  # counted in `redacted`
    shown, used = [], 2
    for skill in every_skill:
        cost = _json_len(skill) + 2
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
        "When a skill fits the task, load it with skill(name) (its SKILL.md and files) and follow it; "
        "run the scripts it names on this machine with run."
    )
    out["guards"] = guards = mask(guard.rules_for(start))
    errors, used = [], 0
    for error in guards.get("errors", []):
        if len(error.get("error", "")) > ERROR_CHARS:
            error = error | {"error": error["error"][: ERROR_CHARS - 1] + "…"}
        used += _json_len(error) + 2
        if used > GUARD_ERRORS_CHARS:
            guards["errors_more"] = len(guards["errors"]) - len(errors)
            break
        errors.append(error)
    if "errors" in guards:
        guards["errors"] = errors
    kept, used = [], 0
    for rule in guards["rules"]:
        used += _json_len(rule) + 2
        if used > GUARDS_CHARS:
            guards["rules_more"] = len(guards["rules"]) - len(kept)
            guards["rules_more_note"] = "the rest are in the guard rule files listed above"
            break
        kept.append(rule)
    guards["rules"] = kept
    from . import hooks

    out["hooks"] = mask(hooks.describe(start))
    out["local_mcp_servers"] = {
        "names": mask([s.name for s in cfg.servers if s.enabled]),
        "note": "mcp_tools() lists their tools, mcp_call(server, tool, arguments) calls one",
    }
    from . import harmonia, secret_store

    stored = secret_store.stored_names()  # names only: the values go to programs, never here
    if stored:
        out["secrets"] = {
            "names": stored,
            "use": "run(..., secrets=['NAME']) or job_start(..., secrets=['NAME']) gives the program "
            "these environment variables; you never see the values. A new one: secret_prompt(message, "
            "key='NAME') asks the person to type it in.",
        }
    keys = harmonia.summary()  # keys synced from the owner's phone, and how to use them unseen
    if keys is not None:
        out["harmonia"] = mask(keys)
    out["next"] = (
        "Follow these rules while you work here. Call context again for another folder, and after the "
        "conversation is compacted."
        if path
        else "Pass path=<the folder you work in>, or repo=<owner/name> when you only know the repository, "
        "to also get that project's rules, skills and guard rules."
    )
    for key in ("path", "project_root", "repo"):
        if key in out:
            out[key] = mask(out[key])

    # the budget is what the masked result will be once it is JSON: backslashes, quotes and line
    # breaks take two characters there; 300 more for the redaction count and note, and room to
    # name the files that do not fit at all
    budget = BUDGET_CHARS - _json_len(out) - 300 - OMITTED_CHARS
    omitted: list[str] = []
    for item in rules.ordered():
        text = rules.texts.get(id(item))
        item = mask(item)
        if text is None:
            cost = _json_len(item) + 2
            if cost > budget:
                omitted.append(item["path"])
                continue
            out["rules"].append(item)
            budget -= cost
            continue
        text = mask(text)
        lines = text.splitlines()
        item["lines"] = len(lines)
        cost = _json_len(item) + len(', "content": ""') + 2
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
    return _sendable(out)


def _sendable(value):
    """The result with any text that is not valid Unicode (a file name in bytes Linux allows)
    made printable, so it can be sent as JSON."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
            return value
        except UnicodeEncodeError:
            return value.encode("utf-8", "surrogatepass").decode("utf-8", "replace")
    if isinstance(value, dict):
        return {k: _sendable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sendable(v) for v in value]
    return value
