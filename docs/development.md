# 开发与命令行

> 从原 README 挪来。部署和接入见 [README](../README.md)，其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [中转](relay.md) · [开发](development.md)。

## 不装桌面端：命令行和本机接入

需要 [uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/konbakuyomu/winhand
cd winhand\agent
uv sync
uv run winhand doctor          # 查看它在这台机器上能找到什么
uv run winhand profiles        # 列出内置 profile
uv run winhand mcp import      # 从 Codex / Claude Desktop / Claude Code 的配置导入本机 MCP 服务（可选）
```

接入本地 MCP 客户端（stdio 方式），以 Codex 的 `config.toml` 为例：

```toml
[mcp_servers.winhand]
command = "uv"
args = ["--directory", 'D:\path\to\winhand\agent', "run", "winhand", "stdio"]
```

也可以用 `uv run winhand http --port 8765`，以 streamable HTTP 方式在本机提供服务。

## 测试

```bash
cd agent
uv run pytest -q          # 用一个模拟交互程序做端到端测试（提示符、密码、浏览器验证、分页器、TUI、串口回环、TCP）
uv run ruff check src tests
```

CI 会在 Ubuntu 和 Windows 上同时运行。

端到端测试（CI 中自动运行）：`relay/test/e2e.py` 在 `wrangler dev` 上模拟 claude.ai，走完注册 → 授权 → 换 token → 经隧道调用 MCP 工具的整个流程。
