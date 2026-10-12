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


@pytest.fixture(autouse=True)
def _no_agent_config_of_this_machine(tmp_path, monkeypatch):
    """context() must not read the Codex and Claude Code settings of the machine running the tests."""
    from winhand import workspace

    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(workspace, "managed_claude_md", lambda: str(tmp_path / "no-managed" / "CLAUDE.md"))


@pytest.fixture(autouse=True)
def _no_hooks_of_this_machine(tmp_path, monkeypatch):
    """The machine's own Codex / Claude Code hooks never run in tests; test_hooks.py turns its own on."""
    from winhand import hooks

    monkeypatch.setattr(hooks, "settings", lambda: {"sources": []})
    monkeypatch.setattr(hooks, "codex_system_dir", lambda: str(tmp_path / "no-codex-system"))
    monkeypatch.setattr(hooks, "claude_managed_dir", lambda: str(tmp_path / "no-claude-managed"))


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
