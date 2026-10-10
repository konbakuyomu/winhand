"""Apply unified diffs (plain or git) to files on this machine, without git or patch.exe.

Built for code edited elsewhere (a cloud sandbox) that must land in a Windows working tree:
- the path prefix to strip (-p) is worked out from which files exist;
- each file keeps its own encoding (UTF-8, BOM, GBK) and line endings (CRLF stays CRLF),
  whatever line endings the patch itself has;
- hunks are matched by content and may sit at other line numbers than the diff says;
- all files are checked first and nothing is written unless every hunk fits (unless partial).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from . import fs, winenv

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(fs.FsError):
    pass


@dataclass
class Hunk:
    header: str
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    old: list[str] = field(default_factory=list)  # context + removed lines
    new: list[str] = field(default_factory=list)  # context + added lines
    ops: list[tuple[str, str]] = field(default_factory=list)  # (" " | "-" | "+", text) in order
    old_eof: bool = False  # old side's last line has no newline
    new_eof: bool = False


@dataclass
class FilePatch:
    old_path: str | None  # None: the file is created
    new_path: str | None  # None: the file is deleted
    hunks: list[Hunk] = field(default_factory=list)
    binary: bool = False
    rename: bool = False
    crlf: bool = False  # the patch text itself used CRLF for this file


def _unquote(quoted: str) -> str:
    """Git's C-style quoting of unusual paths: "\\346\\226\\207.txt" -> 文.txt."""
    inner, out, i = quoted[1:-1], bytearray(), 0
    while i < len(inner):
        ch = inner[i]
        if ch == "\\" and i + 1 < len(inner):
            nxt = inner[i + 1]
            if nxt in "01234567":
                out.append(int(inner[i + 1 : i + 4], 8))
                i += 4
                continue
            out += {"n": b"\n", "t": b"\t", "\\": b"\\", '"': b'"'}.get(nxt, nxt.encode())
            i += 2
            continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _path(raw: str) -> str | None:
    raw = raw.split("\t")[0].strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = _unquote(raw)
    return None if raw == "/dev/null" else raw


def _split(text: str) -> list[str]:
    """Lines without their endings (only CRLF and LF end a line, unlike str.splitlines)."""
    lines = re.split(r"\r\n|\n", text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse(text: str) -> list[FilePatch]:
    lines = text.split("\n")
    files: list[FilePatch] = []
    current: FilePatch | None = None
    git_paths: tuple[str, str] | None = None
    i = 0
    while i < len(lines):
        line = lines[i].rstrip("\r")
        if line.startswith("diff --git "):
            m = re.match(r'^diff --git ("?a/.+?"?) ("?b/.+"?)$', line)
            git_paths = (_path(m.group(1)), _path(m.group(2))) if m else None
            current = FilePatch(*(git_paths or (None, None)))
            current.crlf = lines[i].endswith("\r")
            files.append(current)
            i += 1
            continue
        if line.startswith(("new file mode", "deleted file mode")) and current is not None:
            if line.startswith("new"):
                current.old_path = None
            else:
                current.new_path = None
        elif line.startswith("rename from ") and current is not None:
            current.old_path, current.rename = "a/" + line[12:], True
        elif line.startswith("rename to ") and current is not None:
            current.new_path, current.rename = "b/" + line[10:], True
        elif line.startswith(("Binary files ", "GIT binary patch")) and current is not None:
            current.binary = True
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _path(line[4:]), _path(lines[i + 1].rstrip("\r")[4:])
            if (
                current is None
                or current.hunks
                or (git_paths is None and (current.old_path or current.new_path))
            ):
                current = FilePatch(old, new)
                files.append(current)
            else:
                current.old_path, current.new_path = old, new
            current.crlf = lines[i].endswith("\r")
            git_paths = None
            i += 2
            continue
        elif (m := _HUNK.match(line)) and current is not None:
            hunk = Hunk(
                header=line,
                old_start=int(m.group(1)),
                old_len=int(m.group(2)) if m.group(2) is not None else 1,
                new_start=int(m.group(3)),
                new_len=int(m.group(4)) if m.group(4) is not None else 1,
            )
            i += 1
            old_seen = new_seen = 0
            last = ""

            def mark(tag: str, h: Hunk = hunk) -> None:
                if tag in (" ", "-"):
                    h.old_eof = True
                if tag in (" ", "+"):
                    h.new_eof = True

            while i < len(lines) and (old_seen < hunk.old_len or new_seen < hunk.new_len):
                body = lines[i].rstrip("\r")
                tag, content = (body[:1], body[1:]) if body else (" ", "")
                if body.startswith("\\"):
                    mark(last)
                    i += 1
                    continue
                if tag == " ":
                    hunk.old.append(content)
                    hunk.new.append(content)
                    old_seen += 1
                    new_seen += 1
                elif tag == "-":
                    hunk.old.append(content)
                    old_seen += 1
                elif tag == "+":
                    hunk.new.append(content)
                    new_seen += 1
                if tag in (" ", "-", "+"):
                    hunk.ops.append((tag, content))
                else:
                    raise PatchError(f"unexpected line in hunk {hunk.header!r}: {body[:80]!r}")
                last = tag
                i += 1
            if old_seen != hunk.old_len or new_seen != hunk.new_len:
                raise PatchError(f"hunk {hunk.header!r} is cut short (the patch looks truncated)")
            while i < len(lines) and lines[i].rstrip("\r").startswith("\\"):
                mark(last)
                i += 1
            current.hunks.append(hunk)
            continue
        i += 1
    patches = [
        f for f in files if f.hunks or f.binary or f.rename or f.old_path is None or f.new_path is None
    ]
    if not patches:
        raise PatchError("no file changes found; give a unified diff (diff -u / git diff)")
    return patches


def _strip(path: str, n: int) -> str | None:
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) <= n:
        return None
    return "/".join(parts[n:])


