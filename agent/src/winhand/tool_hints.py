"""Hints on each tool that clients act on.

Claude Code (code.claude.com/docs/en/mcp, checked 2026-11-01):
- `readOnlyHint`: runs the tool's calls in parallel with other read-only calls, and repeats a call
  that was pending when a connection closed, once, on a new connection. Before these hints a
  `git status` waited 41 s behind a pytest, and `proc_list` 58 s behind a `job_status`.
- `idempotentHint`: the tool may change something, but repeating a call is safe; also repeated once
  after a dropped connection.
- `_meta["anthropic/alwaysLoad"]`: the tool is loaded with the session instead of behind tool search,
  so the everyday tools need no search round trip first.

Only the hints are added here; the tools themselves are unchanged. A tool is read-only when no
action of it changes the machine (window and ocr can click, so they are not).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools.base import Tool
from mcp.types import ToolAnnotations

READ_ONLY = frozenset(
    {
        "context",
        "fs_download_url",
        "fs_find",
        "fs_list",
        "fs_read",
        "fs_search",
        "fs_send",
        "fs_stat",
        "help",
        "history",
        "job_status",
        "proc_list",
        "profile_list",
        "screenshot",
        "session_list",
        "session_read",
        "session_screen",
        "session_wait",
        "skill",
        "sys_info",
    }
)

# changes something, but a second identical call does no harm (stopping what already stopped)
IDEMPOTENT = frozenset({"job_stop", "session_stop", "proc_kill"})

# the tools nearly every session uses: loaded up front rather than found through tool search
ALWAYS_LOAD = frozenset(
    {
        "context",
        "fs_edit",
        "fs_find",
        "fs_list",
        "fs_read",
        "fs_search",
        "fs_write",
        "job_start",
        "job_status",
        "remote_run",
        "run",
    }
)


def hinted(tool: Tool) -> Tool:
    """The tool as clients should see it: with its hints and load preference."""
    update: dict = {}
    if tool.name in READ_ONLY or tool.name in IDEMPOTENT:
        base = tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
        if tool.name in READ_ONLY:
            base["read_only_hint"] = True
        else:
            base["idempotent_hint"] = True
            base["destructive_hint"] = False
        update["annotations"] = ToolAnnotations(**base)
    if tool.name in ALWAYS_LOAD:
        update["meta"] = {**(tool.meta or {}), "anthropic/alwaysLoad": True}
    return tool.model_copy(update=update) if update else tool


class ToolHints(Middleware):
    """Adds READ_ONLY / IDEMPOTENT / ALWAYS_LOAD to every tools/list answer."""

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Tool]:
        return [hinted(tool) for tool in await call_next(context)]
