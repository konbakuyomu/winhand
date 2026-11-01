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
import threading
import time
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.tools.base import ToolResult
from fastmcp.utilities.types import Image
from mcp.types import BlobResourceContents, EmbeddedResource, TextContent, TextResourceContents
from pydantic import BaseModel, Field

from . import (
    approvals,
    coalesce,
    desktop,
    fs,
    jobs,
    jobwatch,
    media,
    presence,
    proc,
    secret_store,
    transfer,
    uia,
    waiting,
    winenv,
)
from . import ocr as screen_text
from .secret_prompt import PromptUnavailable, ask_pick


class UiStep(BaseModel):
    action: str
    name: str | None = None
    automation_id: str | None = None
    control_type: str | None = None
    ref: str | None = None
    index: int | None = None
    value: str | None = None
    timeout_s: float | None = None


UIA_SCRIPT = Path(__file__).parent / "scripts" / "uia.ps1"
SEND_LIMIT = 20 * 1024 * 1024
MAX_JOB_WAIT_S = 40.0  # the reply must reach the client within its ~60 s limit
JOB_POLL_S = 0.5


def _new_text(status: dict, stderr_before: int | None) -> str:
    """What a pattern is matched against: the new output, and stderr written during this wait."""
    text = status.get("output") or ""
    size, tail = status.get("stderr_size"), status.get("stderr") or ""
    if stderr_before is not None and size is not None and size > stderr_before:
        text += "\n" + tail[-(size - stderr_before) :]
    return text


async def wait_job(
    fetch,
    wait_s: float,
    regexes: list[re.Pattern[str]],
    ceiling: float = MAX_JOB_WAIT_S,
    watch=None,
) -> dict:
    """Poll a job's status until it is no longer running, its new output matches, or time is up.
    With `watch` (its activity, see jobwatch), the wait also ends when the job opens a new window
    (a dialog waiting for someone) or goes quiet with no CPU while we wait."""
    began = time.monotonic()
    deadline = began + min(wait_s, ceiling)
    stderr_before = None
    first: dict | None = None
    next_look = began + jobwatch.CHECK_EVERY_S
    while True:
        status = await asyncio.to_thread(fetch)
        if status.get("error") or status.get("state") != "running":
            return status
        if stderr_before is None:
            stderr_before = status.get("stderr_size", 0)
        output = _new_text(status, stderr_before)
        hit = next((r.pattern for r in regexes if r.search(output)), None)
        if hit is not None:
            return {**status, "matched": hit}
        now = time.monotonic()
        if watch is not None and (first is None or now >= next_look):
            activity = await asyncio.to_thread(watch)
            next_look = time.monotonic() + jobwatch.CHECK_EVERY_S
            if first is None:
                first = activity  # what was already there is no news
            else:
                before = {w["hwnd"] for w in first.get("windows", [])}
                new = [w for w in activity.get("windows", []) if w["hwnd"] not in before]
                if new:
                    return {
                        **status,
                        "matched": "window",
                        "activity": activity,
                        "note": "the job opened a window: read it (screenshot / ui inspect) before waiting on",
                    }
                if activity.get("stalled") and not first.get("stalled"):
                    return {**status, "matched": "stalled", "activity": activity}
                seen = {p["hwnd"] for p in first.get("prompts", [])}
                if any(p["hwnd"] not in seen for p in activity.get("prompts", [])):
                    return {
                        **status,
                        "matched": "prompt",
                        "activity": activity,
                        "note": jobwatch.PROMPTS_NOTE,
                    }
                if activity.get("secure_screen") and not first.get("secure_screen"):
                    return {
                        **status,
                        "matched": "secure_screen",
                        "activity": activity,
                        "note": jobwatch.SECURE_NOTE,
                    }
        if time.monotonic() >= deadline:
            out = {
                **status,
                "waited_s": round(time.monotonic() - began, 1),
                "note": "still running; call job_status again with since=cursor (and wait_s) to keep waiting",
            }
            if watch is not None:
                out["activity"] = await asyncio.to_thread(watch)
            return out
        await asyncio.sleep(JOB_POLL_S)


def _activity(job_id: str) -> dict:
    """processes, CPU, memory, quiet_s, stalled and windows of a running job or `run` command."""
    info = proc.watch_info(job_id) if _is_run(job_id) else jobs.watch_info(job_id)
    return jobwatch.snapshot(*info, key=job_id) if info else {}


