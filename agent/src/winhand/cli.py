"""winhand command line.

winhand stdio                 serve MCP over stdio (Claude Desktop, Codex, Claude Code ...)
winhand http [--port 8765]    serve MCP over streamable HTTP on localhost
winhand connect               connect out to the relay so claude.ai can reach this machine
winhand mcp list|import|test  local MCP servers offered at <relay>/mcp/<name>
winhand doctor                print what winhand sees on this machine
winhand profiles              list session profiles
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from . import __version__


def _force_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _start_in_home() -> None:
    """Serve from the user's home, not wherever we were launched (e.g. the app's install folder).

    Every command, session and job started without a cwd inherits this directory; left inside the
    install folder, one long-running shell would keep the updater from replacing it.
    """
    try:
        os.chdir(Path.home())
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(prog="winhand", description="Let an AI drive this machine over MCP.")
    parser.add_argument("--version", action="version", version=f"winhand {__version__}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("stdio", help="serve MCP over stdio (default)")
    http = sub.add_parser("http", help="serve MCP over streamable HTTP")
    http.add_argument("--host", default="127.0.0.1")
    http.add_argument("--port", type=int, default=8765)
    conn = sub.add_parser("connect", help="connect to the relay (remote access from claude.ai)")
    conn.add_argument("--url", help="relay agent URL, e.g. wss://mcp.example.com/agent")
    conn.add_argument("--token", help="device token issued by the relay")
    conn.add_argument("--save", action="store_true", help="store --url/--token in config.toml")
    mcp = sub.add_parser("mcp", help="local MCP servers offered through the relay at /mcp/<name>")
    mcp.add_argument("action", choices=["list", "import", "test"])
    mcp.add_argument("names", nargs="*", help="import: which ones (default all); test: which to test")
    sub.add_parser("doctor", help="show environment diagnostics")
    sub.add_parser("profiles", help="list session profiles")
    sub.add_parser("desktop-backend", help="engine for the tray app (JSON lines over stdio)")
    dialog = sub.add_parser("secret-dialog")  # internal: masked input dialog for session_prompt_user
    dialog.add_argument("payload", nargs="?")
    dialog.add_argument("--check", action="store_true", help="only verify the GUI toolkit is present")
    args = parser.parse_args(argv)
    cmd = args.cmd or "stdio"
    if cmd in ("stdio", "http", "connect", "desktop-backend"):
        _start_in_home()

    if cmd == "stdio":
        from .server import build_server

        build_server().run(transport="stdio", show_banner=False)
        return 0
    if cmd == "http":
        from .server import build_server

        build_server().run(transport="http", host=args.host, port=args.port, show_banner=False)
        return 0
    if cmd == "connect":
        from . import config
        from .relay_client import run_forever

        cfg = config.load()
        url = args.url or cfg.relay_url
        token = args.token or cfg.device_token
        if not url or not token:
            parser.error("need --url and --token (or [relay] url/device_token in config.toml)")
        if args.save:
            cfg.relay_url, cfg.device_token = url, token
            print(f"saved to {config.save(cfg)}")
        from .relay_client import AlreadyRunning

        try:
            from .mcp_bridge import Services

            asyncio.run(run_forever(url, token, services=Services(cfg.servers)))
        except AlreadyRunning as exc:
            print(f"winhand: {exc}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            pass
        return 0
    if cmd == "mcp":
        from . import config

        cfg = config.load()
        if args.action == "list":
            for entry in cfg.servers:
                target = entry.url or " ".join([entry.command, *entry.args])
                print(f"{entry.name:<20} {'on ' if entry.enabled else 'off'} {entry.kind:<5} {target}")
            for c in config.import_candidates():
                if c["name"] not in {e.name for e in cfg.servers}:
                    print(f"{c['name']:<20} (can be imported from {c['source']})")
            return 0
        if args.action == "import":
            cfg, added = config.import_servers(args.names or None, cfg)
            path = config.save(cfg)
            print(f"imported {len(added)} server(s): {', '.join(added) or '-'} -> {path}")
            return 0
        from .mcp_bridge import probe

        chosen = [e for e in cfg.servers if not args.names or e.name in args.names]
        for entry in chosen:
            result = asyncio.run(probe(entry))
            tools = ", ".join(t["name"] for t in result.get("tools", []))
            status = "ok" if result["ok"] else f"FAILED: {result.get('error')}"
            print(
                f"{entry.name}: {status} ({result['duration_ms']} ms) {len(result.get('tools', []))} tools {tools}"
            )
        return 0
    if cmd == "doctor":
        from . import config, proc

        info = proc.sys_info()
        info["config"] = str(config.config_path())
        info["local_mcp_servers"] = [s.name for s in config.load().servers]
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if cmd == "profiles":
        from .profiles import list_profiles

        for p in list_profiles():
            needs = f"  vars: {', '.join(p['vars'])}" if p["vars"] else ""
            print(f"{p['name']:<18} [{p['transport']}] {p['description']}{needs}")
        return 0
    if cmd == "desktop-backend":
        from .desktop_backend import main as desktop_main

        return desktop_main()
    if cmd == "secret-dialog":
        if args.check:
            import tkinter  # noqa: F401

            print(f"tk {tkinter.TkVersion}")
            return 0
        from .secret_prompt import dialog_main

        return dialog_main(args.payload)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
