# Outcome

云端 Claude Code 会话一开始（以及清空、恢复、压缩上下文之后），自动带上这台电脑上的规矩和技能清单，不用模型自己想起来调用 `context()`，也不用手动上传。效果接近在本机用 Claude Code：本机的全局规矩、项目里没提交到仓库的规矩，开场就在上下文里。

做法：仓库的 `.claude/settings.json` 里放一个 SessionStart hook。它在云端用 curl 把 hook 的输入发给中转的新入口 `/hook/session-start`。中转用单独的“hook 令牌”认证，转给这台电脑上的 winhand。winhand 返回一段 `additionalContext`。

对应设计文档 `docs/workspace-layer.md` 的 §3“生命周期桥”和 P2，范围收窄见 D1。

# Scope

**中转（relay）**
- 新入口 `POST /hook/session-start`：
  - 用请求头 `X-Winhand-Hook-Token` 认证，与 Cloudflare 上的密钥 `HOOK_TOKEN` 比较（常量时间）。没配置 `HOOK_TOKEN` 时这个入口不存在（404）。
  - 认证失败按 IP 计数，15 分钟内超过 20 次返回 429，与一次性文件链接的做法相同。
  - 请求体最多 64 KiB。
  - 通过后转给电脑，路径 `/hook/session-start`，身份标为 `hook`。
  - 电脑离线或超时（5xx）时，中转自己回 200，`additionalContext` 只有一句“winhand 不在线，本机规矩没带上，连上后可调用 context()”。
- hook 令牌只能用于 `/hook/*`。MCP、文件链接、`/agent` 都不认它。

**电脑（agent）**
- 新路由 `POST /hook/{event}`，只接受身份为 `hook` 的请求；身份为 `hook` 的请求也只能到这个路由（其他路径一律 403）。目前只有 `session-start`，其他事件 404。
- 读 hook 的输入 JSON（`session_id`、`cwd`、`source` 等）和请求头 `X-Winhand-Repo`（云端仓库的远端地址，hook 命令在云端用 `git remote get-url origin` 取得）。
- 按 `X-Winhand-Repo` 用 `context(repo=…)` 的同一套办法找到本机检出目录；找不到时只给全局规矩，并说明原因。
- 返回 `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": …}}`。内容按顺序：
  1. 一行说明：这些来自哪台电脑、对应哪个本机目录；技能和脚本要经 winhand 在电脑上运行；完整内容用 `context(repo=…)`；
  2. 全局规矩，与 `context()` 相同的读法和 `[context] formats` 设置；
  3. 项目里**云端仓库没有**的规矩：本机检出目录里没被 git 跟踪的规则文件（例如 `CLAUDE.local.md`、被忽略的 `AGENTS.md`）。已跟踪的文件云端自己有，不重复；
  4. 技能名单（只列名字）；
  5. 放不下的部分只列文件路径和“用 context() 读”。
- 总长不超过 9,000 字符（Claude Code 对 `additionalContext` 的上限是 10,000，超出会转存到文件，模型不一定去读）。
- 输出经过 `[redact]` 脱敏；读的每个文件先过守卫的 `read` 规则，与 `context()` 相同。

**命令行**
- `winhand bridge init`：打印要放进仓库 `.claude/settings.json` 的片段，以及云端环境里要做的设置。片段对所有仓库都一样，只含中转的地址。`--write <仓库目录>` 时直接合并进那个仓库的 `.claude/settings.json`，保留已有内容。
- `winhand bridge token`：生成一个新的 hook 令牌（32 字节随机数，base64url），只显示一次，并给出把它设为 `HOOK_TOKEN` 的命令（`npx wrangler secret put HOOK_TOKEN`）。

**生成的 hook**
- SessionStart，matcher 为 `startup|resume|clear|compact`，`type: "command"`（SessionStart 不支持 http hook），超时 30 秒。
- 命令：
  - 只在云端运行：`CLAUDE_CODE_REMOTE` 不是 `true` 时直接退出 0（本机的 Claude Code 自己会读规矩）；
  - 把 stdin 原样 POST 到 `https://<中转>/hook/session-start`，带上 `X-Winhand-Repo`；
  - 环境变量 `WINHAND_HOOK_TOKEN` 有值时才带上令牌头（没有时由云端环境的 Network secret 在代理处加上）；
  - curl 失败时安静退出 0，不打断会话。

