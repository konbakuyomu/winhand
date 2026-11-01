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
import threading
import time

STALL_AFTER_S = 300.0  # no new output for this long, and no CPU: probably stuck
CHECK_EVERY_S = 30.0  # how often a long wait looks at the processes
CPU_SAMPLE_S = 0.3
PROMPT_QUIET_S = 5.0  # a prompt counts once the command has been quiet this long
# programs whose windows ask the person for something: a sign-in, a signature, a passphrase
PROMPT_PROCESSES = frozenset(
    {
        "credentialuibroker.exe",
        "1password.exe",
        "keepassxc.exe",
        "bitwarden.exe",
        "git-credential-manager.exe",
        "git-credential-manager-ui.exe",
        "gcmcore.exe",
        "ssh-askpass.exe",
        "openssh-askpass.exe",
    }
)
PROMPTS_NOTE = (
    "a window that asks the person for something (a sign-in, a signature, a confirmation) appeared while "
    "this ran and it has gone quiet: it is probably waiting for that window. Look at it (screenshot) and "
    "tell the person; it is flashing in their taskbar"
)
SECURE_NOTE = (
    "Windows is showing a secure screen (most likely a UAC prompt): the command may be waiting for it, "
    "and only the person at the machine can answer it"
)
_baselines: dict[str, set[int]] = {}  # run / job id -> the windows there when it started
_flashed: dict[str, set[int]] = {}
_lock = threading.Lock()


def remember_windows(key: str) -> None:
    """Note the windows open as a command starts: a prompt appearing later stands out. A cheap
    list of handles only; nothing on other systems."""
    if sys.platform != "win32":
        return
    from . import desktop

    with contextlib.suppress(Exception):
        handles = desktop.top_level_handles()
        with _lock:
            _baselines[key] = handles
            while len(_baselines) > 64:
                del _baselines[next(iter(_baselines))]


def _prompt_like(w: dict) -> bool:
    process = (w.get("process") or "").lower()
    return (
        w.get("class") == "#32770"
        or bool(w.get("owned"))
        or process in PROMPT_PROCESSES
        or process.startswith(("pinentry", "git-credential"))
    )


def prompts_since(key: str, skip_pids: set[int] | frozenset = frozenset()) -> list[dict]:
    """Windows that ask for something and appeared since `key` started, other than its own
    processes' (those are in `windows`): what a command blocked by a signing or sign-in prompt
    waits for (a `git commit` sat behind 1Password's signing prompt in the 2026-11-01 review). Each
    one found is flashed in the taskbar once."""
    with _lock:
        before = _baselines.get(key)
    if before is None or sys.platform != "win32":
        return []
    from . import desktop

    found: list[dict] = []
    with contextlib.suppress(Exception):
        for w in desktop.windows():
            if w["hwnd"] in before or w.get("pid") in skip_pids or not _prompt_like(w):
                continue
            found.append({"hwnd": w["hwnd"], "title": w["title"], "process": w["process"]})
    with _lock:
        flashed = _flashed.setdefault(key, set())
        fresh = [p for p in found if p["hwnd"] not in flashed]
        flashed.update(p["hwnd"] for p in fresh)
        while len(_flashed) > 64:
            del _flashed[next(iter(_flashed))]
    for p in fresh:
        with contextlib.suppress(Exception):
            desktop.flash(p["hwnd"])
    return found[:5]


def secure_screen() -> bool:
    if sys.platform != "win32":
        return False
    from . import desktop

    with contextlib.suppress(Exception):
        return desktop.desktop_state() == "secure"
    return False


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


def snapshot(pid: int | None, created: float | None, quiet_s: float | None, key: str | None = None) -> dict:
    """processes, cpu_s, cpu_busy (cores busy just now), memory_mb, quiet_s, stalled, windows; with
    `key` (its run or job id) also prompts of other programs and a secure screen, once it is quiet."""
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
    pids = {p.pid for p in processes}
    shown = windows_of(pids)
    if shown:
        out["windows"] = shown
    if key is not None and (quiet_s is None or quiet_s >= PROMPT_QUIET_S):
        prompts = prompts_since(key, pids)
        if prompts:
            out["prompts"] = prompts
            out["prompts_note"] = PROMPTS_NOTE
        if secure_screen():
            out["secure_screen"] = True
            out["secure_screen_note"] = SECURE_NOTE
    return out
