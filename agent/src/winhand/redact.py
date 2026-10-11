"""Mask sensitive values in everything winhand hands back to the model.

Configured in ~/.winhand/config.toml:

    [redact]
    values = ["my-secret-domain.example"]      # literal values
    patterns = ['sk-[A-Za-z0-9]{20,}']           # regular expressions
    ssh_hosts = true                             # every HostName in ~/.ssh/config (+ Include files)
    files = ['%LOCALAPPDATA%\\fleet\\secrets\\*.env']  # values of KEY=VALUE lines (or a one-line file)
    ipv4 = false                                 # every IPv4 address except loopback
    min_length = 6                               # shorter values from files are ignored

Values given to secret_prompt during this run are masked too. Each match becomes
«redacted:<kind>». The local MCP servers at /mcp/<name> are separate endpoints and are not
covered.

Masked text must not be written back to files: the placeholder would replace the real value.
The file tools refuse content that contains it (see `refuse_placeholders`).
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import Middleware, MiddlewareContext

PLACEHOLDER = "«redacted:"
_IPV4 = re.compile(
    r"(?<![\d.])(?!127\.|0\.0\.0\.0(?![\d.]))(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)(?![\d.])"
)
# public names nobody needs hidden, and that would make output unreadable if they were
_HARMLESS = {"localhost", "127.0.0.1", "::1", "github.com", "ssh.github.com", "gitlab.com", "bitbucket.org"}
_RELOAD_EVERY_S = 30.0


class RedactedWrite(ValueError):
    pass


def refuse_placeholders(*texts: str | None) -> None:
    """Writing masked output back would replace real values with «redacted:...»."""
    for text in texts:
        if text and PLACEHOLDER in text:
            raise RedactedWrite(
                "this content contains «redacted:…» placeholders from masked output; writing it would "
                "replace the real values. Change only the lines you need (fs_edit with text that does "
                "not include masked parts) and leave the masked ones as they are"
            )


# ------------------------------------------------------------------ collecting values


def ssh_hosts(path: Path | None = None, _seen: set[Path] | None = None) -> list[str]:
    """HostName values from an OpenSSH config file and the files it includes."""
    path = path or Path.home() / ".ssh" / "config"
    seen = _seen if _seen is not None else set()
    if path in seen or not path.is_file():
        return []
    seen.add(path)
    found: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) != 2 or parts[0].startswith("#"):
            continue
        key, value = parts[0].lower(), parts[1].strip().strip('"')
        if key == "hostname" and "%" not in value:
            found.append(value)
        elif key == "include":
            for pattern in value.split():
                pattern = os.path.expanduser(pattern)
                if not os.path.isabs(pattern):
                    pattern = str(Path.home() / ".ssh" / pattern)
                for included in sorted(glob.glob(pattern)):
                    found += ssh_hosts(Path(included), seen)
    return found


def file_values(patterns: Iterable[str], min_length: int = 6) -> list[str]:
    """Values of KEY=VALUE lines in the given files (globs, %VARS% and ~ allowed); a file that is
    a single line without '=' counts as one value (a token file)."""
    found: list[str] = []
    for pattern in patterns:
        expanded = os.path.expanduser(os.path.expandvars(pattern))
        for name in sorted(glob.glob(expanded)):
            try:
                text = Path(name).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if len(lines) == 1 and "=" not in lines[0]:
                found.append(lines[0])
                continue
            for line in lines:
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                value = value.strip().strip("'\"")
                if len(value) >= min_length:
                    found.append(value)
    return found


# ------------------------------------------------------------------ the redactor


class Redactor:
    def __init__(
        self, values: dict[str, Iterable[str]] | None = None, patterns: Iterable[str] = (), ipv4=False
    ):
        self.rules: list[tuple[str, re.Pattern[str]]] = []
        for kind, items in (values or {}).items():
            unique = sorted({v for v in items if v and v not in _HARMLESS}, key=len, reverse=True)
            if unique:
                self.rules.append((kind, re.compile("|".join(re.escape(v) for v in unique))))
        for pattern in patterns:
            try:
                self.rules.append(("pattern", re.compile(pattern)))
            except re.error:
                continue
        if ipv4:
            self.rules.append(("ip", _IPV4))

    @property
    def empty(self) -> bool:
        return not self.rules

    def text(self, value: str) -> tuple[str, int]:
        total = 0
        for kind, regex in self.rules:
            value, n = regex.subn(f"{PLACEHOLDER}{kind}»", value)
            total += n
        return value, total

    def data(self, value: Any) -> tuple[Any, int]:
        """Mask every string inside JSON-like data."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            total = 0
            out = {}
            for k, v in value.items():
                out[k], n = self.data(v)
                total += n
            return out, total
        if isinstance(value, list):
            total = 0
            items = []
            for v in value:
                masked, n = self.data(v)
                items.append(masked)
                total += n
            return items, total
        return value, 0