def _file_tails(paths: list[str], lines: int = 20, max_bytes: int = 8192) -> dict:
    """The end of each file: what a job writes besides its own output (a router's log ...)."""
    out: dict = {}
    for path in paths:
        try:
            full = fs._p(path)
            info = os.stat(full)
            with open(full, "rb") as fh:
                fh.seek(max(0, info.st_size - max_bytes))
                text = proc._decode(fh.read())
            out[path] = {
                "size": info.st_size,
                "quiet_s": int(max(0.0, time.time() - info.st_mtime)),
                "tail": "\n".join(text.splitlines()[-lines:]),
            }
        except OSError as exc:
            out[path] = {"error": f"{type(exc).__name__}: {exc.strerror or exc}"}
    return out


MULTI_JOB_CHARS = 6_000  # output per job when several are waited on together

# What callers were already shown, per job and output cursor: the stderr size when that cursor was
# handed out. A poll that continues from such a cursor gets only what is new (see trim_repeat).
_shown: dict[str, dict[int, int | None]] = {}
_shown_lock = threading.Lock()
_SHOWN_PER_JOB = 64
_REPEATED = ("command", "log_file", "started", "elevated")
_UNSEEN = object()


def trim_repeat(job_id: str, since: int, status: dict, whole_stderr: bool = False) -> dict:
    """A poll used to repeat what the caller already had: the command line (often a whole script),
    the log path, and for programs run directly the last 6 KB of stderr, about half of every
    answer while a long job ran. When the caller continues from a cursor this job already handed
    out, leave those out and keep only the stderr written since. A job that has ended answers in
    full; `whole_stderr` (an explicit tail) keeps the stderr tail too."""
    if status.get("error") or "state" not in status:
        return status
    running = status["state"] == "running"
    size = status.get("stderr_size")
    with _shown_lock:
        marks = _shown.setdefault(job_id, {})
        seen = marks.get(since, _UNSEEN)
        if running:
            # the latest size: a job writing only stderr (Codex) keeps its output cursor, and each
            # poll must move on from the last one. Two callers polling one job at the same cursor
            # may each miss what the other was shown; tail= shows the end of stderr again.
            marks.pop(status.get("cursor", since), None)
            marks[status.get("cursor", since)] = size
            while len(marks) > _SHOWN_PER_JOB:
                marks.pop(next(iter(marks)))
        else:
            _shown.pop(job_id, None)
    if seen is _UNSEEN or not running:
        return status
    out = {k: v for k, v in status.items() if k not in _REPEATED}
    if not whole_stderr and "stderr" in out and size is not None and isinstance(seen, int):
        new = size - seen
        if new <= 0:
            out.pop("stderr")
            out.pop("stderr_note", None)
        elif new < len(out["stderr"]):
            out["stderr"] = out["stderr"][-new:]
            out.pop("stderr_note", None)
    return out


def _job_ids(value: Any) -> Any:
    """job_status(id=...) as models send it: a list, a JSON list written as a string, or several ids
    separated by commas or spaces (no job or run id contains either)."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(i) for i in parsed]
    parts = [p for p in re.split(r"[,\s]+", text.strip("[]")) if p]
    return parts if len(parts) > 1 else text


def _is_run(job_id: str) -> bool:
    """run-<n> is a `run` command that was still going when run answered; a background job may be
    named run-something too (run-all-sim-1016122643), and is a job."""
    return re.fullmatch(r"run-\d+", job_id) is not None


def _job_fetch(job_id: str, since: int, limit: int) -> dict:
    if _is_run(job_id):
        return _errors(proc.run_status, job_id, since, limit)
    return _errors(jobs.status, job_id, since, limit)


async def wait_jobs(
    ids: list[str],
    cursors: dict[str, int],
    limit: int,
    wait_s: float,
    regexes: list[re.Pattern[str]],
    ceiling: float = MAX_JOB_WAIT_S,
) -> dict:
    """Wait on several jobs at once: until one is no longer running, the new output of one matches,
    or time is up. Every job's state and new output come back, keyed by id."""
    began = time.monotonic()
    deadline = began + min(wait_s, ceiling)
    stderr_before: dict[str, int] = {}
    while True:
        statuses = {i: await asyncio.to_thread(_job_fetch, i, cursors[i], limit) for i in ids}
        hit = None
        for i, status in statuses.items():
            if status.get("error") or status.get("state") != "running":
                hit = {"id": i, "condition": "error" if status.get("error") else status.get("state")}
                break
            stderr_before.setdefault(i, status.get("stderr_size", 0))
            text = _new_text(status, stderr_before[i])
            pattern = next((r.pattern for r in regexes if r.search(text)), None)
            if pattern is not None:
                hit = {"id": i, "condition": "pattern", "pattern": pattern}
                break
        if hit or time.monotonic() >= deadline:
            break
        await asyncio.sleep(JOB_POLL_S)
    running = [i for i, st in statuses.items() if st.get("state") == "running"]
    out: dict[str, Any] = {
        "jobs": statuses,
        "cursors": {i: st.get("cursor", cursors[i]) for i, st in statuses.items()},
        "running": running,
    }
    if hit:
        out["hit"] = hit
    else:
        out["waited_s"] = round(time.monotonic() - began, 1)
    done = [i for i in ids if i not in running]
    out["next"] = (
        f"call again with since=cursors{' and without the jobs that ended (' + ', '.join(done) + ')' if done else ''}"
        " to keep waiting"
        if running
        else "every job has ended"
    )
    return out


