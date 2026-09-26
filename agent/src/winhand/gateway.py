"""Re-expose local stdio MCP servers (pyocd-debug, usb-camera, ...) through winhand.

Each configured server is mounted under its name, so its tool `probe_list`
appears as `<name>_probe_list`. Servers start on first use; a broken one only
logs a warning and never takes winhand down.
"""

from __future__ import annotations

import os

from fastmcp import FastMCP
from fastmcp.client.transports.stdio import StdioTransport
from fastmcp.server import create_proxy

from . import winenv
from .config import ServerEntry


def mount_servers(mcp: FastMCP, servers: list[ServerEntry]) -> list[dict]:
    mounted = []
    for entry in servers:
        if not entry.enabled:
            mounted.append({"name": entry.name, "enabled": False})
            continue
        env = winenv.build_env(entry.env)
        command = winenv.resolve_executable(entry.command, env)
        cwd = os.path.expanduser(entry.cwd) if entry.cwd else None
        transport = StdioTransport(command=command, args=entry.args, env=env, cwd=cwd)
        mcp.mount(create_proxy(transport, name=f"gateway-{entry.name}"), namespace=entry.name)
        mounted.append(
            {
                "name": entry.name,
                "enabled": True,
                "command": command,
                "args": entry.args,
                "tool_prefix": f"{entry.name}_",
            }
        )
    return mounted
