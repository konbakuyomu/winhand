# WinHand

> **读者：** 第一次接手 WinHand 的开发者：它现在是什么、一次典型使用怎样发生、各块能力在哪、边界在哪。
> 参数和用法的完整说明在 `README.md`，给 AI 的完整指南是 `help()`。这里写的是“为什么这样、改的时候要守住什么”。目标全景在 `vision/`（还是骨架）。

---

## 这个项目是什么

WinHand 让 AI 通过一个 MCP 服务操作一台 Windows 电脑：跑命令和长任务，驱动调试器、串口这类交互程序，读写并在沙箱和电脑之间传文件，操作桌面程序；同时把电脑主人写给 AI 的规矩、技能和守卫规则交给每个连上来的客户端，主人在电脑前时 AI 给主人让路。

连上来的客户端有两类：

- **本机客户端**：这台电脑上的 Codex、Claude Desktop、Claude Code，用 stdio 或本机 HTTP 直连。
- **经中转的客户端**：claude.ai 连接器和云端 Claude Code 会话，经中转（Cloudflare Worker）到达电脑。电脑主动用 WebSocket 连出去，不开任何入站端口。

两类客户端用的是同一套工具，只在等待上限这类地方有区别。

## 一次典型使用

云端会话里的 AI 要在电脑上改一个固件项目：

1. 先调 `context(path)`，拿到这个目录适用的规矩、技能和守卫规则。
2. 用 `run` 编译。编译超过 40 秒时，`run` 先回一个编号，AI 用 `job_status` 一段段地等（见 [长任务与等待](waiting.md)）。
3. 编译产物用一次性链接下载到自己的沙箱里分析，改好的补丁再用链接传回去（见 [文件进出电脑](files.md)）。
4. 烧录工具只有图形界面时，用 `ui` 按名字点按钮。第一次操作这个程序前，电脑上弹窗问主人（见 [桌面](desktop.md)）。
5. 主人这时正在用鼠标，AI 的点击先等主人停手。主人觉得不对就按 Ctrl+Alt+Shift+Backspace，AI 的桌面操作停一分钟。

## 能力地图

| 想做的事 | 用什么 | 深入读 |
|---|---|---|
| 跑命令、等长任务、知道任务卡没卡住 | `run`、`job_start`、`job_status` | [waiting.md](waiting.md) |
| 驱动交互程序：gdb、串口、REPL、menuconfig、要登录的安装程序 | 会话 `session_*`、profile | README「为什么要自己写」「Profile」 |
| 读写文件，在沙箱和电脑之间传文件，操作别的服务器 | `fs_*`、一次性链接、`remote_run`、`remote_copy` | [files.md](files.md) |
| 操作桌面程序 | `ui`、`ocr`、`input`、`screenshot`、`window` | [desktop.md](desktop.md) |
| 拿到主人写的规矩和技能，动作前按规则裁决 | `context`、`skill`、守卫、本机 hooks | README「本机规则与守卫」 |
| 用密钥但看不到它，结果里不泄露秘密 | `secrets=[…]`、`secret_prompt`、脱敏 | README「工具一览」的秘密一行、「输出脱敏」 |
| 用电脑上已有的其他 MCP 服务 | `mcp_tools`、`mcp_call`、`/mcp/<名称>` | README「本机的其他 MCP 服务」 |

上面三块有单独的页，其余几块在这里说清要点：

- **交互程序（会话）。** 调试器、串口控制台、REPL 要来回对话，还常常停在一个等待点不出声。WinHand 把它们都当作会话：
  - 用真终端（ConPTY）启动，程序以为有人在屏幕前；也能接 COM 口和 TCP 端口。
  - 每次调用都给出状态（`running`、`awaiting_input`、`needs_user`、`idle`、`blocked`、`exited`）和下一步建议。`needs_user` 是要密码、验证码或浏览器登录，必须交给人，AI 不能编。
  - 输出按绝对位置编号，读过的不重复，也不漏。
  - 接一个新工具只要写一份 TOML profile。
- **规矩、技能与守卫。**
  - `context(path)` 按 Codex 和 Claude Code 各自的读法，返回这台电脑上的规矩、技能，以及对这个目录生效的守卫规则。云端会话还可以靠 SessionStart hook 在开场就带上这些内容。
  - 会读写电脑的调用，执行前先被描述成一个动作（`command`、`input`、`write`、`edit`、`delete`、`move`、`read`、`mcp`），按 `~/.winhand/guards.toml`（以及项目目录里的 .winhand/guards.toml）裁决：拒绝、问人或附注。
  - `fs_op` 整个删文件夹时，同时按命令判断，看成 `Remove-Item -Recurse`，所以写给命令的“禁止批量删除”规则也管得到它。
  - 本机 Codex、Claude Code 的 hooks，经 WinHand 的调用也照样运行。
