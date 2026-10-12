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
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_TIMEOUT_S
    return max(1.0, float(value))


# ------------------------------------------------------------------ reading configuration files

_files: dict[str, tuple[tuple, object, str | None]] = {}


def _load(path: str, kind: str) -> tuple[object, str | None]:
    """(data, error) of a JSON or TOML file; (None, None) when it does not exist. Cached by size and
    modification time."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return None, None
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _files.get(path)
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
        text = raw.decode("utf-8-sig")
        data = json.loads(text, parse_constant=_no_constant) if kind == "json" else tomllib.loads(text)
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        error = f"cannot be read: {exc}"
    _files[path] = (stamp, data, error)
    return data, error


def _no_constant(name: str):
    raise ValueError(f"{name} is not JSON")


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
            return str(h.get("commandWindows") or h.get("command_windows") or h.get("command") or "")
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


def _groups(data: object) -> list | str:
    if not isinstance(data, dict):
        return "the top level is not a table"
    hooks = data.get("hooks")
    if hooks is None:
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
    data, error = _load(path, kind)
    if error:
        return [], [{"file": path, "error": error}]
    if data is None:
        return [], []
    if source == "codex":
        problem = _codex_file_problem(data, kind)
        if problem:  # Codex rejects the whole file, so none of its hooks run there either
            return [], [{"file": path, "error": f"Codex would not load it: {problem}"}]
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


_CODEX_HANDLER = {
    "type",
    "command",
    "commandWindows",
    "command_windows",
    "timeout",
    "async",
    "statusMessage",
    "additionalContextLimit",
    "server",
    "tool",
    "input",
    "prompt",
    "model",
}


def _codex_file_problem(data: object, kind: str) -> str | None:
    """What makes Codex refuse a hooks file (its schema denies unknown fields), None if nothing."""
    if not isinstance(data, dict):
        return "the top level is not a table"
    if kind == "json" and set(data) - {"description", "hooks"}:
        return f"unknown top-level key(s) {sorted(set(data) - {'description', 'hooks'})}"
    hooks = data.get("hooks")
    if hooks is None:
        return None
    if not isinstance(hooks, dict):
        return "`hooks` is not a table"
    groups = hooks.get("PreToolUse")
    if groups is None:
        return None
    if not isinstance(groups, list):
        return "`hooks.PreToolUse` is not a list"
    for g, group in enumerate(groups):
        if not isinstance(group, dict) or set(group) - {"matcher", "hooks"}:
            return f"PreToolUse group {g} is not {{matcher, hooks}}"
        if "matcher" in group and not isinstance(group["matcher"], str):
            return f"PreToolUse group {g}: matcher is not a string"
        handlers = group.get("hooks", [])
        if not isinstance(handlers, list):
            return f"PreToolUse group {g}: hooks is not a list"
        for i, handler in enumerate(handlers):
            if not isinstance(handler, dict) or "type" not in handler:
                return f"PreToolUse group {g} hook {i} has no type"
            if set(handler) - _CODEX_HANDLER:
                return (
                    f"PreToolUse group {g} hook {i}: unknown key(s) {sorted(set(handler) - _CODEX_HANDLER)}"
                )
            if handler.get("type") == "command":
                for key in ("command", "commandWindows", "command_windows", "statusMessage"):
                    if key in handler and not isinstance(handler[key], str):
                        return f"PreToolUse group {g} hook {i}: {key} is not a string"
                timeout = handler.get("timeout")
                if timeout is not None and (
                    isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 0
                ):
                    return f"PreToolUse group {g} hook {i}: timeout is not a whole number of seconds"
                if "async" in handler and not isinstance(handler["async"], bool):
                    return f"PreToolUse group {g} hook {i}: async is not true or false"
    return None


def _unsupported(hook: Hook) -> str | None:
    h = hook.handler
    kind = h.get("type", "command")
    if kind != "command":
        return f"`type = {kind!r}` hooks are not run by winhand (only command hooks are)"
    for key in ("async", "asyncRewake", "if", "once"):
        if h.get(key):
            return f"hooks with `{key}` are not run by winhand"
    for key in ("command", "commandWindows", "command_windows"):
        if key in h and not isinstance(h[key], str):
            return f"`{key}` is not a string"
    if not hook.command:
        return "it has no command"
    if hook.source == "claude-code" and "args" in h and not isinstance(h["args"], list):
        return "`args` is not a list"
    timeout = h.get("timeout")
    if timeout is not None:
        whole = isinstance(timeout, int) and not isinstance(timeout, bool)
        number = whole or isinstance(timeout, float)
        if not number or not math.isfinite(timeout) or timeout < 0 or (hook.source == "codex" and not whole):
            return "`timeout` is not a number of seconds" + (
                " (Codex wants a whole number)" if number else ""
            )
    for key in ("statusMessage",):
        if key in h and not isinstance(h[key], str):
            return f"`{key}` is not a string"
    matcher = hook.matcher
    if matcher and matcher.strip() not in ("", "*") and not _EXACT[_dialect(hook)].match(matcher):
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
        command = handler.get("commandWindows") or handler.get("command_windows") or handler.get("command")
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


def _codex_user() -> tuple[dict, dict[str, str], list[dict]]:
    """hooks.state (by normalized key), each project's trust_level (by folder) and errors, from the
    user's config.toml: only the user decides what is trusted, never a project's own files."""
    path = os.path.join(workspace.codex_home(), "config.toml")
    data, error = _load(path, "toml")
    if error:
        return {}, {}, [{"file": path, "error": error}]
    if not isinstance(data, dict):
        return {}, {}, []
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    raw_state = hooks.get("state") if isinstance(hooks.get("state"), dict) else {}
    state = {_state_key(str(k)): v for k, v in raw_state.items() if isinstance(v, dict)}
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    levels = {
        _folder_key(str(k)): str(v.get("trust_level")) for k, v in projects.items() if isinstance(v, dict)
    }
    return state, levels, []


def _codex_hooks_off() -> bool:
    """[features] hooks = false (or the old name codex_hooks) in the user's config.toml."""
    data, _ = _load(os.path.join(workspace.codex_home(), "config.toml"), "toml")
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, dict):
        return False
    return features.get("hooks", features.get("codex_hooks")) is False


