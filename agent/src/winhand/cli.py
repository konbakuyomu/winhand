"""winhand command line.

winhand stdio                 serve MCP over stdio (Claude Desktop, Codex, Claude Code ...)
winhand http [--port 8765]    serve MCP over streamable HTTP on localhost
winhand connect               connect out to the relay so claude.ai can reach this machine
winhand mcp list|import|test  local MCP servers offered at <relay>/mcp/<name>
winhand bridge init|token     cloud Claude Code sessions start with this machine's rules
winhand apps [allow|revoke X|on|off] programs the AI may operate on the desktop (clicks, keys, controls)
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
    bridge = sub.add_parser("bridge", help="cloud Claude Code sessions start with this machine's rules")
    bridge.add_argument("action", choices=["init", "token"])
    bridge.add_argument("--relay", help="relay URL (default: [relay] url in config.toml)")
    bridge.add_argument(
        "--write", metavar="REPO", help="init: merge the hook into REPO/.claude/settings.json"
    )
    apps = sub.add_parser("apps", help="programs the AI may operate on the desktop")
    apps.add_argument("action", nargs="?", choices=["list", "allow", "revoke", "on", "off"], default="list")
    apps.add_argument("exe", nargs="?", help="the program's file name, e.g. notepad.exe")
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

        build_server(local_stdio=True).run(transport="stdio", show_banner=False)
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
    if cmd == "bridge":
        from . import config, session_bridge

        if args.action == "token":
            print(session_bridge.token_text(session_bridge.new_token()))
            return 0
        url = args.relay or config.load().relay_url
        if not url:
            parser.error("no relay: pass --relay https://<relay host> or set [relay] url in config.toml")
        try:
            origin = session_bridge.relay_origin(url)
        except ValueError as exc:
            parser.error(str(exc))
        if args.write:
            target = os.path.join(args.write, ".claude", "settings.json")
            try:
                session_bridge.merge_settings(target, origin)
            except (ValueError, OSError) as exc:
                print(f"winhand: {exc}", file=sys.stderr)
                return 2
            print(f"已写入 {target}（提交并推送后，云端会话才会用到它）\n")
        else:
            print("放进仓库的 .claude/settings.json（已有内容时合并进去，或用 --write <仓库目录>）：\n")
            print(
                json.dumps(
                    {"hooks": {"SessionStart": [session_bridge.hook_group(origin)]}},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print()
        print(session_bridge.setup_text(origin))
        return 0
    if cmd == "apps":
        return _apps(args.action, args.exe)
    if cmd == "doctor":
        from . import config, proc

        info = proc.sys_info()
        info["config"] = str(config.config_path())
        info["local_mcp_servers"] = [s.name for s in config.load().servers]
        if sys.platform == "win32":
            from . import uia

            info["ui_automation"] = uia.status()
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


def _apps(action: str, exe: str | None) -> int:
    from . import approvals

    if action == "list":
        state = approvals.snapshot()
        print(f"程序授权：{'开启' if state['enabled'] else '关闭'}（winhand apps on | off）")
        print(f"授权文件：{state['file']}")
        if state.get("error"):
            print(f"  文件有错：{state['error']}")
        print("一直允许：" + (", ".join(e["exe"] for e in state["always"]) or "（无）"))
        print("终端（所有控制台窗口，以及下列程序；不在一直允许里时拒绝）：" + ", ".join(state["terminals"]))
        return 0
    if action == "on":
        approvals.set_enabled(True)
        print("程序授权：已开启。AI 第一次操作一个程序前会先问你。")
        return 0
    if not exe and action != "off":
        print(f"winhand apps {action}: give the program's file name, e.g. notepad.exe", file=sys.stderr)
        return 2
    if action == "revoke":
        try:
            print(f"已撤销：{approvals.revoke(exe)}")
        except ValueError as exc:
            print(f"winhand apps revoke: {exc}", file=sys.stderr)
            return 2
        return 0
    from .secret_prompt import PromptUnavailable, ask_confirm

    if action == "off":
        question = (
            "确定关闭程序授权吗？\n\n关闭后，AI 点击、输入、操作任何程序前都不再先问你"
            "（WinHand 自己的窗口和管理员窗口仍然不能操作）。\n随时可以用 winhand apps on 或在设置页重新开启。"
        )
    else:
        try:
            key = approvals.normalize(exe)
        except ValueError as exc:
            print(f"winhand apps allow: {exc}", file=sys.stderr)
            return 2
        question = (
            f"确定让 AI 一直可以操作 {key} 吗？\n\n它将可以点击、输入、操作这个程序的控件和窗口，不再先问你。"
            "\n随时可以在 WinHand 的设置页或用 winhand apps revoke 撤销。"
        )
    try:
        yes = ask_confirm("WinHand：程序授权", question)
    except PromptUnavailable as exc:
        print(f"no answer in the confirmation dialog: {exc}", file=sys.stderr)
        return 1
    if not yes:
        print("没有改动（确认框里选了“否”）")
        return 1
    if action == "off":
        approvals.set_enabled(False)
        print("程序授权：已关闭。")
        return 0
    print(f"已允许：{approvals.allow_always(key)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
