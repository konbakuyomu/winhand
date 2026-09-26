from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from winhand.session import SessionManager, SessionSpec

FAKE_APP = str(Path(__file__).parent / "fake_app.py")


@pytest.fixture(autouse=True)
def winhand_home(tmp_path, monkeypatch):
    monkeypatch.setenv("WINHAND_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def manager():
    m = SessionManager()
    yield m
    m.stop_all()


@pytest.fixture
def fake_spec():
    def make(transport: str = "pty", **kw) -> SessionSpec:
        env = {"PYTHONUNBUFFERED": "1"}
        return SessionSpec(
            transport=transport, argv=[sys.executable, "-u", FAKE_APP], prompts=[r"^fake> ?$"], env=env, **kw
        )

    return make


def on_windows() -> bool:
    return os.name == "nt"


async def wait_until_exited(manager, session, total_s: float = 150):
    """Keep calling wait_for like a real client does: one call is capped (MAX_WAIT_S) and a
    timeout only means "call again"."""
    import time

    from winhand.session import wait_for

    deadline = time.monotonic() + total_s
    while True:
        r = await wait_for(manager, [session.id], states=["exited"], timeout_s=45, since={session.id: 0})
        if r.get("hit") or time.monotonic() > deadline:
            return r
