"""Skills on this machine: where they are found, what they are called, and how they are shown.

Found: one table of sources, read the way the tools that own them read them.
- project: every folder from the one worked in up to the project root has .agents/skills (Codex,
  Pi), .claude/skills (Claude Code) and .codex/skills (Codex); .pi/skills counts only in the folder
  itself (Pi). Without a .git above, only that folder.
- user: ~/.agents/skills, Claude Code's and Codex's skills folders, ~/.pi/agent/skills, or the
  folders in [context] skills.
- plugin: the Claude Code plugins enabledPlugins turns on (user, project, local settings) and the
  Codex plugins config.toml turns on or that Codex installed from the owner's account.
Each source walks its folder as its tool does: Codex collects every SKILL.md up to six levels
down, Claude Code only the folders directly inside, Pi stops at the first SKILL.md.

Named: an id that says where the skill is (project:.codex/skills/openspec-flow,
user:~/.codex/skills/officecli/officecli-docx, plugin:figma@claude-plugins-official/figma-use);
a file reached from two places is one skill with both ids. A bare name resolves to the nearest
project folder first, then the user's folders, then plugins; a skill of a project nobody marked
as trusted never stands in for the user's skill of the same name.

Shown: not every skill every time. A short map (the pinned skills, the skills by area, the
projects with skills of their own, and when to search) goes where a client looks first; context
(path) lists that project's own skills; skill(query=...) searches the rest.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
import tomllib
from dataclasses import dataclass, field

from . import fs, guard, redact
from .config import Config

WALK_DEPTH = 6  # as Codex walks a skills folder
WALK_DIRS_PER_ROOT = 2_000
WALK_DIRS_TOTAL = 4_000  # one call
WALK_TIME_S = 2.0
SKIP = {"node_modules", "__pycache__", "synced", "venv", "site-packages"}  # and every .dotted folder
HEAD_BYTES = 20_000
DESCRIPTION_CHARS = 300
SKILL_DESC_LIMIT = 1_536  # Claude Code's cut of description + when_to_use
PROJECT_FULL = 10  # a project's skills shown with their descriptions in context()
PINNED_DESC = 160
MAP_EXAMPLES = 4
MAP_PROJECTS = 8
SEARCH_LIMIT = 10
SEARCH_MAX = 30
SEARCH_DESC = 220
PROJECT_MODES = ("warn", "trusted", "off")
CONTRACT = (
    "When a task needs a dedicated tool for files, devices or a project's workflow, first search the "
    "skills with the task's own words: skill(query=[...]) (one to four words, the person's language and "
    "English; no folder needed, skills not shown here are found too). Load one with skill(<id or name>, "
    "path=<the folder you work in>) and follow it."
)


@dataclass(frozen=True)
class Source:
    """One place skills live: its layer, the tool whose convention it is, where it is and how it is walked."""

    layer: str  # project | user | plugin
    host: str  # agents | claude | codex | pi | config
    rel: str  # for project sources: the folder below each project folder
    walk: str  # all: every SKILL.md below (Codex) | children: one level (Claude Code) | first (Pi)
    where: str = "chain"  # project sources: chain (each folder start..root) | start (that folder only)


PROJECT_SOURCES = (
    Source("project", "agents", os.path.join(".agents", "skills"), "all"),
    Source("project", "claude", os.path.join(".claude", "skills"), "children"),
    Source("project", "codex", os.path.join(".codex", "skills"), "all"),
    Source("project", "pi", os.path.join(".pi", "skills"), "first", "start"),
)


def _home() -> str:
    return os.path.abspath(os.path.expanduser("~"))


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _setting(cfg: Config, key: str, default):
    value = (cfg.context or {}).get(key)
    return default if value is None else value


def _list_setting(cfg: Config, key: str, default: list[str]) -> list[str]:
    value = (cfg.context or {}).get(key)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return list(default)


def claude_home() -> str:
    return _expand(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")


def codex_home() -> str:
    return _expand(os.environ.get("CODEX_HOME") or "~/.codex")


def pi_agent_dir() -> str:
    return _expand(os.environ.get("PI_CODING_AGENT_DIR") or "~/.pi/agent")


def user_sources(cfg: Config) -> list[tuple[str, Source]]:
    """(folder, source) of the user's skills: [context] skills when set, else each tool's own folder."""
    configured = (cfg.context or {}).get("skills")
    if configured is None:
        return [
            (_expand("~/.agents/skills"), Source("user", "agents", "", "all")),
            (os.path.join(claude_home(), "skills"), Source("user", "claude", "", "children")),
            (os.path.join(codex_home(), "skills"), Source("user", "codex", "", "all")),
            (os.path.join(pi_agent_dir(), "skills"), Source("user", "pi", "", "first")),
        ]
    out = []
    for folder in _list_setting(cfg, "skills", []):
        full = _expand(folder)
        norm = full.replace("\\", "/").lower().rstrip("/")
        if norm.endswith("/.claude/skills"):
            out.append((full, Source("user", "claude", "", "children")))
        elif "/.pi/" in norm + "/":
            out.append((full, Source("user", "pi", "", "first")))
        else:
            out.append((full, Source("user", "codex" if "/.codex/" in norm + "/" else "config", "", "all")))
    return out


# ------------------------------------------------------------------ walking a skills folder


@dataclass
class _Budget:
    """What one call may spend walking skills folders."""

    deadline: float = field(default_factory=lambda: time.monotonic() + WALK_TIME_S)
    dirs: int = 0
    incomplete: list[str] = field(default_factory=list)

    def spent(self) -> bool:
        return self.dirs >= WALK_DIRS_TOTAL or time.monotonic() > self.deadline


def _usable(name: str) -> bool:
    if name.startswith(".") or name.lower() in SKIP:
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _subfolders(folder: str) -> list[str]:
    try:
        with os.scandir(folder) as entries:
            return sorted(
                (e.path for e in entries if _usable(e.name) and e.is_dir()), key=lambda p: p.lower()
            )
    except OSError:
        return []


def walk(root: str, mode: str, budget: _Budget) -> list[str]:
    """The SKILL.md files of a skills folder, as `mode` finds them (a SKILL.md right in `root` is not
    a skill of it). Links are followed, a folder reached twice is read once."""
    if not os.path.isdir(root):
        return []
    if mode == "children":
        out = []
        for sub in _subfolders(root):
            budget.dirs += 1
            md = os.path.join(sub, "SKILL.md")
            if os.path.isfile(md):
                out.append(md)
        return out
    out: list[str] = []
    seen: set[str] = set()
    queue = [(root, 0)]
    here = 0
    while queue:
        if budget.spent() or here >= WALK_DIRS_PER_ROOT:
            budget.incomplete.append(root)
            break
        folder, depth = queue.pop(0)
        try:
            real = os.path.normcase(os.path.realpath(folder))
        except (OSError, ValueError):
            continue
        if real in seen:
            continue
        seen.add(real)
        here += 1
        budget.dirs += 1
        if depth and os.path.isfile(os.path.join(folder, "SKILL.md")):
            out.append(os.path.join(folder, "SKILL.md"))
            if mode == "first":
                continue
        if depth < WALK_DEPTH:
            queue += [(sub, depth + 1) for sub in _subfolders(folder)]
    return out


# ------------------------------------------------------------------ what a SKILL.md says

_meta_cache: dict[str, tuple[tuple[int, int], dict]] = {}


def _meta(path: str) -> dict:
    """name, description, when_to_use, disable-model-invocation from a SKILL.md header (cached by size
    and time)."""
    from .workspace import _frontmatter

    try:
        st = os.stat(path)
    except OSError:
        return {}
    stamp = (st.st_size, st.st_mtime_ns)
    key = os.path.normcase(path)
    cached = _meta_cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        with open(path, "rb") as fh:
            head = fh.read(HEAD_BYTES)
        text = head.decode("utf-8-sig", "replace")
    except OSError:
        text = ""
    meta = _frontmatter(text)
    if len(_meta_cache) > 4_000:
        _meta_cache.clear()
    _meta_cache[key] = (stamp, meta)
    return meta


def _describe(meta: dict, desc_chars: int, mask) -> str:
    description = " ".join(" ".join([meta.get("description", ""), meta.get("when_to_use", "")]).split())
    if len(description) > desc_chars:
        # masked before the cut: a secret cut in two would no longer be recognised
        description = (mask or redact.mask)(description)
        if len(description) > desc_chars:
            description = description[: desc_chars - 1] + "…"
    return description


# ------------------------------------------------------------------ plugins


def _json_file(path: str):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _toml_file(path: str) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = tomllib.loads(fh.read())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _same(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except (OSError, ValueError):
        return False


def _claude_settings(root: str | None) -> list[dict]:
    """Claude Code's settings files that carry enabledPlugins and skillOverrides, low to high."""
    paths = [os.path.join(claude_home(), "settings.json")]
    if root:
        paths += [
            os.path.join(root, ".claude", "settings.json"),
            os.path.join(root, ".claude", "settings.local.json"),
        ]
    return [d for d in (_json_file(p) for p in paths) if isinstance(d, dict)]