def _target(root: str, rel: str) -> str:
    if os.path.isabs(rel) or re.match(r"^[A-Za-z]:", rel):
        raise PatchError(f"absolute path in patch: {rel}")
    full = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.normcase(full), os.path.normcase(root)]) != os.path.normcase(root):
        raise PatchError(f"path leaves the root folder: {rel}")
    return full


def choose_strip(patches: list[FilePatch], root: str) -> int:
    """The smallest -p that makes every file the patch changes exist under root."""
    existing = [p.old_path for p in patches if p.old_path is not None]
    if not existing:
        named = [p.new_path for p in patches if p.new_path]
        return 1 if named and all(n.startswith(("a/", "b/")) for n in named) else 0
    for n in range(4):
        ok = True
        for rel in existing:
            stripped = _strip(rel, n)
            if stripped is None or not os.path.isfile(winenv.long_path(os.path.join(root, stripped))):
                ok = False
                break
        if ok:
            return n
    sample = existing[0]
    raise PatchError(
        f"none of -p0..-p3 makes the patched files exist under {winenv.display_path(root)} "
        f"(e.g. {sample!r}); check root, or pass strip"
    )


def _find(lines: list[str], block: list[str], expected: int, start: int) -> tuple[int, str] | None:
    """Where `block` occurs at or after `start`, nearest to `expected`; exact first, then
    ignoring trailing whitespace."""
    if not block:
        return max(start, min(expected, len(lines))), "exact"
    n = len(block)
    last = len(lines) - n
    if last < start:
        return None
    order = sorted(range(start, last + 1), key=lambda p: (abs(p - expected), p))
    for mode, norm in (("exact", lambda s: s), ("whitespace", lambda s: s.rstrip())):
        want = [norm(x) for x in block]
        for pos in order:
            if norm(lines[pos]) == want[0] and [norm(x) for x in lines[pos : pos + n]] == want:
                return pos, mode
    return None


def _replacement(h: Hunk, matched: list[str]) -> list[str]:
    """The hunk's new lines, with context lines taken from the file itself (they may differ in
    trailing whitespace when matched loosely)."""
    out, i = [], 0
    for tag, text in h.ops:
        if tag == " ":
            out.append(matched[i])
            i += 1
        elif tag == "-":
            i += 1
        else:
            out.append(text)
    return out


def _apply_hunks(
    lines: list[str], eof_newline: bool, hunks: list[Hunk]
) -> tuple[list[str], bool, list[dict]]:
    report = []
    delta = 0  # lines added minus removed by the hunks before this one
    drift = 0  # how far this copy of the file is from the diff's line numbers
    floor = 0
    failed = False
    for h in hunks:
        base = (h.old_start - 1 if h.old_len else h.old_start) + delta
        expected = base + drift
        found = _find(lines, h.old, max(expected, 0), floor)
        if found is None:
            near = max(0, min(expected, len(lines) - 1))
            report.append(
                {
                    "hunk": h.header,
                    "error": "context not found",
                    "expected": h.old[:3],
                    "file_near_line": near + 1,
                    "file_has": lines[near : near + 3],
                }
            )
            failed = True
            delta += len(h.new) - len(h.old)
            continue
        pos, mode = found
        touches_end = pos + len(h.old) == len(lines)
        new_lines = _replacement(h, lines[pos : pos + len(h.old)])
        lines = lines[:pos] + new_lines + lines[pos + len(h.old) :]
        entry = {"hunk": h.header, "line": pos + 1, "offset": pos - base}
        if mode != "exact":
            entry["matched"] = "ignoring trailing whitespace"
        report.append(entry)
        drift = pos - base
        delta += len(h.new) - len(h.old)
        floor = pos + len(new_lines)
        if touches_end:
            if h.new_eof:
                eof_newline = False
            elif h.old_eof:
                eof_newline = True
    if failed:
        raise _HunkFailure(report)
    return lines, eof_newline, report


