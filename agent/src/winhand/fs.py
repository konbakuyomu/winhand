"""File tools modelled on what coding agents need: numbered reads, exact edits,
fast search. Handles UTF-8/UTF-8-BOM/GBK files, keeps CRLF files CRLF and copes
with long, spaced and CJK paths on Windows."""

from __future__ import annotations

import fnmatch
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import winenv

_ENCODINGS = ("utf-8", "gb18030")
MAX_LINE = 2000


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


def read_text(path: str) -> tuple[str, str]:
    full = _p(path)
    if not os.path.exists(full):
        raise FsError(f"no such file: {path}")
    if os.path.isdir(full):
        raise FsError(f"{path} is a directory; use fs_list")
    with open(full, "rb") as fh:
        data = fh.read()
    if _is_binary(data):
        raise FsError(f"{path} looks binary ({len(data)} bytes); not shown as text")
    return _decode(data)


def fs_read(path: str, offset: int = 1, limit: int = 2000) -> dict:
    text, encoding = read_text(path)
    lines = text.splitlines()
    total = len(lines)
    start = max(1, offset)
    chosen = lines[start - 1 : start - 1 + max(1, limit)]
    width = len(str(start + len(chosen)))
    body = []
    for i, line in enumerate(chosen, start):
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE] + f"…[{len(line) - MAX_LINE} chars truncated]"
        body.append(f"{i:>{width}}\t{line}")
    out = {
        "path": path,
        "encoding": encoding,
        "total_lines": total,
        "newline": "crlf" if "\r\n" in text else "lf",
        "content": "\n".join(body),
    }
    end = start - 1 + len(chosen)
    if end < total:
        out["more"] = f"lines {end + 1}-{total} not shown: fs_read(path, offset={end + 1})"
    return out


def fs_write(path: str, content: str, encoding: str = "utf-8", create_dirs: bool = True) -> dict:
    full = _p(path)
    if create_dirs:
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
    data = content.encode(encoding, errors="strict")
    with open(full, "wb") as fh:
        fh.write(data)
    return {"path": path, "bytes": len(data), "encoding": encoding}


def fs_edit(path: str, old: str, new: str, replace_all: bool = False) -> dict:
    """Replace an exact snippet. Fails loudly when it is missing or ambiguous."""
    if old == new:
        raise FsError("old and new are identical")
    if not old:
        raise FsError("old must not be empty; use fs_write to create or overwrite a file")
    text, encoding = read_text(path)
    crlf = "\r\n" in text
    if crlf and "\r\n" not in old:
        old = old.replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    count = text.count(old)
    if count == 0:
        hint = ""
        first = old.strip().splitlines()[0].strip() if old.strip() else ""
        if first and first in text:
            line_no = text[: text.index(first)].count("\n") + 1
            hint = f" (its first line appears near line {line_no}; check whitespace/indentation)"
        raise FsError(f"old text not found in {path}{hint}")
    if count > 1 and not replace_all:
        raise FsError(
            f"old text occurs {count} times in {path}; add context to make it unique or pass replace_all=true"
        )
    first_at = text.index(old)
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    write_enc = "utf-8" if encoding == "latin-1" else encoding
    with open(_p(path), "wb") as fh:
        if write_enc == "utf-8-sig":
            fh.write(b"\xef\xbb\xbf" + updated.encode("utf-8"))
        else:
            fh.write(updated.encode(write_enc))
    return {
        "path": path,
        "replacements": count if replace_all else 1,
        "line": text[:first_at].count("\n") + 1,
        "encoding": write_enc,
    }


def fs_list(
    path: str = ".", depth: int = 1, pattern: str | None = None, show_hidden: bool = False, limit: int = 500
) -> dict:
    root = Path(_p(path))
    if not root.is_dir():
        raise FsError(f"not a directory: {path}")
    entries: list[dict] = []
    truncated = False

    def walk(directory: Path, level: int) -> None:
        nonlocal truncated
        try:
            children = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except (PermissionError, OSError) as exc:
            entries.append({"path": winenv.display_path(str(directory)), "error": str(exc)})
            return
        for child in children:
            if not show_hidden and child.name.startswith("."):
                continue
            if len(entries) >= limit:
                truncated = True
                return
            is_dir = child.is_dir()
            if pattern and not is_dir and not fnmatch.fnmatch(child.name, pattern):
                continue
            rel = os.path.relpath(child, root)
            item: dict = {"path": rel + ("/" if is_dir else ""), "type": "dir" if is_dir else "file"}
            if not is_dir:
                try:
                    item["size"] = child.stat().st_size
                except OSError:
                    pass
            entries.append(item)
            if (
                is_dir
                and level < depth
                and child.name not in (".git", "node_modules", ".venv", "__pycache__")
            ):
                walk(child, level + 1)

    walk(root, 1)
    out = {"root": winenv.display_path(str(root)), "entries": entries}
    if truncated:
        out["truncated"] = f"stopped at {limit} entries; narrow with pattern/depth"
    return out


def fs_stat(path: str) -> dict:
    full = _p(path)
    if not os.path.exists(full):
        return {"path": path, "exists": False}
    st = os.stat(full)
    return {
        "path": path,
        "exists": True,
        "type": "dir" if os.path.isdir(full) else "file",
        "size": st.st_size,
        "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        "absolute": winenv.display_path(full),
    }


def fs_search(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    ignore_case: bool = False,
    literal: bool = False,
    max_results: int = 200,
) -> dict:
    root = _p(path)
    rg = shutil.which("rg")
    if rg:
        argv = [
            rg,
            "--line-number",
            "--no-heading",
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
        argv += ["-e", pattern, root]
        proc = subprocess.run(argv, capture_output=True, timeout=60, env=winenv.build_env())
        if proc.returncode not in (0, 1):
            raise FsError(proc.stderr.decode("utf-8", errors="replace").strip() or "ripgrep failed")
        lines = proc.stdout.decode("utf-8", errors="replace").splitlines()
        engine = "ripgrep"
    else:
        lines = list(_py_search(pattern, root, glob, ignore_case, literal, max_results + 1))
        engine = "python"
    prefix = winenv.display_path(root).rstrip("\\/")
    results = [ln[len(prefix) + 1 :] if ln.startswith(prefix) else ln for ln in lines[:max_results]]
    out = {"root": prefix, "engine": engine, "matches": results, "count": len(results)}
    if len(lines) > max_results:
        out["truncated"] = f"more than {max_results} matches; narrow the pattern, path or glob"
    return out


def _py_search(pattern, root, glob, ignore_case, literal, cap):
    flags = re.IGNORECASE if ignore_case else 0
    regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    found = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames if not d.startswith(".") and d not in ("node_modules", "__pycache__")
        ]
        for name in filenames:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, "rb") as fh:
                    data = fh.read(2_000_000)
            except OSError:
                continue
            if _is_binary(data):
                continue
            text, _ = _decode(data)
            for no, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    yield f"{winenv.display_path(full)}:{no}:{line[:400]}"
                    found += 1
                    if found >= cap:
                        return
