# 使用指南

> 从原 README 挪来。部署和接入见 [README](../README.md)，其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [中转](relay.md) · [开发](development.md)。

## 整体结构

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

## 会话：驱动交互程序

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
| 通用等待 | `session_wait` 可以同时设多个条件（正则、状态、静默、屏幕稳定、输出量），并且能**跨多个会话同时等**。遇到 `needs_user` 或程序退出，一定会立刻返回。经中转来的调用单次最多等 40 秒，给中转和客户端返回留出余量（本机客户端接收进度时最多 1800 秒，同 `job_status`）；超时不算失败，再调用一次就能接着等 |
| 虚拟屏幕 | `session_screen` 返回整屏文字、光标位置、反色（选中）行，menuconfig、htop、安装向导都能“看见” |
| 游标读取 | 输出按绝对位置编号，读过的内容不会重复返回，也不会漏读；大段输出会截断并告诉你怎么翻页；完整日志保存在 `~/.winhand/logs` |
| 自动应答 | 可以配置“看到某个提示就自动回答”，比如分页器、`Quit anyway? (y or n)`。永远不会自动回答密码 |
| 保密输入 | `session_prompt_user` 在电脑桌面弹出掩码输入框，答案直接写进会话。原文回显会在返回、写入虚拟屏幕及日志前遮罩，包括分段和带 ANSI 样式的回显；不能保护程序主动编码或改写后的口令 |
| 声明式 profile | 接入一个新工具只需要写一份 TOML 配置，不用写代码 |
| Windows 适配 | 自动补全缺失的 `ComSpec`、`SystemRoot`；统一 UTF-8 编码；关闭分页器；按 `PATHEXT` 找到 `npm.cmd` 这类包装脚本；兼容中文、空格路径和超长路径；自动识别 GBK 文件；编辑文件时保留原有的 CRLF 换行 |

## 工具一览

| 分组 | 工具 |
|---|---|
| 会话 | `session_start` `session_send` `session_wait` `session_read` `session_screen` `session_list` `session_stop` `session_resize` `session_prompt_user` `profile_list` |
| 秘密 | `secret_prompt`：在电脑上弹出掩码输入框，让人输入令牌或密码，直接存进文件（可写成 .env 里的一行）或 Windows 凭据管理器，模型拿不到值。只给 `key`（例如 `CLOUDFLARE_API_TOKEN`）时存进本机密钥库 `~/.winhand/secrets.env`，之后 `run`、`job_start` 用 `secrets=['CLOUDFLARE_API_TOKEN']` 把它作为环境变量交给程序，以后的会话也能用，不用再建新密钥。密钥库在结果里一律遮挡（重启后也是），AI 读不到也改不了。AI 一连上就从 winhand 的说明里看到已存的名字（“Stored here: …”，密钥库一变就更新），不用先调 `context()`；对已存的名字再调 `secret_prompt` 时，输入框会提示“已经存在”，点取消即可。电脑前的人在桌面 App 的“设置 → 密钥”里查看名字、添加、修改、改名、删除、显示或复制值（显示期间截图拍不到 App 窗口） |
| 一次性命令和进程 | `run`（约 40 秒内一定回复；没跑完的命令继续运行，用 `job_status(id, wait_s=40)` 等后续输出）`remote_run`（经本机的 ssh 配置在另一台机器上跑脚本）`proc_list` `proc_kill` `sys_info` |
| 文件 | `fs_read` `fs_write` `fs_edit` `fs_list` `fs_find` `fs_search` `fs_stat` `fs_op` |
| 桌面 | `screenshot`（`wait` 等画面变化或停止变化，`path` 存文件）`window` `input` `ui`（`ref`、`steps` 一次做几步）`ocr`（认屏幕上的字）`clipboard` |
| 文件交接 | `fs_send` `fs_write_bytes` `fs_pick` `fs_upload_url` `fs_download_url` `fs_apply_patch`；`fs_read` 也能看图片（`region` 可放大局部）、PDF、Word、PowerPoint、Excel |
| 后台任务 | `job_start` `job_status` `job_stop` |
| 本机规则 | `context`：这台电脑写给 AI 的规矩（按 Codex、Claude Code 的读法：AGENTS.md、CLAUDE.md、`.claude/rules`、`@` 引用）、可用的技能（SKILL.md）、生效的守卫规则和本机 MCP 服务；`repo=owner/name` 可按仓库名找到本机目录。见[规矩与守卫](rules-and-guards.md) |
| 技能和本机 MCP | `skill(name)`：取一个技能的 SKILL.md 和文件清单（相当于 Claude Code 的 Skill 工具）；`mcp_tools` 列出本机 MCP 服务和它们的工具，`mcp_call` 调用其中的工具。见[本机的其他 MCP 服务](local-mcp.md) |
| 其他 | `help`：给 AI 的使用指南和本机工具清单；`call`：按名字调用任意 winhand 工具（客户端缓存了旧工具列表时用）；`history`：最近处理过的调用（掉线后核对用） |

