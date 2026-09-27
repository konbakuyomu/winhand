"""Infer what an interactive program is doing right now.

A model driving a debugger or installer mostly needs one answer: "can I send
the next thing, must a human step in, or should I keep waiting?" This module
turns output timing, the last visible line and a set of patterns into one of:

  running         output arrived within the last `quiet_ms`
  awaiting_input  a prompt is showing (prompt pattern, y/n question, pager,
                  or a prompt-shaped last line once output has gone quiet)
  needs_user      a password / one-time code / browser sign-in is requested;
                  the model must not answer this itself
  idle            quiet, no prompt recognised (e.g. a target running to a
                  breakpoint, a server waiting for a client)
  blocked         idle for longer than `blocked_after_s`
  exited          the process or connection has ended
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

DEFAULT_PROMPTS = [
    r"^PS .*> ?$",  # PowerShell
    r"^[A-Za-z]:\\[^>]*> ?$",  # cmd.exe
    r"^[\w.-]+@[\w.-]+[^\n]*[$#%] ?$",  # user@host:~$
    r"^[$#%] ?$",
    r"^>>> ?$",  # python
    r"^\.\.\. ?$",
    r"^In \[\d+\]: ?$",  # ipython
    r"^\((?:gdb|lldb|Pdb|pdb|qemu)\) ?$",
    r"^\(gdb-multiarch\) ?$",
    r"^msh ?[^\n>]*> ?$",  # RT-Thread
    r"^J-Link> ?$",
    r"^pyocd> ?$",
    r"^(?:mysql|sqlite|postgres=#|redis[^>]*)> ?$",
    r"^irb[^>]*> ?$",
    r"^> ?$",
    r"^[A-Za-z0-9_./-]{1,40}> ?$",  # generic "name> " consoles (uart shells, RTOS)
]

SECRET_PATTERNS = [
    r"(?i)pass(?:word|phrase)[^\n]{0,60}[:：]\s*$",
    r"(?i)\b(?:otp|one[- ]time (?:password|code)|verification code|2fa|two[- ]factor|authenticator|mfa)\b[^\n]{0,60}[:：]\s*$",
    r"(?i)\benter (?:the )?(?:pin|token|code)\b[^\n]{0,40}[:：]?\s*$",
    r"(?i)(?:密码|口令|验证码)[^\n]{0,20}[:：]\s*$",
    # credentials for services: "xAI API key 必填:", "Enter your access token:", "Client secret:"
    r"(?i)\b(?:api[ _-]?key|access[ _-]?(?:key|token)|secret(?:[ _-]?key)?|auth(?:entication)?[ _-]?token|"
    r"bearer token|private[ _-]?key|client[ _-]?secret|app[ _-]?secret|token)\b[^\n]{0,40}[:：]\s*$",
    r"(?:密钥|秘钥|私钥|令牌|访问凭证)[^\n]{0,20}[:：]\s*$",
]

AUTH_PATTERNS = [
    r"(?i)authenticate your account at",
    r"(?i)press enter to open (?:it )?in (?:the|your) browser",
    r"(?i)(?:open|visit|go to|navigate to) (?:this|the following) (?:url|link|page)",
    r"(?i)\b(?:device code|user code|enter the code)\b",
    r"(?i)waiting for (?:authentication|authorization|approval|you to (?:log|sign) in)",
    r"(?i)(?:log|sign) ?in (?:at|here|via)[:：]?\s*(?:https?://|$)",
]

CONFIRM_PATTERNS = [
    r"\[[yY](?:es)?/[nN]o?\]\s*[:?]?\s*$",
    r"\([yY](?:es)?/[nN]o?\)\s*[:?]?\s*$",
    r"(?i)(?:continue|proceed|overwrite|replace|delete|are you sure)[^\n]{0,60}\?\s*$",
    r"(?i)press (?:any key|enter|return)[^\n]{0,40}$",
]

PAGER_PATTERNS = [
    r"(?i)--\s*more\s*--",
    r"^\(END\)\s*$",
    r"^:\s*$",
    r"(?i)type <ret> for more",
    r"(?i)<return> to continue",
]

_PROMPTISH = re.compile(r"[>$#%:?\]\)»❯]\s?$")
# a selection list drawn by prompt libraries: "> [x] Option", "❯ Option", "› Option"
_CHECK = r"(?:\[[ xX*✓✔]\]\s*|\([ xX*•]\)\s*|[◯◉○●]\s*)?"
_MENU_CURSOR = re.compile(r"^(\s*(?:>|❯|›|➜|→|»)\s*)" + _CHECK + r"\S")
_MENU_OPTION = re.compile(r"^(\s+)" + _CHECK + r"\S")


def _menu_match(since_input: str) -> str | None:
    """The cursor line of an arrow-key menu that ends the output, if there is one.

    Menus draw the marker in front of the current option and indent the others so their
    text lines up with it ("> [x] A" / "  [ ] B"); a quoted "> line" above a paragraph
    does not line up that way."""
    lines = [ln for ln in since_input.rstrip().split("\n")[-14:] if ln.strip()]
    for index, line in enumerate(lines):
        cursor = _MENU_CURSOR.match(line)
        if not cursor:
            continue
        column = len(cursor.group(1))  # where the option text (or its checkbox) starts
        block = [line]
        for other in lines[index + 1 :]:  # options below the cursor, up to the end
            option = _MENU_OPTION.match(other)
            if not option or abs(len(option.group(1)) - column) > 1 or len(other.strip()) > 120:
                break
            block.append(other)
        above = 0
        for other in reversed(lines[:index]):  # options above the cursor
            option = _MENU_OPTION.match(other)
            if not option or abs(len(option.group(1)) - column) > 1 or len(other.strip()) > 120:
                break
            above += 1
        if index + len(block) == len(lines) and len(block) + above >= 2:
            return line.strip()
    return None


def compile_all(patterns: list[str]) -> list[re.Pattern[str]]:
    out = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern, re.MULTILINE))
        except re.error as exc:
            raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return out


@dataclass
class Detectors:
    prompts: list[re.Pattern[str]] = field(default_factory=lambda: compile_all(DEFAULT_PROMPTS))
    secret: list[re.Pattern[str]] = field(default_factory=lambda: compile_all(SECRET_PATTERNS))
    auth: list[re.Pattern[str]] = field(default_factory=lambda: compile_all(AUTH_PATTERNS))
    confirm: list[re.Pattern[str]] = field(default_factory=lambda: compile_all(CONFIRM_PATTERNS))
    pager: list[re.Pattern[str]] = field(default_factory=lambda: compile_all(PAGER_PATTERNS))
    quiet_ms: int = 400
    blocked_after_s: float = 120.0
    heuristic_prompts: bool = True

    @classmethod
    def build(
        cls,
        *,
        prompts: list[str] | None = None,
        needs_user: list[str] | None = None,
        confirm: list[str] | None = None,
        replace_prompts: bool = False,
        quiet_ms: int | None = None,
        blocked_after_s: float | None = None,
        heuristic_prompts: bool | None = None,
    ) -> Detectors:
        det = cls()
        if prompts:
            extra = compile_all(prompts)
            det.prompts = extra if replace_prompts else extra + det.prompts
        if needs_user:
            det.secret = compile_all(needs_user) + det.secret
        if confirm:
            det.confirm = compile_all(confirm) + det.confirm
        if quiet_ms is not None:
            det.quiet_ms = quiet_ms
        if blocked_after_s is not None:
            det.blocked_after_s = blocked_after_s
        if heuristic_prompts is not None:
            det.heuristic_prompts = heuristic_prompts
        return det


def _first(patterns: list[re.Pattern[str]], text: str) -> re.Match[str] | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match
    return None


_ONLY_URL = re.compile(r"^\s*https?://\S+\s*$")


def _auth_match(det: Detectors, since_input: str) -> re.Match[str] | None:
    """Browser sign-in counts only while it is the last thing on screen: the
    request line itself, or a bare URL directly under it. Once the program
    prints anything after the URL, the sign-in has moved on."""
    lines = [ln for ln in since_input.rstrip().split("\n")[-6:] if ln.strip()]
    if not lines:
        return None
    last = lines[-1]
    match = _first(det.auth, last)
    if match:
        return match
    if _ONLY_URL.match(last) and len(lines) >= 2:
        return _first(det.auth, lines[-2])
    return None


def infer(
    *,
    alive: bool,
    exit_code: int | None,
    last_line: str,
    since_input: str,
    idle_s: float,
    det: Detectors,
    awaiting_response: bool = False,
) -> dict:
    """Classify a session.

    `since_input` is cleaned output produced after the last thing we sent,
    `last_line` the line the cursor sits on, `idle_s` the time since the last
    output or input, and `awaiting_response` is true while nothing has been
    printed since our last input (so an old prompt still on screen must not be
    mistaken for a fresh one).
    """
    idle_ms = int(idle_s * 1000)
    base = {"idle_ms": idle_ms}
    if not alive:
        return {
            **base,
            "state": "exited",
            "exit_code": exit_code,
            "reason": f"process/connection ended (exit code {exit_code})",
        }
    if awaiting_response:
        if idle_ms < max(det.quiet_ms, 1500):
            return {**base, "state": "running", "reason": "input sent; waiting for the program to respond"}
        if idle_s >= det.blocked_after_s:
            return {
                **base,
                "state": "blocked",
                "reason": f"no response for {int(idle_s)}s since the last input",
            }
        return {
            **base,
            "state": "idle",
            "reason": "no output since the last input (it may be working silently or waiting on "
            "something external, e.g. a target running to a breakpoint)",
        }

    line = last_line.rstrip("\n")
    stripped_line = line.strip()

    # A prompt can arrive in several reads (or be followed immediately by the response to our input).
    # Let it settle briefly before asking the person to answer it again.
    if idle_ms < min(det.quiet_ms, 100):
        return {**base, "state": "running", "reason": "output is still arriving; waiting for it to settle"}

    match = _first(det.secret, line)
    if match:
        return {
            **base,
            "state": "needs_user",
            "kind": "secret",
            "matched": match.group(0).strip(),
            "reason": "asks for a password/one-time code; a human must enter it",
        }
    match = _auth_match(det, since_input)
    if match:
        return {
            **base,
            "state": "needs_user",
            "kind": "browser_auth",
            "matched": match.group(0).strip(),
            "reason": "waits for a browser sign-in/approval; give the URL to the human",
        }

    quiet = idle_ms >= det.quiet_ms
    if stripped_line:
        match = _first(det.pager, line)
        if match:
            return {
                **base,
                "state": "awaiting_input",
                "kind": "pager",
                "matched": match.group(0).strip(),
                "reason": "pager is waiting (send Enter/Space for more, or q to quit)",
            }
        match = _first(det.confirm, line)
        if match:
            return {
                **base,
                "state": "awaiting_input",
                "kind": "confirm",
                "matched": match.group(0).strip(),
                "reason": "asks a yes/no or press-a-key question",
            }
        match = _first(det.prompts, line)
        if match and (quiet or idle_ms >= 100):
            return {
                **base,
                "state": "awaiting_input",
                "kind": "prompt",
                "matched": stripped_line,
                "reason": "prompt is showing",
            }
        menu = _menu_match(since_input) if quiet and det.heuristic_prompts else None
        if menu:
            return {
                **base,
                "state": "awaiting_input",
                "kind": "menu",
                "matched": menu,
                "reason": "a selection menu is showing: Up/Down move, Space toggles, Enter chooses "
                "(session_screen shows the current selection)",
            }
        if quiet and det.heuristic_prompts and len(stripped_line) <= 160 and _PROMPTISH.search(line):
            return {
                **base,
                "state": "awaiting_input",
                "kind": "prompt_guess",
                "matched": stripped_line,
                "reason": "output stopped on a prompt-shaped line",
            }

    if not quiet:
        return {**base, "state": "running", "reason": "producing output"}
    if idle_s >= det.blocked_after_s:
        return {
            **base,
            "state": "blocked",
            "reason": f"no output and no prompt for {int(idle_s)}s; it may be stuck or waiting on "
            "something external (hardware, network, a breakpoint)",
        }
    return {
        **base,
        "state": "idle",
        "reason": "quiet without a recognisable prompt (still working, or waiting on something external)",
    }


def monotonic() -> float:
    return time.monotonic()
