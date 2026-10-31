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
import math
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlsplit, urlunsplit

from . import bundle, fs, winenv

DEFAULT_TTL_S = 600
MAX_TTL_S = 1800
DEFAULT_MAX_MB = 95  # Cloudflare refuses request bodies over 100 MB on the free plan
TOKEN_CHARS = 43  # secrets.token_urlsafe(32)
DEFAULT_PART_MB = 4
MAX_PART_MB = 64
MAX_PARTS = 128
MAX_PARTED_MB = 2048
LISTED = 20  # names given one by one in an answer


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


def set_relay(url: str | None) -> None:
    """Remember the relay's public origin from the agent's WebSocket URL (wss://host/agent)."""
    global _public_base
    if not url:
        return
    parts = urlsplit(url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme, parts.scheme)
    _public_base = urlunsplit((scheme, parts.netloc, "", "", "")).rstrip("/")


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
    from starlette.responses import FileResponse, JSONResponse

    token = request.path_params.get("token", "")
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
