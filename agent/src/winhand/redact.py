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
# Credentials recognisable by their format are masked wherever they show up (a token printed by
# `scoop config`, a key in a log): GitHub, OpenAI/Anthropic-style keys, AWS access key ids, Slack,
# Google API keys, GitLab, npm, Hugging Face, private key blocks and JWTs. [redact] known_secrets =
# false turns this off.
KNOWN_SECRETS = (
    r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{50,}\b",
    r"\bsk-(?=[A-Za-z0-9_-]*\d)(?:ant-|proj-|svcacct-)?[A-Za-z0-9_-]{20,}",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    r"\bAIza[0-9A-Za-z_-]{35}",
    r"\bglpat-[A-Za-z0-9_-]{20,}",
    r"\bnpm_[A-Za-z0-9]{36}\b",
    r"\bhf_[A-Za-z0-9]{30,}\b",
    r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----",
    r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
)
_SECRET_NAME = re.compile(r"(?i)(token|secret|passw|pass_?phrase|api_?key|apikey|auth|credential|private)")
_SECRET_FILE = re.compile(r"(?i)(passphrase|password|token|secret|credential)")


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
        self,
        values: dict[str, Iterable[str]] | None = None,
        patterns: Iterable[str] = (),
        ipv4=False,
        known: Iterable[str] = (),
    ):
        self.rules: list[tuple[str, re.Pattern[str]]] = []
        if known:
            self.rules.append(("token", re.compile("|".join(f"(?:{k})" for k in known))))
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

    def count_in_blob(self, blob: str) -> int:
        """How many masked values a base64 blob holds (as UTF-8, UTF-16 or single-byte text)."""
        import base64

        try:
            raw = base64.b64decode(blob, validate=False)
        except ValueError:
            return 0
        texts = {raw.decode("latin-1")}
        for encoding in ("utf-8", "utf-16-le"):
            texts.add(raw.decode(encoding, errors="ignore"))
        return max((sum(len(regex.findall(t)) for _, regex in self.rules) for t in texts), default=0)

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


def own_secrets(cfg) -> list[str]:
    """winhand's own credentials, masked whatever [redact] says: the relay device token, the
    headers and secret-looking environment values of local MCP servers, and one-line secret files
    kept next to winhand's settings (owner-passphrase.txt ...). With the device token anyone can
    stand in for this machine at the relay."""
    from . import config

    found = [cfg.device_token] if cfg.device_token else []
    for server in cfg.servers:
        for value in server.headers.values():
            found.append(value)
            scheme, _, credential = value.partition(" ")
            if credential and scheme.lower() in ("bearer", "basic", "token"):
                found.append(credential)
        found += [v for k, v in server.env.items() if _SECRET_NAME.search(k) and len(v) >= 12]
    try:
        files = [f for f in config.home().iterdir() if f.is_file() and _SECRET_FILE.search(f.name)]
    except OSError:
        files = []
    for f in files:
        if f.suffix.lower() in (".toml", ".json", ".lock", ".log", ".jsonl") or f.stat().st_size > 4096:
            continue
        found += file_values([str(f)], 8)
    return [v.strip() for v in found if v and len(v.strip()) >= 8]


def build(settings: dict, cfg=None) -> Redactor:
    min_length = int(settings.get("min_length", 6) or 6)
    values: dict[str, list[str]] = {
        "value": [str(v) for v in settings.get("values", []) or []],
        "secret": sorted(_remembered),
    }
    if cfg is not None:
        values["winhand"] = own_secrets(cfg)
    if settings.get("ssh_hosts"):
        values["host"] = ssh_hosts()
    files = settings.get("files") or []
    if isinstance(files, str):
        files = [files]
    if files:
        values["file"] = file_values(files, min_length)
    patterns = settings.get("patterns") or []
    patterns = [patterns] if isinstance(patterns, str) else list(patterns)
    known = KNOWN_SECRETS if settings.get("known_secrets", True) is not False else ()
    return Redactor(values, patterns, bool(settings.get("ipv4")), known)


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

    cfg = config.load()
    redactor = build(cfg.redact, cfg)
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
        from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, TextResourceContents

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
            elif isinstance(block, EmbeddedResource) and isinstance(block.resource, BlobResourceContents):
                n = redactor.count_in_blob(block.resource.blob)
                if n:  # bytes cannot be masked in place without corrupting the file: hold it back
                    total += n
                    block = TextContent(
                        type="text",
                        text=f"{block.resource.uri}: not sent, it contains {n} value(s) this machine masks. "
                        "Read it with fs_read (text, masked), or ask the person to share it.",
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