def _facts(data: dict) -> TextContent:
    return TextContent(type="text", text=json.dumps(data, ensure_ascii=False))


def media_result(facts: dict, *blocks, structured_extra: dict | None = None) -> ToolResult:
    """A result carrying pictures (or files): the facts as text and as structured content, then
    the blocks. The structured copy matters: a client that cached an older definition of the
    tool (with an output schema) rejects results without one.

    Claude Code hands the model the structured content in place of the result's text blocks
    (pictures still come through), so anything the model must read goes into structured_extra
    too: it is added to the structured copy only, and clients that show the blocks see it once."""
    content = [_facts(facts)]
    for block in blocks:
        content.append(block.to_image_content() if isinstance(block, Image) else block)
    return ToolResult(content=content, structured_content={**facts, **(structured_extra or {})})


_UI_ACTIONS = (
    "inspect",
    "invoke",
    "click",
    "toggle",
    "select",
    "expand",
    "collapse",
    "set_value",
    "focus",
    "wait",
)


def _errors(fn, *args, **kwargs):
    try:
        with approvals.one_call():  # every approval this call waits for shares one ~40 s
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
        argv,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=timeout_s,
        env=winenv.build_env(),
        creationflags=0x08000000,
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


_CANDIDATE = ("name", "type", "automation_id", "value", "enabled", "rect")


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


REFS_KEPT = 8  # windows whose latest inspect keeps its refs
NEW_WINDOW_SETTLE_S = 0.6  # a dialog a button opens takes a moment to appear
UI_STEPS_MAX = 20
_refs: dict[int, list[dict]] = {}
_refs_lock = threading.Lock()


def _inspect_retrying(hwnd: int, *, timeout_s: float = 60) -> dict:
    """_inspect_controls, read again when the window changed while it was being read: a page
    redrawing made inspect itself fail with "the control is gone" (2026-11-01 review)."""
    for attempt in range(3):
        try:
            return _inspect_controls(hwnd, timeout_s=timeout_s)
        except uia.Gone:
            if attempt == 2:
                raise
            time.sleep(0.3)
    raise AssertionError("unreachable")


def _haystack(element: dict) -> str:
    return " ".join(
        str(element.get(k) or "") for k in ("name", "automation_id", "type", "value", "class")
    ).lower()


def _keep_refs(hwnd: int, elements: list[dict]) -> None:
    with _refs_lock:
        _refs.pop(hwnd, None)
        _refs[hwnd] = elements
        while len(_refs) > REFS_KEPT:
            del _refs[next(iter(_refs))]


def _by_ref(hwnd: int, ref: str) -> dict:
    """The control a ref of this window's latest inspect names (e12), as it is now. A live UI
    Automation element is read again; otherwise the control is found again in a new reading by
    its runtime id, its own window or its automation id."""
    with _refs_lock:
        elements = _refs.get(hwnd)
    if elements is None:
        raise desktop.DesktopError(f"no inspect of this window to take ref {ref!r} from: inspect it first")
    m = re.fullmatch(r"e?(\d+)", str(ref).strip(), re.IGNORECASE)
    if not m or not 1 <= int(m.group(1)) <= len(elements):
        raise desktop.DesktopError(
            f"ref {ref!r} is not in this window's latest inspect (e1 to e{len(elements)})"
        )
    stored = elements[int(m.group(1)) - 1]
    gone = f"ref {ref} is gone (the window changed): inspect it again"
    if stored.get("_element") is not None:
        fresh = uia.refresh(stored["_element"])
        if fresh is None:
            raise uia.Gone(gone)
        return {**stored, **fresh}
    candidates = _inspect_retrying(hwnd)["elements"]
    for key in ("_runtime_id", "hwnd"):
        if stored.get(key):
            hits = [e for e in candidates if e.get(key) == stored[key]]
            if len(hits) == 1:
                return hits[0]
    if stored.get("automation_id"):
        hits = [
            e for e in candidates if all(e.get(k) == stored.get(k) for k in ("automation_id", "type", "name"))
        ]
        if len(hits) == 1:
            return hits[0]
    raise uia.Gone(gone)


def _window_handles() -> set[int]:
    try:
        return {w["hwnd"] for w in desktop.windows()}
    except (desktop.DesktopError, OSError):
        return set()


def _brief(w: dict) -> dict:
    return {
        "hwnd": w["hwnd"],
        "title": w.get("title"),
        "process": w.get("process"),
        **({"dialog": True} if w.get("class") == "#32770" or w.get("owned") else {}),
    }


