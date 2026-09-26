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
| `mcp_list` | — | 本机 MCP 服务状态 + `candidates[]`（其他客户端里配置、可导入的服务：`name`、`source`、`summary`、`already`） |
| `mcp_save` | `entry{name,command,args[],env{},cwd,url,headers{},enabled,description,startup_timeout_s,idle_stop_minutes}`，`original_name?`（修改时） | 保存到 config.toml，返回本机 MCP 服务状态 |
| `mcp_delete` | `name` | 同上 |
| `mcp_set_enabled` | `name`，`enabled` | 同上；停用会停止正在运行的进程 |
| `mcp_import` | `names?[]`（省略则全部导入） | 同上 + `added[]` |
| `mcp_test` | `name` | 单独启动一份临时进程并列出能力：`ok`、`server{name,version}`、`protocol`、`tools[{name,description}]`、`prompts[]`、`resources`、`error`、`stderr_tail[]`、`duration_ms` |
| `mcp_start` / `mcp_stop` | `name` | 立即启动或停止（平时第一次被调用时自动启动） |
| `shutdown` | — | `{ok: true}`，随后退出 |

快照字段：`version`、`generation`、`paths{home,config,activity,logs}`、`relay{url,has_token}`、`mcp`（见下）、`status`、`counts{ok,error}`、`activity[]`（最近 200 条，含今天的审计日志）、`sessions[]`。

本机 MCP 服务状态（快照里的 `mcp`、`mcp_*` 方法的结果、`mcp` 事件）：`base_url`（`https://<中转>/mcp`，未配置中转时为 null）和 `servers[]`。每个服务：配置字段（名字像密钥的 env/headers 值替换为 `••••••••`；保存时原样传回表示不修改）、`kind`（`stdio` / `http`）、`public_url`（远程客户端添加连接器用的地址）和 `status{state,error,pid,server{name,version},protocol,calls,sessions,started_at,last_used,stderr_tail[],log_file}`，`state` 为 `stopped` / `starting` / `running` / `error`。

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
- `activity`：时间线条目，用 `id` 去重或更新。字段：`id`、`t`（Unix 秒）、`kind`（`tool` / `connection` / `session` / `mcp`）、`title`、`status`（`running` / `ok` / `error` 或连接状态）、`args`（截断预览，env/token 已隐藏）、`duration_ms`、`summary`。工具调用先发出 `running`，结束后以同一 `id` 发出 `ok` 或 `error`。只有结束的条目写入 `~/.winhand/activity/YYYY-MM-DD.jsonl`。
- `mcp`：本机 MCP 服务的配置或运行状态变化，`data` 同上。通过这些服务的工具调用也进入 `activity`，`title` 为 `服务名 · 工具名`，并带 `server` 字段。
- `sessions`：会话列表变化（新增、结束、状态或是否有未读输出变化），`data.sessions` 为完整列表。
