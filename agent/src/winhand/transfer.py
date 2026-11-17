"""One-time links for moving files between this machine and an AI's sandbox, through the relay.

Sending a file as tool-call text makes the model write out every byte (a 100 KB patch took
16-37 minutes as base64 in parts). Instead winhand hands out a link: the sandbox uploads
with `curl -T` or downloads with `curl -o`, and the bytes go relay -> tunnel -> disk.

Each link is a capability: 256 random bits, single use, short-lived, bound at issue time to
one path (and, for uploads, a size limit and optionally the expected sha256). Only a hash of
the token is kept, in memory, so a restart of winhand voids every open link.

Several files go through one link (bundle.py): a folder downloads as a zip made while it streams,
and an uploaded zip or tar can be unpacked into a folder. A big file goes up in parts, one link
per part: uploads over about 8 MB got an HTTP 502 after about 30 s in some sandboxes even though
the file arrived (2026-11-01 review), so each part is small enough to finish well inside that.
A part's link takes the part again until the whole file is in (a failed part is simply resent).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import json
import math
import os
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from . import bundle, config, fs, winenv

DEFAULT_TTL_S = 600
MAX_TTL_S = 1800
DEFAULT_MAX_MB = 95  # Cloudflare refuses request bodies over 100 MB on the free plan
TOKEN_CHARS = 43  # secrets.token_urlsafe(32)
DEFAULT_PART_MB = 4
MAX_PART_MB = 64
MAX_PARTS = 128
MAX_PARTED_MB = 2048
LISTED = 20  # names given one by one in an answer
WAIT_HOLD_S = 50
WAKE_NOTE = (
    "Run `wake` in the background (Bash run_in_background, timeout 7200000 ms). "
    "It ends when the condition holds and you are woken with its final status; "
    "if it is stopped at 2 h, run it again."
)


class TransferError(RuntimeError):
    pass


@dataclass
class Parted:
    """A file sent in parts: one link per part, all writing into one staging file next to it."""

    path: str  # the file, or with extract the folder
    staging: str
    size: int
    part_bytes: int
    parts: int
    ttl_s: float
    sha256: str | None = None
    overwrite: bool = False
    extract: bool = False
    keys: list[str] = field(default_factory=list)
    received: set[int] = field(default_factory=set)
    finishing: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def length(self, index: int) -> int:
        return min(self.part_bytes, self.size - index * self.part_bytes)


@dataclass
class Ticket:
    kind: str  # upload | download | part
    path: str
    expires: float
    max_bytes: int = 0
    sha256: str | None = None
    overwrite: bool = False
    plan: bundle.Plan | None = None  # download: these files as one zip
    extract: bool = False  # upload: unpack the archive into path (a folder)
    parted: Parted | None = None  # part: the file this part belongs to
    index: int = 0


_tickets: dict[str, Ticket] = {}
_lock = threading.Lock()
_public_base: str | None = None
recent: collections.deque[dict] = collections.deque(maxlen=20)
listeners: list[Callable[[dict], None]] = []
_wait_links: dict[str, dict] | None = None  # token hash -> condition; kept in wait-links.json
_wait_done: dict[str, dict] = {}  # token hash -> the answer once the condition held
WAIT_FORGET_S = 86400  # an expired wait link still answers 410 this long, then 404 like any unknown one


def _waits() -> dict[str, dict]:
    """The wait links, read from disk on first use: they outlive a winhand restart or update."""
    global _wait_links
    if _wait_links is None:
        path = config.home() / "wait-links.json"
        _wait_links = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _wait_links


def set_relay(url: str | None) -> None:
    """Remember this computer's public address space on the relay from the agent's WebSocket URL:
    wss://host/pc/home/agent -> https://host/pc/home; wss://host/agent -> https://host."""
    global _public_base
    if not url:
        return
    from . import config

    _public_base = config.relay_base(url)


def public_base() -> str:
    if _public_base:
        return _public_base
    from . import config

    cfg = config.load()
    if cfg.relay_url:
        set_relay(cfg.relay_url)
    if not _public_base:
        raise TransferError(
            "winhand is not connected through a relay, so there is no public link to give out"
        )
    return _public_base