`run` 有三种用法：`command` + `args` 直接启动程序，不经过 shell；`script` + `shell`（pwsh、powershell、cmd、python、bash、wsl）把多行脚本写成临时文件执行，引号、`$变量` 和中文都不用转义（解释器不在这几种里时，把它写在 `command`，例如 `run(command=r'.venv\Scripts\python.exe', script=...)`、`command='node'`）；只给 `command` 时整行交给 PowerShell 执行。PowerShell 的输出和管道统一是不带 BOM 的 UTF-8，Windows PowerShell 5.1 也一样，中文不会变成问号，传给原生程序的文本开头也不会多出 BOM。输出默认去掉 ANSI 颜色码，混合 UTF-8 和本地代码页的输出按行解码，退出码按有符号数返回，常见崩溃码会附说明。`lang="en"` 让 dotnet、MSBuild 输出英文。

`job_status` 的 `wait_s` 和 `patterns` 会一直等到任务结束、新输出匹配或超时，不用循环轮询；参数相同的并发等待共享一次结果。经中转来的调用（claude.ai 连接器、云端会话）一次最多等 40 秒，等不完就带着游标再问：2026-11-01 实测，云端会话经连接器的调用到 60 秒就被切断，即使请求了进度、进度也发出去了。只有这台电脑上的客户端（stdio，或不经中转的 HTTP）、并且接收进度通知时，一次最多等 1800 秒：开始时立刻发一次进度，之后每 15 秒一次。等待中途，任务弹出新窗口（常常是等人点的对话框）或者长时间没有输出也不占 CPU 时，会提前返回，`matched` 分别是 `window` 和 `stalled`。还在跑的任务带 `activity`：进程数、CPU、内存、多久没输出、是否卡住、弹出的窗口；`files=[…]` 顺便给出其他文件（比如布线器自己的日志）的末尾。`id` 也可以是一组 id（后台任务和 `run` 没跑完的命令都行），哪个先结束或先匹配就先返回，`since` 按 id 分别给。从同一个游标接着问时只回新的东西：启动命令和日志路径只在第一次回，stderr 只回上次之后新写的部分（给了 `tail` 时照常回末尾）。`run-N` 的编号记在 `~/.winhand/run-counter`，winhand 重启后接着编，不会重复；每个 `run` 一开始就有 id，回复在路上丢了时，`history()` 里能看到它，`job_status` 也能接着等。任务命令里写了 `exit N` 也照常记下退出码 N（命令单独存成 `command.ps1` 运行；以前 `exit` 会把外层的包装脚本一起结束，任务显示为 vanished）。`job_start` 的 `timeout_s` 到时结束整棵进程树（退出码 -1，`timed_out`）；`after=[任务 id]` 让它等前面的任务结束再开始，用来把几步串起来；不写 `shell` 时，装了 PowerShell 7 就用它（`*>`、Out-File 写出的是 UTF-8，不再是 UTF-16）。`job_start` 给了 `args` 时直接启动程序、不经过 PowerShell，`stdin` 的文本按 UTF-8 交给它（例如 `job_start(command='codex', args=['exec', '-'], stdin=任务说明)`），运行中 `job_status` 能看到它的 stdout 和 stderr。文件工具的写入是原子的（先写临时文件再替换，保留原文件的 ACL 和属性）；`fs_read` 返回 sha256，传给 `fs_write`/`fs_edit` 的 `expected_sha256` 可以防止覆盖别人刚改过的内容（不检查就别写这个参数，空值也当作没写）；`fs_write` 加 `overwrite=false` 表示只新建、文件已存在就拒绝。可选参数不要写成“有名字没有值”：这样的调用在到达 winhand 之前就会被客户端当作无法解析的 JSON 拒绝。`fs_edit` 的多处修改里，新旧文本一样的那处会跳过并注明，不再连累其他修改；同时给了 `old/new` 和 `edits`、而 `old/new` 正好是其中一处时照常执行。`fs_op` 删除默认进回收站；不递归地删一个空文件夹不算批量删除。`fs_write(append=true)` 和 `fs_edit` 跟着文件原来的换行走：CRLF 文件追加的内容也写成 CRLF（看文件末尾 64 KB 定），只差换行的原文照样找得到，提示也直说只差换行。