def _codex_folder_trusted(folder: str, root: str, levels: dict[str, str]) -> bool:
    """Whether Codex loads the .codex folder of `folder`: decided for that folder, else for the
    project root; an explicit "untrusted" on either wins."""
    found = [levels.get(_folder_key(folder)), levels.get(_folder_key(root))]
    if "untrusted" in found:
        return False
    return next((level for level in found if level is not None), None) == "trusted"


def _codex(root: str, chain: list[str]) -> tuple[list[Hook], list[dict]]:
    system = codex_system_dir()
    requirements = os.path.join(system, "requirements.toml")
    req, req_error = _load(requirements, "toml")
    errors = [{"file": requirements, "error": req_error}] if req_error else []
    managed_only = isinstance(req, dict) and req.get("allow_managed_hooks_only") is True
    state, levels, user_errors = _codex_user()
    errors += user_errors
    switched_off = _codex_hooks_off()
    home = workspace.codex_home()
    layers: list[tuple[str, str, str, str | None]] = [
        ("managed", requirements, "toml", None),
        ("managed", os.path.join(system, "hooks.json"), "json", None),
        ("managed", os.path.join(system, "config.toml"), "toml", None),
        ("user", os.path.join(home, "hooks.json"), "json", None),
        ("user", os.path.join(home, "config.toml"), "toml", None),
    ]
    for folder in chain:
        if _folder_key(os.path.join(folder, ".codex")) == _folder_key(home):
            continue  # Codex does not load its own home twice
        layers.append(("project", os.path.join(folder, ".codex", "hooks.json"), "json", folder))
        layers.append(("project", os.path.join(folder, ".codex", "config.toml"), "toml", folder))
    hooks: list[Hook] = []
    for layer, path, kind, folder in layers:
        found, errs = _from_file("codex", path, kind)
        errors += errs
        for hook in found:
            try:
                _codex_status(hook, layer, folder, root, state, levels, managed_only)
            except Exception as exc:  # a strange file must not stop the others
                hook.status, hook.reason = "unsupported", f"cannot be read ({exc})"
            if switched_off and hook.runs:
                hook.status, hook.reason = (
                    "disabled",
                    "hooks are turned off in Codex ([features] hooks = false)",
                )
            hooks.append(hook)
    return hooks, errors