def _entries(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if isinstance(v, str)]
    return []


@dataclass
class Plugin:
    id: str  # name@marketplace
    host: str  # claude | codex
    root: str  # its installed folder
    skill_roots: list[tuple[str, str]]  # (folder, walk mode); a folder holding SKILL.md is one skill


def claude_plugins(root: str | None) -> list[Plugin]:
    """The Claude Code plugins enabled for this project (enabledPlugins of the user's, the project's
    and the local settings, later ones winning), each with the folders its skills are in."""
    enabled: dict[str, bool] = {}
    for settings in _claude_settings(root):
        value = settings.get("enabledPlugins")
        if isinstance(value, dict):
            enabled.update({str(k): v for k, v in value.items() if isinstance(v, bool)})
    data = _json_file(os.path.join(claude_home(), "plugins", "installed_plugins.json"))
    installed = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(installed, dict):
        return []
    out = []
    for pid, on in enabled.items():
        if not on:
            continue
        records = installed.get(pid)
        records = records if isinstance(records, list) else [records] if isinstance(records, dict) else []
        chosen = None
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("installPath"), str):
                continue
            if record.get("scope") in ("project", "local"):
                if root and isinstance(record.get("projectPath"), str) and _same(record["projectPath"], root):
                    chosen = record
                    break
                continue
            chosen = chosen or record
        if chosen is None or not os.path.isdir(chosen["installPath"]):
            continue
        base = chosen["installPath"]
        name = pid.split("@", 1)[0]
        declared = []
        manifest = _json_file(os.path.join(base, ".claude-plugin", "plugin.json"))
        if isinstance(manifest, dict):
            declared += _entries(manifest.get("skills"))
        market = _json_file(os.path.join(base, ".claude-plugin", "marketplace.json"))
        if isinstance(market, dict) and isinstance(market.get("plugins"), list):
            for entry in market["plugins"]:
                if isinstance(entry, dict) and entry.get("name") == name:
                    declared += _entries(entry.get("skills"))
        folders = [os.path.join(base, "skills")] + [os.path.normpath(os.path.join(base, d)) for d in declared]
        roots = [(f, "children") for f in dict.fromkeys(folders) if _inside(f, base)]
        out.append(Plugin(pid, "claude", base, roots))
    return out


def _version_key(name: str):
    parts = re.findall(r"\d+|[a-z]+", name.lower())
    return [(0, int(p)) if p.isdigit() else (1, p) for p in parts]


