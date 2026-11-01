"""Several files through one link: a folder (or a list of paths) streamed as one zip while it
downloads, and an uploaded zip or tar unpacked into a folder.

From the 2026-11-01 review: 57 batches moved four or more files at once (349 files), nearly all
by packing them with a command first and unpacking them on the other side by hand. Now
fs_download_url takes a folder or several paths, and fs_upload_url(extract=true) unpacks the
archive it receives.

Both directions keep to the owner's rules file by file (guard.PathCheck). A download leaves out
the files the read rules keep from the AI and says which. An unpack is all or nothing: every name
is checked, and every target judged by the write rules, before the first file is written. Names
that could land outside the folder (absolute, `..`, a drive, a link already in the folder) or
that Windows cannot hold (CON, `a:b`, a trailing dot) refuse the whole archive; so do links in it.
"""

from __future__ import annotations

import contextlib
import fnmatch
import hashlib
import os
import re
import tarfile
import time
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field

from . import fs, winenv

MAX_FILES = 20_000
MAX_BYTES = 4 * 2**30  # unpacked size, either direction
LISTED = 200  # files named one by one in an unpack's answer
CHUNK = 1 << 20
ERRORS_NAME = "winhand-errors.txt"
# already compressed: deflating them again costs time and saves nothing
STORED = frozenset(
    ".7z .apk .avif .br .bz2 .docx .gif .gz .heic .jar .jpeg .jpg .m4a .mkv .mov .mp3 .mp4 .nupkg .ogg "
    ".pdf .png .pptx .rar .tgz .webm .webp .whl .xlsx .xz .zip .zst".split()
)
_RESERVED = re.compile(r"(?i)^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$")
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')


class BundleError(ValueError):
    pass


@dataclass
class Plan:
    """What a folder download will hold, fixed when the link is made."""

    name: str  # the zip's file name
    files: list[tuple[str, str, int]] = field(default_factory=list)  # full path, name in zip, size
    left_out: list[str] = field(default_factory=list)  # names the read rules keep from the AI
    skipped: list[str] = field(default_factory=list)  # links and unreadable entries, with why

    @property
    def size(self) -> int:
        return sum(size for _, _, size in self.files)


def _matches(rel: str, patterns: list[str]) -> bool:
    """A pattern matches a path relative to the folder, or just its last part: `*.log` takes
    logs at any depth, `node_modules` a folder of that name anywhere."""
    base = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatchcase(rel, p) or fnmatch.fnmatchcase(base, p) for p in patterns)


def _is_link(entry: os.DirEntry) -> bool:
    with contextlib.suppress(OSError):
        return entry.is_symlink() or entry.is_junction()
    return True


def plan_download(
    paths: list[str],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    tool: str = "fs_download_url",
) -> Plan:
    """Walk `paths` (files and folders) into the list of files one zip will hold. Names in the zip
    start at the folder that holds them all, so a folder unpacks as itself. Links are not
    followed: a link in a folder could lead anywhere on the machine."""
    from . import guard

    include = [p.replace("\\", "/") for p in include or [] if p]
    exclude = [p.replace("\\", "/").rstrip("/") for p in exclude or [] if p]
    fulls = [os.path.abspath(os.path.expanduser(p)) for p in paths]
    for given, full in zip(paths, fulls, strict=True):
        if not os.path.exists(winenv.long_path(full)):
            raise BundleError(f"no such file or folder: {given}")
    try:
        top = os.path.commonpath([os.path.dirname(f) for f in fulls])  # abspath: no trailing slash
    except ValueError:  # on different drives
        top = ""
    single = len(fulls) == 1 and os.path.isdir(fulls[0])
    stem = os.path.basename(fulls[0]) if single else "files"
    plan = Plan(name=f"{stem or 'files'}.zip")
    check = guard.PathCheck("read", tool)
    seen: set[str] = set()

    def arcname(full: str) -> str:
        if top:
            return os.path.relpath(full, top).replace("\\", "/")
        drive, rest = os.path.splitdrive(full)
        return (drive.rstrip(":").strip("\\/") + "/" + rest.lstrip("\\/")).replace("\\", "/")

    def add(full: str, rel_in_walk: str, size: int) -> None:
        name = arcname(full)
        key = name.lower() if winenv.IS_WINDOWS else name  # a path given twice goes in once
        if key in seen:
            return
        if include and not _matches(rel_in_walk, include):
            return
        if check.refused(full):
            plan.left_out.append(name)
            return
        seen.add(key)
        plan.files.append((full, name, size))
        if len(plan.files) > MAX_FILES:
            raise BundleError(f"more than {MAX_FILES} files: narrow it with include=[...] or exclude=[...]")

    for full in fulls:
        long = winenv.long_path(full)
        if os.path.isfile(long):
            add(full, os.path.basename(full), os.path.getsize(long))
            continue
        stack = [(full, "")]
        while stack:
            folder, rel = stack.pop()
            try:
                entries = sorted(os.scandir(winenv.long_path(folder)), key=lambda e: e.name)
            except OSError as exc:
                plan.skipped.append(f"{arcname(folder)}/: {exc.strerror or exc}")
                continue
            for entry in entries:
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                child = os.path.join(folder, entry.name)
                if exclude and _matches(child_rel, exclude):
                    continue
                if _is_link(entry):
                    plan.skipped.append(f"{arcname(child)}: a link (not followed)")
                    continue
                try:
                    if entry.is_dir():
                        stack.append((child, child_rel))
                    elif entry.is_file():
                        add(child, child_rel, entry.stat().st_size)
                except OSError as exc:
                    plan.skipped.append(f"{arcname(child)}: {exc.strerror or exc}")
    if plan.size > MAX_BYTES:
        raise BundleError(
            f"{plan.size / 2**30:.1f} GiB is over the {MAX_BYTES // 2**30} GiB limit: narrow it with include/exclude"
        )
    return plan


