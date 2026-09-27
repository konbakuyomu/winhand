"""MCP tools for the visible desktop and for moving files between the machine and the AI:
screenshots, windows, mouse/keyboard, UI Automation, clipboard, file hand-over, background jobs."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, TextResourceContents
from pydantic import Field

from . import desktop, jobs, media, proc, winenv
from .secret_prompt import PromptUnavailable, ask_pick

UIA_SCRIPT = Path(__file__).parent / "scripts" / "uia.ps1"
SEND_LIMIT = 20 * 1024 * 1024


def _facts(data: dict) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, ensure_ascii=False))


def media_result(facts: dict, *blocks) -> ToolResult:
    """A result carrying pictures (or files): the facts as text and as structured content, then
    the blocks. The structured copy matters: a client that cached an older definition of the
    tool (with an output schema) rejects results without one."""
    content = [_facts(facts)]
    for block in blocks:
        content.append(block.to_image_content() if isinstance(block, Image) else block)
    return ToolResult(content=content, structured_content=facts)


def _errors(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except (desktop.DesktopError, media.MediaError, jobs.JobError, OSError, ValueError) as exc:
        return {"error": str(exc)}


def _run_uia(hwnd: int, *, timeout_s: float = 60, **params: Any) -> dict:
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
        argv, capture_output=True, timeout=timeout_s, env=winenv.build_env(), creationflags=0x08000000
    )
    out = done.stdout.decode("utf-8-sig", errors="replace").strip()
    if done.returncode != 0 or not out.startswith("{"):
        error = done.stderr.decode("utf-8", errors="replace").strip() or out
        raise desktop.DesktopError(
            "UI Automation: " + error.splitlines()[0][:400] if error else "UI Automation failed"
        )
    return json.loads(out)


_VAGUE_TYPES = {"Pane", "Custom", "Window", ""}
_VERB_PATTERNS = {
    "invoke": {"Invoke", "Toggle", "SelectionItem", "ExpandCollapse"},
    "toggle": {"Toggle"},
    "select": {"SelectionItem"},
    "expand": {"ExpandCollapse"},
    "collapse": {"ExpandCollapse"},
    "set_value": {"Value"},
}


def merge_controls(uia: list[dict], native: list[dict]) -> list[dict]:
    """One list of a window's controls: what UI Automation reports, with classic Win32 controls
    (which it often sees only as nameless panes) filled in from the controls themselves."""
    by_hwnd = {c["hwnd"]: c for c in native}
    out: list[dict] = []
    for element in uia:
        item = dict(element)
        item["uia_index"] = item.pop("index")
        item["_expect"] = item.get("name") or ""
        classic = by_hwnd.pop(item.get("hwnd") or 0, None)
        if classic:
            item["native"] = True
            if item.get("type") in _VAGUE_TYPES or not item.get("patterns"):
                item["type"] = classic["type"]
            if classic["type"] in ("Edit", "ComboBox"):
                # an edit box's window text is its content (a password, too), not its name
                item["name"] = item["_expect"] = ""
                if classic.get("value") is not None:
                    item["value"] = classic["value"]
            for key in ("name", "value", "toggle"):
                if classic.get(key) and not item.get(key):
                    item[key] = classic[key]
        elif item.get("type") in _VAGUE_TYPES and not (
            item.get("name") or item.get("automation_id") or item.get("patterns")
        ):
            continue  # a bare container window: nothing to read or operate
        out.append(item)
    out += [{**c, "native": True, "patterns": []} for c in native if c["hwnd"] in by_hwnd]
    for i, item in enumerate(out):
        item["index"] = i
    return out


def match_controls(
    elements: list[dict], name: str | None, automation_id: str | None, control_type: str | None
) -> list[dict]:
    def fits(e: dict) -> bool:
        text = e.get("name") or ""
        if name and not (text == name or fnmatch.fnmatchcase(text, name) or name.lower() in text.lower()):
            return False
        if automation_id and e.get("automation_id") != automation_id:
            return False
        return not control_type or (e.get("type") or "").lower() == control_type.lower()

    found = [e for e in elements if fits(e)]
    exact = [e for e in found if name and e.get("name") == name]
    return exact or found


def _public(element: dict) -> dict:
    return {k: v for k, v in element.items() if not k.startswith("_")}


def _inspect_controls(hwnd: int, *, timeout_s: float = 60) -> dict:
    snapshot = _run_uia(hwnd, Mode="inspect", timeout_s=timeout_s)
    snapshot["elements"] = merge_controls(snapshot["elements"], desktop.native_controls(hwnd))
    return snapshot


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
            return media_result(facts, EmbeddedResource(type="resource", resource=resource))

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
            return media_result(facts, Image(data=data, format=fmt))

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
            return media_result({**done, "screenshot": facts}, Image(data=data, format=fmt))

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
        index: Annotated[int, Field(ge=0, description="Which match, when several controls match")] = 0,
        value: Annotated[str | None, Field(description="For set_value")] = None,
        filter: Annotated[
            str | None, Field(description="For inspect: only controls containing this text")
        ] = None,
        verify_wait_s: Annotated[
            float,
            Field(
                ge=0, le=10, description="Poll read-back state for this long after an action; 0 reads once"
            ),
        ] = 3,
    ) -> dict:
        """Read and operate a window's controls (buttons, fields, checkboxes, lists, menus) by
        name instead of pixels, also in classic Win32/WinForms/Delphi programs and installers.
        inspect lists them; invoke/toggle/select/set_value/... find one by name/automation_id/
        type and use it without moving the mouse; click is a real mouse click on it.
        Actions run once, then read back the control. verified=true means its observable state
        matches; false means a mismatch; null means the outcome cannot be confirmed. For invoke/
        click on a button, inspect the resulting window before deciding whether to repeat it."""

        def work():
            w = desktop.find_window(window)
            hwnd = w["hwnd"]
            elements = _inspect_controls(hwnd)["elements"]
            if action == "inspect":
                shown = [
                    _public(e)
                    for e in elements
                    if not filter
                    or filter.lower()
                    in f"{e.get('name')} {e.get('automation_id')} {e.get('type')} {e.get('value')}".lower()
                ]
                return {
                    "window": w["title"],
                    "count": len(elements),
                    "shown": len(shown[:250]),
                    "elements": shown[:250],
                }
            found = match_controls(elements, name, automation_id, control_type)
            if not found:
                raise desktop.DesktopError(
                    f"no control matches name={name!r} automation_id={automation_id!r} "
                    f"type={control_type!r}; use action='inspect' to see the controls"
                )
            if index >= len(found):
                raise desktop.DesktopError(f"only {len(found)} controls match; index {index} is out of range")
            target = found[index]
            if target.get("enabled") is False:
                raise desktop.DesktopError(f"the control is disabled right now: {_public(target)}")
            done = None
            if action != "click":
                patterns = set(target.get("patterns") or ())
                can = action == "focus" or patterns & _VERB_PATTERNS.get(action, set())
                if target.get("uia_index") is not None and can:
                    done = _run_uia(
                        hwnd, Mode="act", Index=target["uia_index"], ExpectName=target["_expect"],
                        ExpectId=",".join(map(str, target.get("_runtime_id", []))),
                        Do=action, Value=value,
                    )["done"]  # fmt: skip
                if not done and target.get("native"):
                    done = desktop.native_act(target, action, value)
            if not done and action in ("click", "invoke", "toggle", "select") and target.get("rect"):
                left, top, width, height = target["rect"]
                cx, cy = left + width // 2, top + height // 2
                try:
                    desktop.window_action(hwnd, "focus")
                except desktop.DesktopError:
                    pass  # the control may be visible anyway (topmost, or already in front)
                desktop.input_action("click", cx, cy, coordinates="screen")
                done = f"clicked at ({cx}, {cy})"
            if not done:
                raise desktop.DesktopError(f"the control does not support {action}: {_public(target)}")

            expected = {}
            if action == "set_value" and target.get("value") != "(hidden)":
                expected = {"value": value or ""}
            elif (action == "toggle" or done == "toggled") and target.get("toggle") in ("On", "Off"):
                expected = {"toggle": "Off" if target["toggle"] == "On" else "On"}
            elif action == "select" or done == "selected":
                expected = {"selected": True} if "selected" in target else {"toggle": "On"}
            elif action in ("expand", "collapse") or done == "expanded":
                expected = {"expanded": "Collapsed" if action == "collapse" else "Expanded"}
            elif action == "focus":
                expected = {"focused": True}

            deadline = time.monotonic() + verify_wait_s
            after = None
            verified = None
            title = w["title"]
            reason = "Action sent; inspect its outcome before repeating it."
            while True:
                try:
                    snapshot = _inspect_controls(hwnd, timeout_s=max(1, min(3, deadline - time.monotonic())))
                    title = snapshot["window"]
                    candidates = snapshot["elements"]
                    if target.get("_runtime_id"):
                        candidates = [e for e in candidates if e.get("_runtime_id") == target["_runtime_id"]]
                    elif target.get("hwnd"):
                        candidates = [e for e in candidates if e.get("hwnd") == target["hwnd"]]
                    elif target.get("automation_id"):
                        candidates = [
                            e
                            for e in candidates
                            if e.get("automation_id") == target["automation_id"]
                            and e.get("type") == target.get("type")
                        ]
                    else:
                        candidates = []  # an old list index cannot identify a control after redraw
                    after = candidates[0] if len(candidates) == 1 else None
                    if after is not None and expected and all(after.get(key) is not None for key in expected):
                        verified = all(after[key] == val for key, val in expected.items())
                        reason = (
                            "Read-back matches the requested state."
                            if verified
                            else "Read-back does not match yet."
                        )
                    else:
                        verified = None
                        reason = (
                            "Cannot confirm the control state; inspect the resulting window before repeating."
                        )
                except (desktop.DesktopError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                    reason = f"Action sent, but read-back failed: {exc}. Inspect before repeating."
                    after, verified = None, None
                    break
                if verified is True or not expected or time.monotonic() >= deadline:
                    break
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
            return {
                "done": done,
                "matches": len(found),
                "window": title,
                "element": _public(after) if after is not None else None,
                "verified": verified,
                "verification": reason,
            }

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
                return media_result(got, Image(data=data, format=fmt))
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
        if id.startswith("run-"):  # a `run` command that was still going when run answered
            return await asyncio.to_thread(_errors, proc.run_status, id, since)
        return await asyncio.to_thread(_errors, jobs.status, id, since)

    @mcp.tool
    async def job_stop(id: str) -> dict:
        """Stop a running background job (and its child processes)."""
        if id.startswith("run-"):
            return await asyncio.to_thread(_errors, proc.run_stop, id)
        return await asyncio.to_thread(_errors, jobs.stop, id)
