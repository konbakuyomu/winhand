# daily-use-hardening：补上日常使用里的漏洞和短板（0.7.0）

## Outcome

云端会话通过中转用这台电脑时，少掉线、少绊脚、不漏秘密：连接断一下，正在跑的调用不丢；模型按别的工具习惯传的参数也能用；返回的结果不超过客户端上限；winhand 自己的令牌和口令不会出现在模型看到的内容里；本机端口和公开状态页不再给别人可乘之机。

## 依据

- **电脑上的审计日志**（`~/.winhand/activity/*.jsonl`，2026-09-26 至 10-18，13438 次工具调用，177 次报错）：找不到路径、窗口或仓库 45 次；参数名不对（`file_path`、`-A`、毫秒的 `timeout` 等）37 次；连接中途断开导致调用被取消 31 次；等人在电脑上允许 20 次。后台日志（`desktop-backend.log`）里中转连接断开 471 次，所以断线续传排在最前。
- **之前的 8 个云端会话记录**：逐条看模型在哪里卡住、绕了几次、最后怎么解决的。例：PowerShell 管道给原生程序的文本带 BOM，Codex 收到的提示开头多了乱码；GBK 源码里搜不到中文；`fs_read` 一次返回 6 万字左右，超过 Claude Code 单个结果 2.5 万 token 的上限，整条结果作废。
- **代码安全审查**：令牌泄露途径、守卫绕过、本机端口、状态页。
- **同类项目调研**（用电脑上的 Smart Search）：Windows-MCP、DesktopCommanderMCP、FlaUI-MCP、UFO²、ssh-mcp、Playwright MCP、Anthropic computer use 文档等。采纳了其中最便宜、收益最明确的几项（截图像素上限、history 跨重启、同名目标不瞎点）。

## Scope

连接可靠性
- 断线续传：agent 与中转之间按帧续传，连接断了再连上，正在执行的调用接着回复（fcf5238）。
- 本机端口只认隧道：每次启动随机口令，加 Host/Origin 校验（6187ee4）。

安全
- winhand 自己的令牌和口令一律遮挡；默认识别常见令牌格式；含遮挡值的文件不发出；结尾带点或空格的路径绕不过守卫（c0b7aa5）。
- 公开状态页不再显示电脑名和 Windows 版本（5626ac6）。

日常使用
- 参数名容错（Claude Code / 文本编辑器风格）、`run` 的解释器可以写在 `command`、路径里的环境变量展开、Git Bash 在 scoop 下也找得到、`context(repo=)` 找仓库更准、`job_status` 一次等多个任务（7e2c95b）。
- PowerShell 管道不带 BOM、Windows PowerShell 5.1 输出中文不再是问号；`job_start` 直接跑程序并能看到进度（Codex）；GBK 源码里搜得到中文；改了 `config.toml` 不用重启；终端输出里光标保存/恢复的转义被去掉（98c8dc6）。
- 截图总像素不超过约 1.15 MP；同名文字或控件不再默认点第一个；一次性链接的过期时间带时区（93ef5c1）。
- `history()` 写明每条调用的客户端、标出自己的会话；winhand 重启后列出重启前的调用（0085a23）。
- 单次结果按 UTF-8 字节计预算（中文一个字算 3）：`fs_read`、`fs_list`、`fs_search`、`run`、`job_status`、会话输出、`skill()`（2bcb0b5）。
- `ui` 用鼠标点控件前，确认那个位置是这个程序的窗口（84f0a32）。

## Non-goals

- 发布新版本、部署中转：要用户在电脑上确认后再做。
- 调研里成本较高的几项（控件树带 ref、批量操作、主人接管、示教录制）：记在报告里，这次不做。
- `context()` 的预算仍按字符计（规则是主人写的，完整保留到 6 万字符）。

## Acceptance

- **A1**：连接中途断开再连上，正在执行的调用照常回复（e2e：`a call in flight survives the connection dropping`）。
- **A2**：本机端口不带隧道口令的请求 403，Host 不对 421；经隧道的请求照常。
- **A3**：设备令牌、本机 MCP 服务的令牌、`~/.winhand` 里的口令文件、常见格式的令牌不出现在结果里。
- **A4**：之前会话里失败过的调用方式（Claude Code 风格参数、`command` 写解释器加 `script`、`%USERPROFILE%` 路径、管道传中文）现在都能用，并有测试。
- **A5**：中文文件、长中文文件名目录、大量中文匹配、中文命令输出，单次结果都在预算内，并说明从哪里接着读。
- **A6**：屏幕上有两处相同文字或两个同名控件时，不指定 `index` 就不点击，并列出候选。
- **A7**：原有检查全部通过：agent 测试、中转类型检查和单元测试、`wrangler dev` 上的 e2e。

## Constraints

- 只推送到 `claude/laughing-turing-irotxt` 分支，不开 PR（用户没要求）。
- 中转要在用户电脑上执行 `npx wrangler deploy` 才生效（断线续传、状态页）；agent 要发布 0.7.0 并安装后才生效。两件事都要用户确认。
- Windows 专属的测试（ConPTY、UI Automation、真实 PowerShell）在 CI 的 Windows 机器和用户电脑上跑。
