"""The owner's Codex and Claude Code PreToolUse hooks, run before winhand acts.

Only the hooks the tool that owns them would run:
- Codex: managed hooks, and the others whose `trusted_hash` (in ~/.codex/config.toml, hooks.state)
  matches what Codex would compute now; a project's hooks also need the project to be trusted;
- Claude Code: managed hooks, and the others once the project's workspace trust was accepted.

Each action is shown to a hook the way that tool shows its own calls (Bash, Write, Edit, Read; for
Codex apply_patch), and the answer is read the way that tool reads it: exit code 2 or a deny
refuses, Claude Code's ask asks the person, additionalContext becomes a note, and a hook that fails
lets the call through with a note, as both tools do. A hook's wish to rewrite the call
(updatedInput) refuses it with the rewrite in the reason: winhand does not change what the model
sent. The facts these rules come from are in docs/comet/changes/client-hooks/research.md.
"""

from __future__ import annotations

import concurrent.futures
import glob as globlib
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field

from . import config, proc, workspace
from .actions import Action

SOURCES = ("codex", "claude-code")
DEFAULT_TIMEOUT_S = 30.0
HOOK_TIMEOUT_S = 600  # both tools' default
OUTPUT_CHARS = 2_000
MAX_CONFIG_BYTES = 4_000_000
IS_WINDOWS = os.name == "nt"

# ------------------------------------------------------------------ settings

_settings: tuple[object, dict] | None = None


def settings() -> dict:
    """[hooks] of winhand's config.toml, read again when the file changes."""
    global _settings
    try:
        stamp: object = config.config_path().stat().st_mtime_ns
    except OSError:
        stamp = None
    if _settings is None or _settings[0] != stamp:
        try:
            value = dict(config.load().hooks)
        except (OSError, ValueError):
            value = {}
        _settings = (stamp, value)
    return _settings[1]


def sources(cfg: dict | None = None) -> list[str]:
    value = (settings() if cfg is None else cfg).get("sources", list(SOURCES))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return list(SOURCES)
    return [s for s in dict.fromkeys(str(v).strip().lower() for v in value) if s in SOURCES]


def timeout_cap(cfg: dict | None = None) -> float:
    value = (settings() if cfg is None else cfg).get("timeout_s", DEFAULT_TIMEOUT_S)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return DEFAULT_TIMEOUT_S
    return max(1.0, float(value))


# ------------------------------------------------------------------ reading configuration files

_files: dict[tuple[str, str], tuple[tuple, object, str | None]] = {}


