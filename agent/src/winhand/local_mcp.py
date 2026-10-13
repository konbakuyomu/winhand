"""The machine's local MCP servers as seen through winhand's own connection: what tools each offers
(kept in a cache, so describing them never starts a server) and calling one of them.

The cache lives in <WINHAND_HOME>/cache/mcp-tools.json, one entry per server, valid while the
server's configuration is unchanged. It is filled whenever winhand learns a server's tool list:
a client listing them through the /mcp/<name> gateway, mcp_tools(server), or the app's test button.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from typing import Any

from . import config, redact
from .config import ServerEntry

CACHE_FILE = "mcp-tools.json"
INSTRUCTIONS_CHARS = 300
TOOL_LINE_CHARS = 150
MIN_TOOL_CHARS = 40  # what a tool's line keeps of its description when room is short
ANSWER_WITHIN_S = 40.0  # clients abandon a tool call after ~60 s (see session/wait.py)
KEEP_UNCLAIMED_S = 600.0  # a finished call nobody came back for is dropped after this
_lock = threading.Lock()

# the Services of the running agent (set by mcp_bridge.compose_app); None in stdio / http mode
active = None
_own = None


def fingerprint(entry: ServerEntry) -> str:
    """What a cached tool list depends on: how the server is started or reached."""
    parts = {
        "kind": entry.kind,
        "command": entry.command,
        "args": list(entry.args),
        "url": entry.url,
        "cwd": entry.cwd,
        "env": sorted((entry.env or {}).items()),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _path():
    return config.home() / "cache" / CACHE_FILE


def _read_all() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _good_tool(tool) -> bool:
    return (
        isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and isinstance(tool.get("description", ""), str)
        and isinstance(tool.get("inputSchema", {}), dict)
    )


def cached(entry: ServerEntry, data: dict | None = None) -> dict | None:
    """The cached list for this server, None when there is none for its current configuration (or
    the entry is not what remember() writes). `data`: the whole cache, already read."""
    item = (_read_all() if data is None else data).get(entry.name)
    if not isinstance(item, dict) or item.get("fingerprint") != fingerprint(entry):
        return None
    tools = item.get("tools")
    if not isinstance(tools, list) or not all(_good_tool(t) for t in tools):
        return None
    instructions = item.get("instructions")
    return {
        "listed_at": str(item.get("listed_at") or ""),
        "instructions": instructions if isinstance(instructions, str) else "",
        "tools": [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "inputSchema": t.get("inputSchema", {}),
            }
            for t in tools
        ],
    }


def remember(entry: ServerEntry, tools: list, instructions: str | None = None) -> dict:
    """Store a server's tool list (name, description, inputSchema of each); the record kept, also
    when the file could not be written."""
    kept = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        kept.append(
            {
                "name": tool["name"],
                "description": str(tool.get("description") or ""),
                "inputSchema": tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {},
            }
        )
    record = {
        "fingerprint": fingerprint(entry),
        "listed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "instructions": str(instructions or "")[:2000],
        "tools": kept,
    }
    with _lock:
        data = _read_all()
        data[entry.name] = record
        path = _path()
        # a name of its own: another winhand process (the agent and a stdio one) may write at once
        tmp = path.with_name(f"{CACHE_FILE}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            for attempt in range(3):
                try:
                    os.replace(tmp, path)
                    break
                except PermissionError:  # Windows: a reader has the file open for a moment
                    if attempt == 2:
                        raise
                    time.sleep(0.02)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
    return record


def services():
    """The Services that serve local MCP servers here: the running agent's, or our own."""
    global _own
    if active is not None:
        return active
    if _own is None:
        from .mcp_bridge import Services

        _own = Services(config.load().servers)
        atexit.unregister(_stop_own)
        atexit.register(_stop_own)
    return _own


def _stop_own() -> None:
    """At exit (stdio mode): the servers this process started go with it."""
    for service in list(_own.services.values()) if _own else []:
        if service.process is not None:
            with contextlib.suppress(Exception):
                service.process.stop()