def _codex_status(hook, layer, folder, root, state, levels, managed_only) -> None:
    why = _unsupported(hook)
    entry = state.get(_state_key(hook.key), {})
    if why:
        hook.status, hook.reason = "unsupported", why
    elif layer == "managed":
        hook.status = "managed"
    elif managed_only:
        hook.status = "disabled"
        hook.reason = "only managed hooks may run (allow_managed_hooks_only in Codex's requirements.toml)"
    elif entry.get("enabled") is False:
        hook.status, hook.reason = "disabled", "turned off in Codex"
    elif layer == "project" and not _codex_folder_trusted(folder or root, root, levels):
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
    """Workspace trust accepted for the project, or for a folder above it when the project is not a
    repository of its own (trust covers subfolders, not nested repositories)."""
    own_repo = os.path.exists(os.path.join(root, ".git"))
    for folder, entry in workspace._claude_projects().items():
        if not isinstance(entry, dict) or entry.get("hasTrustDialogAccepted") is not True:
            continue
        if _folder_key(str(folder)) == _folder_key(root) or (not own_repo and _under(root, str(folder))):
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


def _main_checkout(root: str, chain: list[str]) -> tuple[str, list[str]]:
    """For a linked git worktree, the main checkout and the folders there that match `chain`
    (Codex takes a worktree's hooks and trust from the main checkout); otherwise unchanged."""
    if not os.path.isfile(os.path.join(root, ".git")):
        return root, chain
    gitdir, common = workspace._git_dirs(root)
    if not gitdir or not common or os.path.basename(os.path.normpath(common)) != ".git":
        return root, chain
    main = os.path.dirname(os.path.normpath(common))
    if _folder_key(main) == _folder_key(root):
        return root, chain
    return main, [os.path.normpath(os.path.join(main, os.path.relpath(f, root))) for f in chain]


def discover(folder: str, cfg: dict | None = None) -> tuple[list[Hook], list[dict]]:
    """Every PreToolUse hook that applies in `folder`, with its status, and the files that could
    not be read."""
    enabled = sources(cfg)
    root = workspace.project_root(folder)
    chain = workspace._chain(root, folder)
    hooks: list[Hook] = []
    errors: list[dict] = []
    if "codex" in enabled:
        found, errs = _codex(*_main_checkout(root, chain))
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
        return {"hooks": [], "errors": [{"file": folder, "error": f"hooks could not be read ({exc})"}]}
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
        out["errors"] = errors[:10]
    return out


# ------------------------------------------------------------------ matching


_EXACT = {"codex": re.compile(r"^[A-Za-z0-9_|]*$"), "claude-code": re.compile(r"^[A-Za-z0-9_\-, |]*$")}


def matches(hook: Hook, names: tuple[str, ...]) -> bool:
    matcher = hook.matcher
    if matcher is None or matcher.strip() in ("", "*"):
        return True
    exact = _EXACT[_dialect(hook)]
    if exact.match(matcher):
        alternatives = {a.strip() for a in re.split(r"[|,]" if hook.source != "codex" else r"\|", matcher)}
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
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
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
