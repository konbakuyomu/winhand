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
import re
import subprocess
import time
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, TextResourceContents
from pydantic import Field

from . import approvals, coalesce, desktop, jobs, media, proc, transfer, uia, winenv
from . import ocr as screen_text
from .secret_prompt import PromptUnavailable, ask_pick

UIA_SCRIPT = Path(__file__).parent / "scripts" / "uia.ps1"
SEND_LIMIT = 20 * 1024 * 1024
MAX_JOB_WAIT_S = 40.0  # the reply must reach the client within its ~60 s limit
JOB_POLL_S = 0.5


async def wait_job(fetch, wait_s: float, regexes: list[re.Pattern[str]]) -> dict:
    """Poll a job's status until it is no longer running, its new output matches, or time is up."""
    began = time.monotonic()
    deadline = began + min(wait_s, MAX_JOB_WAIT_S)
    while True:
        status = await asyncio.to_thread(fetch)
        if status.get("error") or status.get("state") != "running":
            return status
        output = status.get("output") or ""
        hit = next((r.pattern for r in regexes if r.search(output)), None)
        if hit is not None:
            return {**status, "matched": hit}
        if time.monotonic() >= deadline:
            return {
                **status,
                "waited_s": round(time.monotonic() - began, 1),
                "note": "still running; call job_status again with since=cursor (and wait_s) to keep waiting",
            }
        await asyncio.sleep(JOB_POLL_S)


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
    except (
        desktop.DesktopError,
        media.MediaError,
        jobs.JobError,
        transfer.TransferError,
        OSError,
        ValueError,
    ) as exc:
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
    """A window's controls: Windows' native UI Automation in process (each element keeps its live
    `_element`), or the PowerShell script when that cannot start here; classic controls merged in."""
    try:
        snapshot = uia.snapshot(hwnd)  # fast; its own time limit is for programs that hang
        snapshot["backend"] = "native"
    except uia.Unavailable as exc:
        snapshot = _run_uia(hwnd, Mode="inspect", timeout_s=timeout_s)
        snapshot["backend"] = "powershell"
        snapshot["backend_note"] = f"native UI Automation unavailable: {exc}"
    snapshot["elements"] = merge_controls(snapshot["elements"], desktop.native_controls(hwnd))
    return snapshot


def _read_back(hwnd: int, target: dict) -> dict | None:
    """The control's state now, from its own UI Automation element read again (None when it is
    gone), with the classic control's facts merged in as inspect does."""
    fresh = uia.refresh(target["_element"])
    if fresh is None:
        return None
    fresh["index"] = 0
    classic = [c for c in desktop.native_controls(hwnd) if fresh.get("hwnd") and c["hwnd"] == fresh["hwnd"]]
    merged = merge_controls([fresh], classic)
    return merged[0] if merged else fresh


WAIT_POLL_S = 0.7
INVOKE_WAIT_S = 5.0  # UI Automation's Invoke returns only when the control's handler does


def _control_state(element: dict | None, state: str) -> bool:
    if state == "gone":
        return element is None
    if element is None:
        return False
    if state == "exists":
        return True
    if state == "enabled":
        return element.get("enabled") is not False
    kind, _, wanted = state.partition(":")
    if kind == "value":
        return wanted in str(element.get("value") or element.get("name") or "")
    return str(element.get("toggle")) == wanted