def enabled_entries() -> list[ServerEntry]:
    return [s.entry for s in services().services.values() if s.entry.enabled]


def _entry(name: str) -> ServerEntry:
    service = services().get(name)
    if service is None:
        names = ", ".join(e.name for e in enabled_entries()) or "none"
        raise ValueError(f"no local MCP server named {name!r} is enabled in winhand (enabled: {names})")
    return service.entry


def _first_line(text: str, limit: int) -> str:
    line = next((x.strip() for x in str(text or "").splitlines() if x.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


# ------------------------------------------------------------------ talking to a server


async def _request(name: str, method: str, params: dict, timeout: float) -> dict:
    """One JSON-RPC request to a local server (started if need be); its `result`."""
    every = services()
    service = every.get(name)
    if service is None:
        _entry(name)  # raises with the list of enabled servers
    if service.entry.kind == "http":
        return await _http_request(service.entry, method, params, timeout)
    every.watch_idle()
    problem = await service.check_health(before_call=True)
    if problem:
        raise RuntimeError(problem)
    await service.ensure_started()
    service.last_used = time.time()
    try:
        answer = await service._internal(method, params, timeout)
    except TimeoutError:
        raise TimeoutError(f"{name} did not answer {method} within {timeout:g} s") from None
    finally:
        service.last_used = time.time()
    if "error" in answer:
        error = answer["error"] or {}
        raise RuntimeError(f"{name}: {error.get('message') or error}")
    return answer.get("result") or {}


def _http_client(entry: ServerEntry, timeout: float):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    # the client's own read timeout comes later: the caller's asyncio.timeout decides, with its message
    return Client(StreamableHttpTransport(entry.url, headers=entry.headers or None), timeout=timeout + 5)


async def _http_request(entry: ServerEntry, method: str, params: dict, timeout: float) -> dict:
    try:
        async with asyncio.timeout(timeout), _http_client(entry, timeout) as client:
            result = await client.call_tool_mcp(params["name"], params.get("arguments") or {})
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
    except TimeoutError:
        raise TimeoutError(f"{entry.name} did not answer {method} within {timeout:g} s") from None


async def _http_list(entry: ServerEntry, timeout: float) -> tuple[list, str]:
    """All the tools of a server reached over HTTP, and its instructions (one connection)."""
    tools: list = []
    try:
        async with asyncio.timeout(timeout), _http_client(entry, timeout) as client:
            cursor = None
            for _ in range(50):
                page = await client.list_tools_mcp(cursor=cursor)
                tools += [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in page.tools]
                cursor = page.next_cursor
                if not cursor:
                    break
            return tools, client.instructions or ""
    except TimeoutError:
        raise TimeoutError(f"{entry.name} did not answer tools/list within {timeout:g} s") from None


async def list_tools(name: str, timeout: float = 90) -> dict:
    """Start the server if need be, list its tools (all pages) and remember them."""
    entry = _entry(name)
    if entry.kind == "http":
        tools, instructions = await _http_list(entry, timeout)
    else:
        tools = []
        cursor = None
        for _ in range(50):
            result = await _request(name, "tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools += result.get("tools") or []
            cursor = result.get("nextCursor")
            if not cursor:
                break
        service = services().get(name)
        instructions = ((service.init_result or {}).get("instructions") if service else None) or ""
    return remember(entry, tools, instructions)  # what was just listed, even if the cache was not written


async def call_tool(name: str, tool: str, arguments: dict, timeout: float) -> dict:
    return await _request(name, "tools/call", {"name": tool, "arguments": arguments}, timeout)


# ------------------------------------------------------------------ answering within a client's patience

_calls: dict[str, tuple[asyncio.Task, float]] = {}  # call id -> (task, when it finished or started)
_listings: dict[str, asyncio.Task] = {}  # server -> its tools/list in flight


def _forget_old() -> None:
    now = time.time()
    for key, (task, since) in list(_calls.items()):
        if task.done() and now - since > KEEP_UNCLAIMED_S:
            _calls.pop(key, None)
            with contextlib.suppress(BaseException):
                task.result()  # retrieved: no "exception was never retrieved" warning


async def _within(task: asyncio.Task, wait_s: float | None) -> bool:
    """Wait for `task` up to wait_s (default ANSWER_WITHIN_S) without cancelling it; whether it is done."""
    await asyncio.wait({task}, timeout=max(0.0, ANSWER_WITHIN_S if wait_s is None else wait_s))
    return task.done()


async def start_call(name: str, tool: str, arguments: dict, timeout: float, wait_s: float | None = None):
    """Call a tool; (result, None) when it answers within wait_s, else (None, call id)."""
    _entry(name)
    _forget_old()
    # unique across restarts: an id from before one never finds another call's result
    key = "m" + uuid.uuid4().hex[:12]
    task = asyncio.ensure_future(call_tool(name, tool, arguments, timeout))
    # registered before waiting: if this answer is lost (the client gave up), the call is still kept
    _calls[key] = (task, time.time())

    def finished(t: asyncio.Task) -> None:
        if not t.cancelled():
            t.exception()  # retrieved: a failure nobody came back for is not logged as unhandled
        if key in _calls:
            _calls[key] = (t, time.time())  # kept KEEP_UNCLAIMED_S from now

    task.add_done_callback(finished)
    if await _within(task, wait_s):
        _calls.pop(key, None)
        return task.result(), None
    return None, key


async def wait_call(key: str, wait_s: float | None = None):
    """A call that was still running: (result, None) once done, else (None, key) again."""
    _forget_old()
    if key not in _calls:
        raise ValueError(f"no call {key!r} is waiting here (finished ones are handed out once)")
    task = _calls[key][0]
    if not await _within(task, wait_s):
        return None, key
    if _calls.pop(key, None) is None:  # another wait on the same id took it first
        raise ValueError(f"the result of {key!r} was already handed out")
    return task.result(), None


async def listing(name: str, wait_s: float | None = None) -> dict | None:
    """list_tools(name) within wait_s; None while it is still starting (call again to keep waiting)."""
    _entry(name)
    task = _listings.get(name)
    if task is None:
        task = _listings[name] = asyncio.ensure_future(list_tools(name))

        def finished(t: asyncio.Task) -> None:
            if _listings.get(name) is t:
                _listings.pop(name, None)
            if not t.cancelled():
                t.exception()  # retrieved: a failure nobody waited for is not logged as unhandled

        task.add_done_callback(finished)
    if not await _within(task, wait_s):
        return None
    return task.result()


# ------------------------------------------------------------------ describing them


def _rows() -> list[tuple[ServerEntry, dict | None]]:
    """Each enabled server and its cached list, from one reading of the configuration and the cache."""
    data = _read_all()
    return [(entry, cached(entry, data)) for entry in enabled_entries()]


def _masker():
    redactor = redact.current()
    return (lambda text: text) if redactor.empty else (lambda text: redactor.data(text)[0])


def overview() -> list[dict]:
    """Each enabled server: its state and what the cache knows of its tools."""
    mask = _masker()  # before cutting: a secret cut in two would no longer be recognised
    out = []
    status = {s["name"]: s for s in services().snapshot()}
    for entry, item in _rows():
        row: dict[str, Any] = {"server": entry.name, "state": status.get(entry.name, {}).get("state")}
        if item is None:
            row["tools"] = None
            row["note"] = f'not listed yet: mcp_tools("{entry.name}") starts it and lists its tools'
        else:
            row["listed_at"] = item["listed_at"]
            row["tools"] = [
                {"name": t["name"], "description": _first_line(mask(t["description"]), TOOL_LINE_CHARS)}
                for t in item["tools"]
            ]
        out.append(row)
    return out


def _cut(line: str, limit: int) -> str:
    return line if len(line) <= limit else line[: limit - 1] + "…"


def session_text(budget: int) -> str:
    """The tools part of a cloud session's start: every enabled server and its tools, one line each.
    When not everything fits, every description is cut to one common length (at least
    MIN_TOOL_CHARS), so each tool keeps a line of its own; past that, the last servers are listed by
    tool names only, then left out. Masked before it is measured, so the budget holds for what is
    sent."""
    from .session_bridge import _size

    mask = _masker()
    head = mask(
        "Local MCP servers on this PC. Call a tool with mcp_call(server, tool, arguments); "
        "mcp_tools(server) gives each tool's full parameters."
    )
    rows = _rows()
    if not rows:
        return head + "\n(none are enabled in winhand)"
    servers = []  # (name, listed, first line of its instructions, [(tool, first line of its description)])
    for entry, item in rows:
        name = mask(entry.name)
        if item is None:
            servers.append((name, False, "", []))
        else:
            instructions = _first_line(mask(item["instructions"]), 1 << 30) if item["instructions"] else ""
            tools = [(mask(t["name"]), _first_line(mask(t["description"]), 1 << 30)) for t in item["tools"]]
            servers.append((name, True, instructions, tools))

    def block(server, cap: int | None) -> str:
        """cap None: tool names only."""
        name, listed, instructions, tools = server
        if not listed:
            return f'## {name}\n(not listed yet: mcp_tools("{name}") starts it and lists its tools)'
        if cap is None:
            return f"## {name}\n" + (", ".join(tool for tool, _ in tools) or "(no tools)")
        lines = [f"## {name}"]
        if instructions:
            lines.append(_cut(instructions, min(INSTRUCTIONS_CHARS, 2 * cap)))
        lines += [f"- {tool}: {_cut(line, cap)}" if line else f"- {tool}" for tool, line in tools] or [
            "(no tools)"
        ]
        return "\n".join(lines)

    room = budget - 80  # the line saying how many were left out still fits

    def fits(blocks: list[str]) -> bool:
        return _size(head) + sum(_size(b) + 2 for b in blocks) <= room

    def render(cap: int, described: int, shown: int) -> list[str]:
        return [block(s, cap if i < described else None) for i, s in enumerate(servers[:shown])]

    n = len(servers)
    blocks = render(TOOL_LINE_CHARS, n, n)
    if not fits(blocks):
        low, high = MIN_TOOL_CHARS, TOOL_LINE_CHARS  # the longest common cap that fits
        while low < high:
            mid = (low + high + 1) // 2
            if fits(render(mid, n, n)):
                low = mid
            else:
                high = mid - 1
        blocks = render(low, n, n)
        if not fits(blocks):
            # as many servers as fit, the last ones by tool names only; as many of them described as fit.
            # Sizes are summed, not rendered again for each try: many servers stay quick
            described_size = [_size(block(s, MIN_TOOL_CHARS)) + 2 for s in servers]
            named_size = [_size(block(s, None)) + 2 for s in servers]
            space = room - _size(head)
            best = (0, 0)
            for shown in range(n, -1, -1):
                total = sum(named_size[:shown])
                if total > space:
                    continue  # even by names only these do not fit: one server fewer
                described = 0  # a described block is never smaller than the names-only one
                while (
                    described < shown and total + described_size[described] - named_size[described] <= space
                ):
                    total += described_size[described] - named_size[described]
                    described += 1
                best = (described, shown)
                break
            described, shown = best
            blocks = render(MIN_TOOL_CHARS, described, shown)
    parts = [head, *blocks]
    if len(blocks) < n:
        parts.append(
            f"{n - len(blocks)} more server(s) left out for lack of room; mcp_tools() lists them all."
        )
    text = "\n\n".join(parts)
    while _size(text) > budget:  # only a single enormous name gets here
        text = text[: -max(1, (_size(text) - budget + 1) // 2)]
    return text
