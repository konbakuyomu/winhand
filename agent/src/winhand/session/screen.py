"""Virtual terminal screen so full-screen programs (menuconfig, htop, vim,
installers) can be *seen*, not just read as a byte stream."""

from __future__ import annotations

import threading
import time
import unicodedata

import pyte
from pyte.screens import wcwidth  # the width measure pyte used to lay the cells out


def _render(row, columns: int) -> str:
    """One row as text, the way a terminal shows it.

    pyte keeps a wide character (Chinese, emoji) in one cell and a stub in the next: empty, or
    holding the combining marks that followed the character (pyte puts them on the cell before
    the cursor). A program that redraws over half of a wide character (Codex's TUI does, behind
    ConPTY) leaves the other half alone; a terminal blanks it, while pyte keeps it and its own
    `display` then fails with IndexError on the lone stub."""
    out = []
    x = 0
    while x < columns:
        data = row[x].data
        if not data or wcwidth(data[0]) <= 0:  # a stub whose wide character was overwritten
            out.append(" ")
        elif x + 1 < columns and wcwidth(data[0]) == 2:
            stub = row[x + 1].data
            if stub and wcwidth(stub[0]) > 0:  # its right half was overwritten
                out.append(" ")
            else:
                out.append(unicodedata.normalize("NFC", data + stub))
                x += 1  # past its stub
        else:
            out.append(data)
        x += 1
    return "".join(out)


class VirtualScreen:
    def __init__(self, cols: int = 120, rows: int = 40) -> None:
        self._screen = pyte.Screen(cols, rows)
        self._screen.set_mode(pyte.modes.LNM)  # treat LF as CR+LF like real terminals
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.Lock()
        self._last_change = time.monotonic()
        self._rows: dict[int, str] = {}
        self._pending: list[str] = []
        self._pending_chars = 0
        self._last_process = time.monotonic()

    # Output arrives in many small pieces (ConPTY: thousands per second under load). Parsing each
    # piece on its own re-scans every scrolled row each time, so pieces are batched here and
    # parsed at most every BATCH_S, and always before anyone looks at the screen.
    BATCH_S = 0.05
    BATCH_CHARS = 64 * 1024

    def feed(self, text: str) -> None:
        with self._lock:
            self._pending.append(text)
            self._pending_chars += len(text)
            now = time.monotonic()
            if self._pending_chars >= self.BATCH_CHARS or now - self._last_process >= self.BATCH_S:
                self._process()

    @property
    def last_change(self) -> float:
        with self._lock:
            self._process()
            return self._last_change

    def _process(self) -> None:
        """Parse pending output. Caller holds the lock."""
        self._last_process = time.monotonic()
        if not self._pending:
            return
        text = "".join(self._pending)
        self._pending.clear()
        self._pending_chars = 0
        try:
            self._stream.feed(text)
        except Exception:  # pyte chokes on rare malformed sequences; keep going
            self._stream = pyte.Stream(self._screen)
        # Only rows pyte marked dirty can have changed; rendering the whole display each time
        # cost more than the parsing itself.
        changed = False
        buffer, columns = self._screen.buffer, self._screen.columns
        for y in self._screen.dirty:
            row = buffer[y]
            text_now = "".join(row[x].data for x in range(columns))
            if self._rows.get(y) != text_now:
                self._rows[y] = text_now
                changed = True
        self._screen.dirty.clear()
        if changed:
            self._last_change = time.monotonic()

    def resize(self, cols: int, rows: int) -> None:
        with self._lock:
            self._process()
            self._screen.resize(rows, cols)
            self._rows.clear()
            self._last_change = time.monotonic()

    def snapshot(self) -> dict:
        """Screen contents plus what a person would notice: cursor and highlights."""
        with self._lock:
            self._process()
            screen = self._screen
            lines = [_render(screen.buffer[y], screen.columns).rstrip() for y in range(screen.lines)]
            highlighted = []
            for y in range(screen.lines):
                row = screen.buffer[y]
                cells = [row[x] for x in range(screen.columns)]
                marked = [c for c in cells if c.reverse or (c.bg not in ("default", None))]
                text = lines[y].strip()
                if text and len(marked) >= max(3, len(text) // 2):
                    highlighted.append({"row": y, "text": text})
            last = len(lines)
            while last > 0 and not lines[last - 1]:
                last -= 1
            return {
                "cols": screen.columns,
                "rows": screen.lines,
                "cursor": {
                    "row": screen.cursor.y,
                    "col": screen.cursor.x,
                    "hidden": bool(screen.cursor.hidden),
                },
                "lines": lines[:last],
                "highlighted": highlighted,
                "cursor_line": lines[screen.cursor.y] if screen.cursor.y < len(lines) else "",
                "stable_ms": int((time.monotonic() - self._last_change) * 1000),
            }

    def cursor_line(self) -> str:
        with self._lock:
            self._process()
            screen = self._screen
            y = screen.cursor.y
            return _render(screen.buffer[y], screen.columns).rstrip() if y < screen.lines else ""
