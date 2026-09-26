"""Block until something interesting happens in one or more sessions.

Conditions are OR-ed across all given sessions, so a model can say "wait until
gdb shows its prompt OR the serial console prints PANIC OR anything asks for a
password" in one call. A session asking a human for something, or ending,
always stops the wait: those are never things to sleep through.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable

from .manager import SessionManager
from .session import Session
from .text import clean

DEFAULT_STATES = ["awaiting_input", "needs_user", "exited"]
MAX_WAIT_S = 90.0


def _compile(patterns: list[str], ignore_case: bool) -> list[re.Pattern[str]]:
    flags = re.MULTILINE | (re.IGNORECASE if ignore_case else 0)
    try:
        return [re.compile(p, flags) for p in patterns]
    except re.error as exc:
        raise ValueError(f"invalid regex: {exc}") from exc


async def wait_for(
    manager: SessionManager,
    ids: list[str],
    *,
    patterns: list[str] | None = None,
    ignore_case: bool = False,
    states: list[str] | None = None,
    quiet_ms: int | None = None,
    screen_stable_ms: int | None = None,
    min_chars: int | None = None,
    quiet_after_output: bool = False,
    timeout_s: float = 30.0,
    since: dict[str, int] | None = None,
    include_screen: bool = False,
    output_limit: int = 8000,
    max_wait_s: float = MAX_WAIT_S,
    progress: Callable[[float, float], Awaitable[None]] | None = None,
) -> dict:
    sessions: list[Session] = [manager.get(i) for i in ids]
    if not sessions:
        raise ValueError("give at least one session id")
    regexes = _compile(patterns or [], ignore_case)
    no_condition = not (regexes or states or quiet_ms or screen_stable_ms or min_chars)
    wanted_states = set(DEFAULT_STATES if no_condition else (states or []))
    starts = {s.id: (since or {}).get(s.id, s.read_cursor) for s in sessions}
    scan_pos = dict(starts)

    timeout = max(0.0, min(float(timeout_s), max_wait_s))
    began = time.monotonic()
    deadline = began + timeout
    next_progress = began + 5
    hit: dict | None = None

    while hit is None:
        for session in sessions:
            hit = _check(
                session,
                regexes,
                wanted_states,
                quiet_ms,
                screen_stable_ms,
                min_chars,
                starts[session.id],
                scan_pos,
                quiet_after_output,
            )
            if hit:
                break
        if hit or time.monotonic() >= deadline:
            break
        now = time.monotonic()
        if progress and now >= next_progress:
            next_progress = now + 5
            await progress(now - began, timeout)
        await asyncio.sleep(0.05)

    waited_ms = int((time.monotonic() - began) * 1000)
    result: dict = {"matched": hit is not None, "waited_ms": waited_ms}
    if hit:
        result["hit"] = hit
    else:
        result["timed_out"] = True
        capped = float(timeout_s) > max_wait_s
        result["hint"] = (
            (f"Timeout capped at {int(max_wait_s)}s per call. " if capped else "")
            + "Nothing matched yet; this is not an error. Call session_wait again with the same "
            "conditions to keep waiting (output seen so far is below and won't be repeated)."
        )
    result["sessions"] = {
        s.id: s.snapshot(
            since=starts[s.id], limit=output_limit, with_screen=include_screen or bool(screen_stable_ms)
        )
        for s in sessions
    }
    return result


def _visible_since(session: Session, start: int) -> bool:
    return bool(clean(session.buffer.read(start, 50000).text).strip())


def _check(
    session: Session,
    regexes,
    wanted_states,
    quiet_ms,
    screen_stable_ms,
    min_chars,
    start: int,
    scan_pos: dict[str, int],
    quiet_after_output: bool = False,
) -> dict | None:
    sid = session.id
    if regexes:
        window = session.buffer.read(max(start, scan_pos[sid] - 2000))
        text = clean(window.text)
        for regex in regexes:
            match = regex.search(text)
            if match:
                return {
                    "session": sid,
                    "condition": "pattern",
                    "pattern": regex.pattern,
                    "match": match.group(0)[:500],
                    "groups": [g for g in match.groups()][:10],
                }
        scan_pos[sid] = window.end
    if min_chars and session.buffer.end - start >= min_chars:
        return {"session": sid, "condition": "min_chars", "chars": session.buffer.end - start}

    st = session.state()
    if st["state"] == "exited":
        return {"session": sid, "condition": "exited", "exit_code": st.get("exit_code")}
    if st["state"] == "needs_user":
        return {
            "session": sid,
            "condition": "needs_user",
            "kind": st.get("kind"),
            "matched": st.get("matched"),
        }
    if st["state"] in wanted_states:
        return {
            "session": sid,
            "condition": "state",
            "state": st["state"],
            "kind": st.get("kind"),
            "matched": st.get("matched"),
        }
    # "Quiet" after we just started/typed means "it answered and settled", so a
    # slow starter that has printed nothing yet (common on Windows) keeps waiting.
    # ConPTY emits screen-setup escape codes before the program prints anything,
    # so only visible text counts as "it answered".
    if quiet_ms and st["idle_ms"] >= quiet_ms and (not quiet_after_output or _visible_since(session, start)):
        return {"session": sid, "condition": "quiet", "idle_ms": st["idle_ms"]}
    if screen_stable_ms and session.screen is not None:
        stable = int((time.monotonic() - session.screen.last_change) * 1000)
        if stable >= screen_stable_ms:
            return {"session": sid, "condition": "screen_stable", "stable_ms": stable}
    return None
