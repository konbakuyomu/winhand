"""Keep the PC awake while winhand runs something for a client.

The owner's PC sleeps after 15 idle minutes on mains power. In the 2026-11-01 review six sessions
lost work to that (a router run stopped for 72 minutes in the middle), and AIs wrote seven watchdog
scripts of their own that called SetThreadExecutionState. Now a `run` command holds the request
while its program runs, and background jobs hold it in their own wrapper script (jobs.KEEP_AWAKE),
since they outlive winhand. Only sleep is held off; the display may still turn off. When the work
ends the request is released and the usual idle timeout counts again from then.

`[power] keep_awake = false` in config.toml turns it off.
"""

from __future__ import annotations

import contextlib
import sys
import threading

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def enabled() -> bool:
    """[power] keep_awake in config.toml (read each time, so a change applies at once); default on."""
    from . import config

    with contextlib.suppress(Exception):
        value = (config.load().raw.get("power") or {}).get("keep_awake", True)
        return str(value).strip().lower() not in ("false", "0", "no", "off")
    return True


def supported() -> bool:
    return sys.platform == "win32"


def _set_state(flags: int) -> None:
    import ctypes

    ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


class Keeper:
    """SetThreadExecutionState belongs to the thread that calls it, so one thread of its own sets
    the request while anything holds it and clears it when the last holder lets go."""

    def __init__(self) -> None:
        self._holders: set[str] = set()
        self._cond = threading.Condition()
        self._thread: threading.Thread | None = None
        self.awake = False  # the request is set right now

    def hold(self, key: str) -> bool:
        if not supported() or not enabled():
            return False
        with self._cond:
            self._holders.add(key)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="winhand-keep-awake", daemon=True)
                self._thread.start()
            self._cond.notify_all()
        return True

    def release(self, key: str) -> None:
        with self._cond:
            if key in self._holders:
                self._holders.discard(key)
                self._cond.notify_all()

    def holders(self) -> int:
        with self._cond:
            return len(self._holders)

    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: bool(self._holders) != self.awake)
                want = bool(self._holders)
            with contextlib.suppress(Exception):
                _set_state(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
            with self._cond:
                self.awake = want
                self._cond.notify_all()


keeper = Keeper()