**等的时候不花回合（云端会话）。** 经中转的 `job_start` 回复、`run` 回 `still_running` 时，多一个 `wake`：一行 bash。把它放进沙箱后台运行（Claude Code 的 Bash `run_in_background`），任务一结束命令就结束，会话随即被叫醒，带回 `{"done":true,"state":...,"exit_code":...}`；等的时候一个回合也不花，不用 `send_later` 定闹钟。`job_status(id, patterns=[...], wake=true)` 给的命令在新输出匹配时也会结束；`job_status(files=[路径], wake=true)` 等那个文件出现或改动；`job_status(wake=true)` 什么都不带时，等电脑重新在线。命令背后是电脑签发的等待链接 `/t/<令牌>`：每次最多挂 50 秒，只回状态，不回输出；默认 12 小时有效，winhand 重启后照样认得。中转不用改。

后台任务的几个参数和回复：

- `job_status()` 不带 id 时，只列最近 20 个任务的状态行（id、名字、状态、退出码、起止时间），不带输出；`prefix='q19-'` 按 id 或名字的开头筛；`files=[...]` 照样给出文件末尾。`job_stop` 只回状态和最后 20 行。
- `run` 和 `job_status` 的回复带 `now`：电脑的本地时间，带时区。
- `job_start(stdin_file=路径)`：把电脑上的一个 UTF-8 文件当 stdin，和 `stdin` 二选一，配 `args` 用；守卫按读这个文件来判断。
- `job_start(keep=true)`：常驻任务。电脑重启、进程被结束，没留下退出码就没了的，桌面端在一分钟内照原来的命令、目录、名字和密钥名再起一个；`job_stop` 停掉的不再拉起。不能配 `stdin`、`elevated`。
- `job_start(quiet_ok_s=秒数)`：这么久没输出对它是正常的，之前不报「卡住」。
- 回过 `still_running` 的 `run-N`，结束后至少留 1 小时，不会被别的命令挤掉；超时结束的任务带 `reason`。
- `sys_info(load=true)`：可用内存、CPU 占用、在跑的后台任务和 `run` 数，起更多任务之前看一眼。

模型常按 Claude Code 内置工具的习惯传参（`file_path`、`old_string`/`new_string`、`-A`/`-B`/`-C`、`head_limit`、按毫秒给的 `timeout` 等）。winhand 把这些换成自己的参数名再执行，结果里用 `args_note` 说明换了什么；认不出的参数，报错时列出这个工具真正的参数。路径里的 `%USERPROFILE%`、`$env:LOCALAPPDATA`、开头的 `~` 都会展开。

任务和命令运行期间电脑不会睡眠（只是不睡，显示器照常可以关）：`run` 由 winhand 自己向系统声明，后台任务由它自己的包装脚本声明，所以 winhand 重启也不影响；做完就撤销，之后照常按空闲时间睡眠。`config.toml` 里写 `[power] keep_awake = false` 可以关掉，单个后台任务可以用 `job_start(keep_awake=false)`。

工具在列表里带着客户端认得的提示：读类工具标 `readOnlyHint`（Claude Code 会把它们并行执行，连接断了还会在新连接上自动补发一次），停止类工具标 `idempotentHint`；`context`、`run`、`job_start`、`job_status`、`job_stop`、`history` 和常用的 `fs_*` 带 `anthropic/alwaysLoad`，会话开始就加载，不用先搜索工具（名单按 11-02 至 11-17 的用量定）。

