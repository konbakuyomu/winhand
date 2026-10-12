# winhand

让 AI 通过**一个 MCP** 接管一台 Windows 电脑。它能驱动真终端和各种交互式程序，包括调试器、仿真器、REPL、串口和网络控制台、menuconfig 这类全屏界面、需要登录或确认的安装程序；同时提供文件读写、进程管理，还能把本机已有的其他 MCP 服务各自作为独立的远程地址提供出去。

```
claude.ai 连接器 ──HTTPS + OAuth──▶ relay（Cloudflare Worker，你的域名）
                                       │  Durable Object 维持会话，透明转发 HTTP（含 SSE）
                                       ▼  WebSocket（电脑主动连出，不开任何入站端口）
Claude Desktop / Codex ──stdio──▶ winhand agent（跑在你的电脑上）
                                   ├─ 通用会话引擎：pty | pipe | serial | tcp
                                   ├─ 文件 / 进程 / 系统信息
                                   └─ 本机 MCP 服务：pyocd-debug、usb-camera … 各自在 /mcp/<名称>
```

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
| 通用等待 | `session_wait` 可以同时设多个条件（正则、状态、静默、屏幕稳定、输出量），并且能**跨多个会话同时等**。遇到 `needs_user` 或程序退出，一定会立刻返回。单次最多等 45 秒，给中转和客户端返回留出余量；超时不算失败，再调用一次就能接着等 |
| 虚拟屏幕 | `session_screen` 返回整屏文字、光标位置、反色（选中）行，menuconfig、htop、安装向导都能“看见” |
| 游标读取 | 输出按绝对位置编号，读过的内容不会重复返回，也不会漏读；大段输出会截断并告诉你怎么翻页；完整日志保存在 `~/.winhand/logs` |
| 自动应答 | 可以配置“看到某个提示就自动回答”，比如分页器、`Quit anyway? (y or n)`。永远不会自动回答密码 |
| 保密输入 | `session_prompt_user` 在电脑桌面弹出掩码输入框，答案直接写进会话。原文回显会在返回、写入虚拟屏幕及日志前遮罩，包括分段和带 ANSI 样式的回显；不能保护程序主动编码或改写后的口令 |
| 声明式 profile | 接入一个新工具只需要写一份 TOML 配置，不用写代码 |
| Windows 适配 | 自动补全缺失的 `ComSpec`、`SystemRoot`；统一 UTF-8 编码；关闭分页器；按 `PATHEXT` 找到 `npm.cmd` 这类包装脚本；兼容中文、空格路径和超长路径；自动识别 GBK 文件；编辑文件时保留原有的 CRLF 换行 |

## 桌面端（推荐的日常用法）

