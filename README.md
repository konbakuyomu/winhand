# winhand

让 AI 通过**一个 MCP** 接管一台 Windows 电脑。它能驱动真终端和各种交互式程序，包括调试器、仿真器、REPL、串口和网络控制台、menuconfig 这类全屏界面、需要登录或确认的安装程序；同时提供文件读写、进程管理，还能把本机已有的其他 MCP 统一转发出去。

```
claude.ai / Claude Desktop / Codex ──MCP──▶ winhand（跑在你的电脑上）
                                             ├─ 通用会话引擎：pty | pipe | serial | tcp
                                             ├─ 文件 / 进程 / 系统信息
                                             └─ 网关：pyocd-debug、usb-camera … 等本地 stdio MCP
```

> 云端中转（Cloudflare Workers + OAuth，用于从 claude.ai 远程连接）在 `relay/` 中开发，尚未完成。

## 为什么要自己写

通用 MCP 在“交互式、有状态、会卡在某个等待点”的程序上都不好用，例如：

- `continue` 之后什么也不输出、一直等断点命中的 gdb
- 要求输入两步验证码的 npm
- 只能靠方向键操作的 menuconfig
- 会随时打印日志的串口 msh

winhand 把这些都当作**会话**，用同一套模型管理：

| 能力 | 说明 |
|---|---|
| 真终端 | Windows 用 ConPTY（pywinpty），其他平台用 openpty。程序以为有人在屏幕前，所以提示符、颜色、密码提示、全屏界面都和人工操作时一模一样 |
| 多种传输 | `pty`（默认）、`pipe`（普通子进程）、`serial`（COM 口，也支持 pyserial 的 URL 写法）、`tcp`（gdbserver 端口、RTT、OpenOCD 4444 端口、QEMU monitor，telnet 协商会自动处理） |
| 状态推断 | 每次调用都返回 `state`、`reason`（判断依据）和 `next`（下一步建议）。状态分为：<br>`running`：正在输出<br>`awaiting_input`：提示符、确认问题或分页器在等输入<br>`needs_user`：要密码、验证码或浏览器登录，必须由人来处理<br>`idle`：安静但没有提示符<br>`blocked`：长时间无响应<br>`exited`：已退出 |
| 通用等待 | `session_wait` 可以同时设多个条件（正则、状态、静默、屏幕稳定、输出量），并且能**跨多个会话同时等**。遇到 `needs_user` 或程序退出，一定会立刻返回。单次最多等 90 秒，超时不算失败，再调用一次就能接着等 |
| 虚拟屏幕 | `session_screen` 返回整屏文字、光标位置、反色（选中）行，menuconfig、htop、安装向导都能“看见” |
| 游标读取 | 输出按绝对位置编号，读过的内容不会重复返回，也不会漏读；大段输出会截断并告诉你怎么翻页；完整日志保存在 `~/.winhand/logs` |
| 自动应答 | 可以配置“看到某个提示就自动回答”，比如分页器、`Quit anyway? (y or n)`。永远不会自动回答密码 |
| 保密输入 | `session_prompt_user` 在电脑桌面弹出一个掩码输入框，你输入的密码直接写进会话，AI 看不到 |
| 声明式 profile | 接入一个新工具只需要写一份 TOML 配置，不用写代码 |
| Windows 适配 | 自动补全缺失的 `ComSpec`、`SystemRoot`；统一 UTF-8 编码；关闭分页器；按 `PATHEXT` 找到 `npm.cmd` 这类包装脚本；兼容中文、空格路径和超长路径；自动识别 GBK 文件；编辑文件时保留原有的 CRLF 换行 |

## 安装与使用

需要 [uv](https://docs.astral.sh/uv/)。

```powershell
git clone https://github.com/konbakuyomu/winhand
cd winhand\agent
uv sync
uv run winhand doctor          # 查看它在这台机器上能找到什么
uv run winhand profiles        # 列出内置 profile
uv run winhand import-codex    # 把 ~/.codex/config.toml 里的 MCP 导入网关（可选）
```

接入本地 MCP 客户端（stdio 方式），以 Codex 的 `config.toml` 为例：

```toml
[mcp_servers.winhand]
command = "uv"
args = ["--directory", 'D:\path\to\winhand\agent', "run", "winhand", "stdio"]
```

也可以用 `uv run winhand http --port 8765`，以 streamable HTTP 方式在本机提供服务。

## 工具一览

| 分组 | 工具 |
|---|---|
| 会话 | `session_start` `session_send` `session_wait` `session_read` `session_screen` `session_list` `session_stop` `session_resize` `session_prompt_user` `profile_list` |
| 一次性命令和进程 | `run` `proc_list` `proc_kill` `sys_info` |
| 文件 | `fs_read` `fs_write` `fs_edit` `fs_list` `fs_search` `fs_stat` |
| 网关 | `<名称>_<工具>`，例如 `pyocd-debug_pyocd_probe_list` |
| 其他 | `help`：给 AI 的使用指南 |

## Profile

内置的有：`shell` `python` `gdb` `pyocd-gdbserver` `pyocd-commander` `openocd` `jlink` `rtthread-msh` `serial` `telnet` `menuconfig`。

你也可以在 `~/.winhand/profiles/<名称>.toml` 里新增 profile，或覆盖同名的内置 profile：

```toml
description = "实验室设备的调试控制台"
transport = "tcp"            # pty | pipe | serial | tcp
host = "{host}"
tcp_port = 23
vars = { host = "192.168.1.50" }
prompts = ['^LAB# ?$']
needs_user = ['(?i)login:\s*$']
exit_command = "exit"
init = ["terminal length 0"]
autoreply = [{ pattern = '--More--', keys = ["Space"], submit = false }]
notes = "给 AI 看的使用提示"
```

## 示例：多会话联动调试

```text
session_start(profile="pyocd-gdbserver", args=["-t","hc32f460petb"])   -> gdbsrv-1（等到端口开始监听）
session_start(profile="gdb", args=["fw.axf"])                           -> gdb-2（已执行 set pagination off 等初始化命令）
session_send("gdb-2", "target extended-remote :3333", submit=True)
session_send("gdb-2", "break uart_rx_done", submit=True)
session_send("gdb-2", "continue", submit=True)                          -> 状态 idle，目标板在运行
session_start(profile="rtthread-msh", vars={"port":"COM5"})             -> msh-3
session_send("msh-3", "uart_test", submit=True)
session_wait(["gdb-2","msh-3"], patterns=['^\(gdb\) ?$', 'PANIC|assert'], timeout_s=90)
  -> hit: {session: "gdb-2", condition: "pattern"}，同时返回两个会话各自的新输出
```

## 开发

```bash
cd agent
uv run pytest -q          # 用一个模拟交互程序做端到端测试（提示符、密码、浏览器验证、分页器、TUI、串口回环、TCP）
uv run ruff check src tests
```

CI 会在 Ubuntu 和 Windows 上同时运行。