class _Sink:
    """Where zipfile writes: no seek and no tell, so it writes each file's sizes after its data
    and the zip can go out while it is made."""

    def __init__(self) -> None:
        self._parts: list[bytes] = []

    def write(self, data) -> int:
        self._parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        data = b"".join(self._parts)
        self._parts.clear()
        return data


def stream_zip(plan: Plan) -> Iterator[bytes]:
    """The zip of `plan`, a piece at a time. A file that cannot be read any more (deleted, locked)
    is listed in winhand-errors.txt at the end of the zip instead of breaking the download."""
    sink = _Sink()
    problems: list[str] = []
    with zipfile.ZipFile(sink, "w", allowZip64=True) as zf:
        for full, name, size in plan.files:
            long = winenv.long_path(full)
            try:
                info = zipfile.ZipInfo.from_file(long, name, strict_timestamps=False)  # before 1980: 1980
                ext = os.path.splitext(name)[1].lower()
                info.compress_type = zipfile.ZIP_STORED if ext in STORED else zipfile.ZIP_DEFLATED
                with open(long, "rb") as src, zf.open(info, "w", force_zip64=size > 2**31) as dst:
                    while block := src.read(CHUNK):
                        dst.write(block)
                        if data := sink.take():
                            yield data
            except (OSError, ValueError, RuntimeError) as exc:
                problems.append(f"{name}: {exc}")
            if data := sink.take():
                yield data
        if problems:
            zf.writestr(
                ERRORS_NAME, "Files that could not be read while zipping:\n" + "\n".join(problems) + "\n"
            )
    yield sink.take()


# ------------------------------------------------------------------ unpacking


def safe_name(name: str) -> str | None:
    """The archive name as a path below the folder, or None when it could land elsewhere or is
    not a name Windows can hold on every machine."""
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return None
    for part in parts:
        if part == ".." or _BAD_CHARS.search(part) or _RESERVED.match(part) or part != part.rstrip(". "):
            return None
    return "/".join(parts)


@dataclass
class _Member:
    name: str  # checked relative name
    is_dir: bool
    size: int
    mtime: float | None
    source: object  # ZipInfo or TarInfo


def _members(archive) -> tuple[list[_Member], list[str]]:
    """The archive's files and folders, and the names that make it unsafe to unpack."""
    members, bad = [], []
    if isinstance(archive, zipfile.ZipFile):
        for info in archive.infolist():
            kind = (info.external_attr >> 16) & 0o170000
            name = safe_name(info.filename)
            if kind == 0o120000:
                bad.append(f"{info.filename} (a link)")
            elif name is None:
                bad.append(info.filename)
            else:
                when = None
                with contextlib.suppress(ValueError, OverflowError):
                    when = time.mktime((*info.date_time, 0, 0, -1))
                members.append(_Member(name, info.is_dir(), info.file_size, when, info))
        return members, bad
    for info in archive.getmembers():
        name = safe_name(info.name)
        if not (info.isreg() or info.isdir()):
            bad.append(f"{info.name} (a link or device)")
        elif name is None:
            bad.append(info.name)
        else:
            members.append(_Member(name, info.isdir(), info.size, float(info.mtime), info))
    return members, bad


def _open(path: str):
    if zipfile.is_zipfile(path):
        return zipfile.ZipFile(path)
    if tarfile.is_tarfile(path):
        return tarfile.open(path)
    raise BundleError("not a zip or tar archive (.zip, .tar, .tar.gz, .tgz, .tar.bz2, .tar.xz)")


def _real(path: str) -> str:
    """The path with every link or junction already on the way resolved, comparable to another."""
    return os.path.normcase(winenv.display_path(os.path.realpath(winenv.long_path(path))))


def _inside(target: str, real_folder: str) -> bool:
    real = _real(target)
    return real == real_folder or real.startswith(real_folder.rstrip("\\/") + os.sep)