def codex_plugins() -> list[Plugin]:
    """The Codex plugins in its cache that config.toml turns on, or that Codex installed from the
    owner's account (a .codex-remote-plugin-install.json beside the versions) and config.toml does
    not turn off. Of several versions, `local` first, else the highest."""
    settings = _toml_file(os.path.join(codex_home(), "config.toml")).get("plugins")
    settings = settings if isinstance(settings, dict) else {}
    cache = os.path.join(codex_home(), "plugins", "cache")
    out = []
    for market in _subfolders(cache):
        for folder in _subfolders(market):
            pid = f"{os.path.basename(folder)}@{os.path.basename(market)}"
            entry = settings.get(pid)
            on = entry.get("enabled") if isinstance(entry, dict) else None
            remote = os.path.isfile(os.path.join(folder, ".codex-remote-plugin-install.json"))
            if not (on is True or (on is None and remote)):
                continue
            versions = _subfolders(folder)
            if not versions:
                continue
            names = {os.path.basename(v): v for v in versions}
            base = names.get("local") or names[max(names, key=_version_key)]
            manifest = _json_file(os.path.join(base, ".codex-plugin", "plugin.json"))
            declared = _entries(manifest.get("skills")) if isinstance(manifest, dict) else []
            folders = [os.path.normpath(os.path.join(base, d)) for d in declared or ["skills"]]
            out.append(
                Plugin(pid, "codex", base, [(f, "all") for f in dict.fromkeys(folders) if _inside(f, base)])
            )
    return out


def _inside(path: str, base: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(base)]) == os.path.normcase(base)
    except ValueError:
        return False


# ------------------------------------------------------------------ trust


def project_trust(cfg: Config, root: str) -> str:
    """trusted | untrusted | unknown: Codex's trust_level for the project (or its main checkout) first,
    an explicit untrusted there winning over everything; then [context] trusted_roots, then Claude
    Code's accepted workspace trust."""
    from . import hooks

    codex = None
    try:
        _, levels, _ = hooks._codex_user()
        for folder in dict.fromkeys([root, hooks._main_checkout(root)]):
            for spelling in reversed(hooks._spellings(folder)):
                level = levels.get(hooks._folder_key(spelling))
                if level is not None and codex is None:
                    codex = level
    except Exception:  # noqa: BLE001 - a broken config never hides a project's skills from the owner
        pass
    if codex is not None and codex != "trusted":
        return "untrusted"  # the owner said no for this project: trusted_roots does not overrule it
    if codex == "trusted":
        return "trusted"
    for folder in _list_setting(cfg, "trusted_roots", []):
        if _inside(os.path.abspath(root), _expand(folder)):
            return "trusted"
    try:
        if hooks._claude_trusted(root):
            return "trusted"
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def project_mode(cfg: Config) -> str:
    mode = str(_setting(cfg, "project_skills", "warn")).strip().lower()
    return mode if mode in PROJECT_MODES else "warn"


# ------------------------------------------------------------------ host switches