def _new_windows(before: set[int], settle_s: float = 0.0) -> list[dict]:
    """Top-level windows that were not there before (up to 5), waiting up to settle_s for one."""
    if not before:
        return []
    deadline = time.monotonic() + settle_s
    while True:
        try:
            new = [_brief(w) for w in desktop.windows() if w["hwnd"] not in before]
        except (desktop.DesktopError, OSError):
            return []
        if new or time.monotonic() >= deadline:
            return new[:5]
        time.sleep(0.15)


def _ui_inspect(
    w: dict, filter: str | None, name: str | None, automation_id: str | None, control_type: str | None
) -> dict:
    """A window's controls, each with a ref for the actions that follow. `filter` is plain text
    (case ignored) in a control's name, automation id, type, value or class; `a|b` takes either:
    filters written that way found nothing before (2026-11-01 review). name, automation_id and
    control_type narrow the list the way they pick a control for an action."""
    hwnd = w["hwnd"]
    snapshot = _inspect_retrying(hwnd)
    elements = snapshot["elements"]
    _keep_refs(hwnd, elements)
    refs = {id(e): f"e{i}" for i, e in enumerate(elements, 1)}
    picked = (
        match_controls(elements, name, automation_id, control_type)
        if (name or automation_id or control_type)
        else elements
    )
    terms = [t.strip().lower() for t in (filter or "").split("|") if t.strip()]
    shown = [
        {"ref": refs[id(e)], **_public(e)}
        for e in picked
        if not terms or any(t in _haystack(e) for t in terms)
    ]
    listed = {
        "window": w["title"],
        "count": len(elements),
        "shown": len(shown[:250]),
        "elements": shown[:250],
        "backend": snapshot.get("backend"),
        "refs": "act on a control by its ref: ui(window, action, ref='e12'); refs last until the next inspect",
    }
    if snapshot.get("backend_note"):
        listed["backend_note"] = snapshot["backend_note"]
    if not shown and (terms or name or automation_id or control_type):
        listed["note"] = "nothing matched; inspect without filter to see every control"
    return listed


def _ui_act(
    w: dict,
    action: str,
    *,
    name: str | None = None,
    automation_id: str | None = None,
    control_type: str | None = None,
    index: int | None = None,
    value: str | None = None,
    verify_wait_s: float = 3,
    ref: str | None = None,
) -> dict:
    """One action on one control (found by ref, or by name / automation_id / type), then its state
    read back, and any window that opened because of it."""
    hwnd = w["hwnd"]
    desktop.refuse_own(hwnd)  # WinHand's own dialogs: no need to look for the control first
    if ref is not None:
        found = [_by_ref(hwnd, ref)]
        snapshot = {"backend": "native" if found[0].get("_element") is not None else "powershell"}
    else:
        snapshot = _inspect_retrying(hwnd)
        found = match_controls(snapshot["elements"], name, automation_id, control_type)
    if not found:
        raise desktop.DesktopError(
            f"no control matches name={name!r} automation_id={automation_id!r} "
            f"type={control_type!r}; use action='inspect' to see the controls"
        )
    if index is None and len(found) > 1:
        return {
            "error": f"{len(found)} controls match; nothing was done. Pass index (the place in "
            "this list, from 0), automation_id or control_type",
            "matches": [
                {"index": i, **{k: e.get(k) for k in _CANDIDATE if e.get(k) not in (None, "")}}
                for i, e in enumerate(found[:20])
            ],
        }
    chosen = index or 0
    if chosen >= len(found):
        raise desktop.DesktopError(f"only {len(found)} controls match; index {chosen} is out of range")
    target = found[chosen]
    if target.get("enabled") is False:
        raise desktop.DesktopError(f"the control is disabled right now: {_public(target)}")
    # operating the program: the person's approval first (approvals.py), once the control is known
    which = target.get("name") or name or automation_id or control_type or ""
    desktop.require_operable(hwnd, f"控件操作：{action}「{which}」" if which else f"控件操作：{action}")
    before = _window_handles()
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
        if w.get("pid") and desktop.pid_at(cx, cy) != w["pid"]:
            # a real click lands on whatever is there: another program, or what the
            # control scrolled away from
            hidden = " (UI Automation reports it out of sight)" if target.get("offscreen") else ""
            raise desktop.DesktopError(
                f"another window is at ({cx}, {cy}), where the control is{hidden}; nothing was "
                "clicked. Bring the window to the front or scroll to the control, then try again"
            )
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
                    "Read-back matches the requested state." if verified else "Read-back does not match yet."
                )
            else:
                verified = None
                reason = "Cannot confirm the control state; inspect the resulting window before repeating."
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
    if ref is not None:
        out["ref"] = ref
    # a dialog the action opened (a confirmation, an error, a file picker) is what to look at next
    new = _new_windows(before, settle_s=0 if expected else NEW_WINDOW_SETTLE_S)
    if new:
        out["new_windows"] = new
        out["new_windows_note"] = (
            "a window opened after this: read it (screenshot or ui inspect) before going on"
        )
    return out


