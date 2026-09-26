"""Output storage with monotonic cursors.

Every character a session produces gets an absolute offset. Readers keep a
cursor and ask for "everything since N", so no output is ever read twice or
silently skipped; when old output falls out of the in-memory window the reader
is told how much was dropped (the full stream stays in the on-disk log).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Slice:
    text: str
    start: int  # absolute offset of text[0]
    end: int  # cursor to pass next time
    dropped: int  # chars lost before `start` because the window moved on


class OutputBuffer:
    def __init__(self, capacity: int = 4_000_000, log_path: Path | None = None) -> None:
        self._capacity = capacity
        self._data = ""
        self._base = 0  # absolute offset of self._data[0]
        self._lock = threading.Lock()
        self._log = None
        self.log_path = log_path
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log = open(log_path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115

    @property
    def end(self) -> int:
        with self._lock:
            return self._base + len(self._data)

    @property
    def start(self) -> int:
        with self._lock:
            return self._base

    def append(self, text: str) -> int:
        if not text:
            return self.end
        with self._lock:
            self._data += text
            overflow = len(self._data) - self._capacity
            if overflow > 0:
                # Trim in bigger steps so appends stay amortised O(1).
                cut = max(overflow, self._capacity // 8)
                self._data = self._data[cut:]
                self._base += cut
            if self._log is not None:
                self._log.write(text)
                self._log.flush()
            return self._base + len(self._data)

    def read(self, since: int, limit: int | None = None) -> Slice:
        with self._lock:
            end = self._base + len(self._data)
            since = max(0, min(since, end))
            dropped = max(0, self._base - since)
            start = max(since, self._base)
            text = self._data[start - self._base :]
            if limit is not None and len(text) > limit:
                text = text[:limit]
            return Slice(text=text, start=start, end=start + len(text), dropped=dropped)

    def tail(self, chars: int) -> str:
        with self._lock:
            return self._data[-chars:] if chars > 0 else ""

    def close(self) -> None:
        with self._lock:
            if self._log is not None:
                self._log.close()
                self._log = None