def _host_off(cfg: Config, root: str | None) -> tuple[set[str], set[str], set[str]]:
    """Names Claude Code hides from its model (skillOverrides off / user-invocable-only), and the
    names and SKILL.md paths Codex turns off ([[skills.config]] enabled = false, the last entry
    that matches winning)."""
    claude: dict[str, str] = {}
    for settings in _claude_settings(root):
        value = settings.get("skillOverrides")
        if isinstance(value, dict):
            claude.update({str(k).lower(): str(v) for k, v in value.items()})
    off_claude = {k for k, v in claude.items() if v in ("off", "user-invocable-only")}
    names: dict[str, bool] = {}
    paths: dict[str, bool] = {}
    config = _toml_file(os.path.join(codex_home(), "config.toml")).get("skills")
    entries = config.get("config") if isinstance(config, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            continue
        if isinstance(entry.get("path"), str):
            paths[os.path.normcase(os.path.abspath(entry["path"]))] = entry["enabled"]
        elif isinstance(entry.get("name"), str):
            names[entry["name"].lower()] = entry["enabled"]
    return (
        off_claude,
        {k for k, v in names.items() if not v},
        {k for k, v in paths.items() if not v},
    )


# ------------------------------------------------------------------ the index


@dataclass
class Skill:
    id: str
    name: str  # plugin skills are plugin:name, as Claude Code and Codex call them
    short: str  # the skill's own name
    description: str
    layer: str  # project | user | plugin
    host: str
    path: str  # its SKILL.md
    project: str | None = None  # project skills: the project root
    plugin: str | None = None
    trusted: bool | None = None
    model_invocable: bool = True
    also: list[str] = field(default_factory=list)
    host_off: list[str] = field(default_factory=list)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    def listed(self, desc: int | None = None) -> dict:
        out = {"id": self.id, "name": self.name}
        text = (
            self.description
            if desc is None or len(self.description) <= desc
            else self.description[: desc - 1] + "…"
        )
        if text:
            out["description"] = text
        if self.layer == "project":
            out["project"] = self.project
            if self.trusted is False:
                out["trusted"] = False
        elif self.layer == "plugin":
            out["plugin"] = self.plugin
        if not self.model_invocable:
            out["model_invocable"] = False
        if self.host_off:
            out["host_off"] = self.host_off
        return out


def _rel_id(path: str, base: str) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def _user_id(folder: str) -> str:
    home = _home()
    if _inside(folder, home):
        return "user:~/" + _rel_id(folder, home)
    return "user:" + folder.replace(os.sep, "/")


def project_chain(root: str, start: str) -> list[str]:
    """Folders from `start` up to `root`, nearest first; [start] when start is not inside root."""
    out = [start]
    d = start
    while os.path.normcase(d) != os.path.normcase(root):
        parent = os.path.dirname(d)
        if parent == d:
            return [start]
        d = parent
        out.append(d)
    return out


@dataclass
class Index:
    skills: list[Skill]
    incomplete: list[str]
    project: str | None = None
    project_trust: str | None = None


def build(
    cfg: Config,
    start: str | None,
    root: str | None,
    refused=None,
    desc_chars: int = DESCRIPTION_CHARS,
    mask=None,
    budget: _Budget | None = None,
    with_user: bool = True,
    with_plugins: bool = True,
) -> Index:
    """Every skill reachable from `start` (in project `root`), nearest first: the project's folders,
    the user's, then the plugins'. A SKILL.md reached twice is one skill with both ids."""
    budget = budget or _Budget()
    found: list[Skill] = []
    by_file: dict[str, Skill] = {}
    off_claude, off_names, off_paths = _host_off(cfg, root)

    def add(md: str, make) -> None:
        if refused and refused(md):
            return
        try:
            key = os.path.normcase(os.path.realpath(md))
        except (OSError, ValueError):
            return
        meta = _meta(md)
        folder_name = os.path.basename(os.path.dirname(md))
        short = (meta.get("name") or folder_name).strip() or folder_name
        skill = make(short)
        if key in by_file:
            by_file[key].also.append(skill.id)
            return
        skill.description = _describe(meta, desc_chars, mask)
        skill.model_invocable = meta.get("disable-model-invocation", "").strip().lower() != "true"
        offs = []
        if short.lower() in off_claude or skill.name.lower() in off_claude:
            offs.append("claude")
        if short.lower() in off_names or os.path.normcase(os.path.abspath(md)) in off_paths:
            offs.append("codex")
        skill.host_off = offs
        by_file[key] = skill
        found.append(skill)

    trust = None
    if root and start and project_mode(cfg) != "off":
        trust = project_trust(cfg, root)
        if trust != "untrusted" and not (project_mode(cfg) == "trusted" and trust != "trusted"):
            chain = project_chain(root, start)
            for level, folder in enumerate(chain):
                for source in PROJECT_SOURCES:
                    if source.where == "start" and level:
                        continue
                    for md in walk(os.path.join(folder, source.rel), source.walk, budget):
                        add(
                            md,
                            lambda short, md=md, source=source: Skill(
                                id="project:" + _rel_id(os.path.dirname(md), root),
                                name=short,
                                short=short,
                                description="",
                                layer="project",
                                host=source.host,
                                path=md,
                                project=root,
                                trusted=True if trust == "trusted" else False,
                            ),
                        )
    if with_user:
        for folder, source in user_sources(cfg):
            for md in walk(folder, source.walk, budget):
                add(
                    md,
                    lambda short, md=md, source=source: Skill(
                        id=_user_id(os.path.dirname(md)),
                        name=short,
                        short=short,
                        description="",
                        layer="user",
                        host=source.host,
                        path=md,
                    ),
                )
    if with_plugins:
        for plugin in claude_plugins(root) + codex_plugins():
            name = plugin.id.split("@", 1)[0]
            for folder, mode in plugin.skill_roots:
                files = (
                    [os.path.join(folder, "SKILL.md")]
                    if os.path.isfile(os.path.join(folder, "SKILL.md"))
                    else walk(folder, mode, budget)
                )
                for md in files:
                    rel = _rel_id(os.path.dirname(md), plugin.root)
                    rel = rel[len("skills/") :] if rel.startswith("skills/") else rel
                    add(
                        md,
                        lambda short, rel=rel, md=md, plugin=plugin, name=name: Skill(
                            id=f"plugin:{plugin.id}/{rel}",
                            name=f"{name}:{short}",
                            short=short,
                            description="",
                            layer="plugin",
                            host=plugin.host,
                            path=md,
                            plugin=plugin.id,
                        ),
                    )
    return Index(found, budget.incomplete, root if root and start else None, trust)


def find(index: Index, want: str) -> Skill | None:
    """The skill an id or a bare name means: nearest first, but an untrusted project's skill never
    stands in for one of the user's or a plugin's of the same name."""
    want = want.strip()
    low = want.lower()
    for skill in index.skills:
        if skill.id.lower() == low or low in (a.lower() for a in skill.also):
            return skill
    named = [
        s
        for s in index.skills
        if low in (s.name.lower(), s.short.lower(), os.path.basename(s.folder).lower())
    ]
    if not named:
        return None
    first = named[0]
    if first.layer == "project" and first.trusted is False:
        other = next((s for s in named if s.layer != "project"), None)
        if other is not None:
            return other
    return first


# ------------------------------------------------------------------ areas

AREAS = (
    ("hardware", "硬件固件", "Hardware & firmware",
     "kicad pcb schematic footprint firmware flash mcu rp2350 stm32 esp32 jlc lcsc component datasheet eda "
     "easyeda quilter pyocd probe uart serial eide keil jitx freecad oscilloscope rigol scope board chip "
     "硬件 固件 烧录 电路 原理图 封装 元件 器件 芯片 数据手册 嵌入式 单片机 示波器 焊接 板子"),
    ("docs", "文档表格", "Docs & sheets",
     "docx word excel xlsx pptx powerpoint slides pdf spreadsheet csv document report markdown visio ocr "
     "latex office officecli letter memo translate translation "
     "文档 表格 幻灯片 演示 报告 排版 公文 翻译 文稿 论文"),
    ("visual", "视觉界面", "Visual & UI",
     "image diagram chart figma design ui ux frontend css icon mermaid svg render visualize animation motion "
     "video drawing canvas theme impeccable "
     "图 图片 绘图 画图 界面 设计 前端 图标 可视化 动画 视频 配色"),
    ("code", "代码工程", "Software engineering",
     "code refactor test tdd bug debug review lint build compile api sdk library typescript python rust "
     "architecture codebase domain module interface prototype "
     "代码 重构 测试 调试 评审 审查 编译 架构 接口 原型"),
    ("agents", "智能体自动化", "Agents & automation",
     "agent skill prompt claude codex gemini mcp subagent llm model context harness automation workflow hook "
     "orchestrat session "
     "智能体 技能 提示词 自动化 工作流 子代理 模型 会话"),
    ("search", "检索知识", "Search & knowledge",
     "search web fetch crawl research knowledge note obsidian memory retrieval context7 wiki docs lookup "
     "搜索 检索 联网 抓取 研究 资料 知识 笔记 复习 查找"),
    ("project", "项目协作", "Project collaboration",
     "github gh pr issue board kanban ticket spec roadmap plan handoff commit release decision triage "
     "questionnaire retro "
     "项目 看板 工单 需求 规格 计划 交接 发布 提交 决定 复盘 收尾"),
    ("ops", "系统运维", "Systems & operations",
     "windows disk cleanup filesystem process server vps ssh deploy docker network proxy install environment "
     "quicker listary cc-switch routing migration "
     "磁盘 清理 系统 服务器 部署 运维 进程 网络 环境 安装 文件 路由 迁移"),
)  # fmt: skip
AREA_KEYS = tuple(a[0] for a in AREAS)


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"\s+", text) if w]