单次结果有大小上限，按 UTF-8 字节计，约 4 万字节（一个中文字算 3 字节，接近它的 token 成本）。`fs_read`、`fs_list`、`fs_search`、`run`、`job_status` 和会话输出到上限就停，并说明从哪里接着读。以前一次能返回 6 万字左右，中文或密集代码会超过 Claude Code 单个结果 2.5 万 token 的上限，整条结果作废。

截图和图片按大小预算编码（PNG，过大时改用 JPEG 并逐步缩小），保证能通过客户端的结果大小限制；长边不超过 1568 像素、总共不超过约 115 万像素，这是 Claude 不再自行缩小的上限，按截图像素点击才不会偏（以前 1920×1080 的屏幕发出去是 1568×882，会被再缩一次）；原始分辨率的截图同时保存在本机（结果里的 `original_file`），需要看清细节时用 `fs_read` 加 `region` 放大那一块。

需要看界面或确认某个程序、对话框、安装程序的状态时，模型应自己调用 `screenshot`，不要让人截图；要点击时优先用 `ui`。`ui(action="wait")` 可以一直等到某个控件出现、消失、变为可用或显示某个值（比如安装程序的"下一步"按钮变为可点），`window(action="wait")` 等某个窗口出现或关闭，最多等 40 秒。按进程名选窗口时，优先选该程序的主窗口，不会选中它的提示框或弹出菜单。

操作桌面程序有几条路。一条走不通时换下一条，从最稳的开始：

1. **程序自己的命令行、接口或文件**（例如 KiCad 的 kicad-cli）；
2. **`ui` 按名字操作控件**：
   - 用的是 Windows 原生的 UI Automation，在 winhand 进程内调用；
   - 经典 Win32 程序的菜单栏、展开的菜单、树、列表、工具栏也能读到、能操作；
   - 原生接口用不了时，自动退回 PowerShell 脚本，结果里的 `backend` 会写明；
3. **快捷键**：`input(action="keys")`；
4. **`ocr` 认屏幕上的字**：适合没有控件的界面，例如画布、游戏、远程桌面、自绘界面。它用 Windows 自带的 OCR，不需要安装。
   - `ocr(action="read")` 列出每行文字和屏幕位置；
   - `ocr(action="click", text="保存")` 点击那段文字；
   - `ocr(action="wait", text=...)` 等文字出现，`state="gone"` 时等它消失；
   - 查找不区分大小写、全角和半角，忽略空格；
   - 没有完全匹配时，`similar` 给出 OCR 实际读成的相近文字，不会点击；
   - 同一段文字出现在好几处、又没给 `index` 时，列出各处位置，不点击；只有一处独占一行（像按钮）时点那一处。`ui` 遇到多个同名控件也一样，列出候选让模型指定 `index` 或 `automation_id`；
5. **坐标点击**：最后才用，每次点击后截图核对。

`screenshot(wait="change")` 等画面和上一次截图相比有变化再返回；`wait="stable"` 等画面停止变化 `stable_ms` 后再返回。这两种等待都可以代替固定的睡眠，文字光标闪烁不算变化。没给窗口时，`wait="stable"` 只看最前面的窗口：别处在放视频也不影响。仍然等不到时，回复里的 `kept_changing` 写明一直在变的区域和窗口。

截图和输入的几个补充：

- `screenshot(path=...)` 同时把原尺寸的图存进这个文件（`.png`，或 `.jpg`）。这算一次写文件，守卫按写规则判断。
- 截最小化的窗口：先不抢焦点地把它显示出来，截完再放回去；原来是最大化的，还原时仍是最大化。截图的说明里会写上这一点。
- `input` 新增：
  - `modifiers=['ctrl']`：点击、滚动、拖动时按住修饰键；
  - `triple_click`；
  - `hold_key`：按住 `keys` 指定的键 `duration_s` 秒，最多 10 秒；
  - `keys` 加 `repeat=N`：把整串按键按 N 遍；
  - `cursor_position`：鼠标现在在哪，同时给出屏幕坐标和最近一次截图里的坐标；
  - `window=<标题>`：`x`、`y` 是这个窗口里的像素，和给它单独截的图一致；先把它切到前台，按键和文字也发给它。