def _load(path: str, kind: str) -> tuple[object, str | None]:
    """(data, error) of a JSON or TOML file; (None, None) when it does not exist. Cached by size and
    modification time. Kind "codex-json" is JSON read as Codex reads it: a byte order mark is an
    error there (serde_json does not skip it)."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None, None
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _files.get((path, kind))
    if cached and cached[0] == stamp:
        return cached[1], cached[2]
    data: object = None
    error = None
    try:
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        with open(path, "rb") as fh:
            raw = fh.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError(f"larger than {MAX_CONFIG_BYTES:,} bytes")
        if kind == "codex-json":
            text = raw.decode("utf-8")
            if text.startswith("\ufeff"):
                raise ValueError("it starts with a byte order mark, which Codex does not accept")
        else:
            text = raw.decode("utf-8-sig")
        if kind == "toml":
            data = tomllib.loads(text)
        elif kind == "codex-json":
            data = json.loads(
                text, parse_constant=_no_constant, parse_float=_serde_float, parse_int=_serde_int
            )
        else:
            data = json.loads(text, parse_constant=_no_constant)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        error = f"cannot be read: {_clip(str(exc))}"
    _files[(path, kind)] = (stamp, data, error)
    return data, error


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


class _OutOfRange:
    """A number a float cannot hold (1e400, a 309-digit integer). Codex is built with serde_json's
    arbitrary_precision, so such a number never makes it drop a file; it only fails where a field
    wants a whole number (timeout) or a string, as this marker does."""

    def __repr__(self) -> str:
        return "<number out of range>"


OUT_OF_RANGE = _OutOfRange()


def _serde_float(text: str) -> float | _OutOfRange:
    value = float(text)
    return value if math.isfinite(value) else OUT_OF_RANGE


def _serde_int(text: str) -> int | float | _OutOfRange:
    """A whole number as Codex's typed fields see it: -0 and anything past u64 / i64 are not whole
    numbers there (kept as floats, or the marker past what a float holds)."""
    if text == "-0":
        return -0.0
    try:
        value = int(text)
    except ValueError:  # longer than Python allows: far past what a float holds
        return OUT_OF_RANGE
    if -(2**63) <= value < 2**64:
        return value
    try:
        return float(value)
    except OverflowError:
        return OUT_OF_RANGE


def _clip(text: str, limit: int = 300) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


# ------------------------------------------------------------------ hooks


@dataclass
class Hook:
    source: str  # codex | claude-code | guards (a command rule in guards.toml)
    file: str
    group: int
    index: int
    matcher: str | None
    handler: dict
    status: str = "untrusted"  # managed | trusted | untrusted | modified | disabled | unsupported
    reason: str = ""

    @property
    def key(self) -> str:
        if self.source == "codex":
            return f"{self.file}:pre_tool_use:{self.group}:{self.index}"
        if self.source == "guards":
            return f"{self.file} rule {self.group}"
        return f"{self.file} hooks.PreToolUse[{self.group}].hooks[{self.index}]"

    @property
    def runs(self) -> bool:
        return self.status in ("managed", "trusted")

    @property
    def command(self) -> str:
        h = self.handler
        if self.source == "codex" and IS_WINDOWS:
            return _codex_windows_command(h)
        return str(h.get("command") or "")

    def label(self) -> str:
        who = {"codex": "Codex hook", "claude-code": "Claude Code hook", "guards": "guard command"}[
            self.source
        ]
        return f"{who} {self.key}"

    def summary(self) -> dict:
        out: dict = {
            "source": self.source,
            "file": self.file,
            "key": self.key,
            "matcher": self.matcher,
            "command": self.command[:300] + ("…" if len(self.command) > 300 else ""),
            "status": self.status,
            "runs": self.runs,
        }
        if self.reason:
            out["reason"] = self.reason
        return out


def _codex_windows_command(h: dict) -> str:
    """Codex on Windows: commandWindows (or command_windows) when set, even to "", else command."""
    for key in ("commandWindows", "command_windows"):
        if h.get(key) is not None:
            return str(h[key])
    return str(h.get("command") or "")


def _groups(data: object) -> list | str:
    if not isinstance(data, dict):
        return "the top level is not a table"
    hooks = data.get("hooks")
    if hooks is None or hooks == []:
        return []
    if not isinstance(hooks, dict):
        return "`hooks` is not a table"
    groups = hooks.get("PreToolUse")
    if groups is None:
        return []
    if not isinstance(groups, list) or not all(isinstance(g, dict) for g in groups):
        return "`hooks.PreToolUse` is not a list of tables"
    return groups


def _from_file(source: str, path: str, kind: str) -> tuple[list[Hook], list[dict]]:
    data, error = _load(path, "codex-json" if (source, kind) == ("codex", "json") else kind)
    if error:
        return [], [{"file": path, "error": error}]
    if data is None:
        return [], []
    if source == "codex":
        problem = _codex_file_problem(data, kind)
        if problem:  # Codex rejects the whole file, so none of its hooks run there either
            return [], [{"file": path, "error": _clip(f"Codex would not load it: {problem}")}]
    groups = _groups(data)
    if isinstance(groups, str):
        return [], [{"file": path, "error": groups}]
    out = []
    for g, group in enumerate(groups):
        matcher = group.get("matcher")
        if matcher is not None:
            matcher = str(matcher)
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        for i, handler in enumerate(handlers):
            if isinstance(handler, dict):
                out.append(Hook(source, path, g, i, matcher, handler))
    return out, []


# Codex reads a hooks file with serde (codex-rs/config/src/hook_config.rs): only the top level of
# hooks.json refuses keys it does not know; groups and handlers ignore them, and so do the events.
# Anything else it cannot read (a wrong type, an unknown handler type, a missing command) makes it
# drop the whole file.
_CODEX_EVENTS = (
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "SubagentStart",
    "SubagentStop",
    "Stop",
    "Interrupt",
)


def _codex_file_problem(data: object, kind: str) -> str | None:
    """Why Codex would refuse this hooks.json (kind "json") or the [hooks] of this config.toml or
    requirements.toml (kind "toml"); None when it would load it."""
    if not isinstance(data, dict):
        return "the top level is not a table"
    if kind == "json":
        unknown = sorted(set(data) - {"description", "hooks"})
        if unknown:
            return f"unknown top-level key(s) {unknown[:5]}"
        if data.get("description") is not None and not isinstance(data["description"], str):
            return "description is not a string"
        if "hooks" in data and data["hooks"] is None:
            return "`hooks` is null"
    if "hooks" not in data or data["hooks"] == []:
        return None
    events = data["hooks"]
    if not isinstance(events, dict):
        return "`hooks` is not a table"
    for event in _CODEX_EVENTS:
        if event not in events:
            continue
        groups = events[event]
        if not isinstance(groups, list):
            return f"`{event}` is not a list"
        for g, group in enumerate(groups):
            where = f"{event} group {g}"
            if not isinstance(group, dict):
                return f"{where} is not a table"
            if group.get("matcher") is not None and not isinstance(group["matcher"], str):
                return f"{where}: matcher is not a string"
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                return f"{where}: hooks is not a list"
            for i, handler in enumerate(handlers):
                problem = _codex_handler_problem(handler)
                if problem:
                    return f"{where} hook {i}: {problem}"
    return None


def _codex_handler_problem(h: object) -> str | None:
    if not isinstance(h, dict):
        return "it is not a table"
    kind = h.get("type")
    if not isinstance(kind, str):
        return "it has no type"
    if kind not in ("command", "mcp_tool", "prompt", "agent"):
        return f"unknown type {_clip(repr(kind), 60)}"
    strings: tuple[str, ...] = ("statusMessage",)
    if kind == "command":
        if not isinstance(h.get("command"), str):
            return "command is missing or not a string"
        if "commandWindows" in h and "command_windows" in h:
            return "both commandWindows and command_windows are set"
        strings += ("commandWindows", "command_windows")
        if "async" in h and not isinstance(h["async"], bool):
            return "async is not true or false"
        if not _unsigned(h.get("additionalContextLimit")):
            return "additionalContextLimit is not a whole number"
    elif kind == "mcp_tool":
        for key in ("server", "tool"):
            if not isinstance(h.get(key), str):
                return f"{key} is missing or not a string"
        if "input" in h and not (isinstance(h["input"], dict) and _toml_value(h["input"])):
            return "input is not a table TOML can hold"
    if kind in ("command", "mcp_tool"):
        if not _unsigned(h.get("timeout")):
            return "timeout is not a whole number of seconds"
        for key in strings:
            if h.get(key) is not None and not isinstance(h[key], str):
                return f"{key} is not a string"
    return None


def _unsigned(value: object) -> bool:
    """None or a whole number Codex keeps as u64."""
    return value is None or (isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 2**64)


def _toml_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return all(_toml_value(v) for v in value.values())
    if isinstance(value, list):
        return all(_toml_value(v) for v in value)
    if isinstance(value, float):  # only TOML gives inf / nan here, which serde_json cannot hold
        return math.isfinite(value)
    return True


def _unsupported(hook: Hook) -> str | None:
    h = hook.handler
    kind = h.get("type", "command")
    if kind != "command":
        return f"`type = {kind!r}` hooks are not run by winhand (only command hooks are)"
    # Codex ignores keys it does not know; async hooks cannot refuse anything there
    for key in ("async",) if hook.source == "codex" else ("async", "asyncRewake", "if", "once"):
        if h.get(key):
            return f"hooks with `{key}` are not run by winhand"
    # a field set to null counts as not set, as in Codex (serde's Option)
    for key in ("command", "commandWindows", "command_windows", "statusMessage"):
        if h.get(key) is not None and not isinstance(h[key], str):
            return f"`{key}` is not a string"
    if not hook.command.strip():
        return "it has no command"
    if hook.source == "claude-code" and h.get("args") is not None and not isinstance(h["args"], list):
        return "`args` is not a list"
    timeout = h.get("timeout")
    if timeout is not None:
        whole = isinstance(timeout, int) and not isinstance(timeout, bool)
        number = whole or isinstance(timeout, float)
        if not number or not math.isfinite(timeout) or timeout < 0 or (hook.source == "codex" and not whole):
            return "`timeout` is not a number of seconds" + (
                " (Codex wants a whole number)" if number and hook.source == "codex" else ""
            )
    matcher = hook.matcher
    if not _matches_all(hook) and not _EXACT[_dialect(hook)].fullmatch(matcher or ""):
        try:
            re.compile(matcher)
        except re.error as exc:
            return f"its matcher is not a valid regular expression ({exc}); the tool skips this group too"
    return None


def _dialect(hook: Hook) -> str:
    return "codex" if hook.source == "codex" else "claude-code"


def _seconds(hook: Hook, cap: float) -> float:
    """How long a hook may take: its own timeout (Codex: at least 1 s), never more than `cap`."""
    value = hook.handler.get("timeout")
    if (
        value is None
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        value = HOOK_TIMEOUT_S if hook.source != "guards" else cap
    elif hook.source == "codex":
        value = max(value, 1)  # Codex: timeout.max(1)
    elif value <= 0:
        value = HOOK_TIMEOUT_S if hook.source != "guards" else cap
    return min(float(value), cap)


def _folder_key(path: str) -> str:
    return workspace._folder_key(path)


def _under(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([_folder_key(path), _folder_key(folder)]) == _folder_key(folder)
    except ValueError:
        return False


# ------------------------------------------------------------------ Codex


def codex_system_dir() -> str:
    if IS_WINDOWS:
        return os.path.join(os.environ.get("ProgramData") or r"C:\ProgramData", "OpenAI", "Codex")
    return "/etc/codex"


def codex_hash(matcher: str | None, handler: dict) -> str:
    """The hash Codex keeps as trusted_hash for a command hook (codex-rs/hooks discovery.rs and
    config/src/fingerprint.rs): the normalized hook as JSON with sorted keys, compact, SHA-256."""
    if IS_WINDOWS:
        command = _codex_windows_command(handler)
    else:
        command = handler.get("command")
    timeout = handler.get("timeout")
    normalized: dict = {
        "type": "command",
        "command": command,
        "timeout": HOOK_TIMEOUT_S if timeout is None else max(timeout, 1),
        "async": bool(handler.get("async", False)),
    }
    if handler.get("statusMessage") is not None:
        normalized["statusMessage"] = handler["statusMessage"]
    limit = handler.get("additionalContextLimit")
    if limit is not None and limit != 2500:
        normalized["additionalContextLimit"] = limit
    identity: dict = {"event_name": "pre_tool_use", "hooks": [normalized]}
    if matcher is not None:
        identity["matcher"] = matcher
    return fingerprint(identity)


def fingerprint(value) -> str:
    """Codex's version_for_toml: sorted keys, compact JSON, SHA-256."""
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _state_key(key: str) -> str:
    """A hooks.state key with its file part compared the way the file system does."""
    parts = key.rsplit(":", 3)
    if len(parts) != 4:
        return key
    return ":".join([os.path.normcase(os.path.normpath(parts[0])), *parts[1:]])