_lock = threading.Lock()
_cache: tuple[float, tuple, Redactor] | None = None
_remembered: set[str] = set()


def remember(value: str) -> None:
    """Mask this value from now on (a secret the person typed for us)."""
    global _cache
    if value:
        with _lock:
            _remembered.add(value)
            _cache = None


def _config_stamp() -> tuple:
    from . import config

    try:
        return (config.config_path().stat().st_mtime,)
    except OSError:
        return (None,)


def build(settings: dict) -> Redactor:
    min_length = int(settings.get("min_length", 6) or 6)
    values: dict[str, list[str]] = {
        "value": [str(v) for v in settings.get("values", []) or []],
        "secret": sorted(_remembered),
    }
    if settings.get("ssh_hosts"):
        values["host"] = ssh_hosts()
    files = settings.get("files") or []
    if isinstance(files, str):
        files = [files]
    if files:
        values["file"] = file_values(files, min_length)
    patterns = settings.get("patterns") or []
    return Redactor(values, [patterns] if isinstance(patterns, str) else patterns, bool(settings.get("ipv4")))


def current() -> Redactor:
    """The redactor for the current configuration, rebuilt when it changes (and every 30 s, so
    edits to ssh config and secret files are picked up)."""
    global _cache
    stamp = _config_stamp()
    now = time.monotonic()
    with _lock:
        if _cache is not None and _cache[1] == stamp and now - _cache[0] < _RELOAD_EVERY_S:
            return _cache[2]
    from . import config

    redactor = build(config.load().redact)
    with _lock:
        _cache = (now, stamp, redactor)
    return redactor


def reset() -> None:
    global _cache
    with _lock:
        _cache = None
        _remembered.clear()


# ------------------------------------------------------------------ applying it to results


NOTE = "values marked «redacted:…» were masked on the machine; do not write this text back as-is"


class RedactOutput(Middleware):
    """Masks configured values in every tool result before it leaves this machine."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        result = await call_next(context)
        redactor = current()
        if redactor.empty:
            return result
        from fastmcp.tools.base import ToolResult
        from mcp.types import EmbeddedResource, TextContent, TextResourceContents

        total = 0
        content = []
        for block in getattr(result, "content", None) or []:
            if isinstance(block, TextContent):
                text, n = redactor.text(block.text)
                total += n
                block = block.model_copy(update={"text": text}) if n else block
            elif isinstance(block, EmbeddedResource) and isinstance(block.resource, TextResourceContents):
                text, n = redactor.text(block.resource.text)
                total += n
                if n:
                    block = block.model_copy(
                        update={"resource": block.resource.model_copy(update={"text": text})}
                    )
            content.append(block)
        structured = getattr(result, "structured_content", None)
        if structured is not None:
            structured, n = redactor.data(structured)
            if isinstance(structured, dict) and (n or total):
                wrapped = set(structured) == {"result"}
                target = structured.get("result") if wrapped else structured
                if isinstance(target, dict):  # never add fields beside a wrapped plain value
                    target["redacted"] = max(n, total)
                    target["redaction_note"] = NOTE
            total = max(total, n)
        if not total:
            return result
        return ToolResult(
            content=content,
            structured_content=structured,
            meta=getattr(result, "meta", None),
            is_error=bool(getattr(result, "is_error", False)),
        )


def mask(value: Any) -> Any:
    """Masked copy of tool arguments or summaries for logs and history."""
    redactor = current()
    return value if redactor.empty else redactor.data(value)[0]
