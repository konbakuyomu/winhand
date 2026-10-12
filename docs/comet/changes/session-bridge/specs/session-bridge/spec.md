# session-bridge：云端会话开场带上本机规矩

云端 Claude Code 会话开始（以及清空、恢复、压缩之后）时，仓库里的 SessionStart hook 经中转向这台电脑要一段上下文：本机的全局规矩、云端仓库里没有的项目规矩、技能名单。事实依据见 `research.md`。

### R1 中转的 hook 入口

- 入口：`POST /hook/<event>`。`<event>` 目前只有 `session-start`；其他值返回 404。方法不是 POST 时返回 405。
- 只在中转配置了密钥 `HOOK_TOKEN` 时存在；没配置时 `/hook/` 下一律 404。
- 认证：请求头 `X-Winhand-Hook-Token` 与 `HOOK_TOKEN` 比较，用 SHA-256 摘要后常量时间比较。
  - 没带：401，不计数（多半是没设置令牌的环境，例如仓库的克隆；不占 KV 写入）。
  - 带错：401，按来源 IP 计数；同一 IP 15 分钟内错满 20 次后，带错的一律 429，直到计数过期。带对的令牌不受影响，主人不会被别人锁在外面（令牌是 256 位，计数只为挡住大量猜测对 KV 的消耗）。
- 请求体最多 64 KiB，超出返回 413；没有 `content-length` 的分块正文边读边数，超出即停。
- 通过后经现有隧道转给电脑：路径 `/hook/<event>`，身份 `x-winhand-user: hook`。客户端自己带的 `x-winhand-user` 不被采用。
- 等电脑的时间比 MCP 请求短：电脑不在时最多等 5 秒（宽限期更短时按宽限期），等回答最多 15 秒。这样在 hook 的 curl（25 秒）放弃之前就有答复，电脑关机时会话开场也只多等几秒。
- 电脑离线、超时或回 5xx 时，中转回 200，正文为：
  `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "<一句说明>"}}`
  说明写明：winhand 不在线、没有及时回答或出错（附 HTTP 状态），这次没带上本机规矩；电脑在线后可调用 `context(repo=…)`。

Scenario: (A1) 正确的令牌
- 设置：中转配置了 `HOOK_TOKEN`，电脑在线。
- 操作：带正确的 `X-Winhand-Hook-Token` POST `/hook/session-start`。
- 结果：电脑收到身份为 `hook` 的请求；客户端收到电脑的回答。

Scenario: (A1) 错误的令牌
- 操作：不带令牌 POST 30 次；再从同一 IP 带错的令牌连续 POST 21 次；之后带对的令牌 POST 一次。
- 结果：不带令牌的都是 401 且不计数；带错的前 20 次 401，第 21 次 429；带对的照常转给电脑。

Scenario: (A1) 没有配置
- 设置：没有 `HOOK_TOKEN`。
- 结果：`/hook/session-start` 返回 404。

Scenario: (A5) 电脑离线
- 设置：电脑不在线。
- 结果：200，`additionalContext` 是离线说明。

### R2 hook 令牌的范围

- 中转只在 `/hook/*` 上检查 hook 令牌。`/mcp`、`/mcp/<name>`、`/t/<token>`、`/agent`、OAuth 的各入口，对带不带这个请求头一视同仁，原有的认证照常要求。

Scenario: (A2) 令牌不能用于别处
- 操作：带正确的 hook 令牌（不带其他认证）访问 `/mcp`、`/agent`。
- 结果：与不带 hook 令牌时相同：`/mcp` 要求 OAuth（401），`/agent` 要求 agent 令牌（401）。

### R3 电脑这边的隔离

- 电脑上的 HTTP 应用新增路由 `POST /hook/{event}`：
  - 只接受 `x-winhand-user` 为 `hook` 的请求，其他身份 403；
  - `event` 不是 `session-start` 时 404。