def codex_legacy_managed_config() -> str:
    """The legacy managed config.toml, a managed layer above all others (config/src/loader)."""
    if IS_WINDOWS:
        return os.path.join(workspace.codex_home(), "managed_config.toml")
    return "/etc/codex/managed_config.toml"


def _codex_user() -> tuple[dict, dict[str, str], list[dict]]:
    """hooks.state (by normalized key) and errors from the user's config.toml, where Codex keeps
    them, and each project's trust_level (by folder) from the system and user config.toml: never
    from a project's own files."""
    path = os.path.join(workspace.codex_home(), "config.toml")
    data, error = _load(path, "toml")
    errors = [{"file": path, "error": error}] if error else []
    levels: dict[str, str] = {}
    for source in (os.path.join(codex_system_dir(), "config.toml"), path, codex_legacy_managed_config()):
        found, _ = _load(source, "toml")
        projects = found.get("projects") if isinstance(found, dict) else None
        for k, v in (projects if isinstance(projects, dict) else {}).items():
            if isinstance(v, dict) and isinstance(v.get("trust_level"), str):
                levels[_folder_key(str(k))] = v["trust_level"]
    if not isinstance(data, dict):
        return {}, levels, errors
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    raw_state = hooks.get("state") if isinstance(hooks.get("state"), dict) else {}
    state = {_state_key(str(k).strip()): v for k, v in raw_state.items() if isinstance(v, dict)}
    return state, levels, errors


def _codex_hooks_on(config_files: list[str], requirements: object) -> bool:
    """Codex's `hooks` feature: on by default; [features] of its config layers merged, low to high
    (system, user, trusted projects, the legacy managed file), then what requirements.toml pins.
    Within one table Codex applies the keys in order, so `hooks` wins over the old `codex_hooks`."""
    merged: dict[str, bool] = {}
    for path in config_files:
        data, _ = _load(path, "toml")
        features = data.get("features") if isinstance(data, dict) else None
        if isinstance(features, dict):
            merged.update(
                {k: features[k] for k in ("codex_hooks", "hooks") if isinstance(features.get(k), bool)}
            )
    pinned = requirements.get("features") if isinstance(requirements, dict) else None
    on = True
    for table in (merged, pinned if isinstance(pinned, dict) else {}):
        for key in ("codex_hooks", "hooks"):
            if isinstance(table.get(key), bool):
                on = table[key]
    return on


