"""File tools modelled on what coding agents need: numbered reads, exact edits,
fast search, finding and managing files. Handles UTF-8/UTF-8-BOM/GBK files, keeps
CRLF files CRLF and copes with long, spaced and CJK paths on Windows."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import winenv
from .session.text import width

_ENCODINGS = ("utf-8", "gb18030")
MAX_LINE = 2000
# How much one tool result holds, counted as width() (UTF-8 size: a Chinese character counts 3).
# Clients cap results in tokens (Claude Code at 25k by default); 60,000 characters of Chinese or
# dense code went over it, and the whole result was lost.
OUTPUT_BUDGET = 40_000
READ_MAX_CHARS = OUTPUT_BUDGET
SEARCH_BUDGET_S = 35.0  # answer before the client's ~60 s limit, with what was found so far
FIND_BUDGET_S = 35.0
_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "$Recycle.Bin", "System Volume Information"}


class FsError(RuntimeError):
    pass


def _p(path: str) -> str:
    if not path:
        raise FsError("path is empty")
    return winenv.long_path(path)


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _is_binary(data: bytes) -> bool:
    head = data[:8192]
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    return b"\x00" in head


def _read_bytes(path: str) -> bytes:
    full = _p(path)
    if not os.path.exists(full):
        raise FsError(f"no such file: {path}")
    if os.path.isdir(full):
        raise FsError(f"{path} is a directory; use fs_list")
    # Python opens files sharing read and write, so a log another program is writing still reads
    with open(full, "rb") as fh:
        return fh.read()


def _read_text_bytes(path: str, encoding: str | None = None) -> tuple[str, str, bytes]:
    data = _read_bytes(path)
    if encoding:
        try:
            return data.decode(encoding, errors="replace"), encoding, data
        except LookupError:
            raise FsError(f"unknown encoding {encoding!r} (e.g. utf-8, gbk, utf-16, latin-1)") from None
    if _is_binary(data):
        raise FsError(f"{path} looks binary ({len(data)} bytes); not shown as text")
    text, found = _decode(data)
    return text, found, data


def read_text(path: str) -> tuple[str, str]:
    text, encoding, _ = _read_text_bytes(path)
    return text, encoding


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(_p(path), "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check_expected(path: str, expected: str | None) -> None:
    """Optimistic locking: refuse to write when the file is not what the caller last saw.
    expected="" means the file must not exist yet."""
    if expected is None:
        return
    full = _p(path)
    current = sha256_of(path) if os.path.isfile(full) else ""
    if current != expected.strip().lower():
        state = f"its sha256 is now {current[:12]}…" if current else "it does not exist"
        raise FsError(f"{path} changed since you read it ({state}); read it again before writing")


# ------------------------------------------------------------------ writing


def _replace_file_windows(target: str, replacement: str) -> bool:
    """ReplaceFileW keeps the target's ACL, attributes and creation time (os.replace does not)."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    kernel32.ReplaceFileW.restype = wintypes.BOOL
    return bool(kernel32.ReplaceFileW(target, replacement, None, 0x2, None, None))  # IGNORE_MERGE_ERRORS


def replace_with(temp: str, full: str) -> None:
    """Move `temp` over `full`; on Windows the existing file's ACL and attributes are kept."""
    if winenv.IS_WINDOWS and os.path.isfile(full) and _replace_file_windows(full, temp):
        return
    if not winenv.IS_WINDOWS and os.path.isfile(full):
        shutil.copymode(full, temp)
    os.replace(temp, full)