## Source coverage

来源：
- `docs/workspace-layer.md` §3、P2（complete）；
- Claude Code 文档（hooks、settings、cloud-environments、claude-code-on-the-web），本次调研结论记在 `research.md`（complete）；
- 用户原始需求：“全局和项目的提示词、技能……不用手动上传”（complete）。

| 来源条目与位置 | 读取状态 | 需要保留的内容 | Spec 位置 | 验收 ID | 覆盖状态 | 理由或替代关系 |
|---|---|---|---|---|---|---|
| S1：§3 中转新增入口，只读令牌 | complete | 单独的令牌、只能取上下文 | session-bridge R1–R3 | A1、A2、A3 | covered | 入口名用 `/hook/` 而不是 `/bridge/`：中转里已有名为 Bridge 的隧道对象 |
| S2：§3 “运行本机对应事件的 hooks” | complete | | — | — | non-goal | 见 D1 |
| S3：§3 `winhand bridge init --client …` | complete | 生成客户端配置 | session-bridge R6、R7 | A6 | covered | 只做 Claude Code；其他客户端没有可用的 SessionStart 机制，见 Non-goals |
| S4：§3 令牌放进 Network secrets，不进容器 | complete | 令牌不进容器 | session-bridge R6 | A6 | covered | Network secret 只对 Pro/Max 开放，退而用环境变量，见 D3 |
| S5：§3 没有 hooks 的客户端退回到 instructions 加 `context()` | complete | | — | — | covered | 现状如此，不改 |
| S6：“用户发消息、回答结束、上下文压缩”三个时机 | complete | 压缩后重新带上规矩 | session-bridge R4 | A4 | covered | 压缩由 SessionStart 的 `compact` 覆盖；另两个见 D1 |
| S7：用户原始需求，全局和项目提示词、技能 | complete | 开场自动带上 | session-bridge R4、R5 | A4、A5 | covered | |

# Non-goals

- 转发 UserPromptSubmit、Stop、SubagentStop、PostToolUse 等事件，在电脑上运行对应的本机 hooks（D1）。
- Claude Code 以外的客户端（Codex 云端、claude.ai 网页和手机、ChatGPT 等）：没有能在会话开始时调用外部命令的机制，继续用 instructions 加 `context()`。
- 多仓库的云端会话：Claude Code 在这种会话里不读仓库的 hooks（文档明确），无法生效。
- 桌面端的设置界面；自动修改用户的云端环境设置（只能用户在 claude.ai 里做）。
- 自动部署中转（仍由用户执行 `npx wrangler deploy`，或经 winhand 在电脑上执行）。

# Acceptance examples

- **A1** 中转认证：
  - 带正确的 `X-Winhand-Hook-Token` POST `/hook/session-start`：转给电脑，返回电脑的回答；
  - 没带、带错：401；同一 IP 15 分钟内失败超过 20 次：429；
  - 没配置 `HOOK_TOKEN`：404。
- **A2** 令牌的范围：带 hook 令牌访问 `/mcp`、`/mcp/<name>`、`/t/<token>`、`/agent`：与不带时一样（OAuth、链接、agent 令牌照常要求），hook 令牌不起作用；`/hook/` 下的其他事件：404。
- **A3** 电脑这边的隔离：身份为 `hook` 的请求访问 `/mcp`、`/transfer/…`：403；身份不是 `hook` 的请求访问 `/hook/session-start`：403。
- **A4** 开场带上规矩：电脑上有全局 `~/.codex/AGENTS.md`、`~/.claude/rules/*.md`，仓库 `owner/name` 的本机检出目录里有被 git 忽略的 `CLAUDE.local.md` 和已跟踪的 `AGENTS.md`：
  - 回答的 `additionalContext` 含全局规矩、`CLAUDE.local.md`、技能名单；不含已跟踪的 `AGENTS.md`；
  - 总长不超过 9,000 字符；`source` 为 `compact` 时内容相同。