- 身份为 `hook` 的请求访问任何其他路径（`/mcp`、`/mcp/<name>`、`/transfer/<token>`）一律 403，不进入 MCP 处理。
- 这条路由不调用任何工具，不运行任何命令，只读文件。

Scenario: (A3) 身份不对
- 操作：身份为 `owner` 的请求 POST `/hook/session-start`；身份为 `hook` 的请求 POST `/mcp`。
- 结果：都是 403。

### R4 开场上下文的内容

- 输入：hook 的输入 JSON（读取 `source`、`cwd`，其他字段忽略），请求头 `X-Winhand-Repo`（远端地址或 `owner/name`，最长 500 字符）。
- 找本机目录：用 `context(repo=…)` 的同一套办法（Codex、Claude Code 记录的项目和 `[context] repo_roots`）。找到时项目根就是那个检出目录；找不到或没给时没有项目。
- 正文按顺序：
  1. 开头一段说明（不超过约 600 字符）：
     - 这些内容来自哪台电脑（主机名）和哪个本机目录；仓库只以 `owner/name` 的形式出现，远端地址里的凭据不会写进来；
     - 技能和其中的脚本在这台电脑上，要经 winhand 的工具读取和运行；
     - 完整内容用 `context(repo="<仓库>")`。
  2. 全局规矩：与 `context()` 相同的读法，受 `[context] formats`、`rules` 设置控制；每份带路径。
  3. 项目规矩中，本机检出目录里**没被 git 跟踪**的规则文件（例如被忽略的 `CLAUDE.local.md`、`AGENTS.md`），`@` 引用进来的文件同样判断。已跟踪的不放：云端仓库里有它们。判断用 `git ls-files`（最多 5 秒）；本机没有 git 或命令失败时，把项目规矩都当作没跟踪。
  4. 技能名单：只有名字，一行列完，同名去重。
- 格式：纯文本，每份规矩以 `## <路径>` 开头。
- 读的每个文件先过守卫的 `read` 规则；被拒绝的不读，只列路径和“被守卫拒绝”。全文经过 `[redact]` 脱敏。
- `source` 不影响内容（`startup`、`resume`、`clear`、`compact` 都给同样的内容）。

Scenario: (A4) 开场带上规矩
- 设置：全局 `~/.codex/AGENTS.md`、`~/.claude/rules/style.md`；仓库 `owner/name` 的本机检出目录里有已跟踪的 `AGENTS.md`、被 git 忽略的 `CLAUDE.local.md`；有技能 `pdf`、`deploy`。
- 操作：POST `/hook/session-start`，`X-Winhand-Repo: https://github.com/owner/name`，`source` 为 `startup`；再以 `compact` 发一次。
- 结果：两次的 `additionalContext` 相同，含两份全局规矩、`CLAUDE.local.md`、`pdf`、`deploy`；不含已跟踪的 `AGENTS.md`。

### R5 预算和找不到时

- `additionalContext` 总长不超过 9,000 字符：按脱敏后的长度，并按 Claude Code 的算法计数（JavaScript 字符串长度，BMP 以外的字符算 2 个）。
- 技能名单先留出最多 2,500 字符；规矩放完后还有剩余，技能名单可以用掉剩余部分。
- 放不下时依次处理：
  - 技能名单截断，末尾写“另有 N 个”；
  - 规矩从后往前改为只列路径，并注明“未放入，用 context() 读”；
  - 单份规矩本身超出剩余预算时截断，并注明截断位置。
- 找不到本机目录时，正文只有说明、全局规矩、技能，说明里写“这台电脑上没有找到 <仓库> 的检出目录”。
- 读取出错（文件读不了等）不报错，相应条目写明原因，其余照常。

Scenario: (A5) 内容太长
- 设置：全局规矩 20,000 字符。
- 结果：`additionalContext` 不超过 9,000 字符，全局规矩被截断并注明；技能名单和说明仍在。

Scenario: (A5) 找不到仓库
- 操作：`X-Winhand-Repo: nobody/none`。
- 结果：有全局规矩和技能，说明写明本机没有这个仓库的检出目录。