def write_bytes_atomic(path: str, data: bytes, create_dirs: bool = True) -> str | None:
    """Write via a temporary file and a rename, so a crash never leaves half a file.
    Falls back to writing in place when another program holds the file open.
    Returns a note when it had to fall back."""
    full = _p(path)
    directory = os.path.dirname(full) or "."
    if create_dirs:
        os.makedirs(directory, exist_ok=True)
    if os.path.islink(full):  # write through the link, as before, instead of replacing it
        with open(full, "wb") as fh:
            fh.write(data)
        return None
    temp = os.path.join(directory, f".{os.path.basename(full)}.winhand-{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(temp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        with open(full, "wb") as fh:  # e.g. no right to create files in the folder
            fh.write(data)
        return "written in place (could not create a temporary file next to it)"
    try:
        replace_with(temp, full)
        return None
    except OSError:
        with open(full, "wb") as fh:
            fh.write(data)
        return "written in place (the file is held open by another program)"
    finally:
        try:
            os.remove(temp)
        except OSError:
            pass


def _encode_text(text: str, encoding: str) -> tuple[bytes, str]:
    write_enc = "utf-8" if encoding == "latin-1" else encoding
    if write_enc == "utf-8-sig":
        return b"\xef\xbb\xbf" + text.encode("utf-8"), write_enc
    return text.encode(write_enc), write_enc


def write_text_like(path: str, text: str, encoding: str) -> tuple[str, str | None]:
    """Write text back in the file's own encoding. Returns (encoding, note)."""
    data, write_enc = _encode_text(text, encoding)
    return write_enc, write_bytes_atomic(path, data, create_dirs=False)


# ------------------------------------------------------------------ reading and editing


def fs_read(
    path: str,
    offset: int = 1,
    limit: int = 2000,
    tail: int | None = None,
    max_chars: int = READ_MAX_CHARS,
    encoding: str | None = None,
) -> dict:
    text, encoding, data = _read_text_bytes(path, encoding)
    lines = text.splitlines()
    total = len(lines)
    if tail:
        start = max(1, total - tail + 1)
        count = tail
    else:
        start = max(1, offset)
        count = max(1, limit)
    chosen = lines[start - 1 : start - 1 + count]
    digits = len(str(start + len(chosen)))
    body: list[str] = []
    used = 0
    for i, line in enumerate(chosen, start):
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + f"…[{len(line) - MAX_LINE} chars truncated]"
        entry = f"{i:>{digits}}\t{line}"
        size = width(entry) + 1
        if body and max_chars and used + size > max_chars:
            break
        body.append(entry)
        used += size
    out = {
        "path": path,
        "encoding": encoding,
        "total_lines": total,
        "newline": "crlf" if "\r\n" in text else "lf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "content": "\n".join(body),
    }
    end = start - 1 + len(body)
    if end < total:
        why = f" (stopped at max_chars={max_chars})" if len(body) < len(chosen) else ""
        out["more"] = f"lines {end + 1}-{total} not shown{why}: fs_read(path, offset={end + 1})"
    return out


def fs_write(
    path: str,
    content: str,
    encoding: str = "utf-8",
    create_dirs: bool = True,
    append: bool = False,
    expected_sha256: str | None = None,
) -> dict:
    from .redact import refuse_placeholders

    refuse_placeholders(content)
    full = _p(path)
    _check_expected(path, expected_sha256)
    note = None
    if append:
        if create_dirs:
            os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        enc = "utf-8" if encoding == "utf-8-sig" and os.path.isfile(full) else encoding
        data = content.encode(enc, errors="strict")
        with open(full, "ab") as fh:
            fh.write(data)
    else:
        data = content.encode(encoding, errors="strict")
        note = write_bytes_atomic(path, data, create_dirs)
    out = {"path": path, "bytes": len(data), "encoding": encoding, "sha256": sha256_of(path)}
    if append:
        out["size"] = os.path.getsize(full)
    if note:
        out["note"] = note
    return out


def _apply_edit(text: str, path: str, old: str, new: str, replace_all: bool, label: str) -> tuple[str, dict]:
    if old == new:
        raise FsError(f"{label}old and new are identical")
    if not old:
        raise FsError(f"{label}old must not be empty; use fs_write to create or overwrite a file")
    if "\r\n" in text and "\r\n" not in old:
        old = old.replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(old)
    if count == 0:
        hint = ""
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        if first and first in text:
            line_no = text[: text.index(first)].count("\n") + 1
            hint = f" (its first line appears near line {line_no}; check whitespace/indentation)"
        raise FsError(f"{label}old text not found in {path}{hint}")
    if count > 1 and not replace_all:
        raise FsError(
            f"{label}old text occurs {count} times in {path}; add context to make it unique "
            "or pass replace_all=true"
        )
    first_at = text.index(old)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    return updated, {"replacements": count if replace_all else 1, "line": text[:first_at].count("\n") + 1}


def fs_edit(
    path: str,
    old: str | None = None,
    new: str | None = None,
    replace_all: bool = False,
    edits: list[dict] | None = None,
    expected_sha256: str | None = None,
) -> dict:
    """Replace exact snippets. Fails loudly when one is missing or ambiguous; with several
    `edits` either all apply or the file is left untouched."""
    if edits:
        if old is not None or new is not None:
            raise FsError("give either old/new or edits, not both")
        ops = [(e.get("old") or "", e.get("new") or "", bool(e.get("replace_all"))) for e in edits]
    else:
        if old is None or new is None:
            raise FsError("give old and new (or edits=[{old, new}, ...])")
        ops = [(old, new, replace_all)]
    from .redact import refuse_placeholders

    refuse_placeholders(*(o for op in ops for o in op[:2]))
    text, encoding, data = _read_text_bytes(path)
    if expected_sha256 is not None and hashlib.sha256(data).hexdigest() != expected_sha256.strip().lower():
        raise FsError(f"{path} changed since you read it; read it again before editing")
    results = []
    for n, (o, nw, ra) in enumerate(ops, 1):
        text, info = _apply_edit(text, path, o, nw, ra, f"edit {n}: " if len(ops) > 1 else "")
        results.append(info)
    write_enc, note = write_text_like(path, text, encoding)
    out = {
        "path": path,
        "replacements": sum(r["replacements"] for r in results),
        "line": results[0]["line"],
        "encoding": write_enc,
        "sha256": sha256_of(path),
    }
    if len(results) > 1:
        out["edits"] = results
    if note:
        out["note"] = note
    return out


# ------------------------------------------------------------------ listing and finding

_AGE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|min|h|d|w)\s*$", re.IGNORECASE)