- **密钥与脱敏。**
  - 密钥存在电脑上：`secret_prompt` 让人在掩码框里输入，程序用 `run(secrets=[名字])` 拿到值，AI 只知道名字。
  - 所有结果离开电脑前按 `[redact]` 和内置的令牌格式遮挡；写文件的工具拒绝写回遮挡占位符。
- **本机的其他 MCP 服务。** 电脑上已有的 MCP 服务，可以经主连接用 `mcp_call` 调用，也可以各自成为中转上的一个独立地址 `/mcp/<名称>`。

## 三个部分怎样连起来（当前）

```text
claude.ai 连接器 / 云端会话 ──HTTPS + OAuth──▶ 中转（Cloudflare Worker + Durable Object）
                                                 │ 只透明转发 HTTP（含 SSE），不解析 MCP
                                                 ▼ WebSocket（电脑主动连出）
本机客户端 ──stdio / 本机 HTTP──▶ agent（Python，跑在电脑上）──▶ 终端、文件、进程、桌面、本机 MCP 服务
                                    ▲ 启动、更新
                                 桌面端（WinUI 托盘程序）：连接状态、调用时间线、会话
```

- **agent**（`agent/`）：所有工具都在这里。
- **中转**（`relay/`）：负责 OAuth、在线状态、一次性文件链接，以及断线时替正在执行的调用续传。转发的每个请求都加一个 `x-winhand-user` 头，agent 靠它认出经中转的调用。
- **桌面端**（`desktop/`）：日常就用它。安装包由 `vX.Y.Z` 标签触发 `.github/workflows/release.yml` 构建并签名。AI 碰不到 WinHand 自己的窗口，所以更新要主人在桌面端里点。发版和部署的步骤见 `codestable/notes/001-发版与部署.md`。

## 统一语言

- **主人**：电脑前的人，也是程序授权、守卫规则和密钥的所有者。
- **本机客户端 / 经中转的调用**：前者指这台电脑上的 stdio 或本机 HTTP，后者来自 claude.ai 连接器和云端会话。两者的等待上限不同。
- **会话**：一个交互程序的一次运行，带状态和游标。
- **后台任务**：`job_start` 起的任务，独立于 winhand 进程，winhand 重启也不丢。
- **游标**：输出的绝对位置。从同一个游标接着问时，只回新的内容。
- **一次性链接**：中转上的 `/t/<令牌>`，用一次就失效，文件内容不经过对话。
- **暂存目录**：`~/.winhand/scratch`，AI 跨调用存文件的地方，不自动清理。
- **守卫**：`~/.winhand/guards.toml` 和项目目录里 .winhand/guards.toml 的规则，在动作执行前裁决。
- **程序授权**：AI 第一次操作某个程序前，先问主人。
- **让路、急停、边框**：主人在电脑前时，桌面操作的三层保护，见 [desktop.md](desktop.md)。

## 边界与考量

- **不改现有参数的含义。** 新能力用新参数或新取值打开，旧客户端的行为不变。例如等待上限的默认值没有提高，只对能长等的客户端放开。
- **开场说明不超过 4096 字符。** Claude Code 只保留 MCP instructions 的前 4096 个字符，所以开场只放要点，包括已存密钥的名字；完整指南在 `help()`。新功能写进 `help()`、工具说明和 README，不往开场说明里塞。
- **单个结果约 4 万字节。** 按 UTF-8 字节算，一个中文字算 3 字节，接近它的 token 成本。读文件、搜索、命令输出到上限就停，并说明从哪里接着读。
- **中转只转发，不解析 MCP。** 好处是 MCP 协议升级不用改中转；代价是电脑离线时，中转不能替它回答 tools/list 请求。
- **不是沙箱。** 守卫、程序授权、让路都只防误操作，挡不住刻意绕路：`run` 里的程序自己读写什么，守卫管不到。真正要隔离时，用系统权限或单独的账户。
- **长期质量约束：**
  - 可靠性：回复在路上丢了，也能核对和接上（`history()`、游标、任务编号跨重启连续），不让 AI 盲目重发。
  - 信息安全性：密钥的值不进对话；密钥库和程序授权文件，AI 的文件工具读不到也改不了。
  - 安全性：主人随时能叫停 AI 的桌面动作，急停键在 WinHand 一启动就生效。
  - 兼容性：规矩和 hooks 同时按 Codex、Claude Code 两种读法处理；不收进度的客户端仍是 40 秒的等待。

## 想深入时

- 想知道长任务怎么等，为什么经中转每次只能等 40 秒 → [waiting.md](waiting.md)
- 要在沙箱和电脑之间传很多文件或大文件，或者操作服务器 → [files.md](files.md)
- 要改桌面工具，或想弄清 AI 什么时候会停下来等人 → [desktop.md](desktop.md)
- 为什么这样选、否掉了哪些做法 → `.agents/notes/`
- 这些能力是怎样一轮轮做出来、在电脑上怎样实测的 → `codestable/epics/001-x-使用复盘四轮强化/`（已关闭，只作历史和证据）
