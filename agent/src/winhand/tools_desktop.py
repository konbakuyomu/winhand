"""MCP tools for the visible desktop and for moving files between the machine and the AI:
screenshots, windows, mouse/keyboard, UI Automation, clipboard, file hand-over, background jobs."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import subprocess
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, TextResourceContents
from pydantic import Field

from . import desktop, jobs, media, winenv
from .secret_prompt import PromptUnavailable, ask_pick

UIA_SCRIPT = Path(__file__).parent / "scripts" / "uia.ps1"
SEND_LIMIT = 20 * 1024 * 1024


def _facts(data: dict) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, ensure_ascii=False))


def _errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (desktop.DesktopError, media.MediaError, jobs.JobError, OSError, ValueError) as exc:
        return {"error": str(exc)}


def _run_uia(hwnd: int, **params: Any) -> dict:
    shell = jobs._powershell()
    argv = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(UIA_SCRIPT),
        "-Hwnd",
        str(hwnd),
    ]
    for key, value in params.items():
        if value is None or value == "":
            continue
        argv += [f"-{key}", str(value)]
    done = subprocess.run(
        argv, capture_output=True, timeout=60, env=winenv.build_env(), creationflags=0x08000000
    )
    out = done.stdout.decode("utf-8-sig", errors="replace").strip()
    if done.returncode != 0 or not out.startswith("{"):
        error = done.stderr.decode("utf-8", errors="replace").strip() or out
        raise desktop.DesktopError(
            "UI Automation: " + error.splitlines()[0][:400] if error else "UI Automation failed"
        )
    return json.loads(out)


def register(mcp: FastMCP) -> None:
    # ------------------------------------------------------------ hand-over

    @mcp.tool
    async def fs_send(path: str):
        """Hand a file of any format to the AI client as-is (≤ 20 MB): the client can save it or
        open it (images, PDF ...). Use fs_read to *look at* a file, fs_send to *receive* it."""

        def work():
            full = winenv.long_path(path)
            if not os.path.isfile(full):
                raise ValueError(f"no such file: {path}")
            size = os.path.getsize(full)
            if size > SEND_LIMIT:
                raise ValueError(
                    f"{size} bytes is over the 20 MB hand-over limit; compress or split it first"
                )
            data = Path(full).read_bytes()
            mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
            name = Path(path).name
            uri = "file:///" + name
            if mime.startswith("text/") or mime in ("application/json", "application/xml"):
                try:
                    resource = TextResourceContents(uri=uri, mimeType=mime, text=data.decode("utf-8"))
                except UnicodeDecodeError:
                    resource = BlobResourceContents(
                        uri=uri, mimeType=mime, blob=base64.b64encode(data).decode()
                    )
            else:
                resource = BlobResourceContents(uri=uri, mimeType=mime, blob=base64.b64encode(data).decode())
            facts = {
                "path": path,
                "name": name,
                "bytes": size,
                "mime": mime,
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            return [_facts(facts), EmbeddedResource(type="resource", resource=resource)]

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def fs_write_bytes(
        path: str,
        data_base64: Annotated[str, Field(description="File content, base64-encoded")],
        append: Annotated[bool, Field(description="Append (send big files in several parts)")] = False,
    ) -> dict:
        """Write binary content (base64) to a file on this machine, e.g. an image, archive or program."""

        def work():
            data = base64.b64decode(data_base64, validate=True)
            full = winenv.long_path(path)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            with open(full, "ab" if append else "wb") as fh:
                fh.write(data)
            digest = hashlib.sha256(Path(full).read_bytes()).hexdigest()
            return {"path": path, "written": len(data), "size": os.path.getsize(full), "sha256": digest}

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def fs_pick(
        title: str = "选择要交给 AI 的文件",
        mode: Annotated[str, Field(description="file | files | folder | save")] = "file",
        initial_dir: str | None = None,
    ) -> dict:
        """Let the person choose file(s) or a folder in a dialog on this machine (how they "upload"
        something to you). Returns the chosen paths; then fs_read / fs_send them."""
        try:
            value = await asyncio.to_thread(ask_pick, title, mode, initial_dir)
        except (PromptUnavailable, ValueError) as exc:
            return {"error": str(exc)}
        if not value:
            return {"cancelled": True}
        paths = value if isinstance(value, list) else [value]
        return {"paths": [os.path.normpath(p) for p in paths]}

    # ------------------------------------------------------------- screen

    @mcp.tool
    async def screenshot(
        window: Annotated[
            str | int | None, Field(description="Window title part, process name (notepad.exe) or handle")
        ] = None,
        monitor: Annotated[int | None, Field(description="Monitor index from window(action='list')")] = None,
        region: Annotated[
            list[int] | None, Field(description="[left, top, width, height] in screen pixels")
        ] = None,
        max_side: Annotated[int, Field(description="Longest edge of the returned image")] = media.MAX_SIDE,
    ):
        """Look at the screen: the whole desktop (default), one monitor, a region or one window
        (also when it is covered). After this, input() coordinates are pixels of this image."""

        def work():
            data, fmt, facts = desktop.screenshot(window, monitor, region, max_side)
            return [_facts(facts), Image(data=data, format=fmt)]

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def window(
        action: Annotated[
            str, Field(description="list | focus | minimize | maximize | restore | close | move")
        ] = "list",
        target: Annotated[str | int | None, Field(description="Title part, process name or handle")] = None,
        left: int | None = None,
        top: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> dict:
        """List the visible windows and monitors, or act on one window."""

        def work():
            if action == "list":
                return {"monitors": desktop.monitors(), "windows": desktop.windows()}
            if target is None:
                raise ValueError("give target (title part, process name or handle)")
            return desktop.window_action(target, action, left, top, width, height)

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def input(
        action: Annotated[
            str,
            Field(
                description="click | double_click | right_click | move | drag | scroll | hscroll | type | keys"
            ),
        ],
        x: float | None = None,
        y: float | None = None,
        to_x: float | None = None,
        to_y: float | None = None,
        text: Annotated[str | None, Field(description="For type: any text, Chinese included")] = None,
        keys: Annotated[
            str | None, Field(description="For keys: 'ctrl+s', 'alt+tab', 'win+r', 'tab tab enter'")
        ] = None,
        amount: Annotated[int, Field(description="Scroll notches, positive = up/right")] = 3,
        button: str = "left",
        coordinates: Annotated[
            str, Field(description="screenshot (pixels of the latest screenshot, default) | screen")
        ] = "screenshot",
        screenshot_after: Annotated[
            bool, Field(description="Return a fresh screenshot of the result")
        ] = False,
    ):
        """Mouse and keyboard on the real desktop, like a person. Take a screenshot first and use
        its pixel coordinates. Input cannot reach UAC prompts, the lock screen or admin windows."""

        def work():
            done = desktop.input_action(action, x, y, to_x, to_y, button, text, keys, amount, coordinates)
            if not screenshot_after:
                return done
            data, fmt, facts = desktop.screenshot()
            return [_facts({**done, "screenshot": facts}), Image(data=data, format=fmt)]

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def ui(
        window: Annotated[str | int, Field(description="Title part, process name or handle")],
        action: Annotated[
            str,
            Field(
                description="inspect | invoke | click | toggle | select | expand | collapse | set_value | focus"
            ),
        ] = "inspect",
        name: Annotated[str | None, Field(description="Control name (exact, wildcard or part)")] = None,
        automation_id: str | None = None,
        control_type: Annotated[str | None, Field(description="Button, Edit, CheckBox, ListItem ...")] = None,
        index: Annotated[int, Field(description="Which match, when several controls match")] = 0,
        value: Annotated[str | None, Field(description="For set_value")] = None,
        filter: Annotated[
            str | None, Field(description="For inspect: only controls containing this text")
        ] = None,
    ) -> dict:
        """Read and operate a window's controls through UI Automation (buttons, fields, checkboxes,
        lists, menus): more reliable than clicking pixels. inspect lists them; the other actions
        find one by name/automation_id/type and use it (falling back to a real click when needed)."""

        def work():
            w = desktop.find_window(window)
            if action == "inspect":
                return _run_uia(w["hwnd"], Mode="inspect", Filter=filter)
            verb = "invoke" if action == "click" else action
            result = _run_uia(
                w["hwnd"], Mode="act", Name=name, AutomationId=automation_id, ControlType=control_type,
                Index=index, Do=verb, Value=value,
            )  # fmt: skip
            rect = (result.get("element") or {}).get("rect")
            if (
                (action == "click" or not result.get("done"))
                and verb in ("invoke", "toggle", "select")
                and rect
            ):
                cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
                desktop.window_action(w["hwnd"], "focus")
                desktop.input_action("click", cx, cy, coordinates="screen")
                result["done"] = f"clicked at ({cx}, {cy})"
            if not result.get("done"):
                raise desktop.DesktopError(f"the control does not support {action}: {result.get('element')}")
            return result

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def clipboard(
        action: Annotated[str, Field(description="get | set")] = "get",
        text: str | None = None,
        image_path: Annotated[
            str | None, Field(description="For set: put this image on the clipboard")
        ] = None,
    ):
        """Read the clipboard (text, an image, or files copied in Explorer) or put text/an image on it."""

        def work():
            if action == "set":
                return desktop.clipboard_set(text, image_path)
            got = desktop.clipboard_get()
            if got["kind"] == "image":
                data, fmt = got.pop("image")
                return [_facts(got), Image(data=data, format=fmt)]
            return got

        return await asyncio.to_thread(_errors, work)

    # --------------------------------------------------------------- jobs

    @mcp.tool
    async def job_start(
        command: Annotated[str, Field(description="PowerShell command(s) to run")],
        cwd: str | None = None,
        name: str | None = None,
        elevated: Annotated[
            bool, Field(description="Run as administrator; the person must approve a UAC prompt")
        ] = False,
    ) -> dict:
        """Run a command in the background, independent of winhand: it keeps running even if
        winhand restarts or is reinstalled, and its output and exit code stay available via
        job_status. Use for long work, for anything that replaces winhand, and for admin tasks."""
        return await asyncio.to_thread(_errors, jobs.start, command, cwd, name, elevated)

    @mcp.tool
    async def job_status(
        id: Annotated[str | None, Field(description="Job id; omit to list recent jobs")] = None,
        since: Annotated[int, Field(description="Output cursor from the previous call")] = 0,
    ) -> dict:
        """State (running/finished/vanished), exit code and output of a background job."""
        if id is None:
            return await asyncio.to_thread(_errors, lambda: {"jobs": jobs.list_jobs()})
        return await asyncio.to_thread(_errors, jobs.status, id, since)

    @mcp.tool
    async def job_stop(id: str) -> dict:
        """Stop a running background job (and its child processes)."""
        return await asyncio.to_thread(_errors, jobs.stop, id)
