"""Arguments as models actually send them.

Models reach for the parameter names of the tools they know best: Claude Code's Read/Edit/Grep/
Bash (`file_path`, `old_string`, `-A`, `timeout` in ms), Anthropic's text editor (`old_str`), or
names that are merely natural (`fs_op(op=...)`, `job_start(script=...)`, `window(title=...)`).
Each such call used to fail validation and cost a round trip. This middleware, which runs before
the guard judges anything, renames them to winhand's own parameters, clamps numbers to the range
a parameter allows, and turns a lone string into the list a parameter wants. A rename happens
only when the alias is not a parameter of that tool and the parameter it stands for was not given
too; anything still unknown fails as before, but the error names the tool's real parameters.
Lossy adjustments (a clamp, an unsupported option dropped) are reported in the result.
"""

from __future__ import annotations

import copy
import os
import re
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as ArgumentsInvalid
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent
from pydantic import ValidationError

# alias -> parameter, for every tool that has that parameter and not the alias
COMMON = {
    "file_path": "path",
    "filepath": "path",
    "file": "path",
    "filename": "path",
    "session_id": "id",
    "job_id": "id",
    "working_directory": "cwd",
    "working_dir": "cwd",
    "workdir": "cwd",
    "old_string": "old",
    "old_str": "old",
    "old_text": "old",
    "new_string": "new",
    "new_str": "new",
    "new_text": "new",
    "replaceAll": "replace_all",
    "timeout_seconds": "timeout_s",
    "repository": "repo",
}

PER_TOOL: dict[str, dict[str, str]] = {
    "fs_find": {"pattern": "name", "glob": "name", "directory": "path", "dir": "path", "folder": "path"},
    "fs_list": {
        "directory": "path",
        "dir": "path",
        "folder": "path",
        "glob": "pattern",
        "max_depth": "depth",
    },
    "fs_search": {
        "query": "pattern",
        "regex": "pattern",
        "include": "glob",
        "-i": "ignore_case",
        "case_insensitive": "ignore_case",
        "fixed_strings": "literal",
        "-F": "literal",
        "head_limit": "max_results",
        "limit": "max_results",
        "max_count": "max_results",
        "directory": "path",
        "dir": "path",
        "folder": "path",
    },
    "fs_read": {"start_line": "offset", "lines": "limit", "num_lines": "limit", "max_lines": "limit"},
    "fs_write": {"text": "content", "contents": "content", "data": "content"},
    "fs_op": {
        "op": "action",
        "operation": "action",
        "src": "path",
        "source": "path",
        "dst": "to",
        "dest": "to",
        "destination": "to",
        "target": "to",
    },
    "run": {
        "cmd": "command",
        "command_line": "command",
        "commandline": "command",
        "argv": "args",
        "arguments": "args",
        "input": "stdin",
        "code": "script",
        "interpreter": "shell",
        "language": "shell",
    },
    "remote_run": {"command": "script", "cmd": "script", "hostname": "host", "server": "host"},
    "job_start": {
        "script": "command",
        "cmd": "command",
        "id": "name",
        "admin": "elevated",
        "as_admin": "elevated",
    },
    "job_status": {
        "job": "id",
        "cursor": "since",
        "offset": "since",
        "wait": "wait_s",
        "timeout_s": "wait_s",
    },
    "job_stop": {"job": "id"},
    "session_send": {"input": "text", "data": "text", "line": "text", "key": "keys", "enter": "submit"},
    "session_wait": {
        "id": "ids",
        "session_id": "ids",
        "session": "ids",
        "sessions": "ids",
        "pattern": "patterns",
        "state": "states",
        "wait_s": "timeout_s",
    },
    "session_read": {"cursor": "since", "offset": "since"},
    "window": {
        "title": "target",
        "window": "target",
        "hwnd": "target",
        "process": "target",
        "name": "target",
    },
    "ui": {
        "type": "control_type",
        "controlType": "control_type",
        "role": "control_type",
        "title": "window",
        "target": "window",
        "hwnd": "window",
        "process": "window",
    },
    "ocr": {"query": "text", "find": "text", "title": "window", "target": "window"},
    "screenshot": {"title": "window", "target": "window", "hwnd": "window"},
    "input": {"key": "keys", "hotkey": "keys", "shortcut": "keys", "string": "text"},
    "mcp_call": {
        "tool_name": "tool",
        "name": "tool",
        "args": "arguments",
        "params": "arguments",
        "server_name": "server",
    },
    "mcp_tools": {"name": "server", "server_name": "server"},
    "fs_upload_url": {"ttl": "ttl_s"},
    "fs_download_url": {"ttl": "ttl_s"},
    "skill": {"skill": "name", "skill_name": "name", "folder": "path"},
    "context": {"folder": "path", "directory": "path", "dir": "path"},
    "history": {"n": "limit", "count": "limit"},
}

