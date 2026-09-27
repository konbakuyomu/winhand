"""End-to-end check: MCP client -> relay (OAuth) -> WebSocket tunnel -> winhand agent.

Runs against `wrangler dev` (default http://127.0.0.1:8787) and plays the part of
claude.ai's connector: dynamic client registration, the passphrase consent page,
PKCE token exchange, then real MCP tool calls through the tunnel.

  cd relay && npx wrangler dev &      # with .dev.vars holding OWNER_PASSPHRASE / AGENT_TOKEN
  uv run --project ../agent python test/e2e.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import re
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx
from fastmcp.client import Client
from fastmcp.client.transports import StreamableHttpTransport

from winhand.config import Config, ServerEntry
from winhand.mcp_bridge import Services
from winhand.relay_client import run_forever
from winhand.server import build_server

BASE = os.environ.get("RELAY", "http://127.0.0.1:8787")
PASSPHRASE = os.environ.get("OWNER_PASSPHRASE", "test-passphrase-123")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "test-agent-token-456")
REDIRECT = "http://127.0.0.1:9/callback"


def check(cond: bool, what: str) -> None:
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        raise SystemExit(1)


async def oauth_token(http: httpx.AsyncClient, auth_method: str = "none") -> str:
    reg = await http.post(f"{BASE}/oauth/register", json={
        "redirect_uris": [REDIRECT], "client_name": "e2e connector", "token_endpoint_auth_method": auth_method,
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
    })
    check(reg.status_code in (200, 201), f"dynamic client registration, {auth_method} ({reg.status_code})")
    client_id = reg.json()["client_id"]
    client_secret = reg.json().get("client_secret")

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "s123",
              "code_challenge": challenge, "code_challenge_method": "S256", "scope": "winhand",
              "resource": f"{BASE}/mcp"}
    page = await http.get(f"{BASE}/authorize", params=params)
    check(page.status_code == 200 and "主人口令" in page.text, "consent page rendered")
    handle = re.search(r'name="handle" value="([^"]+)"', page.text).group(1)
    form = {"handle": handle, "client": "e2e connector", "redirect": REDIRECT}
    # The consent binding cookie is `Secure` (as it must be in production); plain-HTTP
    # wrangler dev means we hand it back ourselves, like a browser would over HTTPS.
    binding = {"Cookie": page.headers["set-cookie"].split(";")[0]}

    wrong = await http.post(f"{BASE}/authorize", data={**form, "passphrase": "nope", "action": "approve"},
                            headers=binding)
    check(wrong.status_code == 401 and "口令不对" in wrong.text, "wrong passphrase rejected")

    ok = await http.post(f"{BASE}/authorize", data={**form, "passphrase": PASSPHRASE, "action": "approve"},
                         headers=binding)
    check(ok.status_code == 302, f"correct passphrase redirects ({ok.status_code})")
    query = parse_qs(urlparse(ok.headers["location"]).query)
    check(query.get("state") == ["s123"] and "code" in query, "redirect carries code and state")

    data = {
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
        "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp",
    }
    headers = {}
    if client_secret:
        # like the Python MCP SDK: credentials in the header and again in the body
        data["client_secret"] = client_secret
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    token = await http.post(f"{BASE}/oauth/token", data=data, headers=headers)
    check(token.status_code == 200, f"token exchange, {auth_method} ({token.status_code} {token.text[:120]})")
    return token.json()["access_token"]


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as http:
        bad = await http.get(f"{BASE}/agent", headers={"Authorization": "Bearer wrong", "Upgrade": "websocket"})
        check(bad.status_code == 401, "agent with wrong token refused")
        token = await oauth_token(http)
        confidential = await oauth_token(http, "client_secret_basic")
        check(confidential != token, "a confidential client gets its own token")

        offline = await http.post(f"{BASE}/mcp", headers={"Authorization": f"Bearer {token}",
                                  "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        check(offline.status_code == 503 and "not connected" in offline.text, "clear error while agent offline")

        stop = asyncio.Event()
        ws_url = BASE.replace("http", "ws", 1) + "/agent"
        fake = os.path.join(os.path.dirname(__file__), "..", "..", "agent", "tests", "fake_mcp_server.py")
        services = Services([ServerEntry(name="fake", command=sys.executable, args=["-u", fake])])
        agent = asyncio.create_task(
            run_forever(ws_url, AGENT_TOKEN, mcp=build_server(Config()), services=services, stop=stop)
        )
        for _ in range(100):
            if "电脑在线" in (await http.get(f"{BASE}/")).text:
                break
            await asyncio.sleep(0.1)
        check("电脑在线" in (await http.get(f"{BASE}/")).text, "status page shows the agent online")

        transport = StreamableHttpTransport(f"{BASE}/mcp", headers={"Authorization": f"Bearer {token}"})
        async with Client(transport) as mcp:
            names = {t.name for t in await mcp.list_tools()}
            check({"session_start", "session_wait", "fs_edit", "run"} <= names, f"tools listed through tunnel ({len(names)})")
            ran = (await mcp.call_tool("run", {"command": sys.executable, "args": ["-c", "print('隧道 ok')"]})).structured_content
            check(ran["stdout"].strip() == "隧道 ok", "run tool through tunnel (UTF-8 intact)")
            big = "x" * 700_000
            path = os.path.join(os.path.dirname(__file__), "big.tmp")
            wrote = (await mcp.call_tool("fs_write", {"path": path, "content": big})).structured_content
            check(wrote["bytes"] == 700_000, "700 KB request body crosses the tunnel in chunks")
            read = (await mcp.call_tool("fs_read", {"path": path})).structured_content
            check(len(read["content"]) > 2000, "large response streams back")
            os.remove(path)
            start = (await mcp.call_tool("session_start", {"command": [sys.executable, "-q"]})).structured_content
            sid = start["session"]["id"]
            sent = (await mcp.call_tool("session_send", {"id": sid, "text": "print(21*2)", "submit": True})).structured_content
            check("42" in sent["output"] and sent["state"] == "awaiting_input", "interactive session through tunnel")
            await mcp.call_tool("session_stop", {"id": sid, "force": True})

        # a local MCP server of the machine, as its own endpoint (a separate connector in claude.ai)
        challenge = await http.post(f"{BASE}/mcp/fake", json={})
        check(challenge.status_code == 401 and f"{BASE}/.well-known/oauth-protected-resource/mcp" in
              challenge.headers.get("www-authenticate", ""), "local server endpoint asks for OAuth like /mcp")
        for where in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource/mcp/fake"):
            meta = await http.get(f"{BASE}{where}")
            check(meta.status_code == 200 and meta.json()["resource"] == f"{BASE}/mcp", f"resource metadata at {where}")
        transport = StreamableHttpTransport(f"{BASE}/mcp/fake", headers={"Authorization": f"Bearer {token}"})
        async with Client(transport) as local:
            names = {t.name for t in await local.list_tools()}
            check(names >= {"echo", "slow", "picture"} and "session_start" not in names, "local server's own tools, unmixed")
            echoed = await local.call_tool("echo", {"text": "经过中转"})
            check(echoed.content[0].text == "echo: 经过中转", "local server tool call through relay + tunnel")
            picture = await local.call_tool("picture", {})
            check(picture.content[0].type == "image", "image content passes through")
        unknown = await http.post(f"{BASE}/mcp/nope", headers={"Authorization": f"Bearer {token}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        check(unknown.status_code == 404 and "enabled: fake" in unknown.text, "unknown local server explained")

        refused = await http.post(f"{BASE}/mcp", headers={"Authorization": "Bearer forged"}, json={})
        check(refused.status_code == 401, "forged OAuth token refused")
        await services.close()
        stop.set()
        await agent
    print("ALL PASSED")


if __name__ == "__main__":
    asyncio.run(main())