class _HunkFailure(Exception):
    def __init__(self, report: list[dict]) -> None:
        super().__init__("hunks failed")
        self.report = report


def apply(
    patch: str | None = None,
    root: str = ".",
    patch_file: str | None = None,
    strip: int | None = None,
    dry_run: bool = False,
    partial: bool = False,
) -> dict:
    if (patch is None) == (patch_file is None):
        raise PatchError("give patch (the diff text) or patch_file (a .diff/.patch file here)")
    if patch_file is not None:
        patch, _ = fs.read_text(patch_file)
    assert patch is not None
    root_full = os.path.abspath(os.path.expanduser(root))
    if not os.path.isdir(root_full):
        raise PatchError(f"root is not a folder: {root}")
    patches = parse(patch)
    binary = [p.new_path or p.old_path for p in patches if p.binary]
    if binary:
        raise PatchError(f"binary changes cannot be applied from text: {', '.join(map(str, binary))}")
    n = strip if strip is not None else choose_strip(patches, root_full)

    plans: list[dict] = []
    for p in patches:
        old_rel = _strip(p.old_path, n) if p.old_path else None
        new_rel = _strip(p.new_path, n) if p.new_path else None
        if (p.old_path and old_rel is None) or (p.new_path and new_rel is None):
            raise PatchError(f"strip={n} removes the whole path of {p.old_path or p.new_path!r}")
        action = (
            "create"
            if old_rel is None
            else "delete"
            if new_rel is None
            else "rename"
            if old_rel != new_rel
            else "modify"
        )
        plan: dict = {"path": new_rel or old_rel, "action": action}
        if action == "rename":
            plan["from"] = old_rel
        try:
            if old_rel is None:
                target = _target(root_full, new_rel)
                if os.path.exists(winenv.long_path(target)):
                    raise PatchError(f"{new_rel} already exists; the patch creates it")
                lines, eof, report = _apply_hunks([], True, p.hunks)
                newline = "\r\n" if p.crlf else "\n"
                plan.update(
                    target=target, lines=lines, eof=eof, newline=newline, encoding="utf-8", hunks=report
                )
            else:
                source = _target(root_full, old_rel)
                text, encoding = fs.read_text(source)
                newline = "\r\n" if "\r\n" in text else "\n"
                eof = text.endswith(("\n", "\r"))
                lines = _split(text)
                lines, eof, report = _apply_hunks(lines, eof, p.hunks)
                plan.update(
                    source=source, lines=lines, eof=eof, newline=newline, encoding=encoding, hunks=report
                )
                if new_rel is not None:
                    target = _target(root_full, new_rel)
                    if action == "rename" and os.path.exists(winenv.long_path(target)):
                        raise PatchError(f"{new_rel} already exists; the patch renames {old_rel} to it")
                    plan["target"] = target
                elif lines:
                    raise PatchError(
                        f"{old_rel}: the file still has content after removing what the patch deletes"
                    )
            plan["status"] = "ok"
        except _HunkFailure as exc:
            plan.update(status="failed", hunks=exc.report)
        except (PatchError, fs.FsError, OSError) as exc:
            plan.update(status="failed", error=str(exc))
        plans.append(plan)

    failed = [pl for pl in plans if pl["status"] != "ok"]
    write = not dry_run and (not failed or partial)
    for pl in plans:
        if pl["status"] != "ok" or not write:
            continue
        if pl["action"] == "delete":
            os.remove(winenv.long_path(pl["source"]))
            continue
        body = pl["newline"].join(pl["lines"]) + (pl["newline"] if pl["eof"] and pl["lines"] else "")
        os.makedirs(os.path.dirname(winenv.long_path(pl["target"])) or ".", exist_ok=True)
        fs.write_text_like(pl["target"], body, pl["encoding"])
        if pl["action"] == "rename":
            os.remove(winenv.long_path(pl["source"]))

    files = []
    for pl in plans:
        item = {k: pl[k] for k in ("path", "action", "status") if k in pl}
        for k in ("from", "error", "hunks"):
            if k in pl:
                item[k] = pl[k]
        if pl.get("encoding") and pl["action"] != "delete":
            item["encoding"] = pl["encoding"]
            item["newline"] = "crlf" if pl.get("newline") == "\r\n" else "lf"
        files.append(item)
    hunks = sum(len(pl.get("hunks", [])) for pl in plans)
    out = {
        "ok": not failed,
        "strip": n,
        "root": winenv.display_path(root_full),
        "written": write,
        "files": files,
    }
    if dry_run:
        out["summary"] = f"dry run: {len(plans) - len(failed)}/{len(plans)} files would apply ({hunks} hunks)"
    elif failed and not partial:
        out["summary"] = f"nothing written: {len(failed)} of {len(plans)} files do not apply (see files)"
    else:
        out["summary"] = f"applied {len(plans) - len(failed)}/{len(plans)} files ({hunks} hunks)"
    return out