def _ui_steps(w: dict, steps: list[dict], verify_wait_s: float) -> dict:
    """Several actions on one window in order, stopping at the first that fails or does not take;
    then a picture of where it ended (FlaUI-MCP's windows_batch, computer use's action lists)."""
    hwnd = w["hwnd"]
    if not steps:
        raise desktop.DesktopError("give steps=[{action, name | ref | automation_id ..., value}, ...]")
    if len(steps) > UI_STEPS_MAX:
        raise desktop.DesktopError(f"at most {UI_STEPS_MAX} steps in one call")
    results: list[dict] = []
    stopped = None
    shot_of = hwnd
    for n, step in enumerate(steps, 1):
        action = (step.get("action") or "").strip()
        try:
            with presence.acting(f"ui step {n} {action}"):  # the stop keys end the steps here
                if action in ("keys", "type"):
                    text = step.get("value") or ""
                    desktop.require_operable(hwnd, f"按键：{text[:40]}" if action == "keys" else "输入文字")
                    try:
                        desktop.window_action(hwnd, "focus")
                    except (desktop.DesktopUnavailable, approvals.NotOperable):
                        raise
                    except desktop.DesktopError:
                        pass  # it may be in front anyway
                    if action == "keys":
                        done = desktop.input_action("keys", keys=text)
                    else:
                        done = desktop.input_action("type", text=text)
                    result = {"done": action, "foreground": done.get("foreground")}
                elif action == "wait":
                    result = wait_control(
                        hwnd,
                        step.get("name"),
                        step.get("automation_id"),
                        step.get("control_type"),
                        step.get("value") or "exists",
                        min(float(step.get("timeout_s") or 10), 20),
                    )
                elif action in _UI_ACTIONS and action not in ("inspect", "wait"):
                    result = _ui_act(
                        w,
                        action,
                        name=step.get("name"),
                        automation_id=step.get("automation_id"),
                        control_type=step.get("control_type"),
                        index=step.get("index"),
                        value=step.get("value"),
                        verify_wait_s=min(verify_wait_s, 2),
                        ref=step.get("ref"),
                    )
                else:
                    result = {
                        "error": f"step action is one of {', '.join(a for a in _UI_ACTIONS if a != 'inspect')}, keys, type"
                    }
        except (desktop.DesktopError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            result = {"error": str(exc)}
        results.append({"step": n, "action": action, **result})
        if result.get("error"):
            stopped = f"step {n} failed; the steps after it were not done"
        elif result.get("matched") is False:
            stopped = f"step {n}: the wait ran out; the steps after it were not done"
        elif result.get("verified") is False:
            stopped = f"step {n}: the control does not show the change; the steps after it were not done"
        if result.get("new_windows"):
            shot_of = result["new_windows"][0]["hwnd"]
        if stopped:
            break
    out: dict = {
        "window": w["title"],
        "done": sum(1 for r in results if not r.get("error")),
        "steps": results,
    }
    if stopped:
        out["stopped"] = stopped
    return {"out": out, "shot_of": shot_of}


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
        path: Annotated[
            str,
            Field(
                description="Absolute path on this machine to create (with extract: the folder to unpack into)"
            ),
        ],
        sha256: Annotated[
            str | None, Field(description="Expected sha256 of what you send; a mismatch is refused")
        ] = None,
        max_mb: Annotated[float, Field(gt=0, le=95)] = transfer.DEFAULT_MAX_MB,
        ttl_s: Annotated[float, Field(ge=30, le=1800)] = transfer.DEFAULT_TTL_S,
        overwrite: bool = False,
        extract: Annotated[
            bool,
            Field(
                description="Send a zip or tar (.tar.gz, .tgz ...) and have it unpacked into the folder `path`: "
                "many files in one upload. All or nothing: unsafe names, files the rules protect, or files "
                "that exist (without overwrite) refuse the whole archive"
            ),
        ] = False,
        size: Annotated[
            int | None,
            Field(
                gt=0,
                description="The file's size in bytes. Over part_mb it goes in parts, one link each: use it "
                "for anything over about 8 MB (bigger single uploads time out in some sandboxes)",
            ),
        ] = None,
        part_mb: Annotated[float, Field(ge=1, le=64)] = transfer.DEFAULT_PART_MB,
    ) -> dict:
        """A one-time link to upload from your own shell or sandbox straight to `path` on this
        machine (curl -T file URL), so the bytes never pass through the conversation. Use it for
        anything bigger than a few KB: patches, archives, builds. Several files: zip or tar them and
        pass extract=true. Big files: pass size= and send the parts. Single use, expires."""
        return await asyncio.to_thread(
            _errors, transfer.issue_upload, path, sha256, max_mb, ttl_s, overwrite, extract, size, part_mb
        )

    @mcp.tool
    async def fs_download_url(
        path: Annotated[
            str | None, Field(description="Absolute path of a file, or of a folder (downloads as one zip)")
        ] = None,
        ttl_s: Annotated[float, Field(ge=30, le=1800)] = transfer.DEFAULT_TTL_S,
        paths: Annotated[
            list[str] | None,
            Field(description="Several files and folders in one zip (with or instead of path)"),
        ] = None,
        include: Annotated[
            list[str] | None,
            Field(
                description="Folders: only files matching one of these, by path below the folder or by name "
                '("*.log", "src/*.py")'
            ),
        ] = None,
        exclude: Annotated[
            list[str] | None,
            Field(
                description='Folders: leave out files and folders matching these ("node_modules", ".git", "*.tmp")'
            ),
        ] = None,
    ) -> dict:
        """A one-time link to download from this machine into your own shell or sandbox (curl -o
        name URL), for files too big for fs_send. A folder, or several paths, come as one zip made
        while it downloads; files the read rules protect are left out (the answer says which).
        Single use, expires."""
        return await asyncio.to_thread(_errors, transfer.issue_download, path, ttl_s, paths, include, exclude)

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
        max_side: Annotated[
            int,
            Field(
                description="Longest edge of the returned image; it also stays under about 1.15 MP "
                "(more for a larger max_side), the most a model takes without scaling it again"
            ),
        ] = media.MAX_SIDE,
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
        path: Annotated[
            str | None,
            Field(description="Also save the full-resolution picture to this file here (.png, or .jpg)"),
        ] = None,
    ):
        """Look at the screen: the whole desktop (default), one monitor, a region or one window
        (also when it is covered or minimized). Use it whenever you need to see what the person
        sees or check a program's state, instead of asking them for a screenshot. After this,
        input() coordinates are pixels of this image. wait='change'/'stable' waits (up to 40 s) for
        the picture to change or settle (a click's effect, loading, an animation) before returning
        it; without a window, 'stable' looks at the window in front."""

        def work():
            data, fmt, facts = desktop.screenshot(
                window, monitor, region, max_side, wait, timeout_s, stable_ms, path
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
                description="click | double_click | triple_click | right_click | move | drag | scroll | hscroll "
                "| mouse_down | mouse_up | type | keys | hold_key | cursor_position"
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
        modifiers: Annotated[
            list[str] | str | None,
            Field(
                description="Held during a mouse action: ['ctrl'], 'shift', 'ctrl+alt' (ctrl+click, shift+scroll)"
            ),
        ] = None,
        window: Annotated[
            str | int | None,
            Field(
                description="x, y are pixels of this window (as in a screenshot of it); it is brought to the "
                "front first, and keys and text go to it"
            ),
        ] = None,
        duration_s: Annotated[float, Field(ge=0.05, le=10, description="For hold_key: how long")] = 1.0,
        repeat: Annotated[
            int, Field(ge=1, le=50, description="For keys: press the sequence this many times")
        ] = 1,
    ):
        """Mouse and keyboard on the real desktop, like a person. Take a screenshot first and use
        its pixel coordinates (or window= and that window's pixels). cursor_position tells where the
        pointer is. Input cannot reach UAC prompts, the lock screen or admin windows."""

        def work():
            done = desktop.input_action(
                action,
                x,
                y,
                to_x,
                to_y,
                button,
                text,
                keys,
                amount,
                coordinates,
                modifiers,
                duration_s,
                repeat,
                window,
            )
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
        index: Annotated[
            int | None,
            Field(
                ge=0,
                description="Which match, when several controls match (they are listed, nothing is done)",
            ),
        ] = None,
        value: Annotated[str | None, Field(description="For set_value")] = None,
        filter: Annotated[
            str | None,
            Field(
                description="For inspect: only controls with this text in name, automation id, type, value "
                "or class; 'a|b' for either (name/automation_id/control_type narrow it too)"
            ),
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
        ref: Annotated[
            str | None,
            Field(
                description="A control's ref from the latest inspect of this window (e12): exactly that control"
            ),
        ] = None,
        steps: Annotated[
            list[UiStep] | None,
            Field(
                description="Several actions on this window in order, each {action, name | ref | automation_id "
                "| control_type, index, value}; also keys / type (value = the keys or text) and wait (value = "
                "the state). Stops at the first that fails; ends with a picture"
            ),
        ] = None,
    ):
        """Read and operate a window's controls (buttons, fields, checkboxes, lists, trees, menus,
        open menus too) by name instead of pixels, also in classic Win32/WinForms/Delphi programs
        and installers. A program that draws its own interface (no controls): use ocr.
        inspect lists them, each with a ref; invoke/toggle/select/set_value/... find one by ref or
        name/automation_id/type and use it without moving the mouse; click is a real mouse click on it.
        Actions run once, then read back the control. verified=true means its observable state
        matches; false means a mismatch; null means the outcome cannot be confirmed. A window the
        action opened is listed in new_windows. For invoke/click on a button, inspect the resulting
        window before deciding whether to repeat it. steps=[...] does several actions in one call.
        wait polls (up to 40 s) until the control exists, is gone, is enabled or shows a value,
        e.g. an installer's Next button becoming enabled."""

        def work():
            if not steps and action not in _UI_ACTIONS:
                raise desktop.DesktopError("action is one of " + ", ".join(_UI_ACTIONS))
            if not steps and action == "wait":
                return wait_control(window, name, automation_id, control_type, state, timeout_s)
            w = desktop.find_window(window)
            if w.get("admin"):
                raise desktop.admin_error(w)
            if steps:
                done = _ui_steps(w, [step.model_dump() for step in steps], verify_wait_s)
                out = done["out"]
                try:
                    data, fmt, facts = desktop.screenshot(done["shot_of"])
                except desktop.DesktopError as exc:
                    out["screenshot"] = f"no picture: {exc}"
                    return out
                return media_result({**out, "screenshot": facts}, Image(data=data, format=fmt))
            if action == "inspect":
                return _ui_inspect(w, filter, name, automation_id, control_type)
            with presence.acting(f"ui {action}") as said:  # waits while the person uses the machine
                out = _ui_act(
                    w,
                    action,
                    name=name,
                    automation_id=automation_id,
                    control_type=control_type,
                    index=index,
                    value=value,
                    verify_wait_s=verify_wait_s,
                    ref=ref,
                )
            return {**out, **said}

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
        index: Annotated[
            int | None,
            Field(
                ge=0,
                description="Which match, top to bottom, left to right; needed when the text is in "
                "several places (they are listed, nothing is clicked)",
            ),
        ] = None,
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
        command: Annotated[
            str, Field(description="PowerShell command(s) to run; with `args`, the program to run directly")
        ],
        args: Annotated[
            list[str] | None,
            Field(
                description="Run `command` as a program with these arguments (no PowerShell parsing; [] for none). "
                "Its stdout and stderr are kept as written: job_status shows both while it runs"
            ),
        ] = None,
        stdin: Annotated[
            str | None,
            Field(description="With args: text given to the program on stdin, as UTF-8 (a Codex brief ...)"),
        ] = None,
        cwd: str | None = None,
        name: str | None = None,
        elevated: Annotated[
            bool, Field(description="Run as administrator; the person must approve a UAC prompt")
        ] = False,
        shell: Annotated[
            str | None,
            Field(
                description="For a command: pwsh (PowerShell 7, the default when installed) | powershell "
                "(Windows PowerShell 5.1)"
            ),
        ] = None,
        secrets: Annotated[
            list[str] | None,
            Field(
                description="Names in this machine's secret store: given to the job as environment "
                "variables, you never see the values (not for elevated jobs)"
            ),
        ] = None,
        timeout_s: Annotated[
            float | None,
            Field(gt=0, description="End the job and every process it started after this many seconds"),
        ] = None,
        after: Annotated[
            list[str] | None,
            Field(description="Job ids to wait for: this one starts when they have ended (a chain of steps)"),
        ] = None,
        keep_awake: Annotated[
            bool, Field(description="Hold off sleep while it runs (the display may still turn off)")
        ] = True,
    ) -> dict:
        """Run a command in the background, independent of winhand: it keeps running even if
        winhand restarts or is reinstalled, and its output and exit code stay available via
        job_status. Use for long work, for anything that replaces winhand, and for admin tasks.
        The PC does not sleep while it runs. A program fed text on stdin (Codex: command='codex',
        args=['exec', '-'], stdin=brief) is best run with args + stdin: the text and its output
        never pass through a PowerShell pipe."""

        def work():
            try:
                env = secret_store.load(list(secrets or []))
            except secret_store.SecretError as exc:
                return {"error": str(exc)}
            return jobs.start(
                command, cwd, name, elevated, args, stdin, shell, env or None, timeout_s, after, keep_awake
            )

        return await asyncio.to_thread(_errors, work)

    @mcp.tool
    async def job_status(
        ctx: Context,
        id: Annotated[
            str | list[str] | None,
            Field(description="Job id, or a list of ids to wait on together; omit to list recent jobs"),
        ] = None,
        since: Annotated[
            int | dict[str, int],
            Field(description="Output cursor from the previous call ({id: cursor} for a list of ids)"),
        ] = 0,
        wait_s: Annotated[
            float,
            Field(
                ge=0,
                description="Wait up to this long for the job to finish or a pattern to appear: 40 s per "
                "call (up to 1800 s for a client on this machine that shows progress); 0 answers at once. "
                "Call again with since=cursor instead of sleeping or polling",
            ),
        ] = 0,
        patterns: Annotated[
            list[str] | None, Field(description="Regexes: stop waiting once new output matches one")
        ] = None,
        tail: Annotated[
            int | None, Field(ge=1, description="Return at most this many characters of output (the end)")
        ] = None,
        files: Annotated[
            list[str] | None,
            Field(description="Also show the end of these files (a log the job writes besides its output)"),
        ] = None,
    ) -> dict:
        """State (running/finished/vanished), exit code and output of a background job or of a `run`
        that was still going (run-N). With wait_s it blocks until the job ends, new output matches
        `patterns`, the job opens a window or stops working (`matched`: window / stalled), or the
        wait runs out (then call again with since=cursor). A running job's `activity` gives its
        processes, CPU, memory, how long it has been quiet and the windows it shows. Several ids
        wait for whichever job finishes or matches first, and answer with every job's state."""
        if id is None:
            return await asyncio.to_thread(_errors, lambda: {"jobs": jobs.list_jobs()})
        id = _job_ids(id)
        if isinstance(id, list):
            ids = list(dict.fromkeys(str(i) for i in id))
            if len(ids) == 1:
                id = ids[0]
            else:
                return _with_files(await _job_statuses(ctx, ids, since, wait_s, patterns, tail), files)
        start = since.get(id, 0) if isinstance(since, dict) else since
        limit = tail or 20_000

        def fetch() -> dict:
            return _job_fetch(id, start, limit)

        if wait_s <= 0:
            status = await asyncio.to_thread(fetch)
            if status.get("state") == "running":
                status = {**status, "activity": await asyncio.to_thread(_activity, id)}
            return _with_files(trim_repeat(id, start, status, tail is not None), files)
        try:
            regexes = [re.compile(p, re.MULTILINE) for p in patterns or []]
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
        ceiling = waiting.limit(ctx, wait_s)
        key = ("job_status", id, start, round(ceiling, 1), coalesce.freeze(patterns), limit)
        shared = coalesce.shared(
            key, lambda: wait_job(fetch, ceiling, regexes, ceiling, watch=lambda: _activity(id))
        )
        status = await waiting.heartbeat(ctx, shared, ceiling, f"job {id}")
        note = waiting.capped_note(ctx, wait_s)
        if note and status.get("state") == "running":
            status = {**status, "wait_note": note}
        return _with_files(trim_repeat(id, start, status, tail is not None), files)

    async def _job_statuses(ctx, ids, since, wait_s, patterns, tail) -> dict:
        cursors = {i: (since.get(i, 0) if isinstance(since, dict) else since) for i in ids}
        limit = tail or MULTI_JOB_CHARS
        try:
            regexes = [re.compile(p, re.MULTILINE) for p in patterns or []]
        except re.error as exc:
            return {"error": f"invalid regex: {exc}"}
        ceiling = waiting.limit(ctx, wait_s)
        key = (
            "job_statuses",
            tuple(ids),
            coalesce.freeze(cursors),
            round(ceiling, 1),
            coalesce.freeze(patterns),
            limit,
        )
        shared = coalesce.shared(key, lambda: wait_jobs(ids, cursors, limit, ceiling, regexes, ceiling))
        out = await waiting.heartbeat(ctx, shared, ceiling, f"{len(ids)} jobs")
        if not isinstance(out.get("jobs"), dict):
            return out
        running = [i for i, st in out["jobs"].items() if st.get("state") == "running"]
        seen = await asyncio.gather(*(asyncio.to_thread(_activity, i) for i in running))
        activity = dict(zip(running, seen, strict=True))
        trimmed = {
            i: trim_repeat(
                i,
                cursors.get(i, 0),
                {**st, **({"activity": activity[i]} if i in activity else {})},
                tail is not None,
            )
            for i, st in out["jobs"].items()
        }
        return {**out, "jobs": trimmed}

    def _with_files(result: dict, files: list[str] | None) -> dict:
        return {**result, "files": _file_tails(files)} if files and isinstance(result, dict) else result

    @mcp.tool
    async def job_stop(id: str) -> dict:
        """Stop a running background job (and its child processes)."""
        if _is_run(id):
            return await asyncio.to_thread(_errors, proc.run_stop, id)
        return await asyncio.to_thread(_errors, jobs.stop, id)
