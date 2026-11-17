"""Several computers on one relay (Epic 003): each computer's links, hooks and MCP endpoints live under
its own /pc/<name> on the relay, taken from the agent URL it was paired with; `winhand login` saves
that URL and the device token without connecting."""

from __future__ import annotations

import io
import sys

import pytest

from winhand import cli, config, session_bridge, transfer


@pytest.mark.parametrize(
    ("url", "base"),
    [
        ("wss://relay.example/pc/home/agent", "https://relay.example/pc/home"),
        ("wss://relay.example/pc/office-2/agent", "https://relay.example/pc/office-2"),
        ("https://relay.example/pc/home/mcp/figma", "https://relay.example/pc/home"),
        ("https://relay.example/pc/home", "https://relay.example/pc/home"),
        ("wss://relay.example/agent", "https://relay.example"),
        ("https://relay.example/", "https://relay.example"),
        ("ws://127.0.0.1:8787/pc/a/agent", "http://127.0.0.1:8787/pc/a"),
        # not a computer's space: names are lower-case letters, digits and inner hyphens
        ("wss://relay.example/pc/Home/agent", "https://relay.example"),
    ],
)
def test_relay_base_keeps_the_computers_space(url, base):
    assert config.relay_base(url) == base


def test_relay_base_refuses_what_is_not_a_relay_url():
    with pytest.raises(ValueError):
        config.relay_base("ftp://relay.example/agent")


def test_links_carry_the_computers_space(monkeypatch):
    monkeypatch.setattr(transfer, "_public_base", None)
    transfer.set_relay("wss://relay.example/pc/home/agent")
    assert transfer.public_base() == "https://relay.example/pc/home"


def test_single_computer_links_stay_at_the_root(monkeypatch):
    monkeypatch.setattr(transfer, "_public_base", None)
    transfer.set_relay("wss://relay.example/agent")
    assert transfer.public_base() == "https://relay.example"


def test_hooks_go_to_the_computers_space_and_the_allowlist_names_the_host():
    origin = session_bridge.relay_origin("wss://relay.example/pc/home/agent")
    assert origin == "https://relay.example/pc/home"
    assert "'https://relay.example/pc/home/hook/session-start'" in session_bridge.hook_command(origin)
    text = session_bridge.setup_text(origin)
    assert "网站：relay.example；" in text


def test_login_saves_url_and_token_from_stdin_without_printing_it(monkeypatch, capsys):
    config.save(config.Config(redact={"x": "y"}))  # an existing section survives
    monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret-token-value\r\n"))
    assert cli.main(["login", "--url", "wss://relay.example/pc/home/agent", "--token-stdin"]) == 0
    cfg = config.load()
    assert cfg.relay_url == "wss://relay.example/pc/home/agent"
    assert cfg.device_token == "s3cret-token-value"
    assert cfg.redact == {"x": "y"}
    out = capsys.readouterr()
    assert "s3cret" not in out.out + out.err
    assert "saved to" in out.out


def test_login_refuses_a_missing_token_or_a_web_url(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(SystemExit):
        cli.main(["login", "--url", "wss://relay.example/pc/home/agent", "--token-stdin"])
    with pytest.raises(SystemExit):
        cli.main(["login", "--url", "https://relay.example/pc/home/mcp", "--token", "t"])


def test_login_drops_the_bom_windows_powershell_puts_in_front_of_piped_text(monkeypatch):
    class Stdin:
        buffer = io.BytesIO(b"\xef\xbb\xbfabc-TOKEN_123\r\n")

    monkeypatch.setattr(sys, "stdin", Stdin())
    assert cli.main(["login", "--url", "wss://relay.example/pc/home/agent", "--token-stdin"]) == 0
    assert config.load().device_token == "abc-TOKEN_123"
