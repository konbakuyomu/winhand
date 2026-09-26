"""Named keys to the byte sequences an xterm-compatible terminal sends."""

from __future__ import annotations

import re

_KEYS: dict[str, str] = {
    "enter": "\r",
    "return": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "backtab": "\x1b[Z",
    "esc": "\x1b",
    "escape": "\x1b",
    "backspace": "\x7f",
    "space": " ",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    "del": "\x1b[3~",
    "pageup": "\x1b[5~",
    "pgup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "pgdn": "\x1b[6~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f11": "\x1b[23~",
    "f12": "\x1b[24~",
}

_CTRL = re.compile(r"^(?:ctrl|control|c)[+-](.)$|^\^(.)$")
_ALT = re.compile(r"^(?:alt|meta|m)[+-](.+)$")
_CTRL_SPECIAL = {"@": "\x00", "[": "\x1b", "\\": "\x1c", "]": "\x1d", "^": "\x1e", "_": "\x1f", "?": "\x7f"}


def encode_key(name: str) -> str:
    """Encode one key name, e.g. "Enter", "Down", "F5", "Ctrl+C", "^D", "Alt+x".

    Raises ValueError for unknown names so typos are reported instead of typed.
    """
    raw = name.strip()
    key = raw.lower().replace(" ", "")
    if key in _KEYS:
        return _KEYS[key]
    ctrl = _CTRL.match(key)
    if ctrl:
        ch = (ctrl.group(1) or ctrl.group(2)).lower()
        if "a" <= ch <= "z":
            return chr(ord(ch) - ord("a") + 1)
        if ch in _CTRL_SPECIAL:
            return _CTRL_SPECIAL[ch]
    alt = _ALT.match(key)
    if alt:
        inner = alt.group(1)
        return "\x1b" + (encode_key(inner) if len(inner) > 1 else raw[-1])
    if len(raw) == 1:
        return raw
    raise ValueError(
        f"unknown key {name!r}; use names like Enter, Tab, Esc, Up, Down, Left, Right, "
        "PageUp, PageDown, Home, End, F1-F12, Backspace, Delete, Space, Ctrl+C, Alt+X"
    )


def encode_keys(names: list[str]) -> list[str]:
    return [encode_key(n) for n in names]
