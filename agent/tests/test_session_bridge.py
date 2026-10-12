"""A cloud Claude Code session starts with this machine's rules: /hook/session-start, the hook
identity kept to that path, and `winhand bridge init / token`."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import subprocess
import threading

import httpx
import pytest

from winhand import cli, redact, session_bridge, workspace
from winhand.config import Config
from winhand.mcp_bridge import compose_app
from winhand.server import build_server


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "home-dir"
    folder.mkdir()
    for var in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(var, str(folder))
    monkeypatch.setattr(workspace, "managed_claude_md", lambda: str(tmp_path / "managed" / "CLAUDE.md"))
    return folder


def write(path, text: str = ""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def checkout(tmp_path, home):
    """A checkout of owner/name that Codex knows, with a tracked AGENTS.md and an ignored
    CLAUDE.local.md; global rules in ~/.codex and ~/.claude/rules; two skills."""
    if not shutil.which("git"):
        pytest.skip("git is needed")
    root = tmp_path / "work" / "name"
    write(root / "AGENTS.md", "TRACKED PROJECT RULE")
    write(root / ".gitignore", "CLAUDE.local.md\n")
    write(root / "CLAUDE.local.md", "UNTRACKED LOCAL RULE")
    git("init", "-q", cwd=root)
    git("remote", "add", "origin", "https://github.com/owner/name.git", cwd=root)
    git("add", "AGENTS.md", ".gitignore", cwd=root)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init", cwd=root)
    write(home / ".codex" / "AGENTS.md", "GLOBAL CODEX RULE")
    write(home / ".codex" / "config.toml", f'[projects.{json.dumps(str(root))}]\ntrust_level = "trusted"\n')
    write(home / ".claude" / "rules" / "style.md", "GLOBAL CLAUDE RULE")
    for name in ("pdf", "deploy"):
        write(home / ".agents" / "skills" / name / "SKILL.md", f"---\nname: {name}\ndescription: d\n---\n")
    return root


def app():
    return compose_app(build_server(Config()).http_app(), None)


async def post(path, user, body=b'{"source":"startup"}', headers=None):
    transport = httpx.ASGITransport(app=app())
    async with httpx.AsyncClient(transport=transport, base_url="http://agent") as client:
        return await client.post(path, content=body, headers={"x-winhand-user": user, **(headers or {})})


# ------------------------------------------------------------------ A3


async def test_the_hook_identity_reaches_only_the_hook_path(home):
    assert (await post("/hook/session-start", "owner")).status_code == 403
    assert (await post("/hook/session-start", "")).status_code == 403
    for path in ("/mcp", "/mcp/", "/mcp/fake", "/transfer/" + "A" * 43):
        assert (await post(path, "hook")).status_code == 403, path
    assert (await post("/hook/stop", "hook")).status_code == 404
    ok = await post("/hook/session-start", "hook")
    assert ok.status_code == 200
    assert ok.json()["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# ------------------------------------------------------------------ A4


async def test_a_session_starts_with_the_rules_the_cloud_lacks(checkout):
    for source in ("startup", "compact"):
        answer = await post(
            "/hook/session-start",
            "hook",
            body=json.dumps({"source": source, "cwd": "/home/user/name"}).encode(),
            headers={"x-winhand-repo": "https://github.com/owner/name"},
        )
        text = answer.json()["hookSpecificOutput"]["additionalContext"]
        assert "GLOBAL CODEX RULE" in text and "GLOBAL CLAUDE RULE" in text
        assert "UNTRACKED LOCAL RULE" in text
        assert "TRACKED PROJECT RULE" not in text
        assert "Skills on the PC: deploy, pdf" in text
        assert str(checkout) in text
        assert len(text) <= session_bridge.BUDGET_CHARS


def test_without_git_every_project_rule_is_sent(checkout, monkeypatch):
    monkeypatch.setattr(session_bridge, "_tracked", lambda root, paths: set())
    text = session_bridge.session_start("owner/name", Config())
    assert "TRACKED PROJECT RULE" in text and "UNTRACKED LOCAL RULE" in text


# ------------------------------------------------------------------ A5


def test_a_repository_this_machine_does_not_have(checkout):
    text = session_bridge.session_start("nobody/none", Config())
    assert "has no checkout of nobody/none" in text
    assert "GLOBAL CODEX RULE" in text and "UNTRACKED LOCAL RULE" not in text


def test_rules_too_long_are_cut_and_named(checkout, home):
    write(home / ".codex" / "AGENTS.md", "GLOBAL " + "x" * 20_000)
    text = session_bridge.session_start("owner/name", Config())
    assert len(text) <= session_bridge.BUDGET_CHARS
    assert "[cut here" in text
    assert "Skills on the PC:" in text
    assert "Not included for lack of room" in text and "CLAUDE.local.md" in text


def test_secrets_in_rules_are_masked(checkout, home, monkeypatch):
    write(home / ".codex" / "AGENTS.md", "the deploy key is s3cret-value-123")
    monkeypatch.setattr(redact, "current", lambda: redact.Redactor({"value": ["s3cret-value-123"]}))
    text = session_bridge.session_start("owner/name", Config())
    assert "s3cret-value-123" not in text and "redacted" in text


# ------------------------------------------------------------------ A6


def test_init_prints_the_hook_and_the_setup(capsys):
    assert cli.main(["bridge", "init", "--relay", "wss://relay.example.com/agent"]) == 0
    out = capsys.readouterr().out
    snippet = json.loads(out[out.index("{") : out.rindex("}") + 1].split("\n\n云端")[0])
    (group,) = snippet["hooks"]["SessionStart"]
    assert group["matcher"] == "startup|resume|clear|compact"
    assert "https://relay.example.com/hook/session-start" in group["hooks"][0]["command"]
    assert "X-Winhand-Hook-Token" in out and "relay.example.com" in out


def test_init_without_a_relay_says_so(capsys):
    with pytest.raises(SystemExit):
        cli.main(["bridge", "init"])
    assert "no relay" in capsys.readouterr().err


def test_write_merges_and_does_not_repeat(tmp_path):
    settings = write(
        tmp_path / "repo" / ".claude" / "settings.json",
        json.dumps(
            {"permissions": {"allow": ["Bash(ls)"]}, "hooks": {"PreToolUse": [{"hooks": [{"command": "x"}]}]}}
        ),
    )
    for _ in range(2):
        assert (
            cli.main(
                ["bridge", "init", "--relay", "https://relay.example.com", "--write", str(tmp_path / "repo")]
            )
            == 0
        )
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert data["hooks"]["PreToolUse"] == [{"hooks": [{"command": "x"}]}]
    assert len(data["hooks"]["SessionStart"]) == 1


def test_write_refuses_a_broken_settings_file(tmp_path, capsys):
    settings = write(tmp_path / "repo" / ".claude" / "settings.json", "{not json")
    assert (
        cli.main(["bridge", "init", "--relay", "https://r.example.com", "--write", str(tmp_path / "repo")])
        == 2
    )
    assert settings.read_text(encoding="utf-8") == "{not json"


def test_token_is_new_each_time(capsys):
    tokens = []
    for _ in range(2):
        cli.main(["bridge", "token"])
        out = capsys.readouterr().out
        (token,) = [line for line in out.splitlines() if len(line) == 43]
        assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
        tokens.append(token)
    assert tokens[0] != tokens[1]


class _Relay(http.server.BaseHTTPRequestHandler):
    seen: list = []
    status = 200

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length", 0)))
        _Relay.seen.append((self.path, dict(self.headers), body))
        answer = b'{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"RULES"}}'
        self.send_response(_Relay.status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


@pytest.mark.skipif(
    os.name == "nt" or not shutil.which("curl") or not shutil.which("sh"), reason="runs in a POSIX cloud"
)
def test_the_generated_command_behaves(tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), _Relay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    command = session_bridge.hook_command(origin)
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    git("remote", "add", "origin", "https://github.com/owner/name", cwd=repo)
    env = {
        "PATH": os.environ["PATH"],
        "CLAUDE_PROJECT_DIR": str(repo),
        "NO_PROXY": "127.0.0.1",
        "no_proxy": "127.0.0.1",
    }

    def run(extra):
        return subprocess.run(
            ["sh", "-c", command],
            input=b'{"source":"startup"}',
            capture_output=True,
            env={**env, **extra},
            timeout=40,
        )

    _Relay.seen.clear()
    done = run({"CLAUDE_CODE_REMOTE": "true", "WINHAND_HOOK_TOKEN": "tok"})
    assert done.returncode == 0 and b"RULES" in done.stdout
    path, headers, body = _Relay.seen[-1]
    assert path == "/hook/session-start" and body == b'{"source":"startup"}'
    assert headers["X-Winhand-Repo"] == "https://github.com/owner/name"
    assert headers["X-Winhand-Hook-Token"] == "tok"

    _Relay.seen.clear()
    done = run({"CLAUDE_CODE_REMOTE": "true"})
    assert done.returncode == 0 and "X-Winhand-Hook-Token" not in _Relay.seen[-1][1]

    _Relay.seen.clear()
    done = run({})
    assert done.returncode == 0 and done.stdout == b"" and not _Relay.seen

    _Relay.status = 302  # a login page in the way: nothing of it reaches the session
    done = run({"CLAUDE_CODE_REMOTE": "true"})
    assert done.returncode == 0 and done.stdout == b""
    _Relay.status = 200

    git("remote", "set-url", "origin", "https://someone:s3cret@github.com/owner/name", cwd=repo)
    done = run({"CLAUDE_CODE_REMOTE": "true"})
    assert _Relay.seen[-1][1]["X-Winhand-Repo"] == "github.com/owner/name"
    server.shutdown()

    done = run({"CLAUDE_CODE_REMOTE": "true"})  # nothing listens now
    assert done.returncode == 0 and done.stdout == b""


# ------------------------------------------------------------------ round 2


def test_tracked_imported_rules_are_left_out_too(checkout):
    write(checkout / "CLAUDE.md", "@docs/rules.md")
    write(checkout / "docs" / "rules.md", "TRACKED IMPORTED RULE")
    git("add", "CLAUDE.md", "docs/rules.md", cwd=checkout)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "imports", cwd=checkout)
    text = session_bridge.session_start("owner/name", Config())
    assert "TRACKED IMPORTED RULE" not in text and "UNTRACKED LOCAL RULE" in text


def test_many_skills_all_named_when_there_is_room(checkout, home):
    for i in range(150):
        write(
            home / ".agents" / "skills" / f"skill-{i:03}" / "SKILL.md",
            f"---\nname: skill-{i:03}\ndescription: d\n---\n",
        )
    text = session_bridge.session_start("owner/name", Config())
    assert "skill-149" in text and "more)" not in text


def test_the_budget_counts_as_claude_code_does(checkout, home):
    write(home / ".codex" / "AGENTS.md", "\U0001f600" * 6_000)  # each is two UTF-16 units
    text = session_bridge.session_start("owner/name", Config())
    assert session_bridge._size(text) <= session_bridge.BUDGET_CHARS


def test_the_opening_note_stays_short(checkout):
    text = session_bridge.session_start("https://github.com/owner/name" + "/x" * 300, Config())
    assert len(text.split("\n\n")[0]) < 700


def test_a_rule_not_read_says_so_once(checkout, home, monkeypatch):
    real = workspace._collect

    def collect(*args):
        rules = real(*args)
        rules.items.append(
            {"path": "/x/AGENTS.md", "format": "codex", "scope": "global", "skipped": "not read: test"}
        )
        return rules

    monkeypatch.setattr(workspace, "_collect", collect)
    text = session_bridge.session_start("owner/name", Config())
    assert "(not read: test)" in text and "not read: not read" not in text


def test_rules_past_the_bmp_are_cut_not_dropped(checkout, home):
    write(home / ".codex" / "AGENTS.md", "\U0001f600" * 12_000)
    text = session_bridge.session_start("owner/name", Config())
    assert text.count("\U0001f600") > 3_000 and session_bridge._size(text) <= session_bridge.BUDGET_CHARS


def test_a_credential_in_an_odd_address_is_not_echoed(checkout):
    text = session_bridge.session_start("https://user:SECRET@host.example/single.git", Config())
    assert "SECRET" not in text