def unpack(archive_path: str, folder: str, overwrite: bool = False, tool: str = "fs_upload_url") -> dict:
    """Unpack a zip or tar into `folder`. Every check runs before the first file is written;
    each file goes to a temporary name next to it first, then into place."""
    from . import guard

    folder = os.path.abspath(folder)
    long_folder = winenv.long_path(folder)
    if os.path.isfile(long_folder):
        raise BundleError(f"{folder} is a file; give the folder to unpack into")
    with _open(archive_path) as archive:
        members, bad = _members(archive)
        if bad:
            raise BundleError(
                f"unsafe names in the archive (nothing was unpacked): {', '.join(bad[:5])}"
                + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
            )
        files = [m for m in members if not m.is_dir]
        if len(files) > MAX_FILES:
            raise BundleError(f"{len(files)} files is over the {MAX_FILES}-file limit (nothing was unpacked)")
        total = sum(m.size for m in files)
        if total > MAX_BYTES:
            raise BundleError(
                f"unpacked it would take {total / 2**30:.1f} GiB, over the limit (nothing was unpacked)"
            )
        # NTFS ignores case: a.txt and A.txt are one file, and a name cannot be a file and a folder
        kinds: dict[str, bool] = {}
        clashes = []
        for m in members:
            key = m.name.lower()
            for i in range(1, key.count("/") + 1):
                parent = key.rsplit("/", i)[0]
                if kinds.setdefault(parent, True) is not True:
                    clashes.append(m.name)
            if key in kinds and (not m.is_dir or kinds[key] is not True):
                clashes.append(m.name)
            kinds[key] = True if m.is_dir else False
        if clashes:
            raise BundleError(f"names that clash on Windows (nothing was unpacked): {', '.join(clashes[:5])}")
        real_folder = _real(folder)
        check = guard.PathCheck("write", tool)
        exists, refused, outside, blocked = [], [], [], []
        folders = {m.name for m in members if m.is_dir}
        folders.update(m.name.rsplit("/", i)[0] for m in members for i in range(1, m.name.count("/") + 1))
        for name in sorted(folders):  # a file already where the archive has a folder
            if os.path.isfile(winenv.long_path(os.path.join(folder, *name.split("/")))):
                blocked.append(name)
        for m in members:  # folders too: an empty one under a link would be made outside
            target = os.path.join(folder, *m.name.split("/"))
            long = winenv.long_path(target)
            if not _inside(target, real_folder):
                outside.append(m.name)
            elif m.is_dir:
                continue
            elif os.path.isdir(long):
                blocked.append(m.name)
            elif os.path.exists(long) and not overwrite:
                exists.append(m.name)
            elif check.refused(target):
                refused.append(m.name)
        if blocked:
            raise BundleError(
                "a file and a folder would have the same name (nothing was unpacked): "
                + ", ".join(blocked[:5])
            )
        if outside:
            raise BundleError(
                f"these would be written outside {folder} through a link already there (nothing was unpacked): "
                + ", ".join(outside[:5])
            )
        if refused:
            raise BundleError(
                "this machine's rules protect these files from being written (nothing was unpacked): "
                + ", ".join(refused[:10])
            )
        if exists:
            raise BundleError(
                f"{len(exists)} file(s) already exist (nothing was unpacked; pass overwrite=true to replace "
                f"them): {', '.join(exists[:5])}"
            )
        os.makedirs(long_folder, exist_ok=True)
        for m in members:
            if m.is_dir:
                os.makedirs(winenv.long_path(os.path.join(folder, *m.name.split("/"))), exist_ok=True)
        listing, written = [], 0
        for m in files:
            target = winenv.long_path(os.path.join(folder, *m.name.split("/")))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            temp = os.path.join(
                os.path.dirname(target), f".{os.path.basename(target)}.unpack-{uuid.uuid4().hex[:8]}.tmp"
            )
            digest = hashlib.sha256()
            try:
                if isinstance(archive, zipfile.ZipFile):
                    src = archive.open(m.source)
                else:
                    src = archive.extractfile(m.source)
                with src, open(temp, "wb") as dst:
                    while block := src.read(CHUNK):
                        digest.update(block)
                        dst.write(block)
                fs.replace_with(temp, target)
            except (OSError, zipfile.BadZipFile, tarfile.TarError, EOFError) as exc:
                raise BundleError(
                    f"stopped at {m.name} ({exc}); {written} file(s) before it were unpacked into {folder}"
                ) from exc
            finally:
                with contextlib.suppress(OSError):
                    os.remove(temp)
            if m.mtime:
                with contextlib.suppress(OSError, ValueError, OverflowError):
                    os.utime(target, (m.mtime, m.mtime))
            written += 1
            if len(listing) < LISTED:
                listing.append({"path": m.name, "bytes": m.size, "sha256": digest.hexdigest()})
    out: dict = {"folder": folder, "files": written, "bytes": total, "listing": listing}
    if written > LISTED:
        out["more"] = f"{written - LISTED} more file(s) not listed"
    return out