- `ui` 新增：
  - inspect 给每个控件一个编号 `ref`（如 `e12`），之后用 `ref='e12'` 就能操作这一个控件；编号到下一次 inspect 为止；
  - `filter='文件名|打开'` 匹配其中任一段文字，也匹配控件的类名；`name`、`automation_id`、`control_type` 同样能缩小列表；
  - 窗口在读的过程中变了，会自动重读；
  - 动作打开了新窗口（确认框、报错、文件选择框），回复的 `new_windows` 会列出来；
  - `steps=[...]`：一次按顺序做几个动作，也可以是 `keys`、`type`、`wait`；遇到出错、等不到或回读对不上就停下，最后截一张图。
- `ocr` 找不到原样的文字时，再忽略标点找一遍：“COM17 · 115200”里的 “·” 认成 “•”、“-”，或者没认出来，都能找到，结果标上 `loose`。

### 你在用电脑时，AI 让开

- **让路**：你动了鼠标或键盘后，AI 的桌面操作（`input`、`ui` 的动作、`ocr` 的点击、`window` 的操作）先等你停手 5 秒，最多等 10 秒，还在用就告诉 AI “这人正在用电脑”，什么也不做。WinHand 自己发出的输入不算；你在 WinHand 的弹窗里点按钮、输入密钥也不算。
- **急停**：按 **Ctrl+Alt+Shift+Backspace**，AI 的桌面操作在接下来 1 分钟里一律拒绝，回复里说明是你按的。`ui(steps=[...])` 做到一半时，也从下一步起停下。急停键在 WinHand 启动时就注册好；按下的时间记在 `~/.winhand/desktop-stopped`，所以电脑上同时跑着的几个 WinHand 进程（比如本机 Claude Code 用的 stdio）都会停。
- **边框**：AI 操作桌面时，屏幕四周有一圈细细的橙色边框，最后一个动作之后 2 秒消失。截图里看不到它。Windows 10 2004 以前的系统没法把它排除在截图之外，就不显示。
- **设置**：`config.toml` 里

  ```toml
  [desktop]
  yield_to_owner = true   # false：不等你
  owner_idle_s = 5        # 停手多久才算不用了
  stop_keys = true        # false：不注册急停键
  border = true           # false：不显示边框
  ```

  `sys_info()` 的 `desktop_owner` 一项显示当前的设置，以及急停键注册成功没有。

### 命令被弹窗卡住时

`run` 还没结束就先回复时，以及 `job_status` 的 `activity` 里：命令已经超过 5 秒没有输出，又在它运行期间冒出了要你回应的窗口，就会在 `prompts` 里列出它的标题和程序，并让它的任务栏按钮闪烁，直到你切过去。这类窗口包括对话框、Windows 安全中心的登录框、1Password、KeePassXC、Bitwarden、GPG 的 pinentry、Git Credential Manager。复盘里，一次 `git commit` 就卡在 1Password 的签名窗口上，没人去点。

出现 UAC 这类安全桌面时，回复里也会写明：命令可能在等它，只有你能回答。长等待（`job_status(wait_s=...)`）碰到这两种情况会提前返回，`matched` 是 `prompt` 或 `secure_screen`。

以下情况不能操作：

- **锁屏、UAC 提示、远程桌面会话已断开时**：
  - `input`、点击、`window(action="focus")` 直接说明原因，不发送输入；
  - 整个桌面、显示器或区域的截图和读字也直接说明原因，不返回黑图；
  - 窗口截图在窗口自己能画出来时照常返回，并附一句说明。
- **以管理员身份运行的程序**：Windows 会悄悄丢掉普通程序发给它的点击和按键。
  - 窗口列表里，这类窗口标为 `admin: true`；
  - `ui`、`input`、`ocr` 的点击会拒绝，并说明原因；
  - 截图和读字照常；
  - `sys_info(checks=true)` 也会列出这些情况。

不支持高 DPI 的老程序，在缩放过的显示器上截窗口图时，会放大到窗口在屏幕上的实际大小，坐标与屏幕一致。

### 程序授权：AI 操作程序前先问你

AI 第一次点击、输入或操作某个程序时，电脑上会弹出“WinHand：AI 想操作一个程序”，写明是哪个程序、哪个窗口、要做什么。三个按钮：

