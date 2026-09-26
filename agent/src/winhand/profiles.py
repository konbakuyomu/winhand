"""Declarative session profiles: teach winhand a new tool with a TOML file, no code.

Built-in profiles ship in `winhand/profile_data/*.toml`; files in
`~/.winhand/profiles/*.toml` add new ones or override built-ins by name.
Placeholders like `{port}` in command/cwd/port/host/tcp_port are filled from
`vars` given at start time (or the profile's `[vars]` defaults).
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from .session.session import SessionSpec

BUILTIN_DIR = Path(__file__).parent / "profile_data"
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_SPEC_KEYS = {
    "transport",
    "cwd",
    "env",
    "port",
    "baudrate",
    "host",
    "tcp_port",
    "telnet",
    "encoding",
    "cols",
    "rows",
    "screen",
    "line_ending",
    "prompts",
    "replace_prompts",
    "needs_user",
    "confirm",
    "quiet_ms",
    "blocked_after_s",
    "heuristic_prompts",
    "exit_command",
    "autoreply",
    "ready",
    "description",
}


def user_dir() -> Path:
    return Path(os.environ.get("WINHAND_HOME") or Path.home() / ".winhand") / "profiles"


def _load_dir(directory: Path) -> dict[str, dict]:
    profiles: dict[str, dict] = {}
    if not directory.is_dir():
        return profiles
    for path in sorted(directory.glob("*.toml")):
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        data.setdefault("name", path.stem)
        data["_source"] = str(path)
        profiles[data["name"]] = data
    return profiles


def all_profiles() -> dict[str, dict]:
    profiles = _load_dir(BUILTIN_DIR)
    profiles.update(_load_dir(user_dir()))
    return profiles


def list_profiles() -> list[dict]:
    out = []
    for name, data in sorted(all_profiles().items()):
        out.append(
            {
                "name": name,
                "transport": data.get("transport", "pty"),
                "description": data.get("description", ""),
                "vars": sorted(_required_vars(data) | set(data.get("vars", {}))),
            }
        )
    return out


def get_profile(name: str) -> dict:
    profiles = all_profiles()
    if name not in profiles:
        raise KeyError(f"unknown profile {name!r}; available: {', '.join(sorted(profiles))}")
    return profiles[name]


def _required_vars(data: dict) -> set[str]:
    found: set[str] = set()
    for key in ("command", "cwd", "port", "host", "tcp_port"):
        value = data.get(key)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if isinstance(item, str):
                found.update(_PLACEHOLDER.findall(item))
    return found


def _fill(value, variables: dict[str, str], profile: str):
    if isinstance(value, list):
        return [_fill(v, variables, profile) for v in value]
    if not isinstance(value, str):
        return value

    def sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables or variables[key] in (None, ""):
            raise ValueError(f"profile {profile!r} needs vars[{key!r}]")
        return str(variables[key])

    return _PLACEHOLDER.sub(sub, value)


def build_spec(
    profile: dict,
    *,
    variables: dict[str, str] | None = None,
    extra_args: list[str] | None = None,
    overrides: dict | None = None,
) -> tuple[SessionSpec, dict]:
    """Return (spec, extras) where extras carries `init` commands and `notes`."""
    name = profile.get("name", "?")
    merged_vars = {**profile.get("vars", {}), **(variables or {})}
    kwargs: dict = {k: profile[k] for k in _SPEC_KEYS if k in profile}
    command = profile.get("command", [])
    if isinstance(command, str):
        command = command.split()
    kwargs["argv"] = [*_fill(command, merged_vars, name), *(extra_args or [])]
    for key in ("cwd", "port", "host", "tcp_port"):
        if key in kwargs:
            kwargs[key] = _fill(kwargs[key], merged_vars, name)
    if "tcp_port" in kwargs and kwargs["tcp_port"] is not None:
        kwargs["tcp_port"] = int(kwargs["tcp_port"])
    if "serial" in profile:
        kwargs["serial_options"] = dict(profile["serial"])
    for key, value in (overrides or {}).items():
        if value is not None:
            kwargs[key] = value
    kwargs["profile"] = name
    kwargs.setdefault("name", name)
    spec = SessionSpec(**kwargs)
    extras = {"init": list(profile.get("init", [])), "notes": profile.get("notes", "")}
    return spec, extras
