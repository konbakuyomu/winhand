"""One-time links for moving files between this machine and an AI's sandbox, through the relay.

Sending a file as tool-call text makes the model write out every byte (a 100 KB patch took
16-37 minutes as base64 in parts). Instead winhand hands out a link: the sandbox uploads
with `curl -T` or downloads with `curl -o`, and the bytes go relay -> tunnel -> disk.

Each link is a capability: 256 random bits, single use, short-lived, bound at issue time to
one path (and, for uploads, a size limit and optionally the expected sha256). Only a hash of
the token is kept, in memory, so a restart of winhand voids every open link.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from . import fs, winenv

DEFAULT_TTL_S = 600
MAX_TTL_S = 1800
DEFAULT_MAX_MB = 95  # Cloudflare refuses request bodies over 100 MB on the free plan
TOKEN_CHARS = 43  # secrets.token_urlsafe(32)


class TransferError(RuntimeError):
    pass


@dataclass
class Ticket:
    kind: str  # upload | download
    path: str
    expires: float
    max_bytes: int = 0
    sha256: str | None = None
    overwrite: bool = False


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
        del _tickets[key]


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


def issue_upload(
    path: str,
    sha256: str | None = None,
    max_mb: float = DEFAULT_MAX_MB,
    ttl_s: float = DEFAULT_TTL_S,
    overwrite: bool = False,
) -> dict:
    target = _absolute(path)
    full = winenv.long_path(target)
    if os.path.isdir(full):
        raise TransferError(f"{path} is a folder; give the file path to create")
    if os.path.exists(full) and not overwrite:
        raise TransferError(f"{path} already exists; pass overwrite=true to replace it")
    if sha256 and len(sha256.strip()) != 64:
        raise TransferError("sha256 must be the 64-character hex digest")
    max_bytes = int(max(0.001, min(float(max_mb), DEFAULT_MAX_MB)) * 1024 * 1024)
    expires = time.time() + _ttl(ttl_s)
    ticket = Ticket(
        "upload", target, expires, max_bytes, sha256.strip().lower() if sha256 else None, overwrite
    )
    _, url = _issue(ticket)
    return {
        "url": url,
        "method": "PUT",
        "curl": f"curl -fsS -T <local file> '{url}'",
        "path": target,
        "max_bytes": max_bytes,
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)),
        "note": "Single use. Upload from your own shell/sandbox; the reply is JSON with bytes and sha256. "
        "The sandbox's network must allow the relay's domain: an HTTP 403 comes from the sandbox's own "
        "network policy, so ask the person to allow the domain there instead of working around it.",
    }


def issue_download(path: str, ttl_s: float = DEFAULT_TTL_S) -> dict:
    target = _absolute(path)
    full = winenv.long_path(target)
    if not os.path.isfile(full):
        raise TransferError(f"no such file: {path}")
    expires = time.time() + _ttl(ttl_s)
    _, url = _issue(Ticket("download", target, expires))
    name = os.path.basename(target)
    return {
        "url": url,
        "curl": f"curl -fsS -o '{name}' '{url}'",
        "path": target,
        "size": os.path.getsize(full),
        "sha256": fs.sha256_of(target),
        "expires_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(expires)),
        "note": "Single use. Download from your own shell/sandbox; check the sha256 afterwards. "
        "An HTTP 403 comes from the sandbox's own network policy: ask the person to allow the relay's domain.",
    }


def take(token: str, kind: str) -> Ticket | None:
    """The ticket for this token, removed so it cannot be used again; None if unknown,
    expired or for the other direction."""
    if len(token) != TOKEN_CHARS:
        return None
    with _lock:
        _sweep(time.time())
        ticket = _tickets.get(_key(token))
        if ticket is None or ticket.kind != kind:
            return None
        del _tickets[_key(token)]
        return ticket


def _report(entry: dict) -> None:
    entry = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
    recent.append(entry)
    for listener in list(listeners):
        with contextlib.suppress(Exception):
            listener(entry)


# ------------------------------------------------------------------ HTTP side


async def endpoint(request):
    """Starlette handler for /transfer/{token}: GET downloads, PUT/POST uploads."""
    from starlette.responses import FileResponse, JSONResponse

    token = request.path_params.get("token", "")
    if request.method == "GET":
        ticket = take(token, "download")
        if ticket is None:
            return JSONResponse({"error": "unknown, used or expired link"}, status_code=404)
        full = winenv.long_path(ticket.path)
        if not os.path.isfile(full):
            return JSONResponse({"error": "the file is gone"}, status_code=410)
        _report({"kind": "download", "path": ticket.path, "bytes": os.path.getsize(full)})
        return FileResponse(full, filename=os.path.basename(ticket.path))
    ticket = take(token, "upload")
    if ticket is None:
        return JSONResponse({"error": "unknown, used or expired link"}, status_code=404)
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
        if os.path.exists(full) and not ticket.overwrite:
            return JSONResponse({"error": "the file appeared meanwhile; not overwritten"}, status_code=409)
        await asyncio.to_thread(fs.replace_with, temp, full)
    finally:
        with contextlib.suppress(OSError):
            os.remove(temp)
    _report({"kind": "upload", "path": ticket.path, "bytes": size, "sha256": got})
    return JSONResponse({"ok": True, "path": ticket.path, "bytes": size, "sha256": got})