def _spellings(path: str) -> list[str]:
    """`path` and, when links make it differ, the path they lead to."""
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return [path]
    return [path] if _folder_key(real) == _folder_key(path) else [path, real]


def _state_files(path: str, layer: str) -> list[str]:
    """The spellings of a hooks file Codex may have keyed its hooks.state with: as written, and with
    the folder that holds it (a project's folder above .codex, the Codex home) reached through
    links. The .codex folder and the file themselves are never followed: a link there would let a
    repository borrow the trust of another project's hook."""
    holder = os.path.dirname(os.path.dirname(path)) if layer == "project" else os.path.dirname(path)
    rest = os.path.relpath(path, holder)
    return list(dict.fromkeys([path, *(os.path.join(s, rest) for s in _spellings(holder))]))


def _codex_folder_trusted(candidates: list[str], levels: dict[str, str]) -> bool:
    """Codex decides a .codex folder's trust by the first of these with a trust_level: the folder,
    the project root, the repository's main checkout (loader decision_for_dir); each looked up by
    its real path first, then as written."""
    for folder in candidates:
        for spelling in reversed(_spellings(folder)):
            level = levels.get(_folder_key(spelling))
            if level is not None:
                return level == "trusted"
    return False


def _codex(root: str, chain: list[str]) -> tuple[list[Hook], list[dict]]:
    system = codex_system_dir()
    requirements = os.path.join(system, "requirements.toml")
    req, req_error = _load(requirements, "toml")
    errors = [{"file": requirements, "error": req_error}] if req_error else []
    managed_only = isinstance(req, dict) and req.get("allow_managed_hooks_only") is True
    state, levels, user_errors = _codex_user()
    errors += user_errors
    home = workspace.codex_home()
    legacy = codex_legacy_managed_config()
    main = _main_checkout(root)
    layers: list[tuple[str, str, str, bool]] = [
        ("managed", requirements, "toml", True),
        ("managed", os.path.join(system, "hooks.json"), "json", True),
        ("managed", os.path.join(system, "config.toml"), "toml", True),
        ("user", os.path.join(home, "hooks.json"), "json", True),
        ("user", os.path.join(home, "config.toml"), "toml", True),
    ]
    config_files = [os.path.join(system, "config.toml"), os.path.join(home, "config.toml")]
    for folder in chain:
        dot = os.path.join(folder, ".codex")
        if not os.path.isdir(dot) or _folder_key(dot) == _folder_key(home):
            continue  # Codex needs the folder, and does not load its own home twice
        trusted = _codex_folder_trusted([folder, root, main], levels)
        if trusted:
            config_files.append(os.path.join(dot, "config.toml"))
        # a linked worktree takes its hooks from the same folder of the main checkout
        hooks_dir = dot if main == root else os.path.normpath(os.path.join(main, os.path.relpath(dot, root)))
        layers.append(("project", os.path.join(hooks_dir, "hooks.json"), "json", trusted))
        layers.append(("project", os.path.join(hooks_dir, "config.toml"), "toml", trusted))
    layers.append(("managed", legacy, "toml", True))
    config_files.append(legacy)
    switched_on = _codex_hooks_on(config_files, req)
    hooks: list[Hook] = []
    for layer, path, kind, trusted in layers:
        found, errs = _from_file("codex", path, kind)
        errors += errs
        for hook in found:
            try:
                _codex_status(hook, layer, trusted, state, managed_only)
            except Exception as exc:  # a strange file must not stop the others
                hook.status, hook.reason = "unsupported", f"cannot be read ({exc})"
            if not switched_on and hook.runs:
                hook.status, hook.reason = (
                    "disabled",
                    "hooks are turned off in Codex ([features] hooks = false)",
                )
            hooks.append(hook)
    return hooks, errors


def _codex_status(hook, layer, folder_trusted, state, managed_only) -> None:
    why = _unsupported(hook)
    entry = next(
        (
            state[k]
            for f in _state_files(hook.file, layer)
            if (k := _state_key(f"{f}{hook.key[len(hook.file) :]}")) in state
        ),
        {},
    )
    if why:
        hook.status, hook.reason = "unsupported", why
    elif layer == "managed":
        hook.status = "managed"
    elif managed_only:
        hook.status = "disabled"
        hook.reason = "only managed hooks may run (allow_managed_hooks_only in Codex's requirements.toml)"
    elif entry.get("enabled") is False:
        hook.status, hook.reason = "disabled", "turned off in Codex"
    elif not folder_trusted:
        hook.status = "untrusted"
        hook.reason = "this folder is not trusted in Codex ([projects] in ~/.codex/config.toml)"
    else:
        trusted_hash = entry.get("trusted_hash")
        if trusted_hash == codex_hash(hook.matcher, hook.handler):
            hook.status = "trusted"
        elif trusted_hash:
            hook.status = "modified"
            hook.reason = "changed since it was trusted in Codex; review it there (/hooks)"
        else:
            hook.status = "untrusted"
            hook.reason = "not trusted in Codex yet; review it there (/hooks)"


# ------------------------------------------------------------------ Claude Code


def claude_managed_dir() -> str:
    if IS_WINDOWS:
        return os.path.join(os.environ.get("ProgramFiles") or r"C:\Program Files", "ClaudeCode")
    if sys.platform == "darwin":
        return "/Library/Application Support/ClaudeCode"
    return "/etc/claude-code"