- **拒绝**（默认焦点，误按回车不会批准）：这次不执行；之后 10 分钟内对这个程序的操作直接拒绝，不再弹窗，AI 会改为在对话里问你；
- **允许到 WinHand 重启**：只记在这次运行的 WinHand 里；
- **一直允许**：写入 `~/.winhand/desktop-apps.toml`，以后不再问。

几点说明：

- **要授权的操作**：`input` 的点击、拖动、滚动、按键、输入文字；`ui` 除 inspect 和 wait 以外的动作；`ocr` 的点击；`window` 的切到前台、最小化、最大化、还原、关闭、移动。截图、读字、`ui` inspect、剪贴板不受限制，密码管理器除外（见下）。
- **按程序算**：按 exe 文件名，例如 `notepad.exe`。鼠标动作看坐标处的窗口，按键看前台窗口，带 Win 键的组合键算 `explorer.exe`。
- **等你回答**：工具调用最多等约 40 秒；还没回答时先告诉 AI “正在等人允许”，弹窗继续开 2 分钟，你点了允许后，AI 下一次调用直接执行。2 分钟没人回答，这次就算拒绝，不记住。
- **提醒**：浏览器、密码管理器、聊天软件、系统工具和命令行程序的弹窗里多一行红字，说明允许后 AI 能做什么。
- **终端**：所有控制台窗口（命令提示符、PowerShell、python 等），以及 Windows Terminal、PuTTY、mintty 等终端程序，不弹窗，直接拒绝，提示 AI 改用 `run` 或会话工具，那里有守卫规则把关。确实要让 AI 在终端窗口里打字时，把它加入“一直允许”。
- **WinHand 自己的窗口**：授权弹窗、守卫的确认框和桌面端，任何桌面工具都碰不到，即使关掉了授权。
- **窗口列表**：`window(action="list")` 的每个窗口带 `approval`：`always`、`session`、`denied`、`terminal`、`ask` 或 `winhand`。
- **密码管理器**（1Password、Bitwarden、KeePass、KeePassXC、Dashlane、Enpass）：默认既不让 AI 看，也不让 AI 操作，关掉授权也一样。按窗口截它、`ui` inspect 它、点它，都先弹窗问你；你选允许（不论哪一种）也只放开 10 分钟，不记进 `desktop-apps.toml`。整屏截图和 `ocr` 时，没放开的密码管理器窗口涂黑，结果里 `masked` 写明。

管理授权：

- 桌面端“设置”页的“程序授权”：开关“AI 操作程序前先问我”，查看和撤销两个列表，从开着的程序里挑选或输入 exe 名加入“一直允许”；
- 命令行：
  ```
  winhand apps                    # 列出
  winhand apps allow notepad.exe  # 弹确认框，你选“是”才加入一直允许
  winhand apps revoke notepad.exe # 撤销
  winhand apps off                # 弹确认框后关闭授权；winhand apps on 重新开启
  ```
- 授权文件 `~/.winhand/desktop-apps.toml` 也可以手动编辑（`ask = false` 关闭授权，`[always]` 下列出程序）。AI 的文件工具（`fs_write`、`fs_edit`、`fs_op` 等）改不了它，也不能删除或移走它所在的文件夹。

这一层防的是 AI 误操作、越界去动你没交给它的程序，不是安全边界：AI 能执行命令，真想绕过也能绕过，和守卫规则的定位一样。

`sys_info(checks=true)` 会做一次环境体检，逐项列出 PowerShell 7、代码页、RedirectionGuard、长路径、Git Bash、WSL、scoop/mise shim、ssh、ripgrep 和非 ASCII 用户目录的状态，每项附修复建议。

`ui` 的操作只执行一次，之后回读控件状态；`verified=true` 表示可观察状态符合预期，`false` 表示未达到，`null` 表示无法确认。填写内容、切换勾选、选择、展开和聚焦可以按控件支持的状态核验；普通按钮被点击不代表它触发的工作已经完成，还应检查结果窗口。`verify_wait_s` 默认 3 秒，最多 10 秒，用于等待回读状态改变。

`remote_run(host, script)` 用这台电脑的 ssh 配置和密钥（包括别名和 ProxyJump）在另一台机器上执行脚本：