### R6 `winhand bridge init`

- 打印两部分：
  1. 要放进仓库 `.claude/settings.json` 的 JSON 片段；
  2. 云端环境里要做的设置：Network secret（域名为中转的主机名，自定义请求头 `X-Winhand-Hook-Token`，前缀留空，值为 hook 令牌）；没有 Network secrets 时的替代做法（环境变量 `WINHAND_HOOK_TOKEN`，并在 Network access 里放行中转域名）。
- 中转地址取自 winhand 配置里的中转 URL（`wss://<host>/agent` 换成 `https://<host>`）；`--relay <url>` 可以指定。没有配置中转时报错退出。
- 片段：
  ```json
  {"hooks": {"SessionStart": [{"matcher": "startup|resume|clear|compact",
    "hooks": [{"type": "command", "timeout": 30, "command": "<命令>"}]}]}}
  ```
- 命令（POSIX sh，云端是 Linux）做这些事：
  - `CLAUDE_CODE_REMOTE` 不是 `true` 时退出 0；
  - 用 `git -C "$CLAUDE_PROJECT_DIR" remote get-url origin` 取仓库地址，取不到时用空值；
  - `curl -sS --max-time 25` 把 stdin 原样 POST 到 `https://<中转>/hook/session-start`，带 `content-type: application/json`、`X-Winhand-Repo`；`WINHAND_HOOK_TOKEN` 非空时再带 `X-Winhand-Hook-Token`；
  - 远端地址里 `@` 之前的部分（凭据）去掉后再发；
  - 回答先写到临时文件，HTTP 状态是 2xx 时才输出（3xx 的登录页之类不会进入会话）；其他情况不输出，退出 0。
  - 命令是 POSIX sh；Windows 上本机的 Claude Code 用 Git Bash 运行 hook，命令在那里因 `CLAUDE_CODE_REMOTE` 不是 `true` 直接退出。
- `--write <仓库目录>`：把片段合并进 `<仓库目录>/.claude/settings.json`：
  - 文件不存在时新建；
  - 保留已有的键和其他 hooks；
  - 已有一条命令里含 `/hook/session-start` 的 SessionStart hook 时，用新的替换它，不加第二份；
  - 文件不是合法 JSON 时报错，不改文件。

Scenario: (A6) 生成和合并
- 设置：仓库里已有 `.claude/settings.json`，含 `permissions` 和一条 PreToolUse hook。
- 操作：`winhand bridge init --write <仓库>` 执行两次。
- 结果：`permissions` 和 PreToolUse hook 保留；SessionStart 下只有一条指向 `/hook/session-start` 的 hook。

Scenario: (A6) 命令的行为
- 设置：一个本地 HTTP 服务模拟中转。
- 操作：以 `CLAUDE_CODE_REMOTE=true` 运行生成的命令，stdin 给 hook 输入；再以未设置运行；再让服务不可达。
- 结果：第一次服务收到原样的 stdin 和 `X-Winhand-Repo`，命令输出服务的回答；第二次服务没收到请求，退出 0；第三次无输出，退出 0。

### R7 `winhand bridge token`

- 生成 32 字节随机数，base64url 编码（43 个字符，无填充），打印一次，不保存在电脑上。
- 同时打印：在中转目录执行 `npx wrangler secret put HOOK_TOKEN` 并粘贴这个值；在云端环境里设 Network secret（或环境变量）用同一个值；换令牌时两处都要换。

Scenario: (A6) 生成令牌
- 结果：输出含一个 43 字符、只由 `A-Z a-z 0-9 - _` 组成的令牌，两次输出不同。

### R8 兼容

- 中转没有 `HOOK_TOKEN`、仓库里没有这个 hook 时，行为与 0.4.6 相同。

Scenario: (A7) 兼容
- 结果：原有测试全部通过；中转的端到端测试覆盖 R1–R3。
