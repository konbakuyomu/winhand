"""In-memory desktop protocol backend for isolated UI previews."""

import datetime
import json
import ntpath
import os
import re
import sys
import time
import uuid
from urllib.parse import urlsplit

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
scenario = os.environ.get("WINHAND_PREVIEW_SCENARIO", "default")
if scenario not in ("default", "empty"):
    raise ValueError(f"Unknown preview scenario: {scenario}")
now = time.time()
generation = uuid.uuid4().hex
home = r"C:\Users\demo\.winhand"
iso = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(timespec="seconds")
state = {
    "version": "0.3.5-preview", "generation": generation,
    "paths": {"home": home, "config": home + r"\config.toml",
              "activity": home + r"\activity", "logs": home + r"\logs"},
    "relay": {"url": None, "has_token": False},
    "status": {"state": "unconfigured", "since": now, "generation": generation},
    "counts": {"ok": 0, "error": 0}, "activity": [], "sessions": [],
    "secrets": {"file": home + r"\secrets.env", "items": []},
    "apps": {"enabled": True, "file": home + r"\desktop-apps.toml", "always": [],
             "session": [], "denied": [], "terminals": [], "running": []},
    "mcp": {"base_url": None, "servers": []},
}
secret_values = {}
candidates = []


def server_entry(name, runtime="stopped"):
    base = state["mcp"]["base_url"]
    return {
        "name": name, "command": "demo-mcp.exe", "args": ["--preview"],
        "env": {"DEMO_MODE": "preview"}, "cwd": home + r"\demo", "url": None,
        "headers": {}, "enabled": True, "description": "演示用本机 MCP 服务",
        "startup_timeout_s": 60, "idle_stop_minutes": 15,
        "call_timeout_s": 120, "repair_env": False, "kind": "stdio",
        "public_url": f"{base}/{name}" if base else None,
        "status": {"state": runtime, "error": None,
                   "pid": 4200 if runtime == "running" else None,
                   "server": {"name": name, "version": "1.0.0"},
                   "protocol": "2025-03-26", "calls": 18, "sessions": 2,
                   "started_at": now - 360, "last_used": now - 25,
                   "stderr_tail": [], "log_file": home + f"\\logs\\{name}.log"},
    }


if scenario == "default":
    state["relay"] = {"url": "wss://relay.example.com/agent", "has_token": True}
    state["status"].update(state="online", relay="relay.example.com", since=now - 420,
                           connected_at=now - 420)
    state["counts"] = {"ok": 165, "error": 2}
    titles = ["fs_edit", "run", "job_status", "screenshot", "ui",
              "已连接到中转", "打开了 PowerShell 会话", "demo-files · list_files",
              r"经一次性链接上传了 C:\Users\demo\report.zip", "fs_read"]
    kinds = ["tool"] * 5 + ["connection", "session", "mcp", "transfer", "tool"]
    for i in range(30):
        status = "error" if i in (7, 17) else "running" if i == 29 else "ok"
        state["activity"].append({
            "id": f"preview-{i + 1:02}", "t": now - (30 - i) * 11,
            "kind": kinds[i % 10], "title": titles[i % 10], "status": status,
            "args": {"command": "Get-ChildItem C:\\Users\\demo\\project …"},
            "duration_ms": [4, 38, 175, 830, 2400, 12500, 42600][i % 7],
            "summary": "演示任务失败：等待响应超时" if status == "error" else "已完成演示操作",
        })
    state["sessions"] = [
        {"id": "serial-demo", "transport": "serial", "state": "needs_user",
         "port": "COM7", "baudrate": 115200, "profile": "serial-monitor",
         "reason": "请在设备上确认，然后继续", "cwd": home + r"\demo",
         "pid": None, "unread_chars": 128, "log_file": home + r"\logs\serial-demo.log"},
        {"id": "powershell-demo", "transport": "pty", "state": "running",
         "argv": ["pwsh.exe", "-NoLogo"], "profile": "powershell",
         "reason": "正在运行演示命令", "cwd": home + r"\demo", "pid": 4300,
         "unread_chars": 256, "log_file": home + r"\logs\powershell-demo.log"},
    ]
    names = ["API_TOKEN", "BUILD_KEY", "CACHE_KEY", "CLOUD_TOKEN", "DB_PASSWORD",
             "DEPLOY_KEY", "MAIL_KEY", "MAPS_KEY", "REGISTRY_TOKEN", "SIGN_KEY",
             "STORAGE_KEY", "WEBHOOK_KEY"]
    secret_values = {"DEMO_" + name: "x" * (20 + i * 8) for i, name in enumerate(names)}
    state["secrets"]["items"] = [{"name": n, "chars": len(v)} for n, v in sorted(secret_values.items())]
    apps = state["apps"]
    apps["always"] = [{"exe": exe, "since": iso} for exe in
                      ["notepad.exe", "explorer.exe", "msedge.exe", "code.exe", "calc.exe",
                       "mspaint.exe", "devenv.exe", "winword.exe", "excel.exe"]]
    apps["session"] = [{"exe": "photos.exe", "since": now - 180}]
    apps["denied"] = [{"exe": "demo-editor.exe", "left_s": 372}]
    apps["terminals"] = ["cmd.exe", "pwsh.exe", "powershell.exe", "windowsterminal.exe"]
    apps["running"] = [{"exe": exe, "title": title} for exe, title in
                       [("notepad.exe", "演示笔记"), ("explorer.exe", "演示项目"),
                        ("msedge.exe", "演示页面"), ("code.exe", "preview — Code"),
                        ("photos.exe", "演示图片"), ("demo-editor.exe", "演示编辑器")]]
    state["mcp"]["base_url"] = "https://relay.example.com/mcp"
    state["mcp"]["servers"] = [server_entry(n, s) for n, s in
                               [("demo-files", "running"), ("demo-docs", "stopped"),
                                ("demo-camera", "starting"), ("demo-build", "error"),
                                ("demo-notes", "running")]]
    state["mcp"]["servers"][3]["status"].update(
        error="演示错误：服务握手超时",
        stderr_tail=["[preview] starting demo-build", "[preview] waiting for handshake",
                     "[preview] handshake timed out after 60s"])
    candidates = [{"name": "demo-files", "source": "Demo Client", "summary": "演示文件服务",
                   "already": True, "internal": False},
                  {"name": "demo-calendar", "source": "Demo Client", "summary": "演示日历服务",
                   "already": False, "internal": False}]


