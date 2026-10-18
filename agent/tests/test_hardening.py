"""winhand 0.7.0: hardening found by a security review of the code and of how it was used."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest
from fastmcp.client import Client
from mcp.types import TextContent

from winhand.config import Config
from winhand.server import build_server


def _text(result) -> str:
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


# ------------------------------------------------------------------ guard: names Windows trims


def test_trailing_dots_and_spaces_are_trimmed_like_windows_does():
    from winhand.guard import trim_components

    assert (
        trim_components(r"C:\Users\me\.winhand\desktop-apps.toml.")
        == r"C:\Users\me\.winhand\desktop-apps.toml"
    )
    assert trim_components(r"C:\proj\.env ") == r"C:\proj\.env"
    assert trim_components(r"C:\a. \b..\c") == r"C:\a\b\c"
    assert trim_components(r"C:\x\..\y\.") == r"C:\x\..\y\."  # . and .. stay
    assert trim_components("/srv/a/./b") == "/srv/a/./b"


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 path rules")
def test_a_new_approvals_file_spelled_with_a_dot_is_refused(winhand_home):
    from winhand import actions, approvals, guard

    target = approvals.path()
    assert not target.exists()
    sneaky = str(target) + "."
    assert guard.touches_approvals(actions.describe("fs_write", {"path": sneaky}))
    assert guard.touches_approvals(actions.describe("fs_write", {"path": str(target) + " "}))


# ------------------------------------------------------------------ winhand's own secrets never leave


@pytest.fixture
def relay_config(winhand_home):
    from winhand import redact

    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text(
        textwrap.dedent(
            """\
            [relay]
            url = "wss://mcp.example.test/agent"
            device_token = "dev-token-0123456789abcdef"

            [mcp_servers.docs]
            url = "http://127.0.0.1:8000/mcp"
            headers = { Authorization = "Bearer hdr-secret-abcdef123456" }
            """
        ),
        encoding="utf-8",
    )
    (winhand_home / "owner-passphrase.txt").write_text("correct horse battery staple\n", encoding="utf-8")
    redact.reset()
    yield winhand_home
    redact.reset()


async def test_device_token_and_passphrase_are_masked(relay_config):
    async with Client(build_server(Config())) as c:
        read = await c.call_tool("fs_read", {"path": str(relay_config / "config.toml")})
        text = read.structured_content["content"]
        assert "dev-token-0123456789abcdef" not in text and "«redacted:winhand»" in text
        assert "hdr-secret-abcdef123456" not in text
        shown = await c.call_tool(
            "run",
            {
                "command": sys.executable,
                "args": ["-c", f"print(open(r'{relay_config / 'owner-passphrase.txt'}').read())"],
            },
        )
        assert "battery staple" not in json.dumps(shown.structured_content)


async def test_known_token_formats_are_masked(winhand_home):
    from winhand import redact

    redact.reset()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    async with Client(build_server(Config())) as c:
        out = await c.call_tool(
            "run", {"command": sys.executable, "args": ["-c", f"print('token = {token}')"]}
        )
    stdout = out.structured_content["stdout"]
    assert token not in stdout and "«redacted:token»" in stdout
    assert not redact.current().text("sk-learn-tutorial-advanced-features")[1]  # a slug, not a key


async def test_known_secrets_can_be_turned_off(winhand_home):
    from winhand import redact

    winhand_home.mkdir(parents=True, exist_ok=True)
    (winhand_home / "config.toml").write_text("[redact]\nknown_secrets = false\n", encoding="utf-8")
    redact.reset()
    token = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    assert redact.current().text(token)[0] == token
    redact.reset()


async def test_a_binary_file_holding_a_masked_value_is_not_sent(relay_config, tmp_path):
    blob = tmp_path / "dump.bin"
    blob.write_bytes(b"\x00\x01\x02 dev-token-0123456789abcdef \xff\xfe")
    async with Client(build_server(Config())) as c:
        sent = await c.call_tool("fs_send", {"path": str(blob)})
    assert "not sent" in _text(sent) and "dev-token" not in _text(sent)


# ------------------------------------------------------------------ the tunnel's local port


async def test_the_local_port_answers_only_the_tunnel():
    import httpx

    from winhand.mcp_bridge import LocalSecretOnly, compose_app

    app = compose_app(build_server(Config()).http_app(host_origin_protection="auto"), None, "s3cret")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    headers = {"accept": "application/json, text/event-stream"}
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:41234") as http:
        bare = await http.post("/mcp", json=body, headers=headers)
        assert bare.status_code == 403
        wrong = await http.post("/mcp", json=body, headers={**headers, LocalSecretOnly.HEADER: "guess"})
        assert wrong.status_code == 403
        rebinding = await http.post(
            "/mcp",
            json=body,
            headers={**headers, LocalSecretOnly.HEADER: "s3cret", "host": "evil.example:41234"},
        )
        assert rebinding.status_code == 421


async def test_a_tunnel_request_passes_with_the_secret():
    import httpx

    from winhand.mcp_bridge import LocalSecretOnly, compose_app

    mcp = build_server(Config())
    app = compose_app(mcp.http_app(host_origin_protection="auto"), None, "s3cret")
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:41234") as http:
            init = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
            ok = await http.post(
                "/mcp",
                json=init,
                headers={"accept": "application/json, text/event-stream", LocalSecretOnly.HEADER: "s3cret"},
            )
            assert ok.status_code == 200, ok.text
