"""The owner's rules for what an AI may do here: ~/.winhand/guards.toml and the
.winhand/guards.toml of a project folder.

    [[guard]]
    on = ["command", "input"]                 # action kinds (actions.KINDS)
    match = '(?i)\\brm\\s+-\\w*r\\w*f'          # regex searched in the command line / typed line
    path = ["**/.env"]                        # globs; a project file's are relative to its folder
    decision = "deny"                         # deny | ask | note
    reason = "no batch deletes"

A rule only ever restricts: deny refuses the call, ask lets the person at the machine decide in a
dialog (no answer = refused), note lets it run and adds the reason to the result. There is no
allow, so a project's file cannot loosen the global one. A file that cannot be read refuses
everything it governs until it is fixed: rules that silently stop applying are worse than none.

Rules judge a call's arguments, not what a program does once it runs: a guard is a seat belt for
an AI that means well, not a sandbox (`run cat .env` reads what a read rule on .env protects).
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath

from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.types import TextContent

from .actions import KINDS, UNKNOWN_STATE, Action, LineState, describe, is_shell, session_input

FILE_NAME = "guards.toml"
PROJECT_DIR = ".winhand"
DECISIONS = ("deny", "ask", "note")
# a client gives up on a tool call after about 60 s: past that, a late "yes" would run an action
# the AI has already been told failed
ASK_TIMEOUT_S = 40.0
ASK_TIMEOUT_MAX_S = 50.0
_MISSING = (FileNotFoundError, NotADirectoryError, PermissionError)
_KEYS = {"on", "match", "path", "decision", "reason", "timeout_s"}
_PurePath = PureWindowsPath if os.name == "nt" else PurePosixPath


def home() -> Path:
    return Path(os.environ.get("WINHAND_HOME") or Path.home() / ".winhand")


def global_path() -> Path:
    return home() / FILE_NAME


@dataclass
class Rule:
    file: str
    index: int
    on: tuple[str, ...]
    decision: str
    reason: str = ""
    match: re.Pattern[str] | None = None
    path: tuple[str, ...] = ()
    timeout_s: float = ASK_TIMEOUT_S
    base: str | None = None  # the project folder of a project file; None for the global file

    def summary(self) -> dict:
        out: dict = {"file": self.file, "rule": self.index, "on": list(self.on), "decision": self.decision}
        if self.match is not None:
            out["match"] = self.match.pattern
        if self.path:
            out["path"] = list(self.path)
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class RuleFile:
    path: str
    base: str | None
    rules: list[Rule] = field(default_factory=list)
    error: str | None = None


_cache: dict[str, tuple[bytes, RuleFile]] = {}
_cache_lock = threading.Lock()


def _as_list(value, what: str) -> list[str]:
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(v, str) and v for v in items):
        raise ValueError(f"{what} must be a string or a list of strings")
    return items


def _parse(path: str, base: str | None, raw: bytes) -> RuleFile:
    out = RuleFile(path, base)
    try:
        data = tomllib.loads(raw.decode("utf-8-sig"))
        extra = set(data) - {"guard"}
        if extra:
            raise ValueError(f"unknown top-level key(s) {sorted(extra)}; rules are [[guard]] tables")
        entries = data.get("guard", [])
        if not isinstance(entries, list):
            raise ValueError("guard must be an array of tables: write each rule as [[guard]]")
        for i, entry in enumerate(entries, 1):
            try:
                out.rules.append(_rule(path, i, entry, base))
            except (ValueError, re.error) as exc:
                raise ValueError(f"rule {i}: {exc}") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, ValueError) as exc:
        out.rules, out.error = [], f"{type(exc).__name__}: {exc}" if isinstance(exc, OSError) else str(exc)
    return out


def _rule(path: str, index: int, entry, base: str | None) -> Rule:
    if not isinstance(entry, dict):
        raise ValueError("must be a table")
    unknown = set(entry) - _KEYS
    if unknown:
        raise ValueError(f"unknown key(s) {sorted(unknown)}; allowed: {sorted(_KEYS)}")
    if "on" not in entry or "decision" not in entry:
        raise ValueError("needs `on` and `decision`")
    on = _as_list(entry["on"], "on")
    bad = [k for k in on if k not in KINDS]
    if bad:
        raise ValueError(f"unknown action kind(s) {bad}; kinds: {list(KINDS)}")
    decision = entry["decision"]
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {list(DECISIONS)}, not {decision!r}")
    reason = entry.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    match = entry.get("match")
    if match is not None and not isinstance(match, str):
        raise ValueError("match must be a string (a regular expression)")
    timeout = entry.get("timeout_s", ASK_TIMEOUT_S)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("timeout_s must be a positive number")
    return Rule(
        file=path,
        index=index,
        on=tuple(on),
        decision=decision,
        reason=reason,
        match=re.compile(match) if match is not None else None,
        path=tuple(_as_list(entry["path"], "path")) if "path" in entry else (),
        timeout_s=min(float(timeout), ASK_TIMEOUT_MAX_S),
        base=base,
    )


def load(path: str | os.PathLike, base: str | None = None) -> RuleFile | None:
    """The rules in one file, None if there is none. The file is read on every call (it is small)
    and parsed again whenever its bytes change, so an edit applies to the very next call."""
    path = str(path)
    try:
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, "rb") as fh:
            raw = fh.read()
    except _MISSING as exc:
        # a folder winhand cannot look into has no rules it could apply either; a rule file
        # that exists but cannot be read is broken
        if isinstance(exc, PermissionError) and os.path.isfile(path):
            return RuleFile(path, base, error=f"{type(exc).__name__}: {exc}")
        with _cache_lock:
            _cache.pop(path, None)
        return None
    except OSError as exc:
        return RuleFile(path, base, error=f"{type(exc).__name__}: {exc}")
    with _cache_lock:
        hit = _cache.get(path)
    if hit and hit[0] == raw and hit[1].base == base:
        return hit[1]
    parsed = _parse(path, base, raw)
    with _cache_lock:
        _cache[path] = (raw, parsed)
    return parsed


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def variants(path: str) -> list[str]:
    """Spellings of one path a rule must see: as given (without a \\\\?\\ prefix or an NTFS
    stream suffix such as `::$DATA`) and with links, junctions and short names resolved."""
    p = path
    if p.startswith(("\\\\?\\UNC\\", "\\\\.\\UNC\\")):
        p = "\\\\" + p[8:]
    elif p.startswith(("\\\\?\\", "\\\\.\\")):
        p = p[4:]
    if os.name == "nt":
        head, tail = os.path.split(p)
        if ":" in tail:
            p = os.path.join(head, tail.split(":", 1)[0])
    out = [p]
    try:
        real = os.path.realpath(p)
    except (OSError, ValueError):
        real = p
    if _norm(real) != _norm(p):
        out.append(real)
    return out


def _under(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(folder)]) == _norm(folder)
    except ValueError:  # different drives
        return False


def _start_dir(path: str) -> str:
    """The folder a path lives in (the path itself if it is a folder); missing parts are skipped."""
    p = Path(path)
    if p.is_dir():
        return str(p)
    p = p.parent
    while not p.exists() and p != p.parent:
        p = p.parent
    return str(p)


def files_for(anchors: list[str]) -> list[RuleFile]:
    """The global file, then every .winhand/guards.toml in the folders above each anchor."""
    out: list[RuleFile] = []
    seen: set[str] = set()
    glob_file = str(global_path())
    first = load(glob_file)
    seen.add(_norm(glob_file))
    if first is not None:
        out.append(first)
    for anchor in anchors:
        d = Path(_start_dir(anchor))
        while True:
            candidate = str(d / PROJECT_DIR / FILE_NAME)
            key = _norm(candidate)
            if key not in seen:
                seen.add(key)
                found = load(candidate, base=str(d))
                if found is not None:
                    out.append(found)
            if d == d.parent:
                break
            d = d.parent
    return out


def _anchors(action: Action) -> list[str]:
    return [v for p in (list(action.paths) or ([action.cwd] if action.cwd else [])) for v in variants(p)]


def _glob_match(path: str, pattern: str, base: str | None) -> bool:
    pattern = pattern.replace("\\", "/")
    if pattern.endswith("/**") and _glob_match(path, pattern[:-3], base):
        return True  # "secrets/**" covers the folder itself: copying the folder hands it all out
    if _PurePath(pattern).is_absolute() or re.match(r"^[A-Za-z]:/", pattern):
        return _PurePath(path).full_match(pattern)
    if base is not None:
        if not _under(path, base):
            return False
        rel = os.path.relpath(path, base)
        return rel != "." and _PurePath(rel).full_match(pattern)
    # a global rule's relative pattern matches anywhere: "*.pem" is "**/*.pem"
    return _PurePath(path).full_match(pattern if pattern.startswith("**") else "**/" + pattern)


def matches(rule: Rule, action: Action) -> bool:
    if action.kind not in rule.on:
        return False
    if rule.base is not None and not any(_under(a, rule.base) for a in _anchors(action)):
        return False
    if rule.match is not None and not action.opaque and not (action.text and rule.match.search(action.text)):
        return False
    if rule.path and not any(
        _glob_match(v, g, rule.base) for p in action.paths for v in variants(p) for g in rule.path
    ):
        return False
    return True


@dataclass
class Verdict:
    decision: str | None = None  # deny | ask | note | None (nothing applies)
    deny: list[tuple[Rule, Action]] = field(default_factory=list)
    ask: list[tuple[Rule, Action]] = field(default_factory=list)
    notes: list[tuple[Rule, Action]] = field(default_factory=list)
    broken: list[RuleFile] = field(default_factory=list)


def judge(actions: list[Action]) -> Verdict:
    verdict = Verdict()
    for action in actions:
        for rule_file in files_for(_anchors(action)):
            if rule_file.error:
                if rule_file not in verdict.broken:
                    verdict.broken.append(rule_file)
                continue
            for rule in rule_file.rules:
                if matches(rule, action):
                    getattr(verdict, "notes" if rule.decision == "note" else rule.decision).append(
                        (rule, action)
                    )
    if verdict.broken or verdict.deny:
        verdict.decision = "deny"
    elif verdict.ask:
        verdict.decision = "ask"
    elif verdict.notes:
        verdict.decision = "note"
    return verdict


def rules_for(path: str | None) -> dict:
    """The rules that apply at a folder, for context(): summaries plus files that cannot be read."""
    files = files_for([path] if path else [])
    out: dict = {"global_file": str(global_path()), "rules": [], "errors": []}
    for f in files:
        if f.error:
            out["errors"].append({"file": f.path, "error": f.error})
        out["rules"].extend(r.summary() for r in f.rules)
    return out


# ------------------------------------------------------------------------- the middleware

_OWNER = "This limit is the machine owner's rule: do not try to get around it another way; tell the person if it blocks the task."


def _where(rule: Rule) -> str:
    return f"{rule.file} rule {rule.index}"


def refusal(verdict: Verdict, answer: str | None = None) -> str:
    lines = ["refused by a guard rule on this machine"]
    for f in verdict.broken:
        lines.append(f"- {f.path} cannot be used ({f.error}); fix the file, then call again")
    for rule, action in verdict.deny:
        lines.append(f"- {action.label()}: {rule.reason or 'denied'} ({_where(rule)})")
    if answer:
        for rule, action in verdict.ask:
            lines.append(f"- {action.label()}: needs the person's OK ({_where(rule)}); {answer}")
    if any(a.tool == "session_stop" for _, a in verdict.deny + verdict.ask):
        lines.append("session_stop(force=true) ends the session without typing into it.")
    if any(a.opaque for _, a in verdict.deny + verdict.ask):
        lines.append("Clear the line with keys=['Ctrl+C'], then type the command plainly and submit it.")
    lines.append(_OWNER)
    return "\n".join(lines)


def confirmation_text(verdict: Verdict) -> str:
    lines = ["AI 想在这台电脑上执行："]
    for action in dict.fromkeys(a for _, a in verdict.ask):
        lines.append(f"  {action.label()}")
    lines.append("")
    for rule in dict.fromkeys(r.reason or _where(r) for r, _ in verdict.ask):
        lines.append(f"规则：{rule}")
    lines.append(f"（来自 {', '.join(dict.fromkeys(_where(r) for r, _ in verdict.ask))}）")
    lines.append("")
    lines.append("允许这一次吗？")
    return "\n".join(lines)


def _confirm(verdict: Verdict) -> str | None:
    """None when the person said yes, else why not."""
    from .secret_prompt import PromptUnavailable, ask_confirm

    timeout = min(r.timeout_s for r, _ in verdict.ask)
    try:
        yes = ask_confirm("winhand：AI 请求确认", confirmation_text(verdict), timeout_s=timeout)
    except PromptUnavailable as exc:
        return f"not confirmed: {exc}"
    return None if yes else "the person at the machine said no"


def _with_notes(result, verdict: Verdict):
    from fastmcp.tools.base import ToolResult

    notes = list(dict.fromkeys(r.reason or _where(r) for r, _ in verdict.notes))
    text = "guard notes from this machine's owner:\n" + "\n".join(f"- {n}" for n in notes)
    content = list(getattr(result, "content", None) or []) + [TextContent(type="text", text=text)]
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict) and set(structured) != {"result"}:
        structured = {**structured, "guard_notes": notes}
    return ToolResult(
        content=content,
        structured_content=structured,
        meta=getattr(result, "meta", None),
        is_error=bool(getattr(result, "is_error", False)),
    )


_SEARCH_SPLIT = re.compile(r"[:-](?=\d+[:-])")


def _search_files(root: str, line: str, files_only: bool) -> list[str]:
    """The files a result line may come from. ripgrep writes `path:line:text` (`path-line-text`
    for context lines), and a path can itself contain `-12-`: every prefix ending at such a
    separator that is a file below root is a candidate."""
    if files_only:
        return [os.path.normpath(os.path.join(root, line))]
    out = []
    for m in _SEARCH_SPLIT.finditer(line):
        full = os.path.normpath(os.path.join(root, line[: m.start()]))
        if full not in out and os.path.isfile(full):
            out.append(full)
    return out


def _filter_search(result, args: dict):
    """fs_search on a folder reads every file below it: drop what the read rules keep from the AI."""
    import json

    from fastmcp.tools.base import ToolResult

    data = getattr(result, "structured_content", None)
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list) or not data.get("root"):
        return result
    root = data["root"]
    if not os.path.isdir(root):
        return result  # one file: judged as a read before the search ran
    kept, hidden, verdicts = [], set(), {}
    for line in data["matches"]:
        files = _search_files(root, str(line), bool(args.get("files_only")))
        for full in files:
            if full not in verdicts:
                verdicts[full] = judge([Action("read", "fs_search", (full,))]).decision
        refused = [f for f in files if verdicts[f] in ("deny", "ask")]
        if refused:
            hidden.update(os.path.relpath(f, root) for f in refused)
            continue
        kept.append(line)
    if not hidden:
        return result
    data = {
        **data,
        "matches": kept,
        "count": len(kept),
        "guard_hidden": f"{len(hidden)} file(s) left out by this machine's read rules: {', '.join(sorted(hidden)[:10])}",
    }
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return ToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=data,
        meta=getattr(result, "meta", None),
    )


_INPUT_TOOLS = ("session_send", "session_stop", "session_prompt_user")


def _interrupt_only(args: dict) -> bool:
    keys = [str(k).strip().lower() for k in args.get("keys") or []]
    return (
        bool(keys)
        and not args.get("text")
        and not args.get("submit")
        and all(k in ("ctrl+c", "^c", "c-c", "control+c") for k in keys)
    )


def _data(result) -> dict:
    data = getattr(result, "structured_content", None)
    return data if isinstance(data, dict) else {}


class GuardActions(Middleware):
    """Judges every call that reads or changes the machine against the owner's rules."""

    def __init__(self, sessions=None, confirm=None) -> None:
        self.sessions = sessions
        self._confirm = confirm or _confirm  # tests replace the dialog
        # per shell session: what was typed there and not run yet (judged with the next Enter)
        self._typed: dict[str, LineState] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._inflight: dict[str, int] = {}
        self._overlap: set[str] = set()

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        args = message.arguments or {}
        if message.name not in _INPUT_TOOLS:
            result = await self._judged(context, call_next, self._describe(message.name, args))
            if message.name == "fs_search" and not getattr(result, "is_error", False):
                result = await asyncio.to_thread(_filter_search, result, args)
            return result
        sid = str(args.get("id"))
        if (message.name == "session_stop" and args.get("force")) or _interrupt_only(args):
            # these can run nothing typed: never queue them behind a call that is still waiting
            result = await call_next(context)
            self._remember(message.name, args, result)
            return result
        if await asyncio.to_thread(self._input_rules_apply, args.get("id")):
            # judged and sent in order, so a line typed by one call and ended by another is
            # always judged whole
            async with self._locks.setdefault(sid, asyncio.Lock()):
                result = await self._judged(context, call_next, self._describe(message.name, args))
                self._remember(message.name, args, result)
                return result
        # no input rules: nothing waits, but input that overlaps other input to the same session
        # leaves the typed line unknown, so rules added later never judge a stale line
        self._inflight[sid] = self._inflight.get(sid, 0) + 1
        if self._inflight[sid] > 1:
            self._overlap.add(sid)
        try:
            result = await self._judged(context, call_next, self._describe(message.name, args))
        finally:
            self._inflight[sid] -= 1
        self._remember(message.name, args, result)
        if sid in self._overlap and message.name != "session_stop":
            self._typed[args.get("id")] = UNKNOWN_STATE
        if not self._inflight[sid]:
            self._overlap.discard(sid)
        return result

    def _input_rules_apply(self, sid) -> bool:
        try:
            spec = self.sessions.get(sid).spec
        except (KeyError, TypeError, AttributeError):
            return False
        if not is_shell(spec):
            return False
        anchors = [spec.cwd] if getattr(spec, "cwd", None) else []
        return any(f.error or any("input" in r.on for r in f.rules) for f in files_for(anchors))

    def _describe(self, tool: str, args: dict) -> list[Action]:
        try:
            return describe(tool, args, self.sessions, self._typed)
        except Exception:  # malformed arguments: the tool's own validation answers, and nothing runs
            return []

    def _remember(self, tool: str, args: dict, result) -> None:
        """What stays typed after the call, by what actually went out to the program."""
        sid = args.get("id")
        if tool == "session_stop":
            if not getattr(result, "is_error", False):
                self._typed.pop(sid, None)
            return
        if getattr(result, "is_error", False):
            return  # refused, or rejected before anything was sent
        try:
            _, after = session_input(args, self.sessions, self._typed, tool)
        except Exception:
            after = UNKNOWN_STATE
        if after is None:
            return
        data = _data(result)
        if tool == "session_prompt_user":
            if data.get("sent") is True:
                self._typed[sid] = after
            elif "sent" in data and data["sent"] is None:  # delivery uncertain
                self._typed[sid] = UNKNOWN_STATE
            return  # cancelled, or no dialog could open: nothing was typed
        if "error" in data:  # the tool may have sent part of it (text before a bad key name)
            self._typed[sid] = UNKNOWN_STATE
            return
        self._typed[sid] = after

    async def _judged(self, context: MiddlewareContext, call_next, actions: list[Action]):
        from fastmcp.tools.base import ToolResult

        if not actions:
            return await call_next(context)
        verdict = await asyncio.to_thread(judge, actions)
        if verdict.decision == "deny":
            return ToolResult(content=[TextContent(type="text", text=refusal(verdict))], is_error=True)
        if verdict.decision == "ask":
            answer = await asyncio.to_thread(self._confirm, verdict)
            if answer is not None:
                text = refusal(verdict, answer)
                return ToolResult(content=[TextContent(type="text", text=text)], is_error=True)
        result = await call_next(context)
        return _with_notes(result, verdict) if verdict.notes else result