def parse_since(value: str) -> float:
    """A point in time from "30m", "2h", "3d", "1w" (ago) or a date/time such as
    "2026-10-05" or "2026-10-05 13:30"."""
    m = _AGE.match(value)
    if m:
        unit = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2).lower()]
        return time.time() - float(m.group(1)) * unit
    try:
        return datetime.fromisoformat(value.strip().replace("/", "-")).timestamp()
    except ValueError as exc:
        raise FsError(f"newer_than must look like 2h, 3d or 2026-10-05 13:30, not {value!r}") from exc


def _modified(st: os.stat_result) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))


def fs_list(
    path: str = ".",
    depth: int = 1,
    pattern: str | None = None,
    show_hidden: bool = False,
    limit: int = 500,
    sort: str = "name",
    newer_than: str | None = None,
    kind: str = "all",
) -> dict:
    if sort not in ("name", "mtime", "size"):
        raise FsError("sort is name, mtime or size")
    if kind not in ("all", "file", "dir"):
        raise FsError("kind is all, file or dir")
    root = Path(_p(path))
    if not root.is_dir():
        if not root.is_file():
            raise FsError(f"no such folder: {path}")
        st = root.stat()  # a file: describe it rather than fail
        return {
            "root": winenv.display_path(str(root.parent)),
            "entries": [{"path": root.name, "type": "file", "size": st.st_size, "modified": _modified(st)}],
            "note": f"{path} is a file, not a folder: fs_read reads it",
        }
    since = parse_since(newer_than) if newer_than else None
    # sorting by time or size has to see everything before keeping `limit` entries
    cap = limit if sort == "name" else max(limit, 20_000)
    entries: list[dict] = []
    stamps: list[float] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError) as exc:
            entries.append({"path": winenv.display_path(str(directory)), "error": str(exc)})
            stamps.append(0.0)
            return
        for child in children:
            if not show_hidden and child.name.startswith("."):
                continue
            if len(entries) >= cap:
                truncated = True
                return
            is_dir = child.is_dir()
            try:
                st = child.stat()
            except OSError:
                st = None
            shown = kind == "all" or (kind == "dir") == is_dir
            if pattern and (not is_dir or kind == "dir") and not fnmatch.fnmatch(child.name, pattern):
                shown = False
            if since is not None and st is not None and st.st_mtime < since and (kind == "dir" or not is_dir):
                shown = False
            if shown:
                rel = os.path.relpath(child, root)
                item: dict = {"path": rel + ("/" if is_dir else ""), "type": "dir" if is_dir else "file"}
                if st is not None:
                    if not is_dir:
                        item["size"] = st.st_size
                    item["modified"] = _modified(st)
                entries.append(item)
                stamps.append(st.st_mtime if st is not None else 0.0)
            if (
                is_dir
                and level < depth
                and child.name not in (".git", "node_modules", ".venv", "__pycache__")
            ):
                walk(child, level + 1)

    walk(root, 1)
    if sort == "mtime":
        entries = [e for _, e in sorted(zip(stamps, entries, strict=True), key=lambda p: p[0], reverse=True)]
    elif sort == "size":
        entries.sort(key=lambda e: e.get("size", -1), reverse=True)
    if len(entries) > limit:
        entries = entries[:limit]
        truncated = True
    kept = within_budget(entries, lambda e: width(json.dumps(e, ensure_ascii=False)) + 2)
    out = {"root": winenv.display_path(str(root)), "entries": entries[:kept]}
    if kept < len(entries):
        out["truncated"] = (
            f"stopped at {kept} entries, all one result holds; narrow with pattern/depth/newer_than"
        )
    elif truncated:
        out["truncated"] = f"stopped at {limit} entries; narrow with pattern/depth/newer_than"
    return out