def _key(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _sweep(now: float) -> None:
    for key in [k for k, t in _tickets.items() if t.expires < now]:
        ticket = _tickets.pop(key)
        if ticket.parted and ticket.index == 0 and not ticket.parted.finishing:
            with contextlib.suppress(OSError):  # an upload that never finished: its staging file
                os.remove(ticket.parted.staging)


def _issue(ticket: Ticket) -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    with _lock:
        _sweep(time.time())
        _tickets[_key(token)] = ticket
    return token, f"{public_base()}/t/{token}"


def issue_wait(cond: dict, ttl_s: float = 43200) -> dict:
    """A wait link: GET /t/<token> holds up to WAIT_HOLD_S and answers {"done": true, ...} once the
    condition holds ({"job": id} ends, its new output from `cursor` matches `patterns`, {"file":
    path} appears or changes, {"online": true}). `wake` is a bash line that polls it until then:
    run in a cloud sandbox's background, it ends, and so wakes the session, without a turn spent
    waiting. Only the state goes out, never output."""
    if set(cond) not in ({"job"}, {"job", "patterns", "cursor"}, {"file"}, {"online"}):
        raise TransferError("wait condition is job, job with patterns and cursor, file, or online=true")
    if "online" in cond and cond["online"] is not True:
        raise TransferError("online must be true")
    for pattern in cond.get("patterns", []):
        re.compile(pattern, re.MULTILINE)
    base = public_base()
    token = secrets.token_urlsafe(32)
    issued = time.time()
    expires = issued + ttl_s
    with _lock:
        waits = _waits()
        for old in [k for k, t in waits.items() if t["expires"] < issued - WAIT_FORGET_S]:
            del waits[old]
            _wait_done.pop(old, None)
        waits[_key(token)] = {"cond": cond, "issued": issued, "expires": expires}
        path = config.home() / "wait-links.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(waits, separators=(",", ":")), encoding="utf-8")
    url = f"{base}/t/{token}"
    return {
        "url": url,
        "expires": datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds"),
        "wake": (
            f"u='{url}'; while :; do r=$(curl -sS -m 75 \"$u\" 2>/dev/null); "
            'case "$r" in *\'"done":true\'*|*expired*|*unknown*) echo "$r"; break;; '
            "'') sleep 15;; *) sleep 5;; esac; done"
        ),
    }


def _ttl(ttl_s: float) -> float:
    return max(30.0, min(float(ttl_s), MAX_TTL_S))


def _absolute(path: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isabs(expanded):
        raise TransferError(f"give an absolute path on this machine, not {path!r}")
    return expanded


def _names(items: list[str]) -> str:
    return ", ".join(items[:LISTED]) + (f" and {len(items) - LISTED} more" if len(items) > LISTED else "")


_NETWORK_NOTE = (
    "The sandbox's network must allow the relay's domain: an HTTP 403 comes from the sandbox's own "
    "network policy, so ask the person to allow the domain there instead of working around it."
)


def issue_upload(
    path: str,
    sha256: str | None = None,
    max_mb: float = DEFAULT_MAX_MB,
    ttl_s: float = DEFAULT_TTL_S,
    overwrite: bool = False,
    extract: bool = False,
    size: int | None = None,
    part_mb: float = DEFAULT_PART_MB,
) -> dict:
    target = _absolute(path)
    full = winenv.long_path(target)
    if extract:
        if os.path.isfile(full):
            raise TransferError(f"{path} is a file; with extract=true give the folder to unpack into")
    elif os.path.isdir(full):
        raise TransferError(f"{path} is a folder; give the file path to create")
    elif os.path.exists(full) and not overwrite:
        raise TransferError(f"{path} already exists; pass overwrite=true to replace it")
    if sha256 and len(sha256.strip()) != 64:
        raise TransferError("sha256 must be the 64-character hex digest")
    digest = sha256.strip().lower() if sha256 else None
    part_bytes = int(max(1.0, min(float(part_mb), MAX_PART_MB)) * 2**20)
    if size is not None and int(size) > part_bytes:
        return _issue_parts(target, int(size), part_bytes, digest, ttl_s, overwrite, extract)
    max_bytes = int(max(0.001, min(float(max_mb), DEFAULT_MAX_MB)) * 1024 * 1024)
    expires = time.time() + _ttl(ttl_s)
    ticket = Ticket("upload", target, expires, max_bytes, digest, overwrite, extract=extract)
    _, url = _issue(ticket)
    unpacked = (
        " The archive (zip or tar) is unpacked into the folder; every name and every file is checked "
        "first, and nothing is written if one fails."
        if extract
        else ""
    )
    return {
        "url": url,
        "method": "PUT",
        "curl": f"curl -fsS -T <local {'archive' if extract else 'file'}> '{url}'",
        "path": target,
        "max_bytes": max_bytes,
        **_expiry(expires),
        "note": "Single use. Upload from your own shell/sandbox; the reply is JSON with bytes and sha256."
        + unpacked
        + " Over about 8 MB pass size=<bytes> to send it in parts. "
        + _NETWORK_NOTE,
    }


def _issue_parts(
    target: str, size: int, part_bytes: int, digest: str | None, ttl_s: float, overwrite: bool, extract: bool
) -> dict:
    if size > MAX_PARTED_MB * 2**20:
        raise TransferError(f"{size / 2**30:.1f} GiB is over the {MAX_PARTED_MB // 1024} GiB limit")
    part_bytes = max(part_bytes, math.ceil(size / MAX_PARTS))
    count = math.ceil(size / part_bytes)
    full = winenv.long_path(target)
    staging = os.path.join(
        os.path.dirname(full) or ".", f".{os.path.basename(full)}.upload-{uuid.uuid4().hex[:8]}.parts"
    )
    ttl = _ttl(ttl_s)
    parted = Parted(target, staging, size, part_bytes, count, ttl, digest, overwrite, extract)
    expires = time.time() + ttl
    urls = []
    for index in range(count):
        token, url = _issue(Ticket("part", target, expires, parted.length(index), parted=parted, index=index))
        parted.keys.append(_key(token))
        urls.append(url)
    return {
        "parts": urls,
        "part_bytes": part_bytes,
        "size": size,
        "method": "PUT",
        "curl": (
            f"split -b {part_bytes} -d -a 4 <local file> part. && i=0 && for u in $URLS; do "
            'curl -fsS -T part.$(printf %04d $i) "$u" || break; i=$((i+1)); done'
        ),
        "path": target,
        **_expiry(expires),
        "note": f"One link per part, in order (URLS: the parts above, space-separated); part i is bytes "
        f"i*{part_bytes} onwards. Parts may go in any order or in parallel; a failed part is sent again "
        "to its own link. Each reply says how many parts are in; the last one's reply is the result "
        + ("(the archive unpacked into the folder). " if extract else "(bytes, sha256). ")
        + "The links stay valid while parts keep arriving. "
        + _NETWORK_NOTE,
    }


def _expiry(expires: float) -> dict:
    # with the UTC offset: a sandbox reading a bare local time as its own UTC is hours off
    at = datetime.fromtimestamp(expires).astimezone().isoformat(timespec="seconds")
    return {"expires_at": at, "expires_in_s": max(0, round(expires - time.time()))}


def issue_download(
    path: str | None,
    ttl_s: float = DEFAULT_TTL_S,
    paths: list[str] | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> dict:
    given = [p for p in [path, *(paths or [])] if p]
    if not given:
        raise TransferError("give path (a file or folder) or paths=[...]")
    targets = [_absolute(p) for p in given]
    full = winenv.long_path(targets[0])
    if len(targets) == 1 and os.path.isfile(full) and not include and not exclude:
        return _issue_file(targets[0], ttl_s)
    if len(targets) == 1 and not os.path.exists(full):
        raise TransferError(f"no such file or folder: {given[0]}")
    try:
        plan = bundle.plan_download(targets, include, exclude)
    except bundle.BundleError as exc:
        raise TransferError(str(exc)) from exc
    if not plan.files:
        why = f"; {len(plan.left_out)} left out by this machine's read rules" if plan.left_out else ""
        raise TransferError(f"no files to download there (after include/exclude){why}")
    expires = time.time() + _ttl(ttl_s)
    _, url = _issue(Ticket("download", targets[0], expires, plan=plan))
    name = plan.name.replace("'", "_")
    out = {
        "url": url,
        "curl": f"curl -fsS -o '{name}' '{url}'",
        "zip": name,
        "files": len(plan.files),
        "bytes": plan.size,
        **_expiry(expires),
    }
    if plan.left_out:
        out["left_out"] = f"{len(plan.left_out)} file(s) this machine's read rules keep from you: " + _names(
            plan.left_out
        )
    if plan.skipped:
        out["skipped"] = _names(plan.skipped)
    out["note"] = (
        "Single use. One zip, made while it downloads (bytes: before compression), so there is no sha256 "
        "up front: `unzip -t` or `python3 -m zipfile -t` checks every file in it. A file that could not "
        f"be read meanwhile is listed in {bundle.ERRORS_NAME} inside the zip. " + _NETWORK_NOTE
    )
    return out


def _issue_file(target: str, ttl_s: float) -> dict:
    full = winenv.long_path(target)
    expires = time.time() + _ttl(ttl_s)
    _, url = _issue(Ticket("download", target, expires))
    name = os.path.basename(target)
    return {
        "url": url,
        "curl": f"curl -fsS -o '{name}' '{url}'",
        "path": target,
        "size": os.path.getsize(full),
        "sha256": fs.sha256_of(target),
        **_expiry(expires),
        "note": "Single use. Download from your own shell/sandbox; check the sha256 afterwards. "
        "An HTTP 403 comes from the sandbox's own network policy: ask the person to allow the relay's domain.",
    }


def take(token: str, kind: str) -> Ticket | None:
    """The ticket for this token, removed so it cannot be used again; None if unknown,
    expired or for the other direction. A part's ticket stays until its whole file is in."""
    if len(token) != TOKEN_CHARS:
        return None
    with _lock:
        _sweep(time.time())
        ticket = _tickets.get(_key(token))
        if ticket is None or ticket.kind != kind and not (kind == "upload" and ticket.kind == "part"):
            return None
        if ticket.kind != "part":
            del _tickets[_key(token)]
        return ticket


def _report(entry: dict) -> None:
    entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    recent.append(entry)
    for listener in list(listeners):
        with contextlib.suppress(Exception):
            listener(entry)


# ------------------------------------------------------------------ HTTP side


def _zip_response(ticket: Ticket):
    from starlette.responses import StreamingResponse

    plan = ticket.plan

    def body():
        sent = 0
        try:
            for piece in bundle.stream_zip(plan):
                sent += len(piece)
                yield piece
        finally:
            _report({"kind": "download", "path": ticket.path, "bytes": sent, "files": len(plan.files)})

    ascii_name = plan.name.encode("ascii", "replace").decode().replace("?", "_").replace('"', "_")
    disposition = f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(plan.name)}"
    return StreamingResponse(
        body(), media_type="application/zip", headers={"content-disposition": disposition}
    )


def _unpacked(archive: str, ticket_path: str, overwrite: bool, got: str, size: int):
    """Unpack an uploaded archive; the JSON answer either way."""
    from starlette.responses import JSONResponse

    try:
        result = bundle.unpack(archive, ticket_path, overwrite)
    except (bundle.BundleError, OSError) as exc:
        return JSONResponse(
            {"error": str(exc), "archive_sha256": got, "archive_bytes": size}, status_code=422
        )
    _report({"kind": "upload", "path": ticket_path, "bytes": size, "sha256": got, "files": result["files"]})
    return JSONResponse({"ok": True, **result, "archive_sha256": got, "archive_bytes": size})


async def endpoint(request):
    """Starlette handler for /transfer/{token}: GET downloads, PUT/POST uploads."""
    from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response

    token = request.path_params.get("token", "")
    key = _key(token)
    wait = _waits().get(key)
    if wait is not None:
        if request.method != "GET":
            return PlainTextResponse("method not allowed", status_code=405)
        from . import jobs, proc
        from .tools_desktop import _is_run, _new_text

        cond = wait["cond"]
        regexes = [re.compile(p, re.MULTILINE) for p in cond.get("patterns", [])]
        deadline = time.monotonic() + WAIT_HOLD_S
        stderr_before = None
        while True:
            if time.time() >= wait["expires"]:
                return PlainTextResponse("expired wait link", status_code=410)
            if key in _wait_done:
                result = _wait_done[key]
                break
            if "job" in cond:
                fetch = proc.run_status if _is_run(cond["job"]) else jobs.status
                status = await asyncio.to_thread(fetch, cond["job"], cond.get("cursor", 0))
                result = {"done": status["state"] != "running"}
                result.update({k: status[k] for k in ("state", "exit_code") if k in status})
                if not result["done"] and regexes:
                    if stderr_before is None:
                        stderr_before = status.get("stderr_size", 0)
                    text = _new_text(status, stderr_before)
                    matched = next((r.pattern for r in regexes if r.search(text)), None)
                    if matched is not None:
                        result.update(done=True, matched=matched)
            elif "file" in cond:
                path = fs._p(cond["file"])
                done = False
                if os.path.exists(path):
                    stamp = os.stat(path)
                    done = stamp.st_mtime > wait["issued"] or stamp.st_ctime > wait["issued"]
                result = {"done": done}
            else:
                result = {"done": True}
            if result["done"]:
                _wait_done[key] = result
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(1, remaining))
        return Response(json.dumps(result, separators=(",", ":")), media_type="application/json")
    if request.method == "GET":
        ticket = take(token, "download")
        if ticket is None:
            return JSONResponse({"error": "unknown, used or expired link"}, status_code=404)
        if ticket.plan is not None:
            return _zip_response(ticket)
        full = winenv.long_path(ticket.path)
        if not os.path.isfile(full):
            return JSONResponse({"error": "the file is gone"}, status_code=410)
        _report({"kind": "download", "path": ticket.path, "bytes": os.path.getsize(full)})
        return FileResponse(full, filename=os.path.basename(ticket.path))
    ticket = take(token, "upload")
    if ticket is None:
        return JSONResponse({"error": "unknown, used or expired link"}, status_code=404)
    if ticket.parted is not None:
        return await _part(request, ticket)
    full = winenv.long_path(ticket.path)
    directory = os.path.dirname(full) or "."
    os.makedirs(directory, exist_ok=True)
    temp = os.path.join(directory, f".{os.path.basename(full)}.upload-{uuid.uuid4().hex[:8]}.tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with open(temp, "wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > ticket.max_bytes:
                    return JSONResponse(
                        {"error": f"over the {ticket.max_bytes}-byte limit of this link"}, status_code=413
                    )
                digest.update(chunk)
                await asyncio.to_thread(fh.write, chunk)
        got = digest.hexdigest()
        if ticket.sha256 and got != ticket.sha256:
            return JSONResponse({"error": "sha256 mismatch", "sha256": got, "bytes": size}, status_code=422)
        if ticket.extract:
            return await asyncio.to_thread(_unpacked, temp, ticket.path, ticket.overwrite, got, size)
        if os.path.exists(full) and not ticket.overwrite:
            return JSONResponse({"error": "the file appeared meanwhile; not overwritten"}, status_code=409)
        await asyncio.to_thread(fs.replace_with, temp, full)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temp)
    _report({"kind": "upload", "path": ticket.path, "bytes": size, "sha256": got})
    return JSONResponse({"ok": True, "path": ticket.path, "bytes": size, "sha256": got})


def _open_part(up: Parted, index: int):
    """A handle on the staging file at this part's offset; the first part to arrive makes the file
    at its full size, so parts can arrive in any order."""
    with up.lock:
        os.makedirs(os.path.dirname(up.staging) or ".", exist_ok=True)
        with contextlib.suppress(FileExistsError), open(up.staging, "xb") as fh:
            fh.truncate(up.size)
    fh = open(up.staging, "r+b")
    fh.seek(index * up.part_bytes)
    return fh


async def _part(request, ticket: Ticket):
    from starlette.responses import JSONResponse

    up, index = ticket.parted, ticket.index
    expected = up.length(index)
    if up.finishing:
        return JSONResponse(
            {"error": "every part is already in; the file is being finished"}, status_code=409
        )
    fh = await asyncio.to_thread(_open_part, up, index)
    got = 0
    try:
        async for chunk in request.stream():
            got += len(chunk)
            if got > expected:
                return JSONResponse(
                    {"error": f"part {index} is {expected} bytes; this body is longer"}, status_code=413
                )
            await asyncio.to_thread(fh.write, chunk)
    finally:
        fh.close()
    if got != expected:
        return JSONResponse(
            {"error": f"part {index} should be {expected} bytes, got {got}: send it again to the same link"},
            status_code=400,
        )
    now = time.time()
    with _lock:  # parts still arriving: keep the links alive
        for key in up.keys:
            if key in _tickets:
                _tickets[key].expires = max(_tickets[key].expires, now + up.ttl_s)
    with up.lock:
        up.received.add(index)
        last = len(up.received) == up.parts and not up.finishing
        if last:
            up.finishing = True
    if not last:
        return JSONResponse({"ok": True, "part": index, "received": len(up.received), "parts": up.parts})
    return await asyncio.to_thread(_finish, up)


def _finish(up: Parted):
    """Every part is in: check the whole file, then move it into place (or unpack it)."""
    from starlette.responses import JSONResponse

    got = fs.sha256_of(up.staging)
    if up.sha256 and got != up.sha256:
        with up.lock:  # let the parts be sent again over the same links
            up.received.clear()
            up.finishing = False
        return JSONResponse(
            {"error": "sha256 mismatch for the whole file: send the parts again", "sha256": got},
            status_code=422,
        )
    with _lock:
        for key in up.keys:
            _tickets.pop(key, None)
    try:
        if up.extract:
            return _unpacked(up.staging, up.path, up.overwrite, got, up.size)
        full = winenv.long_path(up.path)
        if os.path.exists(full) and not up.overwrite:
            return JSONResponse({"error": "the file appeared meanwhile; not overwritten"}, status_code=409)
        fs.replace_with(up.staging, full)
    finally:
        with contextlib.suppress(OSError):
            os.remove(up.staging)
    _report({"kind": "upload", "path": up.path, "bytes": up.size, "sha256": got, "parts": up.parts})
    return JSONResponse({"ok": True, "done": True, "path": up.path, "bytes": up.size, "sha256": got})
