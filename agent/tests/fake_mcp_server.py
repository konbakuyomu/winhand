"""A small stdio MCP server for the bridge tests (run as a separate process)."""

import asyncio
import base64
import os
import sys

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image

print("fake server starting", file=sys.stderr, flush=True)

mcp = FastMCP("fake-local", version="1.2.3")

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@mcp.tool
def echo(text: str) -> str:
    """Say it back."""
    return f"echo: {text}"


@mcp.tool
def picture() -> Image:
    """A 1x1 image."""
    return Image(data=PNG, format="png")


@mcp.tool
async def slow(steps: int, ctx: Context) -> str:
    """Report progress while working."""
    for i in range(steps):
        await ctx.report_progress(i + 1, steps)
        await asyncio.sleep(0.05)
    return f"done {steps}"


@mcp.tool
async def roots(ctx: Context) -> str:
    """Ask the client for its roots (a server -> client request)."""
    found = await ctx.session.list_roots()
    return ",".join(str(r.uri) for r in found.roots)


@mcp.tool
async def ask(ctx: Context) -> str:
    """Ask the person something (elicitation, another server -> client request)."""
    answer = await ctx.elicit("Your name?", response_type=str)
    return f"{answer.action}: {getattr(answer, 'data', None)}"


@mcp.tool
def crash() -> str:
    """Exit abruptly."""
    print("about to crash", file=sys.stderr, flush=True)
    os._exit(3)


@mcp.tool
def pid() -> int:
    """This process."""
    return os.getpid()


mcp.run(transport="stdio", show_banner=False)
