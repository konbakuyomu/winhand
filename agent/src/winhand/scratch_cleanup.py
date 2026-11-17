"""Once a week, ask the owner whether to move old folders in winhand's scratch directory to the
Recycle Bin. The owner's rules forbid batch deletion; this is the exception they chose on
2026-11-17: only winhand's own scratch directory, only after they click Yes, only into the
Recycle Bin. Folders with a .keep file, junctions and symbolic links are never touched."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import config, fs, secret_prompt

OLD_AFTER_S = 14 * 86400  # neither the folder nor anything directly in it changed for this long
ASK_EVERY_S = 7 * 86400
SIZE_COUNT_S = 3.0  # sizes are counted for this long in all, then shown as partial


def candidates(now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    deadline = time.monotonic() + SIZE_COUNT_S
    out: list[dict] = []
    for folder in sorted(config.scratch_dir().resolve().iterdir()):  # by name: the same order each time
        if folder.is_symlink() or folder.is_junction() or not folder.is_dir() or (folder / ".keep").is_file():
            continue
        last_modified = folder.stat().st_mtime
        for child in folder.iterdir():
            last_modified = max(last_modified, child.lstat().st_mtime)
        if last_modified >= now - OLD_AFTER_S:
            continue
        size, capped = 0, False
        pending = [folder]
        while pending and not capped:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        capped = True
                        break
                    if entry.is_symlink() or entry.is_junction():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
                    else:
                        size += entry.stat(follow_symlinks=False).st_size
        item = {
            "name": folder.name,
            "path": str(folder),
            "last_modified": last_modified,
            "size_mb": size / (1024 * 1024),
        }
        if capped:
            item["size_capped"] = True
        out.append(item)
    return sorted(out, key=lambda item: item["last_modified"])


def ask_and_clean(force: bool = False) -> dict:
    """Ask the owner (Yes/No, 120 s) about the scratch folders unchanged for 14 days; on Yes, move
    each to the Recycle Bin. Without `force` at most once a week. Never while nobody can see a
    dialog (a locked or disconnected screen): the question then waits until the owner is back."""
    now = time.time()
    state_file = config.home() / "scratch-cleanup.json"
    state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {}
    if not force and now - state.get("last_asked", 0) < ASK_EVERY_S:
        return {"skipped": "asked recently"}
    if (screen := secret_prompt._screen_state()) != "ok":
        return {"skipped": f"nobody can see a dialog now: {secret_prompt.SCREENS.get(screen, screen)}"}
    items = candidates(now)
    if not items:
        return {"candidates": 0}

    root = config.scratch_dir().resolve()
    lines = [f"{root} 里这些文件夹超过 14 天没动过（最多列 20 个）："]
    for item in items[:20]:
        date = datetime.fromtimestamp(item["last_modified"]).strftime("%Y-%m-%d")
        size = f"{item['size_mb']:.1f} MB" + ("（没数完）" if item.get("size_capped") else "")
        lines.append(f"{item['name']} | {date} | {size}")
    lines.append(f"合计 {len(items)} 个文件夹，{sum(item['size_mb'] for item in items):.1f} MB。")
    lines.append("会移到回收站，可以还原；带 .keep 的和联接点不动。")
    try:
        approved = secret_prompt.ask_confirm("清理 WinHand 暂存目录", "\n".join(lines), timeout_s=120)
    except secret_prompt.PromptUnavailable as exc:
        if not str(exc).startswith("nobody answered the dialog within "):
            raise
        approved = False
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({"last_asked": time.time()}) + "\n", encoding="utf-8")
    if not approved:
        return {"approved": False}

    moved, failed = [], []
    for item in items:
        folder = Path(item["path"])
        try:  # checked again: the dialog was open for up to two minutes
            if folder.is_symlink() or folder.is_junction() or folder.resolve().parent != root:
                raise fs.FsError(f"not a direct scratch folder: {folder}")
            if (folder / ".keep").is_file():
                raise fs.FsError(f"folder has .keep: {folder}")
            fs._recycle(str(folder))
        except (OSError, fs.FsError) as exc:
            failed.append({"path": str(folder), "error": str(exc)})
        else:
            moved.append(str(folder))
    return {"approved": True, "moved": moved, "failed": failed}
