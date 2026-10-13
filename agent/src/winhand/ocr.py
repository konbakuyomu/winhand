"""Text on the screen: Windows' built-in OCR (Windows.Media.Ocr), for what has no controls to read
(canvases, games, remote desktops, custom-drawn programs).

The picture comes from desktop.capture (covered windows too). Windows OCR reads small interface
text better at twice the size, and its Chinese model misreads English, so each picture is read
at both sizes in both the user's language and English (one PowerShell start, about 0.1 s a pass);
reading keeps the preferred pass, finding takes the first pass with a match.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from . import approvals, desktop, winenv

SCRIPT = Path(__file__).parent / "scripts" / "ocr.ps1"
UPSCALE_UP_TO = 4000  # long side up to which a twice-as-large copy is read too
MAX_DIMENSION = 10_000  # OcrEngine.MaxImageDimension
MAX_LINES = 400
WAIT_POLL_S = 0.5
INSTALL_HINT = (
    "Windows has no OCR language installed. Add one in Settings > Time & language > Language & region "
    "(a language pack that includes optical character recognition), or as administrator: "
    "Add-WindowsCapability -Online -Name 'Language.OCR~~~en-US~0.0.1.0'"
)


class OcrError(desktop.DesktopError):
    pass


@dataclass
class Word:
    text: str
    rect: tuple[float, float, float, float]  # screen pixels: left, top, width, height


@dataclass
class Pass:
    language: str
    scale: float
    lines: list[list[Word]]


# ------------------------------------------------------------------- text

_CJK = re.compile(r"[\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]")
_WIDE_PUNCTUATION = re.compile(
    r"[\u3000-\u3004\u3008-\u303f\ufe30-\ufe4f\uff00-\uff0f\uff1a-\uff20\uff3b-\uff40\uff5b-\uff65]"
)  # not the ideographs 々〆〇 (U+3005-3007), not the full-width digits


def _cjk(char: str) -> bool:
    return bool(char) and bool(_CJK.match(char))


def line_text(words: list[Word]) -> str:
    """A line as a person reads it: Windows OCR splits Chinese and Japanese into one word per
    character, so a space between such characters is kept only where the gap looks like one."""
    out = ""
    previous = None
    for word in words:
        if previous is not None:
            if _WIDE_PUNCTUATION.match(out[-1:]) or _WIDE_PUNCTUATION.match(word.text[:1]):
                pass  # full-width punctuation carries its own spacing (its ink is narrow)
            elif _cjk(out[-1:]) or _cjk(word.text[:1]):
                gap = word.rect[0] - (previous.rect[0] + previous.rect[2])
                height = max(previous.rect[3], word.rect[3], 1)
                if gap >= 0.3 * height:
                    out += " "
            else:
                out += " "
        out += word.text
        previous = word
    return out


def normalize(text: str) -> str:
    """For matching: full-width to half-width (NFKC), case folded, no white space."""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _box(words: list[Word]) -> list[int]:
    left = min(w.rect[0] for w in words)
    top = min(w.rect[1] for w in words)
    right = max(w.rect[0] + w.rect[2] for w in words)
    bottom = max(w.rect[1] + w.rect[3] for w in words)
    return [round(left), round(top), round(right - left), round(bottom - top)]


def _center(rect: list[int]) -> list[int]:
    return [rect[0] + rect[2] // 2, rect[1] + rect[3] // 2]


def find_in(lines: list[list[Word]], text: str) -> list[dict]:
    """Every place the text appears, in reading order (top to bottom, left to right)."""
    wanted = normalize(text)
    if not wanted:
        raise OcrError("give the text to find")
    found = []
    for words in lines:
        owner: list[int] = []
        flat = ""
        for i, word in enumerate(words):
            part = normalize(word.text)
            flat += part
            owner += [i] * len(part)
        start = flat.find(wanted)
        while start != -1:
            first, last = owner[start], owner[start + len(wanted) - 1]
            rect = _box(words[first : last + 1])
            found.append(
                {
                    "text": line_text(words),
                    "match": line_text(words[first : last + 1]),
                    "rect": rect,
                    "center": _center(rect),
                }
            )
            start = flat.find(wanted, start + len(wanted))
    return reading_order(found)


def reading_order(items: list[dict]) -> list[dict]:
    """Top to bottom, and left to right within a row (centers less than half a height apart)."""
    rows: list[list[dict]] = []
    for item in sorted(items, key=lambda m: m["center"][1]):
        if rows and abs(item["center"][1] - rows[-1][0]["center"][1]) < max(4, item["rect"][3] / 2):
            rows[-1].append(item)
        else:
            rows.append([item])
    return [item for row in rows for item in sorted(row, key=lambda m: m["rect"][0])]


def similar(lines: list[list[Word]], text: str, limit: int = 3) -> list[dict]:
    """The lines that come closest to the text (how OCR may have misread it)."""
    wanted = normalize(text)
    scored = []
    for words in lines:
        flat = "".join(normalize(w.text) for w in words)
        if not flat:
            continue
        size = len(wanted)
        windows = [flat] if len(flat) <= size else [flat[i : i + size] for i in range(len(flat) - size + 1)]
        score = max(difflib.SequenceMatcher(None, wanted, part).ratio() for part in windows)
        if score >= 0.5:
            scored.append({"text": line_text(words), "score": round(score, 2), "rect": _box(words)})
    scored.sort(key=lambda s: -s["score"])
    return scored[:limit]


# ------------------------------------------------------------- recognizing


def _powershell() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    path = os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return path if os.path.isfile(path) else "powershell.exe"


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def parse(data: dict, scales: list[float], left: int, top: int) -> tuple[list[Pass], dict]:
    """The script's answer as passes with words in screen pixels; raises for missing languages."""
    available = [str(tag) for tag in _as_list(data.get("available"))]
    if data.get("error") == "no_language":
        raise OcrError(INSTALL_HINT)
    if data.get("error") == "language":
        raise OcrError(
            f"no OCR language {data.get('language')!r} on this machine; available: {', '.join(available) or 'none'}"
        )
    passes = []
    for item in _as_list(data.get("passes")):
        scale = scales[int(item.get("image", 0))]
        lines = []
        raw_lines = _as_list(item.get("lines"))
        if raw_lines and all(isinstance(line, dict) for line in raw_lines):
            raw_lines = [raw_lines]  # PowerShell 5.1 unwrapped a single line into its words
        for line in raw_lines:
            words = []
            for word in _as_list(line):
                if not isinstance(word, dict) or not word.get("t"):
                    continue
                x, y, w, h = (float(v) for v in word["r"])
                words.append(Word(str(word["t"]), (left + x / scale, top + y / scale, w / scale, h / scale)))
            if words:
                lines.append(words)
        passes.append(Pass(str(item.get("language")), scale, lines))
    return passes, {"available": available, "user": data.get("user")}


