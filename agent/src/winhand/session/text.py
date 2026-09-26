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
_URL = re.compile(r"https?://[^\s<>\"'`\x1b\])}]+")


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