下载 [Releases](https://github.com/konbakuyomu/winhand/releases) 里的 `winhand-win-Setup.exe`，安装后在通知区域运行，不需要 Python 或 .NET。在设置里填好中转地址和设备令牌，打开“登录 Windows 后自动启动”即可。窗口里能实时看到连接状态、Claude 的每次工具调用（时间线）和打开的会话。新版本在后台下载，你点“立即更新”或退出时安装。详见 [desktop/README.md](desktop/README.md)。

安装包使用自签名证书（`CN=winhand`，SHA-256 `711736B2…DBEC2`），想让自己的电脑信任它，见 [Windows 签名与信任](docs/windows-signing.md)。

## 安装与使用

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

## 从 claude.ai 远程接入（relay）

`relay/` 是一个 Cloudflare Worker，职责如下：

- 对 claude.ai 提供标准的 MCP 远程端点，包括 OAuth 2.1（支持动态注册和 CIMD），授权页用“主人口令”校验；
- 电脑上的 `winhand connect` 主动用 WebSocket 连上它，**电脑本身不开任何入站端口**；
- 中转层不解析 MCP 协议，只透明转发 HTTP 请求（包括流式 SSE 响应），以后 MCP 协议升级也不用改它。

部署步骤（需要 Cloudflare 账号，以及一个托管在 Cloudflare 上的域名。`*.workers.dev` 在国内经常连不上）：

```powershell
cd relay
npm ci
npx wrangler login                                   # 浏览器里授权
npx wrangler kv namespace create OAUTH_KV            # 把输出的 id 填进 wrangler.jsonc
#   在 wrangler.jsonc 中填写 PUBLIC_ORIGIN = "https://mcp.你的域名"，并取消 routes 那一行的注释
npx wrangler secret put OWNER_PASSPHRASE             # 授权页口令（要足够长）
npx wrangler secret put AGENT_TOKEN                  # 电脑连中转用的设备令牌（随机长串）
npx wrangler deploy
```

在电脑上连接中转（加上 `--save` 会把地址和令牌写进 `~/.winhand/config.toml`，之后直接运行 `winhand connect` 即可）：

```powershell
cd agent
uv run winhand connect --url wss://mcp.你的域名/agent --token <AGENT_TOKEN> --save
```

最后在 claude.ai 的“设置 → 连接器 → 添加自定义连接器”里填入 `https://mcp.你的域名/mcp`，完成授权后，**新开一个会话**即可使用。打开 `https://mcp.你的域名/` 能看到电脑是否在线。

安全设计：

- 授权页口令用常量时间比较；同一 IP 连续输错 5 次锁定 15 分钟。
- 设备令牌只保存在 Worker secret 里。
- OAuth token 由中转校验，不会转发给电脑。
- 授权页有防 CSRF 的一次性 handle 和绑定 cookie，并禁止被嵌入 iframe。
- 电脑短暂掉线（winhand 重启或升级、网络切换）时，请求会先等它重连，默认最多 20 秒（变量 `AGENT_GRACE_S`），之后才报错。报错是带同一请求 id 的 JSON-RPC 错误，写明离线多久和可能原因，客户端会原样交给模型。
- 一次性文件链接 `https://mcp.你的域名/t/<令牌>` 不走 OAuth，靠令牌本身授权：
  - 令牌是 256 位随机数，只能用一次，默认 10 分钟内有效，最长 30 分钟；
  - 目标路径和大小上限在签发时就固定了，上传可以要求 sha256 一致；
  - 电脑上只在内存里保存令牌的哈希，winhand 重启后所有链接作废；
  - 同一 IP 反复用无效令牌会被暂时拒绝。

### 一次性文件链接（大文件、补丁）

`fs_upload_url` / `fs_download_url` 发出一次性链接，模型在自己的沙箱里用 `curl -T 文件 链接` 上传、`curl -o 名字 链接` 下载，文件内容不经过对话。这样传 100 KB 的补丁只要几秒，不必再分段写 base64。传上来的补丁可以直接交给 `fs_apply_patch(root, patch_file=...)`：它会自动判断 `-p`，保留每个文件原来的编码和 CRLF，所有 hunk 都对得上才写入。

要在 Claude Code 云端会话里用这个功能，得在云环境的网络设置里放行中转域名：打开会话标题栏的云环境菜单 → Edit → Network access，在 Allowed domains 里加上 `mcp.你的域名`，Allow package managers 保持勾选。中转更新后要重新执行 `npx wrangler deploy`。

端到端测试（CI 中自动运行）：`relay/test/e2e.py` 在 `wrangler dev` 上模拟 claude.ai，走完注册 → 授权 → 换 token → 经隧道调用 MCP 工具的整个流程。

## 工具一览

| 分组 | 工具 |
|---|---|
| 会话 | `session_start` `session_send` `session_wait` `session_read` `session_screen` `session_list` `session_stop` `session_resize` `session_prompt_user` `profile_list` |
| 秘密 | `secret_prompt`：在电脑上弹出掩码输入框，让人输入令牌或密码，直接存进文件（可写成 .env 里的一行）或 Windows 凭据管理器，模型拿不到值 |
| 一次性命令和进程 | `run`（约 45 秒内一定回复；没跑完的命令继续运行，用 `job_status(id, wait_s=40)` 等后续输出）`remote_run`（经本机的 ssh 配置在另一台机器上跑脚本）`proc_list` `proc_kill` `sys_info` |
| 文件 | `fs_read` `fs_write` `fs_edit` `fs_list` `fs_find` `fs_search` `fs_stat` `fs_op` |
| 桌面 | `screenshot` `window` `input` `ui` `clipboard` |
| 文件交接 | `fs_send` `fs_write_bytes` `fs_pick` `fs_upload_url` `fs_download_url` `fs_apply_patch`；`fs_read` 也能看图片（`region` 可放大局部）、PDF、Word、PowerPoint、Excel |
| 后台任务 | `job_start` `job_status` `job_stop` |
| 本机规则 | `context`：这台电脑写给 AI 的规矩（按 Codex、Claude Code 的读法：AGENTS.md、CLAUDE.md、`.claude/rules`、`@` 引用）、可用的技能（SKILL.md）、生效的守卫规则和本机 MCP 服务；`repo=owner/name` 可按仓库名找到本机目录。见[本机规则与守卫](#本机规则与守卫) |
| 其他 | `help`：给 AI 的使用指南和本机工具清单；`call`：按名字调用任意 winhand 工具（客户端缓存了旧工具列表时用）；`history`：最近处理过的调用（掉线后核对用） |

`run` 有三种用法：`command` + `args` 直接启动程序，不经过 shell；`script` + `shell`（pwsh、powershell、cmd、python、bash、wsl）把多行脚本写成临时文件执行，引号、`$变量` 和中文都不用转义；只给 `command` 时整行交给 PowerShell 执行。输出默认去掉 ANSI 颜色码，混合 UTF-8 和本地代码页的输出按行解码，退出码按有符号数返回，常见崩溃码会附说明。`lang="en"` 让 dotnet、MSBuild 输出英文。

`job_status` 的 `wait_s`（最多 40 秒）和 `patterns` 会一直等到任务结束、新输出匹配或超时，不用循环轮询；参数相同的并发等待共享一次结果。文件工具的写入是原子的（先写临时文件再替换，保留原文件的 ACL 和属性）；`fs_read` 返回 sha256，传给 `fs_write`/`fs_edit` 的 `expected_sha256` 可以防止覆盖别人刚改过的内容。`fs_op` 删除默认进回收站。

截图和图片按大小预算编码（PNG，过大时改用 JPEG 并逐步缩小），保证能通过客户端的结果大小限制；原始分辨率的截图同时保存在本机（结果里的 `original_file`），需要看清细节时用 `fs_read` 加 `region` 放大那一块。

需要看界面或确认某个程序、对话框、安装程序的状态时，模型应自己调用 `screenshot`，不要让人截图；要点击时优先用 `ui`。`ui(action="wait")` 可以一直等到某个控件出现、消失、变为可用或显示某个值（比如安装程序的"下一步"按钮变为可点），`window(action="wait")` 等某个窗口出现或关闭，最多等 40 秒。按进程名选窗口时，优先选该程序的主窗口，不会选中它的提示框或弹出菜单。

`sys_info(checks=true)` 会做一次环境体检，逐项列出 PowerShell 7、代码页、RedirectionGuard、长路径、Git Bash、WSL、scoop/mise shim、ssh、ripgrep 和非 ASCII 用户目录的状态，每项附修复建议。

`ui` 的操作只执行一次，之后回读控件状态；`verified=true` 表示可观察状态符合预期，`false` 表示未达到，`null` 表示无法确认。填写内容、切换勾选、选择、展开和聚焦可以按控件支持的状态核验；普通按钮被点击不代表它触发的工作已经完成，还应检查结果窗口。`verify_wait_s` 默认 3 秒，最多 10 秒，用于等待回读状态改变。

`remote_run(host, script)` 用这台电脑的 ssh 配置和密钥（包括别名和 ProxyJump）在另一台机器上执行脚本：

- 以非交互方式运行（BatchMode），带保活；脚本从 stdin 送过去，CRLF 会转成 LF。
- `env`、`cwd`、`args` 都会正确转义。
- 失败时 `ssh_error` 会说明原因：连不上、主机指纹不对、密钥被拒，还是脚本本身返回了非零退出码。

需要交互登录时用 `session_start(profile="ssh", vars={"host": ...})`。如果要盯日志，加上 `restart_on_exit`，连接断了会自动重新连上。

### 输出脱敏

在 `~/.winhand/config.toml` 里加一个 `[redact]` 段，所有工具结果在离开电脑之前都会把命中的值换成 `«redacted:种类»`：

```toml
[redact]
values = ["我的域名.example"]
patterns = ['sk-[A-Za-z0-9]{20,}']
ssh_hosts = true      # ~/.ssh/config（含 Include）里所有 HostName
files = ['%LOCALAPPDATA%\vps-fleet\secrets\*.env']   # 这些文件里 KEY=VALUE 的值
ipv4 = false          # 是否遮挡所有 IPv4（回环地址除外）
```

`secret_prompt` 收下的值也会自动遮挡。为了防止把占位符写回文件、冲掉真实值，`fs_write`、`fs_edit`、`fs_apply_patch` 和 `run` 会拒绝含有 `«redacted:` 的内容。本机其他 MCP 服务（`/mcp/<名称>`）不经过脱敏。

短暂断网后，agent 会重连，本机仍存活的会话继续运行。连接恢复时先用 `history` 看刚才的调用是否已经执行（带 run id 和退出码），再用 `session_list` 找原会话，再用 `session_read(since=上次实际收到的 cursor)` 补读；游标未知时可从 `since=0` 开始。丢失回复不能证明操作失败，不要直接重发原命令。agent 进程重启属于另一种情况：交互会话不会自动恢复；需要跨 agent 重启继续的非交互工作用 `job_start`，之后以 `job_status` 查询。

## 本机规则与守卫

任何客户端连上 winhand，都能读到这台电脑写好的规矩和技能，并受这台电脑上的规则约束。这一层与客户端无关，只用标准 MCP；没写规则时什么都不拦。

### context：规矩和技能

`context(path)` 按 Codex 和 Claude Code 各自的读法返回规矩，和它们在这台电脑上实际读到的一致：

- **Codex**：
  - 全局 `~/.codex/AGENTS.override.md`，没有时读 `~/.codex/AGENTS.md`（设了 `CODEX_HOME` 时，从那里读）；
  - 项目根（最近的 `.git` 所在目录）到 `path` 沿途每一级，各取一份：`AGENTS.override.md` 优先，没有时取 `AGENTS.md`。
- **Claude Code**：
  - 管理员策略文件，以及 `~/.claude/CLAUDE.md` 和 `~/.claude/rules/`；
  - 从磁盘根目录到 `path` 沿途每一级的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`；
  - 项目的 `.claude/rules/`：写了 `paths` 的规则只在路径匹配时生效，不生效的列在 `rules_not_applied` 里；
  - 文件里的 `@路径` 引用会展开，最多 4 跳。

同一个文件只返回一次。比如 CLAUDE.md 里写了 `@AGENTS.md`，AGENTS.md 只出现一次。每份规则都标出来自哪种格式（`format`）。

除了规矩，还返回：
- **技能列表**：名称、描述、SKILL.md 路径。默认扫 `~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`，以及项目里的 `.agents/skills`、`.claude/skills`，同名时项目优先；
- 对该目录生效的守卫规则；
- 本机 MCP 服务。

文件都是现读的，改完规矩，下一次调用就生效。模型按技能描述挑中技能后，用 `fs_read` 读 SKILL.md，脚本用 `run` 在这台电脑上执行。

输出经过脱敏，按脱敏后的长度控制在约 6 万字符以内：
- 技能列表最多占约 2 万字符，其余技能只列名字；
- 规矩太长时截断，并给出用 `fs_read` 接着读的方法。

**只知道仓库名时**（比如云端会话只知道 `owner/name`），用 `context(repo="owner/name")`。它在以下位置找这个仓库在本机的检出目录：
- Codex 记录的项目；
- Claude Code 记录的项目；
- `repo_roots` 里列出的文件夹。

再给 `path="src"` 时，`path` 相对于检出目录。

**安全**：`context` 读的每个文件，都先过守卫的 `read` 规则，被拒绝的不读。项目里的规矩引用项目外的文件（例如 `@~/.ssh/id_rsa`）时，只有两种情况才会展开：
- Claude Code 已为这个项目批准过外部引用；
- 设置了 `external_imports = true`。

项目里指向项目外的符号链接（例如 `CLAUDE.md` 链接到别处）也一样。这样，别人的仓库没法借规矩文件读走你的私密文件。

instructions 会提醒模型：开始在某个目录工作前，以及上下文被压缩后，调用 `context`。经中转和 `winhand http` 连接的客户端会看到这句提醒；本机 stdio 客户端默认不显示，因为它们自己会读规则。读哪些文件可以在 `config.toml` 里改：

```toml
[context]
formats = ['codex', 'claude-code']   # 按谁的读法读规矩；['claude-code'] 时与 Claude Code 默认一致
rules = ['D:\notes\agents.md']       # 写了就代替两种格式的全局规则文件；[] = 不读
skills = ['~/.agents/skills', '~/.claude/skills', '~/.codex/skills']
repo_roots = ['D:\code']              # context(repo=...) 还会在这些文件夹和它们的子文件夹里找
external_imports = false             # true：项目规矩引用项目外文件时，不用等 Claude Code 批准
stdio_hint = false                   # true：本机 stdio 客户端也显示 context 提醒
```

Claude Code 只保留 MCP instructions 的前 4096 个字符，所以 instructions 只放要点，完整指南由 `help()` 返回。

### 守卫：动作前先看规则

会读写这台电脑的调用，执行前都会被描述成一个“动作”，按规则裁决。经 `call()` 转发的调用也一样。

| 动作 | 来自 |
|---|---|
| `command` | `run`（含 `stdin` 送进去的内容）、`job_start`、`session_start`、`remote_run`（含参数，`host` 为远程主机） |
| `input` | shell 类会话（pwsh、powershell、cmd、bash、sh、zsh、ssh，或没指定程序的默认 shell）里真正被执行的每一行，见下文。gdb、串口等会话不检查 |
| `write` | `fs_write`、`fs_write_bytes`、`fs_upload_url`、`secret_prompt` 的 `save_to`、`fs_op` 的 copy/move 目标（目标是已有文件夹时，按放进去的那个文件判断）和 mkdir |
| `edit` | `fs_edit`、`fs_apply_patch`（补丁涉及的每个文件）。编辑需要看到内容，所以它们同时算对这些文件的 `read`；补丁文件本身也算 `read` |
| `delete` / `move` | `fs_op` |
| `read` | `fs_read`、`fs_send`、`fs_download_url`、`fs_search`、`fs_op` 的 copy 和 move 来源，以及上面 `edit` 的目标 |

`fs_search` 搜索文件夹时，读取规则拒绝或要求确认的文件不会出现在结果里，结果中的 `guard_hidden` 写明略去了几个文件。

**shell 会话里的一行**：winhand 记住每个 shell 会话里“已输入、还没执行”的内容。之后换行、Enter 键、`submit`、`session_stop`（会输入退出命令并回车）或 `session_prompt_user`（会输入口令并回车）让这一行执行时，守卫判断的是整行。
- **续行**：可能续行的行（以 `\`、反引号、`^`、`|` 等结尾，或引号、括号没闭合），既单独判断，也和后面的行连起来判断，连起来时分别试“直接相连”和“加一个空格”两种拼法；
- **删除和清行**：Backspace 删掉最后一个字符；Ctrl+C 清掉这一行（仅限终端会话，管道会话里它只是一个字符）；
- **无法推断的行**：光标移动、历史记录和历史展开（bash 的 `!!`、`^a^b`）、Tab 补全会让最终执行的内容无法从输入推断。工具出错、按键名写错、口令是否送达不确定、同一会话的输入互相重叠时，winhand 也不再知道已输入了什么。这样的行，任何带 `match` 的 `input` 规则都视为命中，拒绝信息会提示先用 Ctrl+C 清行；
- **排队**：存在 `input` 规则时，同一 shell 会话的输入按顺序判断和发送。没有 `input` 规则的会话、单独的 Ctrl+C 和 `session_stop(force=true)` 不排队。

规则写在 `~/.winhand/guards.toml`（全局），或项目目录下的 `.winhand/guards.toml`（只管该目录及子目录）。winhand 不自带任何规则，下面是示例：

```toml
[[guard]]
on = ["command", "input"]
match = '(?i)(\brm\b[^|;&\n]*\s-\w*r|\b(remove-item|ri|rm|rmdir|rd|del|erase)\b[^|;&\n]*\s-r(e(c(u(r(s(e)?)?)?)?)?)?\b|\b(rd|rmdir|del|erase)\b[^|;&\n]*\s/s\b)'
decision = "deny"
reason = "禁止批量删除：一次只删一个明确路径的文件，或请用户手动删除"

[[guard]]
on = "read"
path = ["**/.env", "**/.env.*", "**/secrets/**"]
decision = "deny"

[[guard]]
on = "delete"
decision = "ask"          # 在电脑上弹窗确认
reason = "删除前先问我"
timeout_s = 40

[[guard]]
on = "edit"
path = ["**/*.c", "**/*.h"]
decision = "note"         # 照常执行，结果里附上这句提醒
reason = "改完记得跑单元测试"

[[guard]]
on = ["write", "edit", "delete", "move"]
path = ["**/.winhand/guards.toml"]
decision = "ask"          # 规则文件本身也值得保护
reason = "要改守卫规则"
```

- **字段**：
  - `on`：动作类型；
  - `match`：在命令行或输入的这一行里搜索的正则；
  - `path`：glob 列表，支持 `**`，Windows 上不区分大小写。项目文件里的相对模式以该项目目录为基准；全局文件里的相对模式匹配任意位置（`*.pem` 等于 `**/*.pem`）；
  - `decision`：`deny`、`ask` 或 `note`；
  - `reason`：拒绝、确认或提醒时显示的说明；
  - `timeout_s`：ask 等待确认的秒数，默认 40，最多 50。客户端等一个工具调用大约 60 秒就放弃，再晚的“是”会让 AI 以为失败的操作其实执行了。
- **裁决**：deny 优先于 ask，ask 优先于 note。没有 allow，所以项目规则只能增加限制，不能放宽全局规则。
- **拒绝**：工具不执行，以工具错误返回，写明规则文件、第几条和理由。这次调用仍出现在 `history` 和桌面端时间线里。
- **请人确认**：在电脑上弹出置顶的确认框，写明工具、动作和理由。默认按钮是“否”，正在别处打字时误按回车也不会放行。没人应答、点“否”或弹不出窗口，都不执行。
- **规则文件写错**：解析失败、字段不认识、正则写错时，这个文件管辖的动作一律拒绝，错误里写明文件和位置；改好后下一次调用即恢复。
- **文件夹**：`secrets/**` 这样的模式同时覆盖文件夹本身，复制或移动整个文件夹也会被拦。
- **路径**：按工具自己的方式解析路径（`fs_*` 不展开环境变量，一次性链接工具会展开），并同时按真实路径判断：符号链接、目录联接、8.3 短名、`\\?\` 前缀、`::$DATA` 这类写法不会绕过路径规则。

参数格式不对的调用不经守卫判断，直接由工具自己报错，什么也不会执行。

### 本机的 Codex、Claude Code hooks

在本机用 Codex 或 Claude Code 时会运行的 PreToolUse hooks，经 winhand 的调用也会运行。例如项目里 `.codex/hooks.json` 中“这个阶段不许改 src/”的守卫，云端会话通过 winhand 改文件时同样生效。

- **只运行这两个工具自己会运行的**：
  - Codex：在 Codex 里审核过（`/hooks`，`trusted_hash` 与现在的内容一致）的 hook，项目里的还要求项目被信任；
  - Claude Code：项目已接受工作区信任时才运行；
  - 管理员下发的 hooks 总是运行。

  没信任、改过、被关掉的不运行。`context(path)` 的 `hooks` 一项列出每个 hook 的状态和原因。
- **hook 看到什么**：动作按那个工具自己的样子交给它。
  - 命令、shell 会话里提交的一行，都是 `Bash`；
  - 写文件：Claude Code 是 `Write`、`Edit`，Codex 是 `apply_patch`；
  - 读文件：Claude Code 是 `Read`。Codex 没有读文件工具，读文件不交给 Codex 的 hook。
- **结论**：
  - 退出码 2 或 deny：拒绝；
  - Claude Code 的 ask：弹窗请你确认；
  - `additionalContext`：作为附注给模型；
  - hook 想改写这次调用（`updatedInput`）：拒绝，并告诉模型按 hook 的意思改了再发；
  - hook 超时、出错：照常执行并附注，与两个工具的做法一致。
- hook 的结论和 `guards.toml` 一起裁决，只会更严：hook 说 allow 也放不过 `guards.toml` 拒绝的动作。

`guards.toml` 也可以把判断交给一个命令，协议与 Claude Code 的 PreToolUse hook 相同：

```toml
[[guard]]
on = ["command", "input"]
command = 'python C:\tools\check_command.py'   # 读 stdin 的 JSON，退出码 2 或输出 deny 即拒绝
```

`command` 只能写在全局的 `~/.winhand/guards.toml` 里。项目里的文件写了，按“写错了”处理：仓库里的文件不能让 winhand 执行命令。

在 `config.toml` 里可以关掉或调整：

```toml
[hooks]
sources = ['codex', 'claude-code']   # [] = 一个都不运行
timeout_s = 30                       # 每个 hook 最多等多久
```

守卫按工具的参数判断，**不是沙箱**。它适合拦住善意的 AI 犯错，挡不住刻意绕路：`run` 里的程序自己去读写什么（例如 `run("type .env")`）、正则没覆盖到的写法、`proc_kill` 和桌面工具，它都管不到。真正需要隔离时，用系统权限或单独的账户。

## 本机的其他 MCP 服务

电脑上已有的 MCP 服务（pyocd-debug、usb-camera、FreeCAD …）不会混进 winhand 的工具列表，而是**各自成为一个独立的远程 MCP 地址**：

```
https://<你的中转域名>/mcp/<名称>
```

在 claude.ai 里把每个地址分别添加为自定义连接器即可（授权一次口令；同一个中转下的地址共用授权）。winhand 原样转发，不改工具名，进度通知、图片、服务端发起的请求都能往返；服务在第一次被调用时启动，崩溃后下一次调用自动重启。服务卡住（请求迟迟没有回应，连心跳 ping 也不回）时，新的调用会立刻得到说明而不是一直等待；卡住超过 2 分钟会自动重启，托盘里显示为“无响应”。

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

本机 MCP 服务可以在 `config.toml` 里设置 `call_timeout_s`。单次工具调用超过这个时间，winhand 会取消这次调用并给客户端回一个错误，避免整个服务被一个卡住的调用拖住。

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
session_wait(["gdb-2","msh-3"], patterns=['^\(gdb\) ?$', 'PANIC|assert'], timeout_s=50)
  -> hit: {session: "gdb-2", condition: "pattern"}，同时返回两个会话各自的新输出
```

## 开发

```bash
cd agent
uv run pytest -q          # 用一个模拟交互程序做端到端测试（提示符、密码、浏览器验证、分页器、TUI、串口回环、TCP）
uv run ruff check src tests
```

CI 会在 Ubuntu 和 Windows 上同时运行。
