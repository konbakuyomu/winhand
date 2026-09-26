"""winhand configuration (~/.winhand/config.toml).

```toml
[mcp_servers.pyocd]                # served on its own at https://<relay>/mcp/pyocd
command = "uv"
args = ["--directory", 'D:\\Dev\\PYOCD调试MCP', "run", "pyocd-debug-mcp"]
env = {}
cwd = ""
enabled = true

[mcp_servers.docs]                 # an MCP server that already speaks HTTP on this machine
url = "http://127.0.0.1:8000/mcp"
headers = { Authorization = "Bearer ..." }

[relay]
url = "wss://mcp.example.com/agent"
device_token = "..."
```
"""

from __future__ import annotations

import contextlib
import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("WINHAND_HOME") or Path.home() / ".winhand")


def config_path() -> Path:
    return home() / "config.toml"


@dataclass
class ServerEntry:
    """A local MCP server: a command speaking stdio, or a local HTTP endpoint (`url`)."""

    name: str
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    startup_timeout_s: float = 60.0
    idle_stop_minutes: float = 0.0  # 0 = keep it running once started
    description: str = ""

    @property
    def kind(self) -> str:
        return "http" if self.url else "stdio"


NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def valid_name(name: str) -> bool:
    """Service names become URL paths (/mcp/<name>)."""
    return bool(NAME_RE.match(name))


def safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-_")[:64]
    return cleaned or "server"


def entry_from_dict(name: str, entry: dict) -> ServerEntry:
    return ServerEntry(
        name=name,
        command=str(entry.get("command") or ""),
        args=[str(a) for a in entry.get("args", [])],
        env={str(k): str(v) for k, v in (entry.get("env") or {}).items()},
        cwd=entry.get("cwd") or None,
        url=entry.get("url") or None,
        headers={str(k): str(v) for k, v in (entry.get("headers") or {}).items()},
        enabled=bool(entry.get("enabled", True)),
        startup_timeout_s=float(entry.get("startup_timeout_s") or entry.get("startup_timeout_sec") or 60),
        idle_stop_minutes=float(entry.get("idle_stop_minutes") or 0),
        description=str(entry.get("description") or ""),
    )


@dataclass
class Config:
    servers: list[ServerEntry] = field(default_factory=list)
    relay_url: str | None = None
    device_token: str | None = None
    raw: dict = field(default_factory=dict)


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    if not path.exists():
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    servers = [
        entry_from_dict(n, e) for n, e in (raw.get("mcp_servers") or {}).items() if isinstance(e, dict)
    ]
    known = {s.name for s in servers}
    # before 0.3 local servers lived under [gateway.servers]; they are moved on the next save
    for name, entry in (raw.get("gateway", {}).get("servers", {}) or {}).items():
        if isinstance(entry, dict) and name not in known:
            servers.append(entry_from_dict(name, entry))
    relay = raw.get("relay", {})
    return Config(
        servers=servers, relay_url=relay.get("url"), device_token=relay.get("device_token"), raw=raw
    )


def _toml_str(value: str) -> str:
    if "'" not in value and "\n" not in value:
        return f"'{value}'"  # literal string: Windows backslashes stay as-is
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return _toml_str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_toml_value(v)}" for k, v in value.items()) + " }"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _key(name: str) -> str:
    return name if name.replace("-", "").replace("_", "").isalnum() and name.isascii() else _toml_str(name)


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# winhand configuration", ""]
    if cfg.relay_url or cfg.device_token:
        lines.append("[relay]")
        if cfg.relay_url:
            lines.append(f"url = {_toml_value(cfg.relay_url)}")
        if cfg.device_token:
            lines.append(f"device_token = {_toml_value(cfg.device_token)}")
        lines.append("")
    for s in cfg.servers:
        lines.append(f"[mcp_servers.{_key(s.name)}]")
        if s.description:
            lines.append(f"description = {_toml_value(s.description)}")
        if s.url:
            lines.append(f"url = {_toml_value(s.url)}")
            if s.headers:
                lines.append(f"headers = {_toml_value(s.headers)}")
        else:
            lines.append(f"command = {_toml_value(s.command)}")
            lines.append(f"args = {_toml_value(s.args)}")
            if s.env:
                lines.append(f"env = {_toml_value(s.env)}")
            if s.cwd:
                lines.append(f"cwd = {_toml_value(s.cwd)}")
        lines.append(f"enabled = {_toml_value(s.enabled)}")
        if s.startup_timeout_s != 60:
            lines.append(f"startup_timeout_s = {_toml_value(s.startup_timeout_s)}")
        if s.idle_stop_minutes:
            lines.append(f"idle_stop_minutes = {_toml_value(s.idle_stop_minutes)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _client_internal(entry: dict) -> bool:
    """A client's own helper (Codex ships node_repl for its browser tooling): useless to anyone else."""
    command = str(entry.get("command") or "").replace("/", "\\").lower()
    env = entry.get("env") or {}
    return "\\openai\\codex\\" in command or any(str(k).startswith("CODEX_") for k in env)


def import_candidates() -> list[dict]:
    """MCP servers configured for other clients on this machine (Codex, Claude Desktop,
    Claude Code), offered for import. Nothing is read from them after importing."""
    import json

    found: list[dict] = []

    def add(source: str, name: str, entry: dict) -> None:
        if not isinstance(entry, dict) or not (entry.get("command") or entry.get("url")):
            return
        kind = str(entry.get("type") or entry.get("transport") or "")
        if entry.get("url") and kind in ("sse",):
            return  # the legacy SSE transport is not bridged
        found.append({"source": source, "name": name, "entry": entry, "internal": _client_internal(entry)})

    codex = Path.home() / ".codex" / "config.toml"
    if codex.exists():
        with contextlib.suppress(OSError, tomllib.TOMLDecodeError):
            with open(codex, "rb") as fh:
                for name, entry in (tomllib.load(fh).get("mcp_servers") or {}).items():
                    add("Codex", name, entry)
    appdata = os.environ.get("APPDATA")
    json_sources = [
        ("Claude Desktop", Path(appdata) / "Claude" / "claude_desktop_config.json" if appdata else None),
        ("Claude Code", Path.home() / ".claude.json"),
    ]
    for source, path in json_sources:
        if path is None or not path.exists():
            continue
        with contextlib.suppress(OSError, ValueError):
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, entry in (data.get("mcpServers") or {}).items():
                add(source, name, entry)
    return [c for c in found if c["name"] != "winhand"]


def import_servers(names: list[str] | None = None, cfg: Config | None = None) -> tuple[Config, list[str]]:
    """Copy the chosen candidates into winhand's own configuration (`names` None: all of them
    except other clients' internal helpers)."""
    cfg = cfg or load()
    existing = {s.name for s in cfg.servers}
    added = []
    for candidate in import_candidates():
        if names is not None and candidate["name"] not in names:
            continue
        if names is None and candidate["internal"]:
            continue  # only when asked for by name
        name = safe_name(candidate["name"])
        if name in existing:
            continue
        entry = entry_from_dict(name, candidate["entry"])
        entry.description = f"从 {candidate['source']} 导入"
        cfg.servers.append(entry)
        existing.add(name)
        added.append(name)
    return cfg, added
