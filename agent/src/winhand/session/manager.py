"""Owns all live sessions."""

from __future__ import annotations

import atexit
import re
import threading

from .session import Session, SessionSpec
from .transports import TransportError

MAX_SESSIONS = 32


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._counter = 0
        self._lock = threading.Lock()
        atexit.register(self.stop_all)

    def _new_id(self, spec: SessionSpec) -> str:
        base = spec.name or spec.profile or spec.transport
        slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")[:24] or "s"
        self._counter += 1
        return f"{slug}-{self._counter}"

    def create(self, spec: SessionSpec) -> Session:
        with self._lock:
            live = [s for s in self._sessions.values() if s.alive]
            if len(live) >= MAX_SESSIONS:
                raise TransportError(f"too many live sessions ({len(live)}); stop some first")
            sid = self._new_id(spec)
        session = Session(sid, spec)
        with self._lock:
            self._sessions[sid] = session
        return session

    def get(self, sid: str) -> Session:
        session = self._sessions.get(sid)
        if session is None:
            known = ", ".join(self._sessions) or "none"
            raise KeyError(f"no session {sid!r} (known: {known})")
        return session

    def list(self) -> list[Session]:
        return list(self._sessions.values())

    def remove(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def stop_all(self) -> None:
        for session in list(self._sessions.values()):
            try:
                session.stop(force=True, grace_s=0.5)
            except Exception:
                pass