def within_budget(items: list, size, budget: int = OUTPUT_BUDGET) -> int:
    """How many of the items fit in one tool result (at least one), each taking size(item)."""
    used = 0
    for i, item in enumerate(items):
        used += size(item)
        if used > budget and i:
            return i
    return len(items)


def fs_find(
    name: str,
    path: str = ".",
    max_depth: int | None = None,
    kind: str = "all",
    newer_than: str | None = None,
    max_results: int = 200,
    show_hidden: bool = False,
    budget_s: float = FIND_BUDGET_S,
) -> dict:
    """Find files/folders by name below `path`: a glob (*.py, report-*.xlsx) or, without
    wildcards, any name containing the text. Case-insensitive."""
    if kind not in ("all", "file", "dir"):
        raise FsError("kind is all, file or dir")
    if not name:
        raise FsError("name is empty")
    root = _p(path)
    if not os.path.isdir(root):
        raise FsError(f"not a directory: {path}")
    needle = name.lower()
    wildcard = any(c in name for c in "*?[")
    since = parse_since(newer_than) if newer_than else None
    deadline = time.monotonic() + budget_s
    matches: list[dict] = []
    scanned = skipped = 0
    stack: list[tuple[str, int]] = [(root, 0)]
    timed_out = truncated = False
    while stack and not truncated:
        if time.monotonic() > deadline:
            timed_out = True
            break
        directory, level = stack.pop()
        scanned += 1
        try:
            with os.scandir(directory) as it:
                children = sorted(it, key=lambda e: e.name.lower())
        except OSError:
            skipped += 1
            continue
        subdirs = []
        for entry in children:
            if not show_hidden and entry.name.startswith("."):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            lowered = entry.name.lower()
            hit = fnmatch.fnmatchcase(lowered, needle) if wildcard else needle in lowered
            if hit and (kind == "all" or (kind == "dir") == is_dir):
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    st = None
                if since is None or (st is not None and st.st_mtime >= since):
                    rel = winenv.display_path(os.path.relpath(entry.path, root))
                    item: dict = {"path": rel + ("/" if is_dir else ""), "type": "dir" if is_dir else "file"}
                    if st is not None:
                        if not is_dir:
                            item["size"] = st.st_size
                        item["modified"] = _modified(st)
                    matches.append(item)
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if is_dir and entry.name not in _SKIP_DIRS and (max_depth is None or level + 1 < max_depth):
                subdirs.append(entry.path)
        stack.extend((d, level + 1) for d in reversed(subdirs))
    out: dict = {
        "root": winenv.display_path(root),
        "matches": matches,
        "count": len(matches),
        "scanned_dirs": scanned,
    }
    if skipped:
        out["unreadable_dirs"] = skipped
    if truncated:
        out["truncated"] = f"stopped at {max_results} matches; narrow name, path or max_depth"
    if timed_out:
        out["timed_out"] = (
            f"searched for {budget_s:.0f}s and stopped; these are the matches so far. "
            "Narrow path or max_depth to search the rest"
        )
    return out