def emit(message):
    print(json.dumps(message, ensure_ascii=False), flush=True)


def handle(method, params):
    events = []
    if method in ("initialize", "get_state"):
        if method == "initialize":
            if params.get("protocol_version") != 1:
                raise ValueError("protocol", "只支持协议版本 1")
            if not params.get("connect", True) and state["relay"]["url"]:
                state["status"]["state"] = "paused"
            events.append(("status", state["status"]))
        return ({"protocol_version": 1, **state} if method == "initialize" else state), events
    if method in ("set_relay", "reconnect", "disconnect"):
        if method == "set_relay":
            url = params["url"]
            parsed = urlsplit(url)
            if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
                raise ValueError("invalid_url", "中转地址必须是 ws:// 或 wss:// 地址")
            state["relay"].update(url=url, has_token=bool(params.get("token")) or state["relay"]["has_token"])
            state["mcp"]["base_url"] = ("https" if parsed.scheme == "wss" else "http") + "://" + parsed.netloc + "/mcp"
            for server in state["mcp"]["servers"]:
                server["public_url"] = state["mcp"]["base_url"] + "/" + server["name"]
        status = state["status"]
        status.update(state="paused" if method == "disconnect" else "online" if state["relay"]["url"] else "unconfigured",
                      since=time.time(), relay=urlsplit(state["relay"]["url"] or "").hostname)
        if status["state"] == "online":
            status["connected_at"] = status["since"]
        events.append(("status", status))
        return state if method == "set_relay" else {"status": status}, events
    if method == "stop_session":
        session = next(s for s in state["sessions"] if s["id"] == params["id"])
        if params.get("force"):
            state["sessions"].remove(session)
        else:
            session.update(state="exited", reason="已停止演示会话", pid=None)
        return {"id": params["id"], "stopped": True}, [("sessions", {"sessions": state["sessions"]})]
    if method.startswith("secrets_"):
        name = params.get("name", "")
        if method == "secrets_save":
            original = params.get("original_name")
            value = params.get("value") or secret_values.get(original, "")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise ValueError("secrets", "名字只能包含字母、数字和下划线，不能以数字开头")
            if name in secret_values and name != original:
                raise ValueError("secrets", "这个名字已经存在")
            if not value or "\n" in value or "\r" in value:
                raise ValueError("secrets", "值不能为空，也不能有多行")
            if original and original != name:
                del secret_values[original]
            secret_values[name] = value
        elif method == "secrets_delete":
            if name not in secret_values:
                raise ValueError("secrets", "没有这个名字")
            del secret_values[name]
        elif method == "secrets_reveal":
            if name not in secret_values:
                raise ValueError("secrets", "没有这个名字")
            return {"name": name, "value": "demo-not-a-real-secret-0001"}, events
        elif method == "secrets_hide":
            return {"ok": True}, events
        elif method != "secrets_list":
            raise ValueError("method", "未知方法：" + method)
        secrets = state["secrets"]
        secrets["items"] = [{"name": n, "chars": len(v)} for n, v in sorted(secret_values.items())]
        if method != "secrets_list":
            events.append(("secrets", secrets))
        return {**secrets, **({"saved": name} if method == "secrets_save" else {})}, events
    if method.startswith("apps_"):
        apps = state["apps"]
        if method == "apps_set_enabled":
            apps["enabled"] = params["enabled"]
        elif method in ("apps_allow", "apps_revoke"):
            exe = ntpath.basename(params["exe"].strip()).lower()
            if not exe:
                raise ValueError("invalid_exe", "请输入程序名")
            if not exe.endswith(".exe"):
                exe += ".exe"
            for group in ("always", "session"):
                apps[group] = [a for a in apps[group] if a["exe"] != exe]
            if method == "apps_allow":
                apps["always"].append({"exe": exe, "since": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")})
        elif method != "apps_get":
            raise ValueError("method", "未知方法：" + method)
        if method != "apps_get":
            events.append(("apps", apps))
        return {**apps, **({"added": exe} if method == "apps_allow" else {})}, events
    if method.startswith("mcp_"):
        mcp = state["mcp"]
        servers = mcp["servers"]
        if method == "mcp_list":
            return {**mcp, "candidates": candidates}, events
        if method == "mcp_save":
            entry = params["entry"]
            original = params.get("original_name", entry["name"])
            old = next((s for s in servers if s["name"] == original), None)
            saved = {**(old or server_entry(entry["name"])), **entry}
            saved["kind"] = "http" if saved.get("url") else "stdio"
            saved["public_url"] = mcp["base_url"] + "/" + saved["name"] if mcp["base_url"] else None
            if old:
                servers[servers.index(old)] = saved
            else:
                servers.append(saved)
        elif method == "mcp_import":
            added = []
            for candidate in candidates:
                if candidate["name"] in params.get("names", [c["name"] for c in candidates if not c["internal"]]) and not candidate["already"]:
                    servers.append(server_entry(candidate["name"]))
                    added.append(candidate["name"])
        elif method in ("mcp_delete", "mcp_set_enabled", "mcp_test", "mcp_start", "mcp_stop"):
            server = next(s for s in servers if s["name"] == params["name"])
            status = server["status"]
            if method == "mcp_delete":
                servers.remove(server)
            elif method == "mcp_test":
                ok = status["state"] != "error"
                return {"ok": ok, "server": status["server"], "protocol": status["protocol"],
                        "tools": [{"name": "demo_list", "description": "列出演示内容"}] if ok else [],
                        "prompts": [], "resources": 0, "error": status["error"],
                        "stderr_tail": status["stderr_tail"], "duration_ms": 42}, events
            else:
                if method == "mcp_set_enabled":
                    server["enabled"] = params["enabled"]
                if method != "mcp_set_enabled" or not server["enabled"]:
                    running = method == "mcp_start"
                    status.update(state="running" if running else "stopped", pid=4200 if running else None,
                                  error=None, stderr_tail=[], started_at=time.time() if running else None)
        else:
            raise ValueError("method", "未知方法：" + method)
        for candidate in candidates:
            candidate["already"] = any(s["name"] == candidate["name"] for s in servers)
        return {**mcp, **({"added": added} if method == "mcp_import" else {})}, [("mcp", mcp)]
    if method == "shutdown":
        return {"ok": True}, events
    raise ValueError("method", "未知方法：" + method)


for line in sys.stdin:
    request = json.loads(line)
    try:
        result, events = handle(request["method"], request.get("params") or {})
    except ValueError as error:
        code, message = error.args
        emit({"id": request["id"], "error": {"code": code, "message": message}})
        continue
    emit({"id": request["id"], "result": result})
    for event, data in events:
        emit({"event": event, "data": {**data, "generation": generation}})
    if request["method"] == "shutdown":
        break
