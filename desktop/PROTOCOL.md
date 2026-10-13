# 桌面端协议（winhand desktop-backend）

托盘 App 启动打包好的 `backend\winhand.exe desktop-backend`，通过 stdin/stdout 按行收发 JSON（UTF-8，每行一条）。后端不开任何本地监听端口给 App；MCP 的本地 HTTP 端口只给中转隧道使用。

- 后端的 stdout 只输出协议消息，日志写入 `~/.winhand/logs/desktop-backend.log`，stderr 仅作诊断。
- App 关闭 stdin（包括 App 崩溃）后，后端停止隧道和所有会话并退出，不会留下孤儿进程。
- 后端和终端里的 `winhand connect` 共用 `~/.winhand/connect.lock`，同一台机器只会有一个 agent 连接中转。

## 请求与响应

```json
{"id": 1, "method": "initialize", "params": {"protocol_version": 1}}
{"id": 1, "result": {...}}
{"id": 2, "error": {"code": "invalid_url", "message": "..."}}
```

| 方法 | 参数 | 结果 |
| --- | --- | --- |
| `initialize` | `protocol_version: 1`，`connect?: bool`（默认 true，立即连接中转） | `protocol_version` + 快照 |
| `get_state` | — | 快照 |
| `set_relay` | `url`（ws:// 或 wss://），`token?`（留空沿用原令牌） | 保存到 config.toml 并重连，返回快照 |
| `reconnect` | — | `{status}` |
| `disconnect` | — | `{status}`，状态变为 `paused` |
| `stop_session` | `id`，`force?` | 会话停止结果 |
| `mcp_list` | — | 本机 MCP 服务状态 + `candidates[]`（其他客户端里配置、可导入的服务：`name`、`source`、`summary`、`already`，`internal` 表示该客户端自带的内部组件，默认不导入） |
| `mcp_save` | `entry{name,command,args[],env{},cwd,url,headers{},enabled,description,startup_timeout_s,idle_stop_minutes,call_timeout_s?,repair_env?}`，`original_name?`（修改时） | 保存到 config.toml，返回本机 MCP 服务状态。没给 `call_timeout_s`、`repair_env` 时保留原值 |
| `mcp_delete` | `name` | 同上 |
| `mcp_set_enabled` | `name`，`enabled` | 同上；停用会停止正在运行的进程 |
| `mcp_import` | `names?[]`（省略则全部导入） | 同上 + `added[]` |
| `mcp_test` | `name` | 单独启动一份临时进程并列出能力：`ok`、`server{name,version}`、`protocol`、`tools[{name,description}]`、`prompts[]`、`resources`、`error`、`stderr_tail[]`、`duration_ms` |
| `mcp_start` / `mcp_stop` | `name` | 立即启动或停止（平时第一次被调用时自动启动） |
| `apps_get` | — | 程序授权状态（见下） |
| `apps_set_enabled` | `enabled` | 开关“AI 操作程序前先问我”，写入授权文件的 `ask`；返回程序授权状态。授权文件有错时不改写它，错误码 `approvals_file` |
| `apps_allow` | `exe`（如 `notepad.exe`，大小写和路径不限，没有扩展名时补 `.exe`） | 加入“一直允许”（人在设置页操作，不再弹确认框）；返回程序授权状态，另加 `added`（规范化后的 exe 名）和 `caution?`（浏览器、终端等要提醒的话）。`exe` 为空时错误码 `invalid_exe`，授权文件有错时 `approvals_file` |
| `apps_revoke` | `exe` | 同时撤销“一直允许”和“允许到 WinHand 重启”；返回程序授权状态 |
| `shutdown` | — | `{ok: true}`，随后退出 |

快照字段：`version`、`generation`、`paths{home,config,activity,logs}`、`relay{url,has_token}`、`mcp`（见下）、`apps`（见下）、`status`、`counts{ok,error}`、`activity[]`（最近 200 条，含今天的审计日志）、`sessions[]`。

本机 MCP 服务状态（快照里的 `mcp`、`mcp_*` 方法的结果、`mcp` 事件）：`base_url`（`https://<中转>/mcp`，未配置中转时为 null）和 `servers[]`。每个服务：配置字段（名字像密钥的 env/headers 值替换为 `••••••••`；保存时原样传回表示不修改）、`kind`（`stdio` / `http`）、`public_url`（远程客户端添加连接器用的地址）和 `status{state,error,pid,server{name,version},protocol,calls,sessions,started_at,last_used,stderr_tail[],log_file}`，`state` 为 `stopped` / `starting` / `running` / `error`。

程序授权状态（快照里的 `apps`、`apps_*` 方法的结果、`apps` 事件）：AI 用桌面工具点击、输入、操作控件之前，要先经过电脑前的人允许（`agent/src/winhand/approvals.py`）。

- `enabled`：是否先问（授权文件里的 `ask`）；
- `file`：授权文件 `~/.winhand/desktop-apps.toml` 的路径；`error?`：文件坏了时的说明（这时视为开启、没有“一直允许”）；
- `always[{exe,since}]`：一直允许的程序，`since` 为加入时间（ISO 8601）；
- `session[{exe,since}]`：允许到 WinHand 重启的程序，`since` 为 Unix 秒。只记在这个后端进程里：本机客户端各自启动的 winhand 各算各的；
- `denied[{exe,left_s}]`：刚被拒绝、还在 10 分钟冷却里的程序；
- `terminals[]`：算作终端的程序（另外所有控制台窗口都算终端），不在“一直允许”里时直接拒绝；
- `running[{exe,title}]`：现在开着窗口的程序（不含 WinHand 自己），供设置页挑选。

## 事件

```json
{"event": "status", "data": {"state": "online", "since": 1790416846.0, "relay": "winhand.example.com", "generation": "..."}}
```

每个事件的 `data.generation` 与 `initialize` 返回的一致；App 丢弃不一致的事件（例如旧后端残留输出）。

- `status`：连接状态变化。`state` 取值：
  - `unconfigured`：未配置中转
  - `connecting`：正在连接
  - `online`：在线（`connected_at`）
  - `offline`：断线后等待重试（`reason`、`retry_in_s`，重试期间带 `connecting: true`）
  - `refused`：HTTP 401，令牌错误
  - `replaced`：被另一个 agent 接管，不再自动重连
  - `conflict`：终端里已有 `winhand connect`
  - `paused`：手动断开
- `activity`：时间线条目，用 `id` 去重或更新。字段：`id`、`t`（Unix 秒）、`kind`（`tool` / `connection` / `session` / `mcp` / `transfer` / `settings`）、`title`、`status`（`running` / `ok` / `error` 或连接状态）、`args`（截断预览，env/token 已隐藏）、`duration_ms`、`summary`。工具调用先发出 `running`，结束后以同一 `id` 发出 `ok` 或 `error`。只有结束的条目写入 `~/.winhand/activity/YYYY-MM-DD.jsonl`。
- `mcp`：本机 MCP 服务的配置或运行状态变化，`data` 同上。通过这些服务的工具调用也进入 `activity`，`title` 为 `服务名 · 工具名`，并带 `server` 字段。
- `sessions`：会话列表变化（新增、结束、状态或是否有未读输出变化），`data.sessions` 为完整列表。
- `apps`：这个后端里的程序授权变化（人回答了授权弹窗、在设置页增删或开关），`data` 同程序授权状态。命令行 `winhand apps` 或手动改文件不会推送；App 打开设置页时调用 `apps_get` 重新获取。