def fs_stat(path: str, hash: str | None = None) -> dict:
    full = _p(path)
    if not os.path.exists(full):
        return {"path": path, "exists": False}
    st = os.stat(full)
    out = {
        "path": path,
        "exists": True,
        "type": "dir" if os.path.isdir(full) else "file",
        "size": st.st_size,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "absolute": winenv.display_path(full),
    }
    if hash:
        if hash not in ("sha256", "md5", "sha1"):
            raise FsError("hash is sha256, md5 or sha1")
        if out["type"] != "file":
            raise FsError(f"{path} is a directory; hash needs a file")
        digest = hashlib.new(hash)
        with open(full, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        out[hash] = digest.hexdigest()
    return out


# ------------------------------------------------------------------ search


def _decode_match(line: bytes) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return line.decode("gb18030")
        except UnicodeDecodeError:
            return line.decode("utf-8", errors="replace")


_LINE_NO = re.compile(rb"(\d+)([:-])")
_MATCH_LINE = re.compile(r"^(?:.*?:)?\d+:")  # `path:12:text`, or `12:text` when one file is searched


def _decode_path(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:  # not what ripgrep writes on Windows, but never fail over a name
        return raw.decode("mbcs" if winenv.IS_WINDOWS else "utf-8", errors="replace")


def _rel(path: str) -> str:
    return path[2:] if path.startswith(("./", ".\\")) else path


class _RgOutput:
    """ripgrep's --null output turned into `path:line:text` (`path-line-text` for context lines):
    the path and the text are decoded on their own, so a GBK line no longer garbles the UTF-8
    path in front of it. Counts matches, not context lines."""

    def __init__(self, files_only: bool, single: bool) -> None:
        self.files_only, self.single = files_only, single
        self.lines: list[str] = []
        self.matches = 0

    def add(self, raw: bytes) -> None:
        raw = raw.rstrip(b"\r\n")
        if not raw:
            return
        if self.files_only:
            self.lines.append(_rel(_decode_path(raw.rstrip(b"\0"))))
            self.matches += 1
            return
        if raw == b"--":
            self.lines.append("--")
            return
        path, nul, rest = raw.partition(b"\0")
        m = _LINE_NO.match(rest) if nul else None
        if m is None:
            self.lines.append(_decode_match(raw))
            return
        number, mark = m.group(1).decode(), m.group(2).decode()
        text = _decode_match(rest[m.end() :])
        where = "" if self.single else _rel(_decode_path(path)) + mark
        self.lines.append(f"{where}{number}{mark}{text}")
        if mark == ":":
            self.matches += 1


def _run_rg(
    argv: list[str], max_results: int, budget_s: float, cwd: str | None = None, out: _RgOutput | None = None
) -> tuple[list[str], int | None, str, bool]:
    """Run ripgrep (with --null), keeping what it found when it runs out of time or finds enough.
    Returns (lines, returncode or None when stopped early, stderr, timed_out)."""
    out = out or _RgOutput(False, False)
    kwargs: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if winenv.IS_WINDOWS else {}
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=winenv.build_env(),
        **kwargs,
    )
    errors: list[bytes] = []
    lock = threading.Lock()
    split = b"\0" if out.files_only else b"\n"  # a list of files is NUL-terminated with --null

    def read_out() -> None:
        assert proc.stdout is not None
        rest = b""
        for chunk in iter(lambda: proc.stdout.read1(65536), b""):
            rest += chunk
            *items, rest = rest.split(split)
            with lock:
                for item in items:
                    out.add(item)
        with lock:
            out.add(rest)

    def read_err() -> None:
        assert proc.stderr is not None
        errors.append(proc.stderr.read())

    readers = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    for t in readers:
        t.start()
    deadline = time.monotonic() + budget_s
    timed_out = stopped = False
    while proc.poll() is None:
        if out.matches > max_results:
            stopped = True
            break
        if time.monotonic() > deadline:
            timed_out = stopped = True
            break
        time.sleep(0.05)
    if stopped:
        proc.kill()
    proc.wait()
    for t in readers:
        t.join(timeout=2)
    stderr = b"".join(errors).decode("utf-8", errors="replace").strip()
    with lock:
        return list(out.lines), None if stopped else proc.returncode, stderr, timed_out


def _first_matches(lines: list[str], limit: int, files_only: bool) -> tuple[list[str], bool]:
    """The lines of the first `limit` matches (with their context lines); whether more were found."""
    if files_only:
        return lines[:limit], len(lines) > limit
    kept, seen = [], 0
    for line in lines:
        if _MATCH_LINE.match(line):
            seen += 1
            if seen > limit:
                return kept, True
        kept.append(line)
    return kept, False


def fs_search(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    literal: bool = False,
    max_results: int = 200,
    context: int = 0,
    files_only: bool = False,
    encoding: str | None = None,
    budget_s: float = SEARCH_BUDGET_S,
) -> dict:
    root = _p(path)
    rg = shutil.which("rg")
    timed_out = False
    warnings = ""
    engine = "ripgrep"
    single = os.path.isfile(root)
    prefix = winenv.display_path(root).rstrip("\\/")
    if rg:
        # searched from inside the root: paths come back relative to it, and a glob with folders
        # in it (src/**/*.c, {user,lib}/**/*.h) means what it says
        cwd, target = (os.path.dirname(root), os.path.basename(root)) if single else (root, ".")
        argv = [
            rg,
            "--line-number",
            "--no-heading",
            "--null",
            "--color",
            "never",
            "--max-columns",
            "400",
            "--max-columns-preview",
        ]
        if ignore_case:
            argv.append("-i")
        if literal:
            argv.append("-F")
        if glob:
            argv += ["--glob", glob]
        if files_only:
            argv.append("--files-with-matches")
        elif context:
            argv.append(f"--context={int(context)}")
        if encoding:
            argv += ["--encoding", encoding]
        began = time.monotonic()

        def search(extra: list[str], budget: float):
            out = _RgOutput(files_only, single)
            return _run_rg(argv + extra + ["-e", pattern, target], max_results, budget, cwd, out)

        lines, code, stderr, timed_out = search([], budget_s)
        if code == 2 and not lines and re.search(r"look-?around|backreference", stderr, re.IGNORECASE):
            engine = "ripgrep (pcre2)"
            argv.append("--pcre2")
            lines, code, stderr, timed_out = search([], budget_s)
        if code not in (None, 0, 1):
            if not lines:
                raise FsError(stderr or "ripgrep failed")
            warnings = stderr[-600:]  # e.g. some files could not be read; the matches still count
        # Chinese in GBK/GB18030 files (common in embedded C sources): ripgrep reads UTF-8, so a
        # non-ASCII pattern finds nothing there. Those files are searched once more, read as GB18030.
        left = budget_s - (time.monotonic() - began)
        if not encoding and not pattern.isascii() and not timed_out and left > 1:
            more, _, _, more_timed_out = search(["--encoding", "gb18030"], left)
            known = set(lines)
            extra = [line for line in more if line not in known]
            if files_only or any(_MATCH_LINE.match(line) for line in extra):
                if extra:
                    lines = lines + (["--"] if context and lines and not files_only else []) + extra
                    engine += " + gb18030"
            timed_out = timed_out or more_timed_out
    else:
        lines = list(
            _py_search(pattern, root, glob, ignore_case, literal, max_results + 1, context, files_only)
        )
        engine = "python"
    results, more = _first_matches(lines, max_results, files_only)
    kept = within_budget(results, lambda line: width(line) + 4)
    out = {"root": prefix, "engine": engine, "matches": results[:kept], "count": kept}
    if kept < len(results):
        out["truncated"] = (
            f"the first {kept} lines are all one result holds; narrow the pattern, path or glob, "
            "or files_only=true"
        )
    elif more:
        out["truncated"] = f"more than {max_results} matches; narrow the pattern, path or glob"
    if timed_out:
        out["timed_out"] = (
            f"searched for {budget_s:.0f}s and stopped; these are the matches so far. "
            "Narrow path or glob to search the rest"
        )
    if warnings:
        out["warnings"] = warnings
    return out


def _brace(glob: str) -> list[str]:
    """{a,b} alternatives spelled out, as ripgrep reads a glob: src/*.{c,h} -> src/*.c, src/*.h."""
    m = re.search(r"\{([^{}]*)\}", glob)
    if not m:
        return [glob]
    return [g for part in m.group(1).split(",") for g in _brace(glob[: m.start()] + part + glob[m.end() :])]


def _glob_regex(glob: str) -> re.Pattern:
    """A glob as a regex on a /-separated path: ** spans folders, * and ? stay within one."""
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "[" and "]" in glob[i + 2 :]:
            end = glob.index("]", i + 2)
            body = glob[i + 1 : end]
            out, i = out + "[" + ("^" + body[1:] if body.startswith("!") else body) + "]", end + 1
        else:
            out += {"*": "[^/]*", "?": "[^/]"}.get(glob[i]) or re.escape(glob[i])
            i += 1
    return re.compile(out + r"\Z")


def _globber(glob: str | None):
    """keep(rel, folder=False) as ripgrep's --glob decides: a glob with a / is matched against the
    path from the searched folder, one without against the name; !glob leaves out what it
    matches (folders too)."""
    if not glob:
        return lambda rel, folder=False: True
    exclude = glob.startswith("!")
    patterns = [("/" in g, _glob_regex(g.lstrip("/"))) for g in _brace(glob[1:] if exclude else glob)]

    def keep(rel: str, folder: bool = False) -> bool:
        name = rel.rsplit("/", 1)[-1]
        hit = any(rx.match(rel if whole else name) for whole, rx in patterns)
        if folder:
            return not (exclude and hit)
        return hit != exclude

    return keep


def _py_files(root: str, keep):
    """(full path, path from root) of the files to search under root."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d not in ("node_modules", "__pycache__")
            and keep(os.path.relpath(os.path.join(dirpath, d), root).replace(os.sep, "/"), folder=True)
        ]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if keep(rel.replace(os.sep, "/")):
                yield full, rel


def _py_search(pattern, root, glob, ignore_case, literal, cap, context=0, files_only=False):
    """ripgrep's search where it is not installed, answered the way fs_search answers with it:
    paths from root (none when root is one file), the same globs, GBK files decoded."""
    flags = re.IGNORECASE if ignore_case else 0
    regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    files = [(root, None)] if os.path.isfile(root) else _py_files(root, _globber(glob))
    found = 0
    for full, rel in files:
        try:
            with open(full, "rb") as fh:
                data = fh.read(2_000_000)
        except OSError:
            continue
        if _is_binary(data):
            continue
        text, _ = _decode(data)
        lines = text.splitlines()
        for no, line in enumerate(lines, 1):
            if not regex.search(line):
                continue
            if files_only:
                yield rel or os.path.basename(full)  # as ripgrep names one file it searched
                found += 1
                break
            for before in range(max(1, no - context), no):
                yield f"{rel + '-' if rel else ''}{before}-{lines[before - 1][:400]}"
            yield f"{rel + ':' if rel else ''}{no}:{line[:400]}"
            for after in range(no + 1, min(len(lines), no + context) + 1):
                yield f"{rel + '-' if rel else ''}{after}-{lines[after - 1][:400]}"
            found += 1
            if found >= cap:
                return
        if found >= cap:
            return


# ------------------------------------------------------------------ managing files


def _guard(full: str) -> None:
    """Refuse to delete or move a drive root, the home folder or the Windows folder."""
    resolved = os.path.normcase(os.path.abspath(winenv.display_path(full))).rstrip("\\/")
    protected = {
        os.path.normcase(os.path.abspath(p)).rstrip("\\/")
        for p in (os.path.expanduser("~"), os.environ.get("SystemRoot", r"C:\Windows"))
        if p
    }
    is_root = not resolved or os.path.dirname(resolved) == resolved or re.fullmatch(r"(?i)[a-z]:", resolved)
    if is_root or resolved in protected:
        raise FsError(f"refusing to change {winenv.display_path(full)}: it is a drive root or system folder")


def _recycle(full: str) -> None:
    """Move to the Recycle Bin (Explorer's delete), so a mistake can be undone."""
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", ctypes.c_uint16),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    plain = winenv.display_path(full)
    if len(plain) >= 260:
        raise FsError("the Recycle Bin cannot take paths this long; pass permanent=true to delete it")
    op = SHFILEOPSTRUCTW()
    op.wFunc = 3  # FO_DELETE
    op.pFrom = plain + "\0"  # the list is double-NUL terminated; ctypes adds the second
    op.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400  # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise FsError(f"could not move {plain} to the Recycle Bin (code {code:#x})")


def _force_writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def fs_op(
    action: str,
    path: str,
    to: str | None = None,
    overwrite: bool = False,
    recursive: bool = False,
    permanent: bool = False,
) -> dict:
    """copy | move | delete | mkdir, without a shell in between."""
    full = _p(path)
    if action == "mkdir":
        existed = os.path.isdir(full)
        os.makedirs(full, exist_ok=True)
        return {"action": action, "path": path, "created": not existed}
    if action not in ("copy", "move", "delete"):
        raise FsError("action is copy, move, delete or mkdir")
    if not os.path.lexists(full):
        raise FsError(f"no such file or directory: {path}")
    is_dir = os.path.isdir(full) and not os.path.islink(full)
    if action == "delete":
        _guard(full)
        if is_dir and not recursive and any(os.scandir(full)):
            raise FsError(f"{path} is a folder that is not empty; pass recursive=true to delete it")
        if winenv.IS_WINDOWS and not permanent:
            _recycle(full)
            return {"action": action, "path": path, "recycled": True}
        if is_dir:
            shutil.rmtree(full, onexc=_force_writable)
        else:
            try:
                os.remove(full)
            except PermissionError:
                os.chmod(full, stat.S_IWRITE)
                os.remove(full)
        out = {"action": action, "path": path, "recycled": False}
        if not winenv.IS_WINDOWS and not permanent:
            out["note"] = "deleted permanently (this system has no Recycle Bin)"
        return out
    if not to:
        raise FsError(f"{action} needs `to`")
    dest = _p(to)
    if os.path.isdir(dest):
        dest = os.path.join(dest, os.path.basename(full.rstrip("\\/")))
    if os.path.lexists(dest) and not overwrite:
        raise FsError(f"{winenv.display_path(dest)} already exists; pass overwrite=true to replace it")
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if action == "copy":
        if is_dir:
            shutil.copytree(full, dest, dirs_exist_ok=overwrite)
        else:
            shutil.copy2(full, dest)
    else:
        _guard(full)
        if is_dir and os.path.isdir(dest):
            raise FsError(
                f"{winenv.display_path(dest)} is an existing folder; remove it first or move elsewhere"
            )
        if os.path.isfile(dest) and not is_dir:
            os.replace(full, dest)
        else:
            shutil.move(full, dest)
    return {
        "action": action,
        "path": path,
        "to": winenv.display_path(dest),
        "type": "dir" if is_dir else "file",
    }