_AREA_WORDS = {key: _words(words) for key, _, _, words in AREAS}


def _hits(term: str, text: str) -> bool:
    if not term:
        return False
    if re.search(r"[㐀-鿿]", term):
        return term in text
    if len(term) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def areas_of(cfg: Config, skill: Skill) -> list[str]:
    """The skill's areas: the owner's correction in [context] skill_areas (by id or name) when there is
    one, else the one or two areas its name and description match best."""
    fixed = (cfg.context or {}).get("skill_areas")
    if isinstance(fixed, dict):
        for key in (skill.id, skill.name, skill.short):
            value = fixed.get(key)
            if value is not None:
                return [v for v in _entries(value) if v in AREA_KEYS]
    name = f"{skill.short} {skill.name}".lower().replace("-", " ")
    desc = skill.description.lower()
    scores = {}
    for key, words in _AREA_WORDS.items():
        scores[key] = sum(3 for w in words if _hits(w, name)) + sum(1 for w in words if _hits(w, desc))
    best = max(scores.values(), default=0)
    if best <= 0:
        return []
    ranked = sorted((k for k in scores if scores[k] > 0), key=lambda k: -scores[k])
    return [k for k in ranked[:2] if scores[k] >= best * 0.75]


# ------------------------------------------------------------------ the map

_radar_cache: dict[str, tuple[float, dict[str, int]]] = {}
RADAR_TTL_S = 600.0


def _known_projects() -> list[str]:
    from . import workspace

    folders = workspace._codex_projects() + [str(k) for k in workspace._claude_projects()]
    out = []
    for folder in folders:
        folder = folder[4:] if folder.startswith("\\\\?\\") else folder
        if os.path.isdir(folder):
            out.append(workspace.project_root(os.path.abspath(folder)))
    home = os.path.normcase(_home())
    return [f for f in dict.fromkeys(out) if os.path.normcase(f) != home and os.path.dirname(f) != f]


def projects_with_skills(cfg: Config) -> dict[str, int]:
    """Projects Codex and Claude Code know that have skills of their own at their root: root -> count
    (remembered for ten minutes)."""
    if project_mode(cfg) == "off":
        return {}
    key = "radar"
    cached = _radar_cache.get(key)
    if cached and time.monotonic() - cached[0] < RADAR_TTL_S:
        return cached[1]
    budget = _Budget()
    out: dict[str, int] = {}
    for root in _known_projects():
        if budget.spent():
            break
        trust = project_trust(cfg, root)
        if trust == "untrusted" or (project_mode(cfg) == "trusted" and trust != "trusted"):
            continue
        files = set()
        for source in PROJECT_SOURCES:
            for md in walk(os.path.join(root, source.rel), source.walk, budget):
                files.add(os.path.normcase(os.path.realpath(md)))
        if files:
            out[root] = len(files)
    _radar_cache[key] = (time.monotonic(), out)
    return out


def pinned(cfg: Config) -> list[str]:
    return _list_setting(cfg, "pinned_skills", [])


def skill_map(cfg: Config, index: Index, with_projects: bool = True) -> dict:
    """The short map shown where a client looks first: pinned skills, skills by area, projects with
    skills of their own, and when to search."""
    machine_wide = [s for s in index.skills if s.layer != "project"]
    by_area: dict[str, list[Skill]] = {k: [] for k in AREA_KEYS}
    unsorted = 0
    for skill in machine_wide:
        keys = areas_of(cfg, skill)
        if not keys:
            unsorted += 1
        for k in keys:
            by_area[k].append(skill)
    pins = []
    for want in pinned(cfg):
        skill = find(index, want)
        if skill is not None:
            pins.append(skill)
    areas = []
    for key, zh, en, _ in AREAS:
        members = by_area[key]
        if not members:
            continue
        examples = [s for s in members if s.model_invocable and not s.host_off][:MAP_EXAMPLES]
        areas.append(
            {
                "area": key,
                "label": f"{zh} {en}",
                "count": len(members),
                "examples": [s.name for s in examples],
            }
        )
    out = {
        "total": len(index.skills),
        "pinned": [s.listed(PINNED_DESC) for s in pins],
        "areas": areas,
    }
    if unsorted:
        out["unsorted"] = unsorted
    if with_projects:
        radar = projects_with_skills(cfg)
        if index.project and index.project not in radar:
            count = sum(1 for s in index.skills if s.layer == "project")
            if count:
                radar = {**radar, index.project: count}
        ranked = sorted(radar.items(), key=lambda kv: -kv[1])
        out["projects"] = [{"project": root, "skills": n} for root, n in ranked[:MAP_PROJECTS]]
        if len(ranked) > MAP_PROJECTS:
            out["projects_more"] = len(ranked) - MAP_PROJECTS
    out["how"] = CONTRACT
    return out


def map_text(cfg: Config, index: Index, room: int) -> str:
    """The map as lines of text in at most `room` characters (the cloud session's start)."""
    data = skill_map(cfg, index)
    lines = [f"Skills on this PC: {data['total']}. " + CONTRACT]
    if data["pinned"]:
        lines.append("Pinned (use without searching):")
        lines += [f"- {p['name']}: {p.get('description', '')}".rstrip(": ") for p in data["pinned"]]
    if data["areas"]:
        lines.append("By area (count: examples), search to see the rest:")
        lines += [f"- {a['label']} ({a['count']}): {', '.join(a['examples'])}" for a in data["areas"]]
    if data.get("projects"):
        names = [f"{os.path.basename(p['project'])} ({p['skills']})" for p in data["projects"]]
        more = f" and {data['projects_more']} more" if data.get("projects_more") else ""
        lines.append(
            "Projects with skills of their own (context(path) lists them): " + ", ".join(names) + more
        )
    text = "\n".join(lines)
    while len(text) > room and len(lines) > 1:
        lines.pop()
        text = "\n".join(lines)
    return text[:room]