def _claude_trusted(root: str) -> bool:
    """Workspace trust accepted for the project (for a linked worktree, also its main checkout,
    where Claude Code keeps it), or for a folder above it when the project is not a repository of
    its own (trust covers subfolders, not nested repositories)."""
    own_repo = os.path.exists(os.path.join(root, ".git"))
    exact = {_folder_key(s) for f in (root, _main_checkout(root)) for s in _spellings(f)}
    for folder, entry in workspace._claude_projects().items():
        if not isinstance(entry, dict) or entry.get("hasTrustDialogAccepted") is not True:
            continue
        if _folder_key(str(folder)) in exact or (not own_repo and _under(root, str(folder))):
            return True
    return False


def _claude(root: str) -> tuple[list[Hook], list[dict]]:
    managed_dir = claude_managed_dir()
    managed = [os.path.join(managed_dir, "managed-settings.json")]
    managed += sorted(globlib.glob(os.path.join(globlib.escape(managed_dir), "managed-settings.d", "*.json")))
    local = os.path.join(root, ".claude", "settings.local.json")
    project = os.path.join(root, ".claude", "settings.json")
    user = os.path.join(workspace.claude_home(), "settings.json")
    layers = [("managed", p) for p in managed] + [("project", local), ("project", project), ("user", user)]
    data = {path: _load(path, "json") for _, path in layers}
    managed_data = [data[p][0] for p in managed if isinstance(data[p][0], dict)]
    managed_only = any(d.get("allowManagedHooksOnly") is True for d in managed_data)
    all_off = any(d.get("disableAllHooks") is True for d in managed_data)
    disabled = False
    for _, path in layers:  # most specific setting first: managed, local, project, user
        value = data[path][0]
        if isinstance(value, dict) and "disableAllHooks" in value:
            disabled = value["disableAllHooks"] is True
            break
    trusted = None
    hooks: list[Hook] = []
    errors: list[dict] = []
    for layer, path in layers:
        found, errs = _from_file("claude-code", path, "json")
        errors += errs
        for hook in found:
            why = _unsupported(hook)
            if why:
                hook.status, hook.reason = "unsupported", why
            elif all_off:
                hook.status, hook.reason = (
                    "disabled",
                    "disableAllHooks is set in Claude Code's managed settings",
                )
            elif layer == "managed":
                hook.status = "managed"
            elif managed_only:
                hook.status = "disabled"
                hook.reason = (
                    "only managed hooks may run (allowManagedHooksOnly in Claude Code's managed settings)"
                )
            elif disabled:
                hook.status, hook.reason = "disabled", "disableAllHooks is set in Claude Code's settings"
            else:
                if trusted is None:
                    trusted = _claude_trusted(root)
                if trusted:
                    hook.status = "trusted"
                else:
                    hook.status = "untrusted"
                    hook.reason = "the project's workspace trust has not been accepted in Claude Code"
            hooks.append(hook)
    return hooks, errors


# ------------------------------------------------------------------ finding them for a folder


def _main_checkout(root: str) -> str:
    """The main checkout of a linked git worktree; `root` itself otherwise. As Codex does
    (git-utils trust.rs), only when the records agree: the worktree's git folder sits in
    <common>/worktrees/, points back at this checkout, names that common folder, and the main
    checkout's .git is that common folder."""
    dot = os.path.join(root, ".git")
    try:
        if not os.path.isfile(dot) or os.path.islink(dot) or os.path.getsize(dot) > 64 * 1024:
            return root
        gitdir = _gitdir_file(dot)
        if not gitdir or not os.path.isdir(gitdir) or os.path.islink(gitdir):
            return root
        real = os.path.realpath(gitdir)
        if os.path.basename(os.path.dirname(real)) != "worktrees":
            return root
        common = os.path.dirname(os.path.dirname(real))
        back = _read_small(os.path.join(real, "gitdir"))
        commondir = _read_small(os.path.join(real, "commondir"))
        if not back or not commondir:
            return root
        registered = os.path.join(real, back)
        if os.path.basename(registered) != ".git":
            return root
        if os.path.realpath(os.path.dirname(registered)) != os.path.realpath(root):
            return root
        if os.path.realpath(os.path.join(real, commondir)) != common:
            return root
        main = os.path.dirname(os.path.dirname(os.path.dirname(gitdir)))
        main_dot = os.path.join(main, ".git")
        main_git = main_dot if os.path.isdir(main_dot) else _gitdir_file(main_dot)
        if not main_git or os.path.realpath(main_git) != common:
            return root
        return main
    except (OSError, ValueError):
        return root


def _read_small(path: str) -> str:
    """A git record as Codex reads one: a regular file, not a link, at most 64 KiB; else ""."""
    return workspace.read_git_record(path) or ""


def _gitdir_file(dot: str) -> str | None:
    """The folder a `.git` file points at (`gitdir: <path>`, relative to the file's folder)."""
    text = _read_small(dot)
    if not text.startswith("gitdir:"):
        return None
    target = text[len("gitdir:") :].strip()
    return os.path.normpath(os.path.join(os.path.dirname(dot), target)) if target else None


def discover(folder: str, cfg: dict | None = None) -> tuple[list[Hook], list[dict]]:
    """Every PreToolUse hook that applies in `folder`, with its status, and the files that could
    not be read."""
    enabled = sources(cfg)
    root = workspace.project_root(folder)
    chain = workspace._chain(root, folder)
    hooks: list[Hook] = []
    errors: list[dict] = []
    if "codex" in enabled:
        found, errs = _codex(root, chain)
        hooks += found
        errors += errs
    if "claude-code" in enabled:
        found, errs = _claude(root)
        hooks += found
        errors += errs
    return hooks, errors