def wait_control(
    window, name, automation_id, control_type, state: str, timeout_s: float, inspect=None
) -> dict:
    """Poll a window's controls until one matching name/automation_id/type reaches `state`."""
    if not (name or automation_id or control_type):
        raise ValueError("wait needs name, automation_id or control_type")
    if not (state in ("exists", "gone", "enabled") or state.startswith(("value:", "toggle:"))):
        raise ValueError("state is exists, gone, enabled, value:<text>, toggle:On or toggle:Off")
    inspect = inspect or (lambda hwnd: _inspect_controls(hwnd, timeout_s=10)["elements"])
    began = time.monotonic()
    deadline = began + min(timeout_s, 40)
    element = None
    title = None
    problem = None  # the latest reading error while the window was there
    while True:
        unknown = False
        try:
            w = desktop.find_window(window)
        except desktop.DesktopError:
            w = None  # the window itself is not there (yet, or any more)
        element = None
        if w is not None:
            if w.get("admin"):
                raise desktop.admin_error(w)
            title = w["title"]
            try:
                found = match_controls(inspect(w["hwnd"]), name, automation_id, control_type)
                element = found[0] if found else None
            except uia.NoAnswer:
                raise  # a hung program is not a control that went away
            except desktop.DesktopError as exc:
                try:  # changing under the reading: gone only if the window is gone
                    desktop.find_window(w["hwnd"])
                    unknown, problem = True, str(exc)
                except desktop.DesktopError:
                    pass
        if not unknown and _control_state(element, state):
            return {
                "matched": True,
                "state": state,
                "window": title,
                "element": _public(element) if element else None,
                "waited_s": round(time.monotonic() - began, 1),
            }
        if time.monotonic() >= deadline:
            out = {
                "matched": False,
                "state": state,
                "window": title,
                "element": _public(element) if element else None,
                "waited_s": round(time.monotonic() - began, 1),
                "note": "not reached yet; call again to keep waiting, or look with screenshot",
            }
            if unknown and problem:
                out["last_error"] = problem  # the window could not be read at the end
            return out
        time.sleep(WAIT_POLL_S)


