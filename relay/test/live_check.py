"""Check a deployed relay end to end with the real agent, then leave it as it was.

Pairs a throwaway computer from the home page (one-time install code), connects `winhand connect` with
the token it got, signs a client in for that computer only, calls help() through MCP, checks that the
token opens nothing else, and removes the computer again (its token and sign-ins go with it).
Prints no secrets.

    WINHAND_RELAY=https://mcp.example.com OWNER_PASSPHRASE_FILE=<file> \
        uv run --project agent python relay/test/live_check.py
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import subprocess
import tempfile
import time
from urllib.parse import parse_qs, urlparse

import httpx

BASE = os.environ["WINHAND_RELAY"].rstrip("/")
with open(os.environ["OWNER_PASSPHRASE_FILE"], encoding="utf-8-sig") as fh:
    PASSPHRASE = fh.read().strip()
NAME = "check-" + secrets.token_hex(3)
CALLBACK = "https://client.example/callback"


def check(condition: bool, what: str) -> None:
    print(("PASS " if condition else "FAIL ") + what, flush=True)
    if not condition:
        raise SystemExit(1)


def rpc_result(response: httpx.Response, rpc_id: int) -> dict:
    if "text/event-stream" in response.headers.get("content-type", ""):
        for line in response.text.splitlines():
            if line.startswith("data:"):
                message = json.loads(line[5:])
                if message.get("id") == rpc_id:
                    return message
        return {}
    return response.json()


async def main() -> None:
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
        started = time.monotonic()
        home = await http.get(f"{BASE}/")
        check(home.status_code == 200, f"home page ({(time.monotonic() - started) * 1000:.0f} ms)")
        page = await http.post(f"{BASE}/pair", data={"name": NAME, "passphrase": PASSPHRASE})
        found = re.search(r"/i/([A-Za-z0-9_-]{22}) \| iex", page.text)
        check(page.status_code == 200 and bool(found), f"the home page made an install command for {NAME}")
        code = found.group(1)
        script = await http.get(f"{BASE}/i/{code}")
        check("Invoke-RestMethod -Method Post" in script.text, "GET /i/<code> gives the install script")
        claimed = await http.post(f"{BASE}/i/{code}")
        check(claimed.status_code == 200, "the script's POST gets the device token")
        pair = claimed.json()
        check(pair["agent_url"] == "wss" + BASE[len("https") :] + f"/pc/{NAME}/agent", "the agent URL is the computer's own")
        check((await http.post(f"{BASE}/i/{code}")).status_code == 410, "the code works once")

        home_dir = tempfile.mkdtemp(prefix="winhand-live-")
        env = {**os.environ, "WINHAND_HOME": home_dir}
        subprocess.run(
            ["winhand", "login", "--url", pair["agent_url"], "--token-stdin"],
            input=pair["token"] + "\n", text=True, env=env, check=True, capture_output=True,
        )
        log = open(os.path.join(home_dir, "agent.log"), "w")  # noqa: SIM115 - lives as long as the agent
        agent = subprocess.Popen(["winhand", "connect"], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            status: dict = {}
            for _ in range(60):
                status = (await http.get(f"{BASE}/pc/{NAME}/status.json")).json()
                if status.get("online"):
                    break
                await asyncio.sleep(1)
            check(status.get("online") is True, f"the real agent is online as /pc/{NAME} (winhand {status.get('version')})")

            registered = await http.post(
                f"{BASE}/oauth/register",
                json={
                    "client_name": "winhand live check",
                    "redirect_uris": [CALLBACK],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
            check(registered.status_code == 201, "dynamic client registration")
            client = registered.json()
            verifier = secrets.token_urlsafe(32)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            resource = f"{BASE}/pc/{NAME}/mcp"
            consent = await http.get(
                f"{BASE}/authorize",
                params={
                    "client_id": client["client_id"], "redirect_uri": CALLBACK, "response_type": "code",
                    "scope": "winhand", "state": "live", "code_challenge": challenge,
                    "code_challenge_method": "S256", "resource": resource,
                },
            )
            check(consent.status_code == 200 and f"<strong>{NAME}</strong>" in consent.text, "the consent page names the computer")
            handle = re.search(r'name="handle" value="([^"]+)"', consent.text).group(1)
            approved = await http.post(
                f"{BASE}/authorize",
                data={"handle": handle, "client": "winhand live check", "redirect": CALLBACK, "pc": NAME,
                      "passphrase": PASSPHRASE, "action": "approve"},
                headers={"origin": BASE},
            )
            check(approved.status_code == 302, "the owner passphrase signs the client in")
            auth_code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]
            exchanged = await http.post(
                f"{BASE}/oauth/token",
                data={"grant_type": "authorization_code", "client_id": client["client_id"], "redirect_uri": CALLBACK,
                      "code": auth_code, "code_verifier": verifier, "resource": resource},
            )
            check(exchanged.status_code == 200, "token for this computer")
            token = exchanged.json()["access_token"]

            headers = {"authorization": f"Bearer {token}", "content-type": "application/json",
                       "accept": "application/json, text/event-stream", "mcp-protocol-version": "2025-06-18"}
            started = time.monotonic()
            init = await http.post(resource, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live-check", "version": "1"}}})
            check(init.status_code == 200 and "result" in rpc_result(init, 1), f"MCP initialize ({(time.monotonic() - started) * 1000:.0f} ms)")
            if session := init.headers.get("mcp-session-id"):
                headers["mcp-session-id"] = session
            await http.post(resource, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = rpc_result(await http.post(resource, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}), 2)
            check(any(t["name"] == "help" for t in listed.get("result", {}).get("tools", [])), "tools/list through the relay")
            started = time.monotonic()
            called = rpc_result(await http.post(resource, headers=headers, json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                                                             "params": {"name": "help", "arguments": {}}}), 3)
            check("result" in called and not called["result"].get("isError"), f"help() on the computer ({(time.monotonic() - started) * 1000:.0f} ms)")
            legacy = await http.post(f"{BASE}/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
            check(legacy.status_code in (401, 404), f"the token does not open the old /mcp ({legacy.status_code})")
            other = await http.post(f"{BASE}/pc/{NAME}x/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
            check(other.status_code == 404, "no other computer answers to it")
        finally:
            agent.terminate()
            removed = await http.post(f"{BASE}/remove", data={"name": NAME, "passphrase": PASSPHRASE})
            print(("PASS " if removed.status_code == 200 else "FAIL ") + f"removed {NAME} again", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