def describe(folder: str | None, max_chars: int = 6_000) -> dict:
    """The hooks part of context()."""
    cfg = settings()
    if not sources(cfg):
        return {
            "hooks": [],
            "note": "running Codex and Claude Code hooks is turned off ([hooks] sources = [])",
        }
    if not folder:
        return {"hooks": [], "note": "pass path= to see the hooks that apply in a folder"}
    try:
        hooks, errors = discover(folder, cfg)
    except Exception as exc:  # reported, not raised: context() still answers
        return {"hooks": [], "errors": [{"file": folder, "error": _clip(f"hooks could not be read ({exc})")}]}
    listed, used = [], 0
    for hook in hooks:
        item = hook.summary()
        used += len(json.dumps(item, ensure_ascii=False)) + 2
        if used > max_chars:
            break
        listed.append(item)
    out: dict = {
        "hooks": listed,
        "note": "hooks with runs=true run before each matching call here, as in Codex / Claude Code",
    }
    if len(listed) < len(hooks):
        out["hooks_more"] = len(hooks) - len(listed)
    if errors:
        shown = []
        for error in errors[:10]:
            item = {"file": error.get("file"), "error": _clip(str(error.get("error")))}
            used += len(json.dumps(item, ensure_ascii=False)) + 2
            if used > max_chars + 4_000:  # a little more room, so the errors are not crowded out
                break
            shown.append(item)
        out["errors"] = shown
        if len(shown) < len(errors):
            out["errors_more"] = len(errors) - len(shown)
    return out


# ------------------------------------------------------------------ matching


_EXACT = {"codex": re.compile(r"[A-Za-z0-9_|]*"), "claude-code": re.compile(r"[A-Za-z0-9_\-, |]*")}


def _matches_all(hook: Hook) -> bool:
    """No matcher, "" or "*". Codex compares the matcher as written (hooks/src/engine/matcher.rs)."""
    matcher = hook.matcher
    if matcher is None:
        return True
    return (matcher if hook.source == "codex" else matcher.strip()) in ("", "*")


def matches(hook: Hook, names: tuple[str, ...]) -> bool:
    if _matches_all(hook):
        return True
    matcher = hook.matcher or ""
    if _EXACT[_dialect(hook)].fullmatch(matcher):
        if hook.source == "codex":
            alternatives = set(matcher.split("|"))
        else:
            alternatives = {a.strip() for a in re.split(r"[|,]", matcher)}
        return any(n in alternatives for n in names)
    try:
        regex = re.compile(matcher)
    except re.error:
        return False
    return any(regex.search(n) for n in names)


# ------------------------------------------------------------------ what a hook is shown


