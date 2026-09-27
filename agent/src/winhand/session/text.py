"""Turn raw terminal output into text a model can read."""

from __future__ import annotations

import re

# CSI, OSC (BEL or ST terminated), DCS/SOS/PM/APC strings, charset selection and
# the remaining two-byte escapes.
_ANSI = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]            # CSI ... final byte
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\) # OSC ... BEL | ST
    | \x1b[PX^_][^\x1b]*\x1b\\         # DCS / SOS / PM / APC ... ST
    | \x1b[()*+][0-9A-Za-z]            # charset designation
    | \x1b[@-Z\\-_]                    # other 2-byte escapes
    | \x9b[0-?]*[ -/]*[@-~]            # 8-bit CSI
    """,
    re.VERBOSE,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# stops at CJK text and full-width punctuation: "地址 https://x.com/v1）然后" -> https://x.com/v1
_URL = re.compile(r"https?://[^\s<>\"'`\x1b\])}　-〿一-鿿＀-￯]+")

# Do not release half an escape sequence: it could separate two pieces of an echoed secret.
_PARTIAL_ANSI = re.compile(
    r"(?:\x1b(?:\[[0-?]*[ -/]*|[\]PX^_][^\x07\x1b]*(?:\x1b)?|[()*+])?|\x9b[0-?]*[ -/]*)\Z"
)


class SecretFilter:
    """Mask known literal echoes before they reach output, terminal state or disk.

    Prefixes are held across reads; ANSI decoration between characters is ignored when matching.
    This is not protection against a program deliberately encoding or transforming a secret.
    """

    def __init__(self) -> None:
        self._secrets: list[str] = []
        self._pattern: re.Pattern[str] | None = None
        self._pending = ""
        self._discard_ansi: str | None = None
        self._escape_tail = ""

    def add(self, secret: str) -> None:
        if secret and secret not in self._secrets:
            self._secrets.append(secret)
            self._pattern = re.compile(
                "|".join(re.escape(s) for s in sorted(self._secrets, key=len, reverse=True))
            )

    def feed(self, text: str) -> str:
        if self._pattern is None:
            return text
        if self._discard_ansi:
            text = self._escape_tail + text
            closing = re.search(self._discard_ansi, text)
            if closing is None:
                self._escape_tail = "\x1b" if text.endswith("\x1b") else ""
                return ""
            text = text[closing.end() :]
            self._discard_ansi, self._escape_tail = None, ""
        text = self._pending + text
        partial = _PARTIAL_ANSI.search(text)
        end = partial.start() if partial else len(text)
        complete = text[:end]
        # Map visible characters back to the raw stream so ordinary terminal escapes survive.
        positions: list[int] = []
        at = 0
        for escape in _ANSI.finditer(complete):
            positions.extend(range(at, escape.start()))
            at = escape.end()
        positions.extend(range(at, len(complete)))
        visible = "".join(complete[i] for i in positions)
        keep = 0
        for secret in self._secrets:
            for size in range(min(len(secret) - 1, len(visible)), keep, -1):
                if visible.endswith(secret[:size]):
                    keep = size
                    break
        cut = len(visible) - keep
        matches = list(self._pattern.finditer(visible))
        for match in matches:
            if match.start() < cut < match.end():
                cut = match.start()
        raw_cut = positions[cut] if cut < len(positions) else end
        self._pending = text[raw_cut:]
        if len(self._pending) > 4096:
            # ponytail: discard oversized ANSI metadata; raise this cap if a real terminal needs it.
            trailing = _PARTIAL_ANSI.search(self._pending)
            if trailing and len(trailing.group()) > 4096:
                self._discard_ansi = (
                    r"[@-~]" if trailing.group().startswith(("\x1b[", "\x9b")) else r"\x07|\x1b\\"
                )
                self._escape_tail = "\x1b" if self._pending.endswith("\x1b") else ""
                self._pending = self._pending[: trailing.start()]
            self._pending = strip_ansi(self._pending)
        result = []
        at = 0
        for match in matches:
            if match.end() > cut:
                break
            start, stop = positions[match.start()], positions[match.end() - 1] + 1
            result.extend((text[at:start], "*" * len(match.group())))
            at = stop
        result.append(text[at:raw_cut])
        # Also mask secrets inside OSC titles and other non-visible escape payloads.
        return self._pattern.sub(lambda m: "*" * len(m.group()), "".join(result))

    def finish(self) -> str:
        # An unfinished prefix must not be disclosed merely because the process exited.
        tail = "[redacted]" if self._pending else ""
        self._pending = ""
        self._discard_ansi, self._escape_tail = None, ""
        return tail


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def clean(text: str) -> str:
    """Remove escape sequences and resolve carriage-return overwrites.

    Progress bars redraw a line with `\\r`; only the final state of each line is
    kept, which is what a person watching the terminal would see.
    """
    text = strip_ansi(text).replace("\r\n", "\n")
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            parts = [p for p in line.split("\r") if p]
            line = parts[-1] if parts else ""
        lines.append(_CONTROL.sub("", line))
    return "\n".join(lines)


def extract_urls(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for match in _URL.finditer(strip_ansi(text)):
        seen.setdefault(match.group(0).rstrip(".,;:"), None)
    return list(seen)


def clip(text: str, limit: int, *, keep: str = "tail") -> tuple[str, int]:
    """Clip text to `limit` characters. Returns (text, omitted_count).

    keep="tail" keeps the most recent output, "both" keeps head and tail.
    """
    if limit <= 0 or len(text) <= limit:
        return text, 0
    omitted = len(text) - limit
    if keep == "both":
        head = limit // 3
        tail = limit - head
        marker = f"\n…[{omitted} chars omitted]…\n"
        return text[:head] + marker + text[-tail:], omitted
    return text[-limit:], omitted