def wait_window(target, state: str, timeout_s: float, find=None) -> dict:
    if state not in ("exists", "gone"):
        raise ValueError("state is exists or gone")
    find = find or desktop.find_window
    began = time.monotonic()
    deadline = began + min(timeout_s, 40)
    while True:
        try:
            w = find(target)
        except desktop.DesktopError:
            w = None
        if (w is not None) == (state == "exists"):
            return {
                "matched": True,
                "state": state,
                "window": w,
                "waited_s": round(time.monotonic() - began, 1),
            }
        if time.monotonic() >= deadline:
            return {
                "matched": False,
                "state": state,
                "window": w,
                "waited_s": round(time.monotonic() - began, 1),
                "note": "not reached yet; call again to keep waiting",
            }
        time.sleep(WAIT_POLL_S)


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
        offset: Annotated[
            int | None,
            Field(
                ge=0, description="Write at this byte offset, cutting the file there first (resume a part)"
            ),
        ] = None,
        sha256: Annotated[
            str | None, Field(description="Expected sha256 of the whole file after this write (last part)")
        ] = None,
    ) -> dict:
        """Write binary content (base64) to a file on this machine, e.g. an image, archive or program.
        Returns the file's size and sha256 so a transfer in parts can be checked and resumed.
        For anything over a few KB prefer fs_upload_url: the bytes then skip the conversation."""

        def work():
            data = base64.b64decode(data_base64, validate=True)
            full = winenv.long_path(path)
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
            if offset is not None:
                if offset > (os.path.getsize(full) if os.path.exists(full) else 0):
                    raise ValueError(f"offset {offset} is past the end of {path}; resume at its current size")
                with open(full, "r+b" if os.path.exists(full) else "wb") as fh:
                    fh.truncate(offset)
                    fh.seek(offset)
                    fh.write(data)
            else:
                with open(full, "ab" if append else "wb") as fh:
                    fh.write(data)
            digest = hashlib.sha256(Path(full).read_bytes()).hexdigest()
            out = {"path": path, "written": len(data), "size": os.path.getsize(full), "sha256": digest}
            if sha256 and digest != sha256.strip().lower():
                out["error"] = "sha256 mismatch: the file is not what you sent; resend from a known offset"
            return out

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def fs_upload_url(
        path: Annotated[str, Field(description="Absolute path on this machine to create")],
        sha256: Annotated[str | None, Field(description="Expected sha256; a mismatch is refused")] = None,
        max_mb: Annotated[float, Field(gt=0, le=95)] = transfer.DEFAULT_MAX_MB,
        ttl_s: Annotated[float, Field(ge=30, le=1800)] = transfer.DEFAULT_TTL_S,
        overwrite: bool = False,
    ) -> dict:
        """A one-time link to upload one file from your own shell or sandbox straight to `path` on
        this machine (curl -T file URL), so its bytes never pass through the conversation. Use it
        for anything bigger than a few KB: patches, archives, builds. Single use, expires."""
        return await asyncio.to_thread(_errors, transfer.issue_upload, path, sha256, max_mb, ttl_s, overwrite)

    @mcp.tool
    async def fs_download_url(
        path: Annotated[str, Field(description="Absolute path of a file on this machine")],
        ttl_s: Annotated[float, Field(ge=30, le=1800)] = transfer.DEFAULT_TTL_S,
    ) -> dict:
        """A one-time link to download one file from this machine into your own shell or sandbox
        (curl -o name URL), for files too big for fs_send. Single use, expires."""
        return await asyncio.to_thread(_errors, transfer.issue_download, path, ttl_s)

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
        wait: Annotated[
            str | None,
            Field(
                description="change: return once the picture differs from the previous screenshot of the "
                "same target | stable: once it has stopped changing for stable_ms. Instead of sleeping"
            ),
        ] = None,
        timeout_s: Annotated[float, Field(ge=0, le=40, description="For wait: how long at most")] = 20,
        stable_ms: Annotated[
            int, Field(ge=200, le=10_000, description="For wait='stable': how long unchanged")
        ] = 1000,
    ):
        """Look at the screen: the whole desktop (default), one monitor, a region or one window
        (also when it is covered). Use it whenever you need to see what the person sees or check
        a program's state, instead of asking them for a screenshot. After this, input()
        coordinates are pixels of this image. wait='change'/'stable' waits (up to 40 s) for the
        picture to change or settle (a click's effect, loading, an animation) before returning it."""

        def work():
            data, fmt, facts = desktop.screenshot(
                window, monitor, region, max_side, wait, timeout_s, stable_ms
            )
            return media_result(facts, Image(data=data, format=fmt))

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def window(
        action: Annotated[
            str, Field(description="list | focus | minimize | maximize | restore | close | move | wait")
        ] = "list",
        target: Annotated[str | int | None, Field(description="Title part, process name or handle")] = None,
        left: int | None = None,
        top: int | None = None,
        width: int | None = None,
        height: int | None = None,
        state: Annotated[str, Field(description="For wait: exists (default) | gone")] = "exists",
        timeout_s: Annotated[float, Field(ge=0, le=40, description="For wait: how long at most")] = 20,
    ) -> dict:
        """List the visible windows and monitors, act on one window, or wait (up to 40 s) for a
        window to appear or go away (an installer step, a dialog, a program starting)."""

        def work():
            if action == "list":
                out = {"monitors": desktop.monitors(), "windows": desktop.windows()}
                screen = desktop.desktop_state()
                if screen != "ok":
                    out.update(desktop=screen, note=desktop.DESKTOP_MESSAGES[screen])
                return out
            if target is None:
                raise ValueError("give target (title part, process name or handle)")
            if action == "wait":
                return wait_window(target, state, timeout_s)
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
                description="inspect | invoke | click | toggle | select | expand | collapse | set_value | focus | wait"
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
        state: Annotated[
            str,
            Field(description="For wait: exists | gone | enabled | value:<text> | toggle:On | toggle:Off"),
        ] = "exists",
        timeout_s: Annotated[float, Field(ge=0, le=40, description="For wait: how long at most")] = 20,
    ) -> dict:
        """Read and operate a window's controls (buttons, fields, checkboxes, lists, trees, menus,
        open menus too) by name instead of pixels, also in classic Win32/WinForms/Delphi programs
        and installers. A program that draws its own interface (no controls): use ocr.
        inspect lists them; invoke/toggle/select/set_value/... find one by name/automation_id/
        type and use it without moving the mouse; click is a real mouse click on it.
        Actions run once, then read back the control. verified=true means its observable state
        matches; false means a mismatch; null means the outcome cannot be confirmed. For invoke/
        click on a button, inspect the resulting window before deciding whether to repeat it.
        wait polls (up to 40 s) until the control exists, is gone, is enabled or shows a value,
        e.g. an installer's Next button becoming enabled."""

        def work():
            if action == "wait":
                return wait_control(window, name, automation_id, control_type, state, timeout_s)
            w = desktop.find_window(window)
            if w.get("admin"):
                raise desktop.admin_error(w)
            hwnd = w["hwnd"]
            if action != "inspect":  # operating a program: its approval first (approvals.py)
                which = name or automation_id or control_type or ""
                desktop.require_operable(
                    hwnd, f"控件操作：{action}「{which}」" if which else f"控件操作：{action}"
                )
            snapshot = _inspect_controls(hwnd)
            elements = snapshot["elements"]
            if action == "inspect":
                shown = [
                    _public(e)
                    for e in elements
                    if not filter
                    or filter.lower()
                    in f"{e.get('name')} {e.get('automation_id')} {e.get('type')} {e.get('value')}".lower()
                ]
                listed = {
                    "window": w["title"],
                    "count": len(elements),
                    "shown": len(shown[:250]),
                    "elements": shown[:250],
                    "backend": snapshot.get("backend"),
                }
                if snapshot.get("backend_note"):
                    listed["backend_note"] = snapshot["backend_note"]
                return listed
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
                if (
                    target.get("native")
                    and action in ("invoke", "toggle", "select")
                    and target.get("type") in ("Button", "CheckBox", "RadioButton")
                ):
                    # a posted click: UI Automation's Invoke waits while the button's handler runs,
                    # all the time a modal dialog it opens stays open
                    done = desktop.native_act(target, action, value)
                if not done and target.get("_element") is not None and can:
                    handler = action in ("invoke", "toggle", "select", "expand")
                    try:
                        done = uia.act(
                            target["_element"],
                            action,
                            value,
                            timeout=INVOKE_WAIT_S if handler else uia.TIMEOUT_S,
                        )
                    except uia.NoAnswer:
                        if not handler:
                            raise
                        # the program is still in the control's handler, typically a dialog it opened
                        done = "sent; the program has not finished handling it (a dialog it opened may be waiting)"
                elif not done and target.get("uia_index") is not None and can:
                    done = _run_uia(
                        hwnd, Mode="act", Index=target["uia_index"], ExpectName=target["_expect"],
                        ExpectId=",".join(map(str, target.get("_runtime_id", []))),
                        Do=action, Value=value,
                    )["done"]  # fmt: skip
                if not done and target.get("native"):
                    done = desktop.native_act(target, action, value)
            rect = target.get("rect")
            if (
                not done
                and action in ("click", "invoke", "toggle", "select")
                and target.get("offscreen")
                and target.get("_element") is not None
                and "ScrollItem" in (target.get("patterns") or ())
            ):
                rect = uia.scroll_into_view(target["_element"]) or rect  # a list row out of sight
            if not done and action in ("click", "invoke", "toggle", "select") and rect:
                left, top, width, height = rect
                cx, cy = left + width // 2, top + height // 2
                try:
                    desktop.window_action(hwnd, "focus")
                except approvals.NotOperable:
                    raise
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
                    if target.get("_element") is not None:
                        after = _read_back(hwnd, target)
                        try:
                            title = desktop.find_window(hwnd)["title"]
                        except desktop.DesktopError:
                            pass  # the window closed: the element read-back says the rest
                    else:
                        snapshot = _inspect_controls(
                            hwnd, timeout_s=max(1, min(3, deadline - time.monotonic()))
                        )
                        title = snapshot["window"]
                        candidates = snapshot["elements"]
                        if target.get("_runtime_id"):
                            candidates = [
                                e for e in candidates if e.get("_runtime_id") == target["_runtime_id"]
                            ]
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
            out = {
                "done": done,
                "matches": len(found),
                "window": title,
                "element": _public(after) if after is not None else None,
                "verified": verified,
                "verification": reason,
                "backend": snapshot.get("backend"),
            }
            if snapshot.get("backend_note"):
                out["backend_note"] = snapshot["backend_note"]
            return out

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def ocr(
        action: Annotated[
            str, Field(description="read | find | click | double_click | right_click | move | wait")
        ] = "read",
        text: Annotated[
            str | None,
            Field(description="Text to find (all but read); case, full/half width and spaces do not matter"),
        ] = None,
        window: Annotated[
            str | int | None, Field(description="Window title part, process name or handle")
        ] = None,
        monitor: Annotated[int | None, Field(description="Monitor index from window(action='list')")] = None,
        region: Annotated[
            list[int] | None, Field(description="[left, top, width, height] in screen pixels")
        ] = None,
        state: Annotated[str, Field(description="For wait: exists (default) | gone")] = "exists",
        index: Annotated[int, Field(ge=0, description="Which match: top to bottom, left to right")] = 0,
        language: Annotated[
            str | None,
            Field(description="OCR language (en-US, zh-Hans-CN, ja ...); default: the user's plus English"),
        ] = None,
        timeout_s: Annotated[float, Field(ge=0, le=40, description="For wait: how long at most")] = 20,
    ) -> dict:
        """Read text on the screen with Windows' built-in OCR, also where there are no controls to
        read (canvases, games, remote desktops, self-drawn programs). read lists the lines with
        their rectangles; find locates a text; click/double_click/right_click/move act on it like
        a person (the window is brought to the front first); wait waits (up to 40 s) until the
        text appears or, with state='gone', disappears. Without an exact match, `similar` shows
        how OCR read the closest lines. Rectangles and centers are screen pixels: use them with
        input(..., coordinates='screen')."""

        def work():
            if action == "read":
                return screen_text.read(window, monitor, region, language)
            if not text:
                raise ValueError("give the text to find")
            if action == "find":
                return screen_text.find(text, window, monitor, region, language)
            if action == "wait":
                return screen_text.wait(text, window, monitor, region, state, timeout_s, language)
            return screen_text.act(action, text, window, monitor, region, index, language)

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
        wait_s: Annotated[
            float,
            Field(
                ge=0,
                description="Wait up to this long (max 40 s) for the job to finish or a pattern to appear; "
                "0 answers at once. Use instead of sleeping or polling",
            ),
        ] = 0,
        patterns: Annotated[
            list[str] | None, Field(description="Regexes: stop waiting once new output matches one")
        ] = None,
        tail: Annotated[
            int | None, Field(ge=1, description="Return at most this many characters of output (the end)")
        ] = None,
    ) -> dict:
        """State (running/finished/vanished), exit code and output of a background job or of a `run`
        that was still going (run-N). With wait_s it blocks until the job ends, new output matches
        `patterns`, or the wait runs out (then call again with since=cursor)."""
        if id is None:
            return await asyncio.to_thread(_errors, lambda: {"jobs": jobs.list_jobs()})
        limit = tail or 20_000

        def fetch() -> dict:
            if id.startswith("run-"):  # a `run` command that was still going when run answered
                return _errors(proc.run_status, id, since, limit)
            return _errors(jobs.status, id, since, limit)

        if wait_s <= 0:
            return await asyncio.to_thread(fetch)
        try:
            regexes = [re.compile(p, re.MULTILINE) for p in patterns or []]
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
        key = (
            "job_status",
            id,
            since,
            round(min(wait_s, MAX_JOB_WAIT_S), 1),
            coalesce.freeze(patterns),
            limit,
        )
        return await coalesce.shared(key, lambda: wait_job(fetch, wait_s, regexes))

    @mcp.tool
    async def job_stop(id: str) -> dict:
        """Stop a running background job (and its child processes)."""
        if id.startswith("run-"):
            return await asyncio.to_thread(_errors, proc.run_stop, id)
        return await asyncio.to_thread(_errors, jobs.stop, id)