def _ps(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


def _rel(path: str, root: str) -> str:
    """A path as Codex writes it in a patch: relative to the working folder when inside it."""
    if _under(path, root):
        return os.path.relpath(path, root).replace("\\", "/")
    return path


def _patch(action: Action, tool: str, args: dict, root: str) -> str:
    lines = ["*** Begin Patch"]
    for path in action.paths:
        name = _rel(path, root)
        if tool == "fs_write" and action.kind == "write":
            content = str(args.get("content") or "")
            exists = os.path.exists(path)
            lines.append(f"*** {'Update' if exists else 'Add'} File: {name}")
            if exists:
                lines.append("@@")
            lines += ["+" + line for line in content.split("\n")]
        elif tool == "fs_edit" and action.kind == "edit":
            lines.append(f"*** Update File: {name}")
            for old, new in _edits(args):
                lines.append("@@")
                lines += ["-" + line for line in old.split("\n")]
                lines += ["+" + line for line in new.split("\n")]
        else:
            lines.append(f"*** Update File: {name}")
    lines.append("*** End Patch")
    return "\n".join(lines)


def _edits(args: dict) -> list[tuple[str, str]]:
    if isinstance(args.get("edits"), list):
        return [
            (str(e.get("old") or ""), str(e.get("new") or "")) for e in args["edits"] if isinstance(e, dict)
        ]
    return [(str(args.get("old") or ""), str(args.get("new") or ""))]


def calls(
    action: Action, source: str, tool: str, args: dict, root: str
) -> list[tuple[str, dict, tuple[str, ...]]]:
    """(tool_name, tool_input, names the matcher is tried against) for each call a hook from
    `source` sees for this action; [] when that tool has no such call."""
    bash = ("Bash", "PowerShell") if source != "codex" else ("Bash",)
    kind = action.kind
    if kind in ("command", "input"):
        return [("Bash", {"command": action.text or ""}, bash)]
    if kind == "delete":
        return [("Bash", {"command": f"Remove-Item -LiteralPath {_ps(p)}"}, bash) for p in action.paths]
    if kind == "move" and action.paths:
        command = f"Move-Item -LiteralPath {_ps(action.paths[0])} -Destination {_ps(action.paths[-1])}"
        return [("Bash", {"command": command}, bash)]
    if kind == "read":
        return [] if source == "codex" else [("Read", {"file_path": p}, ("Read",)) for p in action.paths]
    if kind in ("write", "edit"):
        if source == "codex":
            return [
                (
                    "apply_patch",
                    {"command": _patch(action, tool, args, root)},
                    ("apply_patch", "Write", "Edit"),
                )
            ]
        if kind == "write":
            content = str(args.get("content") or "") if tool == "fs_write" else ""
            return [("Write", {"file_path": p, "content": content}, ("Write",)) for p in action.paths]
        old, new = (_edits(args) or [("", "")])[0] if tool == "fs_edit" else ("", "")
        return [
            (
                "Edit",
                {
                    "file_path": p,
                    "old_string": old,
                    "new_string": new,
                    "replace_all": bool(args.get("replace_all")),
                },
                ("Edit",),
            )
            for p in action.paths
        ]
    return []


def payload(hook: Hook, tool_name: str, tool_input: dict, cwd: str) -> dict:
    out = {
        "session_id": "winhand",
        "cwd": cwd,
        "hook_event_name": "PreToolUse",
        "permission_mode": "default",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": "winhand-" + uuid.uuid4().hex,
    }
    if hook.source == "codex":
        out |= {"turn_id": "winhand-" + uuid.uuid4().hex, "model": "winhand", "transcript_path": None}
    return out


# ------------------------------------------------------------------ running one


@dataclass
class Run:
    code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def _keep_exit_code(command: str) -> str:
    """`pwsh -Command` turns a program's exit code 2 into 1, and a hook that refuses with 2 would be
    let through. When the last command failed, winhand passes its code on (1 when it has none, as
    pwsh would); a command that succeeded, or that exits itself, is left as it is."""
    return command + "\nif (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }"


def argv(hook: Hook) -> list[str] | str:
    """How the tool that owns the hook starts it."""
    command = hook.command
    h = hook.handler
    if hook.source == "claude-code" and isinstance(h.get("args"), list):
        return [command, *[str(a) for a in h["args"]]]  # exec form: no shell
    if hook.source == "codex" or hook.source == "guards":
        if IS_WINDOWS:
            shell = _powershell()
            if shell:
                return [shell, "-NoProfile", "-Command", _keep_exit_code(command)]
            return f'"{os.environ.get("COMSPEC") or "cmd.exe"}" /C "{command}"'
        if hook.source == "guards":
            return ["sh", "-c", command]
        return [os.environ.get("SHELL") or "/bin/sh", "-c", command]
    # Claude Code: Git Bash on Windows (PowerShell without it, or when asked), sh elsewhere
    if IS_WINDOWS:
        bash = None if h.get("shell") == "powershell" else proc._git_bash(None)
        if bash:
            return [bash, "-c", command]
        return [_powershell() or "powershell", "-NoProfile", "-Command", _keep_exit_code(command)]
    if h.get("shell") == "powershell" and _powershell():
        return [_powershell() or "pwsh", "-NoProfile", "-Command", _keep_exit_code(command)]
    return ["sh", "-c", command]


def run(hook: Hook, stdin: dict, cwd: str, timeout: float) -> Run:
    """Run one hook. Its input and output go through files, not pipes, so a process it leaves behind
    cannot hold its answer hostage; whatever it started ends with it (a job object on Windows,
    its process group elsewhere)."""
    env = dict(os.environ)
    if hook.source == "claude-code":
        env["CLAUDE_PROJECT_DIR"] = cwd
    extra: dict = (
        {"creationflags": subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED}
        if IS_WINDOWS
        else {"start_new_session": True}
    )
    with tempfile.TemporaryFile() as fin, tempfile.TemporaryFile() as fout, tempfile.TemporaryFile() as ferr:
        fin.write(json.dumps(stdin, ensure_ascii=False).encode("utf-8", "replace"))
        fin.seek(0)
        try:
            child = subprocess.Popen(
                argv(hook), stdin=fin, stdout=fout, stderr=ferr, cwd=cwd, env=env, **extra
            )
        except (OSError, ValueError) as exc:
            return Run(error=f"could not start: {exc}")
        job = None
        if IS_WINDOWS:
            job = _windows_job(child.pid)  # before it runs: whatever it starts is in the job too
            if not _resume(child.pid):
                _end_all(child, job)
                return Run(error="could not be started (resume failed)")
        try:
            code: int | None = child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            code = None
        finally:
            _end_all(child, job)
        if code is None:
            return Run(timed_out=True)
        fout.seek(0)
        ferr.seek(0)
        out, err = fout.read(MAX_OUTPUT_BYTES), ferr.read(MAX_OUTPUT_BYTES)
    return Run(code=code, stdout=proc._decode(out), stderr=proc._decode(err))


MAX_OUTPUT_BYTES = 1_000_000
_CREATE_SUSPENDED = 0x00000004


def _resume(pid: int) -> bool:
    """Let a process started suspended run (Windows)."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x0800, False, pid)  # PROCESS_SUSPEND_RESUME
        if not handle:
            return False
        try:
            return ntdll.NtResumeProcess(handle) == 0
        finally:
            kernel32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False


def _end_all(child: subprocess.Popen, job) -> None:
    """End the hook (if it still runs) and everything it started."""
    if job is not None:
        _close_job(job)  # kill-on-close: the whole tree, orphans included
    else:
        proc._kill_tree(child.pid)
    if not IS_WINDOWS:
        try:
            os.killpg(child.pid, signal.SIGKILL)  # its own session: orphans included
        except (OSError, ProcessLookupError):
            pass
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _windows_job(pid: int):
    """A job object holding the hook's process, closed (and so ended) with everything in it."""
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", Basic),
                ("IoInfo", ctypes.c_ulonglong * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        handle = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        assigned = bool(handle) and bool(kernel32.AssignProcessToJobObject(job, handle))
        if handle:
            kernel32.CloseHandle(handle)
        if not assigned:
            kernel32.CloseHandle(job)
            return None
        return job
    except (OSError, AttributeError, ValueError):
        return None


def _close_job(job) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(job)


# ------------------------------------------------------------------ reading the answer


_CODEX_FIELDS = {
    "continue",
    "stopReason",
    "suppressOutput",
    "systemMessage",
    "decision",
    "reason",
    "hookSpecificOutput",
}
_CODEX_SPECIFIC = {
    "hookEventName",
    "permissionDecision",
    "permissionDecisionReason",
    "updatedInput",
    "additionalContext",
}


@dataclass
class Outcome:
    decision: str | None = None  # deny | ask | note | None
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def _short(text: str) -> str:
    text = text.strip()
    return text if len(text) <= OUTPUT_CHARS else text[: OUTPUT_CHARS - 1] + "…"


def interpret(hook: Hook, result: Run, timeout: float) -> Outcome:
    claude = hook.source != "codex"  # guard commands speak Claude Code's protocol
    block_on_failure = claude and hook.handler.get("onFailure") == "block"

    def failed(why: str) -> Outcome:
        if block_on_failure:
            return Outcome("deny", f"the hook failed ({why}) and is set to block on failure")
        return Outcome(
            "note", notes=[f"{hook.label()} failed ({why}); the call went ahead, as it would there"]
        )

    if result.error:
        return failed(result.error)
    if result.timed_out:
        return failed(f"no answer within {timeout:g} s")
    if result.code == 2:
        if result.stderr.strip():
            return Outcome("deny", _short(result.stderr))
        return (
            Outcome("deny", "the hook refused without saying why")
            if claude
            else failed("exit 2 without a reason")
        )
    stdout = result.stdout.strip()
    data = None
    if stdout.startswith(("{", "[")):
        try:
            data = json.loads(stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            if result.code == 0 or not claude:
                return failed("its output is not a JSON object")
            data = None
    if result.code != 0 and data is None:
        return failed(f"exit code {result.code}")
    if result.code != 0 and not claude:
        return failed(f"exit code {result.code}")
    if data is None:
        return Outcome()
    specific = data.get("hookSpecificOutput") if isinstance(data.get("hookSpecificOutput"), dict) else {}
    if not claude:
        if (
            set(data) - _CODEX_FIELDS
            or set(specific) - _CODEX_SPECIFIC
            or ("hookSpecificOutput" in data and not isinstance(data["hookSpecificOutput"], dict))
        ):
            return failed("its output has fields Codex does not accept")
        if data.get("continue") is False or "stopReason" in data or data.get("suppressOutput") is True:
            return failed("continue, stopReason and suppressOutput are not supported for PreToolUse in Codex")
    decision = specific.get("permissionDecision")
    reason = str(specific.get("permissionDecisionReason") or "").strip()
    notes = [str(n) for n in (specific.get("additionalContext"), data.get("systemMessage")) if n]
    if "updatedInput" in specific:
        rewrite = json.dumps(specific["updatedInput"], ensure_ascii=False)[:500]
        return Outcome(
            "deny",
            f"the hook wants this call changed to {rewrite}; winhand does not change calls, so send it again "
            "that way" + (f" ({reason})" if reason else ""),
        )
    if decision == "deny":
        if claude or reason:
            return Outcome("deny", _short(reason or "denied"))
        return failed("deny without a reason")
    if decision == "ask":
        if claude:
            return Outcome("ask", _short(reason or hook.label()), notes)
        return failed("ask is not supported for PreToolUse in Codex")
    if decision == "allow" and not claude:
        return failed("allow without updatedInput is not supported in Codex")
    if decision not in (None, "allow", "defer"):
        return failed(f"unknown permissionDecision {decision!r}")
    if decision is None and data.get("decision") == "block":
        legacy = str(data.get("reason") or "").strip()
        if claude or legacy:
            return Outcome("deny", _short(legacy or "blocked"))
        return failed("block without a reason")
    if data.get("continue") is False:
        if claude:
            return Outcome("deny", _short(str(data.get("stopReason") or "the hook stopped the call")))
        return failed("continue: false is not supported for PreToolUse in Codex")
    if notes:
        return Outcome("note", notes=[_short(n) for n in notes])
    return Outcome()


# ------------------------------------------------------------------ for the guard


@dataclass
class Finding:
    hook: Hook
    action: Action
    decision: str  # deny | ask | note
    text: str


def anchor(action: Action) -> str:
    """The folder whose hooks apply to an action (the command's working folder, the file's folder)."""
    from .guard import _start_dir

    where = action.cwd or (action.paths[0] if action.paths else None) or os.getcwd()
    return _start_dir(where)


def guard_hook(rule_file: str, index: int, command: str, timeout_s: float) -> Hook:
    return Hook("guards", rule_file, index, 0, None, {"command": command, "timeout": timeout_s}, "trusted")


def check(
    actions: list[Action],
    tool: str,
    args: dict,
    commands: list[tuple[Hook, Action]] | None = None,
) -> list[Finding]:
    """Run the hooks (and guard command rules) that apply to these actions; what they said."""
    cfg = settings()
    cap = timeout_cap(cfg)
    jobs: list[tuple[Hook, Action, dict, str, float]] = []
    found: dict[str, list[Hook]] = {}
    if sources(cfg):
        for action in actions:
            folder = anchor(action)
            if folder not in found:
                found[folder] = [h for h in discover(folder, cfg)[0] if h.runs]
            root = workspace.project_root(folder)
            seen: set[tuple] = set()
            for hook in found[folder]:
                for tool_name, tool_input, names in calls(action, hook.source, tool, args, root):
                    if not matches(hook, names):
                        continue
                    if hook.source == "claude-code":  # the same handler in two files runs once
                        same = (
                            hook.command,
                            json.dumps(hook.handler.get("args")),
                            tool_name,
                            json.dumps(tool_input),
                        )
                        if same in seen:
                            continue
                        seen.add(same)
                    timeout = _seconds(hook, cap)
                    jobs.append((hook, action, payload(hook, tool_name, tool_input, root), root, timeout))
    for hook, action in commands or []:
        root = workspace.project_root(anchor(action))
        for tool_name, tool_input, _ in calls(action, "claude-code", tool, args, root):
            timeout = _seconds(hook, cap)
            jobs.append((hook, action, payload(hook, tool_name, tool_input, root), root, timeout))
    if not jobs:
        return []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(32, len(jobs))) as pool:
        results = list(pool.map(lambda job: run(job[0], job[2], job[3], job[4]), jobs))
    findings = []
    for (hook, action, _, _, timeout), result in zip(jobs, results, strict=True):
        outcome = interpret(hook, result, timeout)
        if outcome.decision in ("deny", "ask"):
            findings.append(Finding(hook, action, outcome.decision, outcome.reason))
        for note in outcome.notes:
            findings.append(Finding(hook, action, "note", note))
    return findings


def apply_to_input(cwd: str | None) -> bool:
    """Whether any hook would see what is typed into a shell session working in `cwd`."""
    cfg = settings()
    if not sources(cfg):
        return False
    folder = anchor(Action("input", "session_send", cwd=cwd))
    return any(
        h.runs and matches(h, ("Bash",) if h.source == "codex" else ("Bash", "PowerShell"))
        for h in discover(folder, cfg)[0]
    )