- 以非交互方式运行（BatchMode），带保活；脚本从 stdin 送过去，CRLF 会转成 LF。
- `env`、`cwd`、`args` 都会正确转义；`shell='python3'` 的脚本也拿得到 `env` 和 `cwd`。
- `secrets=['NAME']` 把本机密钥库里的值作为环境变量交给远程脚本，模型看不到值。
- 失败时 `ssh_error` 会说明原因：连不上、主机指纹不对、密钥被拒，还是脚本本身返回了非零退出码。
- 结果里网址中的 `用户:密码@` 一律遮挡。

`remote_copy(host, local, remote_path, direction='up'|'down')` 用同一套 ssh 配置经 scp 传文件或文件夹。用 `run` 直接调 ssh、scp 时，结果里会提示改用这两个工具。

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

不用配置也会遮挡的：

- winhand 自己的秘密：设备令牌；本机 MCP 服务的请求头（含其中的 Bearer 令牌）和它们 `env` 里名字带 token、secret、password、api_key 之类的值；`~/.winhand` 里文件名带 passphrase、password、token、secret、credential 的小文件（如 `owner-passphrase.txt`）里的值；
- 常见令牌格式：GitHub（`ghp_`、`github_pat_`…）、OpenAI / Anthropic（`sk-`）、AWS 访问密钥、Slack、Google API key、GitLab、npm、Hugging Face、私钥块和 JWT。`known_secrets = false` 可以关掉这一项。

`fs_send` 交出的文件里若含有被遮挡的值，文件不发出，换成一句说明。`secret_prompt` 收下的值也会自动遮挡。为了防止把占位符写回文件、冲掉真实值，`fs_write`、`fs_edit`、`fs_apply_patch` 和 `run` 会拒绝含有 `«redacted:` 的内容。本机其他 MCP 服务（`/mcp/<名称>`）不经过脱敏。

短暂断网后，agent 会重连，本机仍存活的会话继续运行。连接恢复时先用 `history` 看刚才的调用是否已经执行（带 run id 和退出码；几个客户端同时用这台电脑时，`client` 写明每条是谁发的，客户端保持 MCP 会话时 `this_session` 标出你自己的；winhand 重启后，桌面端日志里重启前的调用也列出来，标 `before_restart`），再用 `session_list` 找原会话，再用 `session_read(since=上次实际收到的 cursor)` 补读；游标未知时可从 `since=0` 开始。丢失回复不能证明操作失败，不要直接重发原命令。agent 进程重启属于另一种情况：交互会话不会自动恢复；需要跨 agent 重启继续的非交互工作用 `job_start`，之后以 `job_status` 查询。

### 用 Harmonia 管理密钥

