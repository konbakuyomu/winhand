"""What a running job or command is doing: its processes, CPU, memory, how long it has been quiet,
and the windows it shows.

From the 2026-11-01 review: a router stuck behind a warning dialog held six runs for 8 to 95 s and a
smoke test for its whole 600 s until the owner clicked OK; a hung job was polled 29 more times
before someone sampled its CPU by hand. job_status now reports this activity, and a long wait
ends early when the job opens a new window or looks stuck (quiet for STALL_AFTER_S with no CPU).
"""

from __future__ import annotations

import contextlib
import sys
import time

STALL_AFTER_S = 300.0  # no new output for this long, and no CPU: probably stuck
CHECK_EVERY_S = 30.0  # how often a long wait looks at the processes
CPU_SAMPLE_S = 0.3


def tree(pid: int | None, created: float | None = None) -> list:
    """The process and all its descendants (empty when it is gone or the pid was reused)."""
    import psutil

    if not pid:
        return []
    try:
        root = psutil.Process(pid)
        if created is not None and abs(root.create_time() - created) >= 2:
            return []
        return [root, *root.children(recursive=True)]
    except psutil.Error:
        return []


def _cpu(processes: list) -> float:
    total = 0.0
    for p in processes:
        with contextlib.suppress(Exception):
            times = p.cpu_times()
            total += times.user + times.system
    return total


def windows_of(pids: set[int]) -> list[dict]:
    """Top-level windows these processes show (Windows only); dialogs are marked."""
    if sys.platform != "win32" or not pids:
        return []
    from . import desktop

    with contextlib.suppress(Exception):
        return [
            {
                "hwnd": w["hwnd"],
                "title": w["title"],
                "process": w["process"],
                **({"dialog": True} if w.get("class") == "#32770" or w.get("owned") else {}),
            }
            for w in desktop.windows()
            if w.get("pid") in pids
        ]
    return []


def snapshot(pid: int | None, created: float | None, quiet_s: float | None) -> dict:
    """processes, cpu_s, cpu_busy (cores busy just now), memory_mb, quiet_s, stalled, windows."""
    processes = tree(pid, created)
    if not processes:
        return {}
    first = _cpu(processes)
    time.sleep(CPU_SAMPLE_S)
    second = _cpu(processes)
    rss = 0
    for p in processes:
        with contextlib.suppress(Exception):
            rss += p.memory_info().rss
    busy = max(0.0, (second - first) / CPU_SAMPLE_S)
    out: dict = {
        "processes": len(processes),
        "cpu_s": round(second, 1),
        "cpu_busy": round(busy, 2),
        "memory_mb": round(rss / 2**20),
    }
    if quiet_s is not None:
        out["quiet_s"] = int(quiet_s)
        if quiet_s >= STALL_AFTER_S and busy < 0.02:
            out["stalled"] = True
            out["stalled_note"] = (
                f"no output for {int(quiet_s)} s and almost no CPU: it may be stuck (a dialog, a "
                "prompt, a lock). Look at windows / take a screenshot before waiting longer"
            )
    shown = windows_of({p.pid for p in processes})
    if shown:
        out["windows"] = shown
    return out
