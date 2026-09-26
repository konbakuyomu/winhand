"""winhand configuration (~/.winhand/config.toml).

```toml
[gateway.servers.pyocd]            # exposed as tools pyocd_<tool>
command = "uv"
args = ["--directory", 'D:\\Dev\\PYOCD调试MCP', "run", "pyocd-debug-mcp"]
env = {}
cwd = ""
enabled = true

[relay]
url = "wss://mcp.example.com/agent"
device_token = "..."
```
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("WINHAND_HOME") or Path.home() / ".winhand")


def config_path() -> Path:
    return home() / "config.toml"


@dataclass
class ServerEntry:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = True


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
    servers = []
    for name, entry in (raw.get("gateway", {}).get("servers", {}) or {}).items():
        servers.append(
            ServerEntry(
                name=name,
                command=entry["command"],
                args=list(entry.get("args", [])),
                env=dict(entry.get("env", {})),
                cwd=entry.get("cwd") or None,
                enabled=entry.get("enabled", True),
            )
        )
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
        lines.append(f"[gateway.servers.{_key(s.name)}]")
        lines.append(f"command = {_toml_value(s.command)}")
        lines.append(f"args = {_toml_value(s.args)}")
        if s.env:
            lines.append(f"env = {_toml_value(s.env)}")
        if s.cwd:
            lines.append(f"cwd = {_toml_value(s.cwd)}")
        lines.append(f"enabled = {_toml_value(s.enabled)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def import_codex(codex_config: Path | None = None, cfg: Config | None = None) -> tuple[Config, list[str]]:
    """Copy stdio MCP servers from Codex's config.toml into winhand's gateway."""
    codex_config = codex_config or Path.home() / ".codex" / "config.toml"
    cfg = cfg or load()
    if not codex_config.exists():
        return cfg, []
    with open(codex_config, "rb") as fh:
        raw = tomllib.load(fh)
    existing = {s.name for s in cfg.servers}
    added = []
    for name, entry in (raw.get("mcp_servers") or {}).items():
        if not isinstance(entry, dict) or "command" not in entry or name in existing or name == "winhand":
            continue
        cfg.servers.append(
            ServerEntry(
                name=name,
                command=entry["command"],
                args=list(entry.get("args", [])),
                env={k: str(v) for k, v in (entry.get("env") or {}).items()},
                cwd=entry.get("cwd"),
                enabled=bool(entry.get("enabled", True)),
            )
        )
        added.append(name)
    return cfg, added