def instructions_line(cfg: Config) -> str:
    """The skills line of a server's instructions (every client reads them first; the room there goes
    to the secret store's names, so the pinned skills come with context() and the session start)."""
    return (
        "- Skills, not all listed: for a task needing a dedicated tool, first skill(query=['<task>', "
        "'<English>']). Local MCP: mcp_tools(), mcp_call(server, tool, arguments)."
    )


def map_line(cfg: Config) -> str:
    """One line for a server's instructions: the pinned names and when to search (no folder walked)."""
    names = pinned(cfg)
    head = "- Skills: " + (f"pinned here: {', '.join(names)}. " if names else "")
    return (
        head + "For anything else that needs a dedicated tool, search first with the task's own words: "
        "skill(query=[...]); context(path) lists a project's own skills; load one with skill(<id or name>, "
        "path=<folder>)."
    )


# ------------------------------------------------------------------ search

ALIASES = {
    "搜索": ["search"], "检索": ["search", "retrieval"], "联网": ["web", "search"], "网页": ["web", "browser", "page"],
    "浏览器": ["browser", "playwright"], "抓取": ["fetch", "crawl", "scrape"], "文档": ["doc", "docx", "document", "word"],
    "表格": ["excel", "xlsx", "spreadsheet", "sheet", "csv"], "幻灯片": ["ppt", "pptx", "slides", "powerpoint"],
    "演示": ["ppt", "pptx", "slides", "presentation"], "报告": ["report"], "图": ["diagram", "chart", "image"],
    "图片": ["image", "picture"], "画图": ["diagram", "draw"], "流程图": ["flowchart", "diagram", "mermaid"],
    "设计": ["design", "figma", "ui"], "界面": ["ui", "interface", "frontend"], "前端": ["frontend", "ui", "css"],
    "烧录": ["flash", "firmware", "burn"], "固件": ["firmware"], "编译": ["build", "compile"],
    "调试": ["debug", "gdb", "pyocd"], "电路": ["circuit", "schematic", "pcb"], "原理图": ["schematic"],
    "电路板": ["pcb", "board"], "封装": ["footprint", "package"], "元件": ["component", "part"],
    "器件": ["component", "part", "device"], "芯片": ["chip", "mcu"], "数据手册": ["datasheet"],
    "磁盘": ["disk"], "清理": ["clean", "cleanup"], "文件": ["file"], "部署": ["deploy"],
    "服务器": ["server", "vps"], "测试": ["test", "tdd"], "评审": ["review"], "审查": ["review", "audit"],
    "重构": ["refactor"], "架构": ["architecture", "design"], "代码": ["code"],
    "仓库": ["repo", "repository", "github", "git"], "提交": ["commit", "pr"], "工单": ["issue", "ticket"],
    "看板": ["board", "kanban"], "笔记": ["note", "notes", "obsidian"], "决定": ["decision", "notes"],
    "技能": ["skill"], "提示词": ["prompt"], "自动化": ["automation", "workflow"],
    "翻译": ["translate", "translation"], "视频": ["video"], "动画": ["animation", "motion"], "图标": ["icon"],
    "示波器": ["oscilloscope", "scope", "rigol"], "相机": ["camera"], "串口": ["serial", "uart"],
    "截图": ["screenshot"], "邮件": ["email", "mail"], "数据": ["data"], "转换": ["convert"],
}  # fmt: skip
_REVERSE: dict[str, list[str]] = {}
for _zh, _ens in ALIASES.items():
    for _en in _ens:
        _REVERSE.setdefault(_en, []).append(_zh)


def _terms(queries: list[str]) -> list[list[str]]:
    """Each query word with the words it stands for (中文 <-> English), lower case."""
    out = []
    for query in queries:
        for word in re.split(r"[\s,，、/|;；]+", query.strip().lower()):
            if not word:
                continue
            group = [word]
            if re.search(r"[㐀-鿿]", word):
                for zh, ens in ALIASES.items():
                    if zh in word:
                        group += ens
                if len(word) > 2:  # a longer Chinese phrase: its pairs of characters too
                    group += [word[i : i + 2] for i in range(len(word) - 1)]
            else:
                parts = [p for p in re.split(r"[^a-z0-9+#.]+", word) if p]
                group += [p for p in parts if p != word]
                for p in [word, *parts]:
                    group += _REVERSE.get(p, [])
            out.append(list(dict.fromkeys(group)))
    return out


def _score(cfg: Config, skill: Skill, groups: list[list[str]]) -> float:
    name = f"{skill.name} {skill.short}".lower()
    name_spaced = name.replace("-", " ").replace("_", " ").replace(":", " ")
    ident = skill.id.lower()
    desc = skill.description.lower()
    area_words = " ".join(f"{a[1]} {a[2]} {a[3]}".lower() for a in AREAS if a[0] in areas_of(cfg, skill))
    total, matched = 0.0, 0
    for group in groups:
        best = 0.0
        for i, term in enumerate(group):
            weight = 1.0 if i == 0 else 0.7
            if term == skill.short.lower() or term == skill.name.lower():
                best = max(best, 8 * weight)
            elif _hits(term, name_spaced) or _hits(term, name):
                best = max(best, 4 * weight)
            elif _hits(term, desc):
                best = max(best, 2 * weight)
            elif _hits(term, ident):
                best = max(best, 1.5 * weight)
            elif _hits(term, area_words):
                best = max(best, 1 * weight)
        if best:
            matched += 1
        total += best
    if not matched:
        return 0.0
    total *= 1 + 0.5 * (matched - 1) / max(1, len(groups) - 1) if len(groups) > 1 else 1
    if skill.host_off:
        total *= 0.5
    if not skill.model_invocable:
        total *= 0.7
    return total