def recognize(image, left: int, top: int, language: str | None = None) -> tuple[list[Pass], dict]:
    """Read a full-resolution picture whose top-left corner sits at (left, top) on the screen."""
    from PIL import Image

    width, height = image.size
    shrink = min(1.0, MAX_DIMENSION / max(width, height, 1))
    copies = []
    if max(width, height) <= UPSCALE_UP_TO:
        copies.append((image.resize((width * 2, height * 2), Image.Resampling.LANCZOS), 2.0))
    base = image if shrink >= 1 else image.resize((round(width * shrink), round(height * shrink)))
    copies.append((base, shrink))
    with tempfile.TemporaryDirectory(prefix="winhand-ocr-") as folder:
        paths = []
        for i, (picture, _) in enumerate(copies):
            path = os.path.join(folder, f"{i}.png")
            picture.convert("RGB").save(path, "PNG", compress_level=1)
            paths.append(path)
        argv = [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        argv += ["-File", str(SCRIPT), "-Images", "|".join(paths)]
        if language:
            argv += ["-Languages", language]
        try:
            done = subprocess.run(
                argv, capture_output=True, timeout=120, env=winenv.build_env(), creationflags=0x08000000
            )
        except subprocess.TimeoutExpired:
            raise OcrError("Windows OCR did not finish within 120 s") from None
    out = done.stdout.decode("utf-8-sig", errors="replace").strip()
    if done.returncode != 0 or not out.startswith("{"):
        error = done.stderr.decode("utf-8", errors="replace").strip() or out
        raise OcrError("Windows OCR failed: " + (error.splitlines()[0][:400] if error else "no output"))
    return parse(json.loads(out), [scale for _, scale in copies], left, top)


def _preferred(passes: list[Pass]) -> Pass:
    first = passes[0].language
    return max((p for p in passes if p.language == first), key=lambda p: p.scale)


def _search_order(passes: list[Pass], text: str) -> list[Pass]:
    english_first = text.isascii()

    def rank(p: Pass) -> tuple:
        english = p.language.lower().startswith("en")
        return (0 if english == english_first else 1, -p.scale)

    return sorted(passes, key=rank)


# ------------------------------------------------------------------ tool


def _look(window, monitor, region, language):
    image, place = desktop.capture(window, monitor, region)
    passes, languages = recognize(image, place["left"], place["top"], language)
    area = [place["left"], place["top"], image.size[0], image.size[1]]
    facts = {"area": area, **place["target"]}
    if place.get("note"):
        facts["note"] = place["note"]
    return passes, languages, facts


def read(window=None, monitor=None, region=None, language: str | None = None) -> dict:
    passes, _, facts = _look(window, monitor, region, language)
    chosen = _preferred(passes)
    lines = [{"text": line_text(words), "rect": _box(words)} for words in chosen.lines]
    out = {"count": len(lines), "lines": lines[:MAX_LINES], "language": chosen.language, **facts}
    if len(lines) > MAX_LINES:
        out["note"] = f"showing the first {MAX_LINES} lines; look at a window or region for the rest"
    return out


def find(text: str, window=None, monitor=None, region=None, language: str | None = None) -> dict:
    passes, _, facts = _look(window, monitor, region, language)
    for chosen in _search_order(passes, text):
        matches = find_in(chosen.lines, text)
        if matches:
            return {"count": len(matches), "matches": matches, "language": chosen.language, **facts}
    best = _preferred(passes)
    near = similar(best.lines, text)
    out = {"count": 0, "matches": [], "language": best.language, **facts}
    if near:
        out["similar"] = near
        out["note"] = "no exact match; `similar` shows how OCR read the closest lines (search for that text)"
    return out


_MOUSE = {"click": "click", "double_click": "double_click", "right_click": "right_click", "move": "move"}


def act(
    action: str, text: str, window=None, monitor=None, region=None, index: int = 0, language=None
) -> dict:
    """Find the text, then move the mouse there or click it like a person."""
    if action not in _MOUSE:
        raise OcrError("action is read, find, click, double_click, right_click, move or wait")
    target = desktop.find_window(window) if window is not None else None
    if target is not None and target.get("admin"):
        raise desktop.admin_error(target)
    found = find(text, target["hwnd"] if target else None, monitor, region, language)
    if not found["matches"]:
        return {**found, "error": f"{text!r} is not on the screen there; nothing was clicked"}
    if index >= len(found["matches"]):
        raise OcrError(f"only {len(found['matches'])} matches; index {index} is out of range")
    x, y = found["matches"][index]["center"]
    if target is not None:
        if action != "move":  # clicking operates the program: its approval first, then to the front
            desktop.require_operable(target["hwnd"], f"按文字点击「{text}」")
            try:
                desktop.window_action(target["hwnd"], "focus")
            except (desktop.DesktopUnavailable, approvals.NotOperable):
                raise
            except desktop.DesktopError:
                pass  # it may be in front anyway; checked below
        if desktop.root_window_at(x, y) != target["hwnd"]:
            raise OcrError(
                "another window covers that spot of the window; bring it to the front (window action=focus) "
                "or close what covers it, then try again"
            )
    done = desktop.input_action(_MOUSE[action], x, y, coordinates="screen")
    return {**found, "done": f"{action} at ({x}, {y})", "foreground": done.get("foreground")}


def wait(
    text: str, window=None, monitor=None, region=None, state="exists", timeout_s: float = 20, language=None
) -> dict:
    if state not in ("exists", "gone"):
        raise OcrError("state is exists or gone")
    began = time.monotonic()
    deadline = began + max(0.0, min(timeout_s, 40))
    while True:
        found = find(text, window, monitor, region, language)
        here = bool(found["matches"])
        if here == (state == "exists"):
            out = {"matched": True, "state": state, "waited_s": round(time.monotonic() - began, 1)}
            if here:
                out["match"] = found["matches"][0]
            return out
        if time.monotonic() >= deadline:
            out = {
                "matched": False,
                "state": state,
                "waited_s": round(time.monotonic() - began, 1),
                "note": "not reached yet; call again to keep waiting, or look with screenshot",
            }
            if found.get("similar"):
                out["similar"] = found["similar"]
            return out
        time.sleep(WAIT_POLL_S)