# fields some clients add to every call; no winhand tool has them, so they are dropped
DROP = {"description", "explanation", "rationale", "purpose", "reasoning", "intent", "comment"}

# fs_search(type="py"), as Claude Code's Grep has it
_TYPES = {
    "py": "*.py",
    "python": "*.py",
    "js": "*.{js,mjs,cjs,jsx}",
    "ts": "*.{ts,tsx,mts,cts}",
    "md": "*.md",
    "markdown": "*.md",
    "json": "*.json",
    "toml": "*.toml",
    "yaml": "*.{yml,yaml}",
    "c": "*.{c,h}",
    "cpp": "*.{cpp,cc,cxx,hpp,hh,h}",
    "h": "*.h",
    "cs": "*.cs",
    "go": "*.go",
    "rust": "*.rs",
    "rs": "*.rs",
    "java": "*.java",
    "ps1": "*.{ps1,psm1,psd1}",
    "powershell": "*.{ps1,psm1,psd1}",
    "sh": "*.sh",
    "html": "*.{html,htm}",
    "css": "*.css",
    "xml": "*.xml",
    "txt": "*.txt",
}


def _types(schema: dict) -> set[str]:
    """The JSON types a parameter's schema accepts (anyOf/oneOf flattened)."""
    found: set[str] = set()
    kind = schema.get("type")
    if isinstance(kind, str):
        found.add(kind)
    elif isinstance(kind, list):
        found.update(k for k in kind if isinstance(k, str))
    for key in ("anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            if isinstance(sub, dict):
                found |= _types(sub)
    return found


def _bounds(schema: dict) -> dict:
    out = {k: schema[k] for k in ("minimum", "maximum", "exclusiveMinimum") if k in schema}
    for key in ("anyOf", "oneOf"):
        for sub in schema.get(key) or []:
            if isinstance(sub, dict):
                out = {**_bounds(sub), **out}
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", value):
        return float(value)
    return None


# parameters that hold a path on this machine, per tool: %VARS%, $env:VARS and ~ are expanded
# here, before the guard judges the call, so the rules and the tool see the same path
PATHS: dict[str, tuple[str, ...]] = {
    **{
        t: ("path",)
        for t in (
            "fs_read",
            "fs_write",
            "fs_edit",
            "fs_list",
            "fs_find",
            "fs_search",
            "fs_stat",
            "fs_send",
            "fs_write_bytes",
            "context",
            "skill",
        )
    },
    "fs_op": ("path", "to"),
    "fs_apply_patch": ("root", "patch_file"),
    "run": ("cwd",),
    "session_start": ("cwd",),
    "job_start": ("cwd",),
    "fs_pick": ("initial_dir",),
    "clipboard": ("image_path",),
}
_WINDOWS_VAR = re.compile(r"%([A-Za-z_][A-Za-z0-9_()]*)%|\$env:([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def expand_path(value: str) -> str:
    """%TEMP%\\x, $env:USERPROFILE\\x and ~\\x as the person means them; unknown variables stay as
    written. POSIX $VARS are left alone: `$` is common in real Windows paths (C:\\$Recycle.Bin, c$)."""

    def var(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        found = os.environ.get(name)
        return found if found is not None else m.group(0)

    expanded = _WINDOWS_VAR.sub(var, value)
    return os.path.expanduser(expanded) if expanded.startswith("~") else expanded


class Normalized:
    """One call's arguments made to fit the tool, and what was done that the model should know."""

    def __init__(self, tool: str, props: dict, args: dict) -> None:
        self.tool, self.props = tool, props
        self.args = dict(args)
        self.notes: list[str] = []

    def rename(self, alias: str, real: str) -> None:
        if alias in self.props or real not in self.props or alias not in self.args:
            return
        value = self.args.pop(alias)
        if real in self.args:
            self.notes.append(f"{alias} ignored: {real} was given too")
            return
        self.args[real] = value

    def run(self) -> None:
        special = getattr(self, f"_{self.tool}", None)
        if special:
            special()
        for alias, real in {**COMMON, **PER_TOOL.get(self.tool, {})}.items():
            self.rename(alias, real)
        for key in [k for k in self.args if k in DROP and k not in self.props]:
            del self.args[key]
        for key in PATHS.get(self.tool, ()):
            value = self.args.get(key)
            if isinstance(value, str) and ("%" in value or "$" in value or value.startswith("~")):
                self.args[key] = expand_path(value)
        for key, value in list(self.args.items()):
            schema = self.props.get(key)
            if not isinstance(schema, dict):
                continue
            types = _types(schema)
            if isinstance(value, str) and "array" in types and "string" not in types:
                self.args[key] = [value]
                continue
            bounds = _bounds(schema)
            number = _number(value)
            if number is None or not bounds or not (types & {"integer", "number"}):
                continue
            low = bounds.get("minimum")
            if low is None and "exclusiveMinimum" in bounds:
                continue  # nothing sensible to clamp to
            high = bounds.get("maximum")
            fixed = number
            if low is not None and fixed < low:
                fixed = low
            if high is not None and fixed > high:
                fixed = high
            if fixed != number:
                fixed = int(fixed) if "integer" in types or float(fixed).is_integer() else fixed
                self.args[key] = fixed
                self.notes.append(
                    f"{key}={value} is outside {low if low is not None else ''}..{high if high is not None else ''}; used {fixed}"
                )

    # ---------------------------------------------------------------- per-tool shapes

    def _fs_edit(self) -> None:
        edits = self.args.get("edits")
        if isinstance(edits, list):
            fixed = []
            for item in edits:
                if isinstance(item, dict):
                    item = dict(item)
                    for alias, real in (
                        ("old_string", "old"),
                        ("old_str", "old"),
                        ("old_text", "old"),
                        ("oldText", "old"),
                        ("new_string", "new"),
                        ("new_str", "new"),
                        ("new_text", "new"),
                        ("newText", "new"),
                        ("replaceAll", "replace_all"),
                    ):
                        if alias in item and real not in item:
                            item[real] = item.pop(alias)
                fixed.append(item)
            self.args["edits"] = fixed

    def _fs_search(self) -> None:
        args = self.args
        widths = [args.pop(k) for k in ("-A", "-B", "-C", "A", "B", "C", "context_lines") if k in args]
        numbers = [n for n in (_number(w) for w in widths) if n is not None]
        if numbers and "context" not in args:
            args["context"] = int(max(numbers))
        for key in ("-n", "n", "line_numbers", "-H"):
            args.pop(key, None)  # results always carry line numbers
        mode = args.pop("output_mode", None)
        if mode in ("files_with_matches", "files", "count") and "files_only" not in args:
            args["files_only"] = True
            if mode == "count":
                self.notes.append("output_mode=count is not supported; listed the matching files instead")
        if args.pop("multiline", None):
            self.notes.append("multiline is not supported: each line is matched on its own")
        kind = args.get("type")
        if isinstance(kind, str) and "type" not in self.props:
            args.pop("type")
            pattern = _TYPES.get(kind.lower().lstrip("."))
            if pattern and "glob" not in args:
                args["glob"] = pattern
            elif not pattern:
                self.notes.append(f"type={kind!r} is not known; searched every file (use glob='*.ext')")

    def _fs_list(self) -> None:
        if self.args.get("recursive") and "recursive" not in self.props:
            self.args.pop("recursive")
            self.args.setdefault("depth", 5)
        for key in ("hidden", "all"):
            if key in self.args and "show_hidden" not in self.args:
                self.args["show_hidden"] = bool(self.args.pop(key))

    def _fs_read(self) -> None:
        args = self.args
        end = _number(args.get("end_line"))
        if end is not None and "end_line" not in self.props:
            args.pop("end_line")
            start = _number(args.get("offset", args.get("start_line", 1))) or 1
            args.setdefault("limit", max(1, int(end - start + 1)))
        if "offset" in args and _number(args["offset"]) is not None and _number(args["offset"]) < 0:
            # Claude Code reads a negative offset as "from the end"
            args["tail"] = int(-_number(args.pop("offset")))

    def _run(self) -> None:
        self._timeout_ms("timeout_s")
        if self.args.pop("run_in_background", None):
            self.notes.append(
                "run_in_background is not a run option: it ran as usual (job_start runs PowerShell in the "
                "background; a run still going after ~45 s continues anyway, see still_running)"
            )

    def _remote_run(self) -> None:
        self._timeout_ms("timeout_s")

    def _session_wait(self) -> None:
        self._timeout_ms("timeout_s")

    def _job_status(self) -> None:
        if "timeout" in self.args and "wait_s" not in self.args:
            value = _number(self.args.pop("timeout"))
            if value is not None:
                self.args["wait_s"] = value / 1000 if value > 1000 else value
        if "pattern" in self.args and "patterns" not in self.args:
            value = self.args.pop("pattern")
            self.args["patterns"] = value if isinstance(value, list) else [value]

    def _ui(self) -> None:
        # ui(text=...): the value to type for set_value, otherwise the control's name
        if "text" in self.args and "text" not in self.props:
            target = "value" if self.args.get("action") == "set_value" else "name"
            if target not in self.args:
                self.args[target] = self.args.pop("text")

    def _timeout_ms(self, real: str) -> None:
        """`timeout` as Claude Code's Bash takes it, in milliseconds (values that look like seconds stay)."""
        if real in self.args:
            return
        for key, scale in (("timeout_ms", 0.001), ("timeout", None)):
            if key in self.args and key not in self.props:
                value = _number(self.args.pop(key))
                if value is None:
                    continue
                if scale is None:
                    scale = 0.001 if value > 1000 else 1
                self.args[real] = value * scale
                return


def _note(result: Any, notes: list[str]) -> Any:
    """Say in the result what was adjusted: in the structured data and as a short text block."""
    if not isinstance(result, ToolResult):
        return result
    text = "arguments adjusted: " + "; ".join(notes)
    data = copy.deepcopy(result.structured_content)
    if isinstance(data, dict):
        wrapped = set(data) == {"result"}
        target = data["result"] if wrapped else data
        if isinstance(target, dict):  # never add fields beside a wrapped plain value
            target["args_note"] = text
    return ToolResult(
        content=[*result.content, TextContent(type="text", text=text)],
        structured_content=data,
        meta=result.meta,
        is_error=result.is_error,
    )


class ArgumentAliases(Middleware):
    def __init__(self, mcp) -> None:
        self.mcp = mcp
        self._props: dict[str, dict] = {}

    async def _properties(self, name: str) -> dict | None:
        if name not in self._props:
            try:
                tool = await self.mcp.get_tool(name)
            except Exception:
                return None
            if tool is None:
                return None
            self._props[name] = dict((tool.parameters or {}).get("properties") or {})
        return self._props[name]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        message = context.message
        name = message.name
        props = await self._properties(name)
        if props is None:
            return await call_next(context)
        fixed = Normalized(name, props, message.arguments or {})
        fixed.run()
        if fixed.args != (message.arguments or {}):
            context = context.copy(message=message.model_copy(update={"arguments": fixed.args}))
        try:
            result = await call_next(context)
        except (ArgumentsInvalid, ValidationError) as exc:
            cause = exc.__cause__ if isinstance(exc, ArgumentsInvalid) else exc
            if not isinstance(cause, ValidationError) or not str(cause.title).startswith("call["):
                raise
            raise ToolError(self._explain(name, props, cause)) from None
        return _note(result, fixed.notes) if fixed.notes else result

    @staticmethod
    def _explain(name: str, props: dict, exc: ValidationError) -> str:
        problems = []
        for error in exc.errors()[:6]:
            where = ".".join(str(p) for p in error.get("loc", ()) if p != "call") or "(arguments)"
            kind = error.get("type", "")
            if kind == "unexpected_keyword_argument":
                problems.append(f"{where}: not a parameter of {name}")
            elif kind in ("missing_argument", "missing"):
                problems.append(f"{where}: required")
            else:
                problems.append(f"{where}: {error.get('msg', kind)}")
        params = ", ".join(props) or "(none)"
        return f"{name}: {'; '.join(problems)}. Its parameters: {params}"