- **A5** 找不到仓库、内容太长、离线：
  - `X-Winhand-Repo` 在本机找不到：只有全局规矩和技能，并说明“本机没有这个仓库的检出目录”；
  - 规矩超出预算：放得下的照常给，其余列路径，提示用 `context(repo=…)`；
  - 电脑离线：中转回一句离线说明，200。
- **A6** 生成配置：
  - `winhand bridge init` 打印的片段放进 `.claude/settings.json` 后：在 `CLAUDE_CODE_REMOTE=true` 时把 stdin POST 到中转并输出回答；不是 `true` 时什么也不做、退出 0；curl 连不上时退出 0、无输出；
  - `--write` 合并进已有的 `.claude/settings.json`，原有的键和 hooks 保留，重复执行不会加第二份；
  - `winhand bridge token` 输出 43 个字符的 base64url 令牌和设置命令。
- **A7** 兼容：没有配置 `HOOK_TOKEN`、仓库里没有这个 hook 时，行为与 0.4.6 完全相同；原有测试全部通过；中转的端到端测试（wrangler dev）覆盖 A1–A3。

# Constraints and invariants

- hook 令牌只能取开场上下文，不能调用任何工具、不能触发电脑上的任何命令。
- 回答里的文字与 `context()` 同样经过守卫的 `read` 规则和脱敏；hook 令牌泄露时，最多泄露这些已脱敏的规矩文字和技能名单。
- 不改变 MCP 的认证方式；中转只为 `/hook/*` 认 hook 令牌。
- 生成的命令不在本机运行，不打断云端会话（任何失败都退出 0）。

# Decisions

- **D1 只做会话开始（含压缩后）**：
  - 用户发消息、回答结束这两个时机，原设计是在电脑上运行对应的本机 hooks。但云端会话改的是云端容器里的工作树，本机 hooks 检查的是电脑上的检出目录，两者对不上。
  - 用户电脑上的本机配置里没有 UserPromptSubmit hooks；Stop hooks 只在一个文件夹里有。
  - 先做价值明确的部分，这两个事件以后有具体需求再开 change。
- **D2 单独的令牌，放在自定义请求头里**：
  - 不复用 OAuth，也不复用 agent 令牌：它只开放 `/hook/*`，泄露的后果有限。
  - 用 `X-Winhand-Hook-Token` 而不是 `Authorization`：Network secret 会给发往这个域名的所有请求加上这个头，不能覆盖其他请求原有的认证。
- **D3 令牌优先放在 Network secret**：
  - 代理在请求离开容器后才加上令牌，令牌不进容器。
  - 这个功能只对 Pro/Max 开放。没有时，退而在云端环境里设环境变量 `WINHAND_HOOK_TOKEN`（使用这个环境的人都能看到），README 写明差别。
- **D4 离线时由中转回一句说明**：电脑不在线时，模型知道规矩没带上，可以稍后调用 `context()`，而不是悄无声息地缺了规矩。
- **D5 只补云端没有的项目规矩**：已跟踪的文件云端仓库里本来就有，Claude Code 会按自己的方式读；重复注入会占用 10,000 字符的预算。
- **D6 一份配置适用于所有仓库**：仓库由云端的 `git remote get-url origin` 决定，配置里只有中转地址。

# Open questions

- 用户的云端环境是否有 Network secrets（只对 Pro/Max 开放）——不阻塞开发，只影响验收时用哪种方式放令牌。
- 部署中转：经 winhand 在用户电脑上执行 `npx wrangler deploy`（需要电脑上已登录 wrangler），还是由用户自己执行——不阻塞开发。

# Verification expectations

- 中转：`npm run typecheck`、`npm test`（单元测试），以及 relay.yml 里 wrangler dev 的端到端测试，覆盖 A1–A3。
- 电脑：`ruff check`、`ruff format --check`、`pytest`；新增测试覆盖 A3–A6。
- CI：`agent.yml`、`relay.yml` 在分支上通过。
- 真机：部署中转、设置令牌、把配置放进一个仓库后，开一个新的云端会话，确认开场的上下文里有本机的规矩；离线时有那句说明。
- 独立的只读 Verifier 逐项判定 A1–A7。
