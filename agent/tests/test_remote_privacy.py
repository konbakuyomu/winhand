"""winhand 0.4.2: remote_run, sessions that restart, output redaction and secret_prompt."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import wait_until_exited
from fastmcp.client import Client

from winhand import config, redact, remote, secret_store
from winhand.config import Config
from winhand.server import build_server
from winhand.session import SessionSpec

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shebang stand-ins")


@pytest.fixture(autouse=True)
def fresh_redactor():
    redact.reset()
    yield
    redact.reset()


def fake_ssh(directory: Path, body: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ssh"
    path.write_text(f"#!{sys.executable}\nimport sys, json\n{body}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


# ------------------------------------------------------------------ remote_run


@POSIX
def test_remote_run_builds_a_safe_ssh_call(tmp_path, monkeypatch):
    fake_ssh(tmp_path / "bin", "print(json.dumps({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}))")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    out = remote.remote_run(
        "lax-via-sjc", "echo $1\r\nuname -a\r\n", args=["a b", "$HOME"], env={"MODE": "it's"}, cwd="/opt/x y"
    )
    seen = json.loads(out["stdout"])
    assert seen["argv"][-2:] == ["lax-via-sjc", "bash -s -- 'a b' '$HOME'"]
    assert "BatchMode=yes" in seen["argv"] and "-T" in seen["argv"]
    assert seen["stdin"] == "export MODE='it'\"'\"'s'\ncd -- '/opt/x y' || exit 97\necho $1\nuname -a\n"
    assert out["host"] == "lax-via-sjc" and out["exit_code"] == 0


@POSIX
def test_remote_run_explains_ssh_failures(tmp_path, monkeypatch):
    fake_ssh(tmp_path / "bin", "sys.stderr.write('Host key verification failed.\\n'); sys.exit(255)")
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    out = remote.remote_run("dmit", "true")
    assert out["exit_code"] == 255 and out["ssh_error"]["kind"] == "host_key"
    assert remote.classify("ssh: connect to host x port 22: Connection refused")["kind"] == "refused"
    assert remote.classify("Permission denied (publickey).")["kind"] == "auth"
    assert remote.classify("all fine") is None


def test_remote_run_refuses_option_injection_and_odd_shells():
    assert "ssh alias" in remote.remote_run("-oProxyCommand=calc", "x")["error"]
    assert "ssh alias" in remote.remote_run("host name", "x")["error"]
    assert "program name" in remote.remote_run("h", "x", shell="bash; rm -rf /")["error"]
    argv, body = remote.build("h", "print(1)", shell="python3", args=["x"])
    assert argv[-1] == "python3 - x" and body == "print(1)\n"


# ------------------------------------------------------------------ sessions that restart


async def test_session_restarts_after_exit(manager):
    code = "import sys; print('tick', flush=True); sys.exit(3)"
    s = manager.create(
        SessionSpec(
            transport="pipe", argv=[sys.executable, "-c", code], restart_on_exit=2, restart_delay_s=0.1
        )
    )
    r = await wait_until_exited(manager, s, total_s=30)
    out = r["sessions"][s.id]["output"]
    assert out.count("tick") == 3, out
    assert "restarted after exit code 3 (1/2)" in out and "(2/2)" in out
    assert any(e["event"] == "restarting" for e in s.events)


async def test_stopping_a_restarting_session_stops_it(manager):
    s = manager.create(
        SessionSpec(
            transport="pipe",
            argv=[sys.executable, "-c", "raise SystemExit(1)"],
            restart_on_exit=50,
            restart_delay_s=5,
        )
    )
    import asyncio

    await asyncio.sleep(0.5)
    out = s.stop(force=True)
    assert out["stopped"]


# ------------------------------------------------------------------ redaction


def test_redactor_masks_longest_values_patterns_and_ips():
    r = redact.Redactor({"value": ["abc", "abcdef"]}, [r"sk-[a-z0-9]{8,}"], ipv4=True)
    text, n = r.text("abcdef abc sk-12345678zz 8.8.8.8 127.0.0.1 v1.2.3.4.5")
    assert (
        text == ("«redacted:value» «redacted:value» «redacted:pattern» «redacted:ip» 127.0.0.1 v1.2.3.4.5")
        and n == 4
    )
    data, n = r.data({"a": ["abc", {"b": "x abcdef"}], "n": 3})
    assert data == {"a": ["«redacted:value»", {"b": "x «redacted:value»"}], "n": 3} and n == 2
    assert redact.Redactor({"host": ["github.com"]}).empty


def test_ssh_hosts_follow_includes_and_env_files(tmp_path):
    inc = tmp_path / "more.conf"
    inc.write_text("Host b\n  HostName 203.0.113.9\n")
    cfg = tmp_path / "config"
    cfg.write_text(
        f"Host a\n  HostName vps.secret.example\n  HostName %h.proxy\nInclude {inc}\nHost gh\n  HostName github.com\n"
    )
    assert redact.ssh_hosts(cfg) == ["vps.secret.example", "203.0.113.9", "github.com"]
    env = tmp_path / "secrets" / "dmit.env"
    env.parent.mkdir()
    env.write_text("# comment\nHOST=198.51.100.7\nPORT=22\nTOKEN='abcdefgh12'\n")
    (tmp_path / "secrets" / "token.txt").write_text("cf-token-0123456789\n")
    found = redact.file_values([str(tmp_path / "secrets" / "*")])
    assert found == ["198.51.100.7", "abcdefgh12", "cf-token-0123456789"]


def test_redact_section_survives_saving_the_config(winhand_home):
    cfg = Config(relay_url="wss://r.example/agent", redact={"values": ["x.example"], "ssh_hosts": True})
    config.save(cfg)
    again = config.load()
    assert again.redact == {"values": ["x.example"], "ssh_hosts": True}
    assert again.relay_url == "wss://r.example/agent"


async def test_results_history_and_writes_are_protected(winhand_home, tmp_path):
    config.save(Config(redact={"values": ["secret.example.com"]}))
    async with Client(build_server(Config())) as client:
        ran = await client.call_tool(
            "run", {"command": sys.executable, "args": ["-c", "print('host secret.example.com ok')"]}
        )
        assert "secret.example.com" not in ran.content[0].text
        assert "host «redacted:value» ok" in ran.structured_content["stdout"]
        assert ran.structured_content["redacted"] == 1
        await client.call_tool("fs_stat", {"path": "/no/secret.example.com"})
        calls = (await client.call_tool("history", {})).structured_content["calls"]
        assert "secret.example.com" not in json.dumps(calls)
        target = tmp_path / "cfg.txt"
        refused = await client.call_tool(
            "fs_write", {"path": str(target), "content": "host = «redacted:value»\n"}
        )
        assert "placeholders" in refused.structured_content["error"] and not target.exists()
        again = await client.call_tool("run", {"command": "echo «redacted:value»"})
        assert "placeholders" in again.structured_content["error"]


# ------------------------------------------------------------------ secret_prompt


def test_secret_prompt_saves_without_returning_the_value(tmp_path, monkeypatch):
    answers = iter(["short", "cf_ABCDEFGHIJKLMNOPQRSTUVWX"])
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: next(answers))
    env = tmp_path / "secrets" / "cloudflare.env"
    env.parent.mkdir()
    env.write_text("ZONE=example\nCF_TOKEN=old\n")
    out = secret_store.prompt_and_save("token", save_to=str(env), key="CF_TOKEN", validate=r"cf_\w{20,}")
    assert out["saved"] and "cf_ABC" not in json.dumps(out)
    assert env.read_text() == "ZONE=example\nCF_TOKEN=cf_ABCDEFGHIJKLMNOPQRSTUVWX\n"
    if sys.platform != "win32":
        assert env.stat().st_mode & 0o777 == 0o600
    masked, n = redact.current().text("token cf_ABCDEFGHIJKLMNOPQRSTUVWX used")
    assert n == 1 and "cf_ABC" not in masked


def test_secret_prompt_cancel_and_argument_checks(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: None)
    assert secret_store.prompt_and_save("x", save_to=str(tmp_path / "t.txt")) == {
        "saved": False,
        "reason": "the person cancelled",
    }
    with pytest.raises(secret_store.SecretError, match="save_to"):
        secret_store.prompt_and_save("x")
    with pytest.raises(secret_store.SecretError, match="key must"):
        secret_store.save_to_file(str(tmp_path / "a.env"), "v", key="bad key")


async def test_secret_prompt_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_store, "ask_secret", lambda title, message: "pw-0123456789")
    async with Client(build_server(Config())) as client:
        res = await client.call_tool(
            "secret_prompt", {"message": "panel password", "save_to": str(tmp_path / "p.txt")}
        )
        assert res.structured_content["saved"] and "pw-0123" not in res.content[0].text
        echoed = await client.call_tool("fs_read", {"path": str(tmp_path / "p.txt")})
        assert "pw-0123456789" not in echoed.content[0].text and "«redacted:secret»" in echoed.content[0].text