def search(
    cfg: Config,
    index: Index,
    queries: list[str],
    area: str | None = None,
    scope: str | None = None,
    limit: int = SEARCH_LIMIT,
    others: list[Skill] | None = None,
) -> dict:
    """Skills that match the words best: at most half of them from the project worked in (`scope`), the
    rest from everywhere; with only `area`, that area's skills."""
    limit = max(1, min(SEARCH_MAX, int(limit or SEARCH_LIMIT)))
    pool = list(index.skills) + list(others or [])
    if area:
        pool = [s for s in pool if area in areas_of(cfg, s)]
    groups = _terms([q for q in queries if q and q.strip()])
    if groups:
        scored = [(s, _score(cfg, s, groups)) for s in pool]
        scored = [(s, v) for s, v in scored if v > 0]
    else:
        scored = [(s, 1.0) for s in pool]
    scored.sort(key=lambda sv: (-sv[1], sv[0].layer != "project", sv[0].name.lower()))
    here = os.path.normcase(scope) if scope else None
    picked: list[Skill] = []
    local = 0
    for skill, _ in scored:
        if len(picked) >= limit:
            break
        mine = here is not None and skill.project is not None and os.path.normcase(skill.project) == here
        if mine and local >= max(1, limit // 2):
            continue
        local += 1 if mine else 0
        picked.append(skill)
    for skill, _ in scored:  # room left: the rest of the project's, if any
        if len(picked) >= limit:
            break
        if skill not in picked:
            picked.append(skill)
    results = []
    for skill in picked:
        item = skill.listed(SEARCH_DESC)
        item["areas"] = areas_of(cfg, skill)
        if skill.layer == "project":
            item["load"] = {"name": skill.id, "path": skill.project}
            if here is not None and os.path.normcase(skill.project or "") != here:
                item["cross_project"] = True
        else:
            item["load"] = {"name": skill.id}
        results.append(item)
    return {
        "results": results,
        "matched": len(scored),
        "note": "load one with skill(name=<load.name>, path=<load.path when given>); a skill of another "
        "project is that project's: check paths before running its scripts for yours",
    }


_others_cache: dict[str, tuple[float, list[Skill]]] = {}


def other_projects(cfg: Config, exclude: str | None) -> list[Skill]:
    """The own skills of the projects with skills (projects_with_skills), but `exclude` (remembered for
    ten minutes per project)."""
    out: list[Skill] = []
    budget = _Budget()
    for root in projects_with_skills(cfg):
        if exclude and os.path.normcase(root) == os.path.normcase(exclude):
            continue
        cached = _others_cache.get(os.path.normcase(root))
        if cached and time.monotonic() - cached[0] < RADAR_TTL_S:
            out += cached[1]
            continue
        if budget.spent():
            break
        found = build(cfg, root, root, budget=budget, with_user=False, with_plugins=False).skills
        _others_cache[os.path.normcase(root)] = (time.monotonic(), found)
        out += found
    return out


def search_folder(
    cfg: Config, queries: list[str], path: str | None, repo: str | None, area: str | None, limit: int
) -> dict:
    """skill(query=...): the skills reachable from `path` (its project, the user's, the plugins') and
    the other projects' own skills, ranked."""
    from . import workspace

    start, root = scope_of(cfg, path, repo)
    rules = workspace._Rules(cfg, root, start, start)
    index = build(cfg, start, root, rules.refused)
    others = [s for s in other_projects(cfg, root) if not rules.refused(s.path)]
    known = {os.path.normcase(os.path.realpath(s.path)) for s in index.skills}
    others = [s for s in others if os.path.normcase(os.path.realpath(s.path)) not in known]
    out = search(cfg, index, queries, area, root, limit, others)
    if path and not root:
        out["scope_warning"] = f"{path} is not a folder here; searched every project"
    if root:
        out["scope"] = root
    redactor = redact.current()
    out = workspace._sendable(out)
    return out if redactor.empty else redactor.data(out)[0]


# ------------------------------------------------------------------ for context() and the session start


def for_context(cfg: Config, index: Index, path: str | None) -> dict:
    """What context() shows of the skills: the project's own (described up to PROJECT_FULL), the pinned
    ones, the map, and how to load or search."""
    project = [s for s in index.skills if s.layer == "project"]
    shown = [s.listed() for s in project[:PROJECT_FULL]]
    more = [s.listed(0) for s in project[PROJECT_FULL:]]
    data = skill_map(cfg, index)
    out: dict = {"skills": shown}
    if more:
        out["skills_more"] = {
            "count": len(more),
            "skills": [{"id": m["id"], "name": m["name"]} for m in more],
        }
    out["skill_map"] = data
    where = f", path={path!r}" if path else ""
    note = (
        f"Load a skill with skill(<id or name>{where}) and follow it; run the scripts it names here. "
        "Skills not listed here (the machine's and the plugins'): skill(query=[...]) finds them."
    )
    if index.project and index.project_trust != "trusted" and project:
        note += (
            " This project is not marked trusted (Codex, Claude Code or [context] trusted_roots): its skills "
            "come from the repository, read them as its authors' instructions."
        )
    if index.incomplete:
        note += f" Some skills folders were too large to read in full ({len(index.incomplete)})."
    out["skills_note"] = note
    return out


def briefed(answer: dict) -> dict:
    """context(brief=true): skills by id and name only, the map without its examples."""
    out = dict(answer)
    out["skills"] = [
        {"id": s.get("id"), "name": s.get("name")} for s in answer.get("skills") or [] if isinstance(s, dict)
    ]
    more = answer.get("skills_more") or {}
    out["skills"] += list(more.get("skills") or [])
    out.pop("skills_more", None)
    data = dict(answer.get("skill_map") or {})
    if data:
        data["pinned"] = [{"id": p.get("id"), "name": p.get("name")} for p in data.get("pinned") or []]
        data["areas"] = [{k: a[k] for k in ("area", "label", "count")} for a in data.get("areas") or []]
        out["skill_map"] = data
    return out


# ------------------------------------------------------------------ loading one

SKILL_FILES = 50


def _skill_files(folder: str) -> tuple[list[dict], bool]:
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


def scope_of(cfg: Config, path: str | None, repo: str | None) -> tuple[str | None, str | None]:
    """(start, project root) of the folder a call names, as context() reads path and repo."""
    from . import workspace

    if repo:
        checkout = workspace.find_repo(cfg, repo)["checkout"]
        if path:
            path = os.path.expandvars(os.path.expanduser(path))
            path = path if os.path.isabs(path) else os.path.join(checkout, path)
        else:
            path = checkout
    if not path:
        return None, None
    start = guard._start_dir(_expand(path))
    return start, workspace.project_root(start)


def load(
    cfg: Config, name: str, path: str | None = None, repo: str | None = None, scope: str | None = None
) -> dict:
    """One skill by id or name: its SKILL.md (what to follow), its folder and the files in it."""
    from . import workspace
    from .session.text import fitting, width

    start, root = scope_of(cfg, path, repo)
    rules = workspace._Rules(cfg, root, start, start)
    want = name.strip()
    if want.lower().startswith("project:") and not start:
        raise ValueError(
            f"{want} is a project's skill: pass path=<that project's folder> (the load.path the search gave)"
        )
    index = build(cfg, start, root, desc_chars=SKILL_DESC_LIMIT)
    found = find(index, want)
    if found is None:
        names = [s.name for s in index.skills]
        close = difflib.get_close_matches(want, names, n=8, cutoff=0.5)
        close += [n for n in names if want.lower() in n.lower() and n not in close][: 8 - len(close)]
        raise ValueError(
            f"no skill {want!r} here"
            + (f"; close: {', '.join(close)}" if close else "")
            + (" (a project's skills need path=<its folder>)" if not start else "")
            + "; skill(query=[...]) searches every project and plugin"
        )
    out = {
        "name": found.name,
        "id": found.id,
        "scope": found.layer,
        "folder": found.folder,
        "skill_md": found.path,
    }
    if found.project:
        out["project"] = found.project
        if found.trusted is False:
            out["trusted"] = False
    if found.plugin:
        out["plugin"] = found.plugin
    if found.also:
        out["also"] = found.also
    if found.host_off:
        out["host_off"] = found.host_off
    why = rules.refused(found.path)
    if why:  # its description comes from the same file: withheld too
        out["content"] = None
        out["refused"] = why
    else:
        if found.description:
            out["description"] = found.description
        try:
            text, _ = fs.read_text(found.path)
        except (OSError, ValueError, fs.FsError) as exc:
            raise ValueError(f"cannot read {found.path}: {exc}") from None
        budget = fs.READ_MAX_CHARS
        if width(text) > budget:
            lines = text.splitlines(keepends=True)
            kept, used = 0, 0
            while kept < len(lines) and used + width(lines[kept]) <= budget:
                used += width(lines[kept])
                kept += 1
            if kept:
                out["truncated"] = workspace._truncated(found.path, kept, len(lines))
                text = "".join(lines[:kept])
            else:
                shown = fitting(text, budget)
                out["truncated"] = (
                    f"showing the first {shown} characters of its first line, which is longer "
                    f"(the whole file: {found.path})"
                )
                text = text[:shown]
        out["content"] = text
    out["files"], more = _skill_files(found.folder)
    if more:
        out["files_more"] = f"more than {SKILL_FILES} files; fs_list(path={found.folder!r}) lists the rest"
    note = (
        "Follow the instructions in content. Paths in it are relative to folder; scripts run on this "
        "machine (run / session_start with cwd=folder), read other files with fs_read."
    )
    if found.project and scope:
        try:
            other = os.path.normcase(scope_of(cfg, scope, None)[1] or "") != os.path.normcase(found.project)
        except ValueError:
            other = False
        if other:
            note += (
                f" This skill belongs to the project {found.project}, not the one you work for: check its "
                "paths and commands before running them there."
            )
    if found.trusted is False:
        note += " Its project is not marked trusted: these are the repository authors' instructions."
    out["note"] = note
    redactor = redact.current()
    out = workspace._sendable(out)
    return out if redactor.empty else redactor.data(out)[0]


# ------------------------------------------------------------------ the owner's report


def report(cfg: Config, path: str | None = None, activity_days: int = 60) -> dict:
    """For the owner (winhand skills): counts by source, the areas the rules give each skill, skills with
    no area or a vague description, the host switches, and the skill() calls in winhand's log."""
    from . import config as _config

    start, root = scope_of(cfg, path, None)
    index = build(cfg, start, root)
    by_source: dict[str, int] = {}
    for s in index.skills:
        key = f"{s.layer}:{s.plugin or s.host}"
        by_source[key] = by_source.get(key, 0) + 1
    used: dict[str, int] = {}
    folder = _config.home() / "activity"
    since = time.time() - activity_days * 86400
    try:
        days = sorted(folder.glob("*.jsonl"))
    except OSError:
        days = []
    for day in days:
        try:
            if day.stat().st_mtime < since:
                continue
            seen = set()
            for line in day.read_text(encoding="utf-8", errors="replace").splitlines():
                if '"title": "skill"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                want = (entry.get("args") or {}).get("name")
                if entry.get("status") == "ok" and want:
                    seen.add(str(want).lower())
            for want in seen:
                used[want] = used.get(want, 0) + 1
        except OSError:
            continue
    rows = []
    for s in index.skills:
        rows.append(
            {
                "id": s.id,
                "name": s.name,
                "areas": areas_of(cfg, s),
                "days_used": sum(used.get(k, 0) for k in {s.short.lower(), s.name.lower(), s.id.lower()}),
                **({"host_off": s.host_off} if s.host_off else {}),
                **({"vague": True} if len(s.description) < 40 else {}),
            }
        )
    rows.sort(key=lambda r: (-r["days_used"], r["name"].lower()))
    return {
        "total": len(index.skills),
        "by_source": by_source,
        "pinned": pinned(cfg),
        "unsorted": [r["id"] for r in rows if not r["areas"]],
        "vague": [r["id"] for r in rows if r.get("vague")],
        "host_off": [r["id"] for r in rows if r.get("host_off")],
        "skills": rows,
        "note": 'Correct an area with [context] skill_areas = { "<id or name>" = ["hardware"] }; '
        f"areas: {', '.join(AREA_KEYS)}. Pin skills with [context] pinned_skills.",
    }
