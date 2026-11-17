# 本机的其他 MCP 服务

> 从原 README 挪来。部署和接入见 [README](../README.md)，其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [中转](relay.md) · [开发](development.md)。

电脑上已有的 MCP 服务（pyocd-debug、usb-camera、FreeCAD …）有两种用法。

**经 winhand 主连接调用**（不用另加连接器）：

- `mcp_tools()`：列出启用的服务、运行状态和缓存里的工具，不启动任何服务；
- `mcp_tools("pyocd-debug")`：必要时启动它，列出每个工具的说明和参数（inputSchema），并更新缓存；
- `mcp_call("pyocd-debug", "read_memory", {...})`：必要时启动服务并调用，原样返回文字和图片；服务报错时作为工具错误返回。约 40 秒内没有结果时先返回 `call_id`，用 `mcp_call(call_id=...)` 接着等。`timeout_s`（默认 120 秒）之后放弃这次调用并通知服务取消。

`mcp_call` 和其他工具一样受守卫（动作类型 `mcp`）和本机 hooks 约束；本机 hooks 看到的工具名是 `mcp__<服务>__<工具>`，和 Claude Code、Codex 的命名相同，所以写给它们的 matcher（例如 `mcp__pyocd-debug__.*`）同样生效。守卫拒绝时不会启动服务。`mcp_tools(服务名)` 只列出工具，不受守卫判断：它仍会启动服务。不想让 AI 启动某个服务时，在托盘应用里停用它。

工具清单缓存在 `~/.winhand/cache/mcp-tools.json`，以下三种情况会更新它：任何客户端经下面的独立地址列出工具时、`mcp_tools(服务名)`、托盘应用的“测试”按钮或 `winhand mcp test`。服务的启动命令、参数、地址、工作目录或环境变量改了以后，旧的缓存不再使用。缓存里只有工具清单，没有调用参数和结果。

**各自成为一个独立的远程 MCP 地址**：

```
https://<你的中转域名>/pc/<电脑名称>/mcp/<服务名称>
```

在 claude.ai 里把每个地址分别添加为自定义连接器即可。同一台电脑的这些地址和它的 `/pc/<电脑名称>/mcp` 共用一个登录；别的电脑的登录调不到它们。还用单台电脑旧部署（设着 `AGENT_TOKEN`）的中转，旧地址 `/mcp/<服务名称>` 也照常可用。托盘应用的“MCP 服务”页给出的就是这台电脑的地址。winhand 原样转发，不改工具名，进度通知、图片、服务端发起的请求都能往返；服务在第一次被调用时启动，崩溃后下一次调用自动重启。服务卡住（请求迟迟没有回应，连心跳 ping 也不回）时，新的调用会立刻得到说明而不是一直等待；卡住超过 2 分钟会自动重启，托盘里显示为“无响应”。

在托盘应用的“MCP 服务”页添加、编辑、测试、启停，或从 Codex / Claude Desktop / Claude Code 的配置导入；配置保存在 `~/.winhand/config.toml`：

```toml
[mcp_servers.pyocd-debug]
command = "uv"
args = ["--directory", 'D:\Dev\PYOCD调试MCP', "run", "pyocd-debug-mcp"]
env = { PYOCD_LOG = "info" }

[mcp_servers.docs]            # 本机已经以 HTTP 提供的 MCP 服务，直接转发
url = "http://127.0.0.1:8000/mcp"
```

命令行：`winhand mcp list`、`winhand mcp import [名称…]`、`winhand mcp test [名称…]`。

**Python 环境坏了时自动修好**：用 `uv run` 启动的服务（例如 `uv --directory D:\项目 run 入口`），项目的虚拟环境（`.venv`）记着建它时用的 Python 的完整路径。mise、pyenv 之类的工具升级 Python 后会删掉旧版本，这个环境就坏了：`uv run` 每次都报 `No Python at '…'`，而 uv 自己发现不了。winhand 每次启动这类服务前检查一次：
- 环境指向的 Python 已不存在时，先在项目目录里运行 `uv venv --clear` 和 `uv sync` 重建它（带上启动参数里的 `--locked`、`--frozen`、`--python` 等），再照常启动；
- 修复记在服务日志里，服务状态里也有一句说明；修不好时不启动，错误里写明原因和手动修复的命令；
- 测试按钮、`mcp_tools` / `mcp_call`、经网关的启动都会检查；`sys_info(checks=true)` 也会列出坏掉的环境；
- 修复只重建 `.venv`，不改项目里的其他文件。不想自动修复的服务在 `config.toml` 里写 `repair_env = false`，这时只报出原因和修法。

npx、uvx 启动的服务，以及直接指定某个虚拟环境里的 python.exe 的服务不自动修复；后者启动失败时同样会说明原因。

本机 MCP 服务可以在 `config.toml` 里设置 `call_timeout_s`。单次工具调用超过这个时间，winhand 会取消这次调用并给客户端回一个错误，避免整个服务被一个卡住的调用拖住。