[Harmonia](https://github.com/harmonia-vault/harmonia)（和弦）在手机上管理 API Key，端到端加密后同步到你批准的电脑和服务器；`harmonia exec -- <程序>` 只把密钥交给这一个程序。这台电脑装好 Harmonia 并接入账号后，winhand 会自动配合它：

- **遮挡**：Harmonia 给这台电脑的每个值（所有环境的，同名变量在不同环境里的值也都算），在任何工具结果里都换成 `«redacted:harmonia:变量名»`。手机上新加的变量同步到电脑后，从下一个结果起就被遮挡。winhand 用 `harmonia export` 自己读这些值，值只留在 winhand 进程里。`[redact]` 里写 `harmonia = false` 可以关掉。
- **保护**：AI 读不到、也改不了 Harmonia 的数据目录（`~/.harmonia`，里面的设备密钥能解开同步到这台电脑的全部变量）；`harmonia manager` 一律拒绝；`harmonia export`、`var list --show`、`var rm`、`logout`、`uninstall` 这类会输出明文或改变设置的命令，要你在电脑上点允许。这几条规则是 winhand 内置的，不用写进 guards.toml，`context()` 里能看到。
- **使用**：`context()` 列出可用的环境，AI 用 `run(command='harmonia', args=['exec', '--env', 'Cloudflare', '--', 'npx', 'wrangler', 'deploy'])` 这样运行需要密钥的程序。
- **存新密钥**：`secret_prompt(message, harmonia='Cloudflare/CLOUDFLARE_API_TOKEN')` 在电脑上弹出掩码输入框，输入的值直接写进 Harmonia 的这个环境，再同步到其他设备，AI 看不到。这台电脑对该环境要有读写权限。

接入 Harmonia（按它自己的说明）：

1. 部署服务端：Fork [harmonia-worker](https://github.com/harmonia-vault/harmonia-worker)，在 Cloudflare 控制台 Workers & Pages → Create → Import a repository 选这个 Fork 部署，免费套餐即可。部署后立即用手机 App 注册：默认只有第一个注册的账号能成功。
2. 手机装 App（Android 直接装 APK；iPhone 需要用 AltStore 这类工具自签），建环境（例如 Cloudflare、GitHub），添加变量。
3. 电脑上装命令行：在 PowerShell 里运行 `$env:HARMONIA_YES = '0'; irm https://github.com/harmonia-vault/harmonia/releases/download/update-feed/install.ps1 | iex`，再运行 `harmonia login <服务器地址>`，在手机上核对核对码后批准，只勾选需要的环境。

几点建议：

- Windows 上不要开 Harmonia 的“写入用户环境变量”（它的 Shell 集成）：值会以明文写进注册表，之后启动的每个程序都读得到。只用 `harmonia exec`，范围最小。
- Harmonia 还在 v0.x，作者建议先用于非关键密钥。
- 遮挡是安全带，不是沙箱：存心绕过的程序可以把值编码后再输出。

## 文件进出电脑

`fs_upload_url` / `fs_download_url` 发出一次性链接，模型在自己的沙箱里用 `curl -T 文件 链接` 上传、`curl -o 名字 链接` 下载，文件内容不经过对话。这样传 100 KB 的补丁只要几秒，不必再分段写 base64。传上来的补丁可以直接交给 `fs_apply_patch(root, patch_file=...)`：它会自动判断 `-p`，保留每个文件原来的编码和 CRLF，所有 hunk 都对得上才写入。

一次传多个文件：

- **下载文件夹**：`fs_download_url(path=文件夹)` 或 `paths=[...]`，可加 `include=["*.log"]`、`exclude=["node_modules", ".git"]`。内容边打包成一个 zip 边下载，所以没有事先算好的 sha256，用 `unzip -t` 检查。读规则保护的文件不放进去，回复里列出名字；文件夹里的链接和联接点不跟进去。打包途中读不了的文件，记在 zip 里的 `winhand-errors.txt`。
- **上传后解开**：`fs_upload_url(path=文件夹, extract=true)`，传一个 zip 或 tar（`.tar.gz`、`.tgz` 等）。要么全部解开，要么一个也不写：名字不安全（绝对路径、`..`、盘符、`CON` 这类设备名、末尾带点）、包里有链接、会经文件夹里已有的链接写到外面、写规则保护的文件、已存在的文件（没给 `overwrite=true`），都会让整包拒收。回复里逐个列出解开的文件和 sha256（前 200 个）。
- **大文件分片**：给 `size=字节数`，超过 `part_mb`（默认 4 MB）就每片一个链接。复盘时发现，一次传 8–10 MB 以上，有的沙箱约 30 秒就报 502（文件其实传到了）；分片后每片都能在这之前传完。分片可以乱序、并行，传坏的片再传一次即可；最后一片的回复就是结果。整个文件最大 2 GB。

`fs_read(paths=[...])` 一次读最多 20 个文本文件或文档，字数上限按文件平分；图片仍要一张张用 `fs_read(path)` 看。

AI 要跨调用留着的文件，放 winhand 自己的暂存目录：默认 `~/.winhand/scratch`，`config.toml` 里 `[scratch] folder = "D:/..."` 可以改。`context()` 会给出这个路径，在电脑上启动的程序可以用 `%WINHAND_SCRATCH%` 找到它。这里的东西不会自动删除；`%TEMP%` 你会手动清理，复盘时有两次暂存在那里的文件因此丢了。

暂存目录每周问你一次：桌面端开着时，距上次问满 7 天，就弹窗列出这里第一层 14 天没动过的文件夹（名字、最后改动日期、大小），你点「是」才逐个移进回收站，可以还原。放了 `.keep` 的文件夹、联接点和符号链接不动；点「否」或两分钟没人答，下周再问；屏幕锁着时不问，等你回来。AI 也可以用 `fs_op(action="clean_scratch")` 当场问一次，同样由你在弹窗里决定。回收站装不下的，Windows 会先警告再永久删除。

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
