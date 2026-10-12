# Outcome

经 winhand 的调用也会运行本机已有的 PreToolUse hooks，和在本机直接用 Codex、Claude Code 时一样。例如用户项目里 `.codex/hooks.json` 中的 comet 写入阶段守卫，在云端会话通过 winhand 改文件时同样生效。

只运行来源工具自己会运行的 hooks：
- Codex：信任状态与 `trusted_hash` 一致的；
- Claude Code：所在项目已经接受过工作区信任的。

其余 hooks 只在 `context()` 里列出，并说明为什么没运行。

`guards.toml` 增加 `command` 规则：用一个命令来判断，协议和 Claude Code 的 PreToolUse hook 相同。

对应设计文档 `docs/workspace-layer.md` 中 §1 的第 1、2 种规则来源，以及 P1 的“hooks、信任”部分。

# Scope

**来源与位置**
- Codex：
  - 用户层：`$CODEX_HOME`（默认 `~/.codex`）下的 `hooks.json` 和 `config.toml` 的 `[hooks]`；
  - 项目层：项目根到动作所在目录沿途每一级的 `.codex/hooks.json` 和 `.codex/config.toml` 的 `[hooks]`；
  - 系统层：`%ProgramData%\OpenAI\Codex\`，其他系统为 `/etc/codex/`；
  - 托管层：`requirements.toml` 的 `[hooks]`。
- Claude Code：
  - `~/.claude/settings.json`（`CLAUDE_CONFIG_DIR` 可改位置）；
  - 项目根的 `.claude/settings.json` 和 `.claude/settings.local.json`；
  - 托管文件 `managed-settings.json` 和 `managed-settings.d/*.json`。
- 只处理 `PreToolUse` 事件，只处理 `type = "command"` 的 hook。其他事件和类型在 `context()` 中列为“不支持”。

**信任：只运行来源工具会运行的**
- Codex：
  - 托管来源（系统层、托管层）总是运行；
  - 其余每个 hook 都要满足两点：
    - `enabled` 不是 false；
    - 用户 `config.toml` 中 `[hooks.state."<声明文件>:pre_tool_use:<组号>:<序号>"]` 的 `trusted_hash`，等于 winhand 按 Codex 算法算出的当前哈希。
  - 项目层还要求该项目在用户 `config.toml` 的 `[projects]` 中是 `trust_level = "trusted"`；
  - `requirements.toml` 里设了 `allow_managed_hooks_only = true` 时，只运行托管来源。
- Claude Code：
  - 托管设置里的 hooks 总是运行；
  - 其余设置文件中的 hooks（包括用户级），只有动作所在项目已接受工作区信任时才运行，即 `~/.claude.json` 的 `projects["<项目根或其上级>"].hasTrustDialogAccepted` 为 true；
  - `disableAllHooks` 按 Claude Code 的优先级生效；托管设置的 `allowManagedHooksOnly` 也生效。
- 未信任、已修改、已禁用的 hooks 不运行，并在 `context()` 中列出状态和原因。

**把动作交给 hooks**

| 动作 | Claude Code 来源的 `tool_name` 和 `tool_input` | Codex 来源的 `tool_name` 和 `tool_input` |
|---|---|---|
| `command`、`input` | `Bash`：`{command}`（matcher 也按 `PowerShell` 匹配） | `Bash`：`{command}` |
| `write` | `Write`：`{file_path, content}`（拿不到内容时为空字符串） | `apply_patch`：`{command: <补丁>}`（matcher 也按 `Write`、`Edit` 匹配） |
| `edit` | `Edit`：`{file_path, old_string, new_string, replace_all}`，`fs_apply_patch` 每个文件一份 | `apply_patch`：`{command: <补丁>}` |
| `delete`、`move` | `Bash`：等价的 PowerShell 命令（`Remove-Item -LiteralPath …` / `Move-Item …`） | 同左 |
| `read` | `Read`：`{file_path}` | 不交给 hooks：Codex 没有读文件工具 |

- matcher 按各自来源的规则匹配：
  - 空或 `*` 匹配全部；
  - 只含字母、数字、`_`、`|`（Claude Code 另外还允许 `-`、空格、`,`）时，按名字精确匹配；
  - 其他写法按正则匹配，不锚定。
- stdin 的 JSON 带上来源工具需要的字段：`session_id`、`cwd`、`hook_event_name`、`permission_mode`、`tool_name`、`tool_input`、`tool_use_id`，以及 Codex 的 `turn_id`、`model`、`transcript_path`（为 null）。

**运行**
- 工作目录为动作所在项目的根目录，没有项目时为动作所在目录。
- Codex 来源的 hook：
  - 在 Windows 上，用 `pwsh -NoProfile -Command <commandWindows 或 command>` 运行，依次退回 `powershell.exe`、`cmd.exe /C`；
  - 在其他系统上，用 `$SHELL -c` 运行。
- Claude Code 来源的 hook：
  - `shell = "powershell"` 时用 pwsh；
  - 有 `args` 时直接执行，不经过 shell；
  - 其余情况：Windows 上用 Git Bash，找不到时用 PowerShell；其他系统用 `sh -c`；
  - 设置 `CLAUDE_PROJECT_DIR`。
- 多个 hook 并行运行。每个 hook 的超时取它自己的 `timeout` 与 `[hooks] timeout_s`（默认 30 秒）中较小的一个。
- 运行时不弹出窗口；超时后结束整个进程树。

**结论翻译成守卫结论**

| 来源与输出 | 守卫结论 |
|---|---|
| 退出码 2，且 stderr 非空 | 拒绝，理由取自 stderr |
| Claude Code：`permissionDecision` 为 `deny`；或旧写法 `decision: "block"` | 拒绝 |
| Codex：`deny` 且理由非空；或旧写法 `decision: "block"` | 拒绝 |
| Claude Code：`ask` | 弹窗确认（与 `guards.toml` 的 ask 相同） |
| `updatedInput`（两种来源） | 拒绝，理由说明 hook 想改写这次调用，请按 hook 的意思改参数后重发。winhand 不替模型改参数 |
| Claude Code：`continue: false` | 拒绝，理由取 `stopReason` |
| `additionalContext`、`systemMessage` | 附注 |
| `allow`、`defer` | 不改变结论。winhand 没有“放行”，`guards.toml` 照常裁决 |
| 其他退出码、超时、无效输出 | 按来源工具的做法放行，并附注 hook 失败；Claude Code 设了 `onFailure: "block"` 时改为拒绝 |

**`guards.toml` 的 `command` 规则**
- 新字段 `command`（字符串），可与 `on`、`path`、`match`、`timeout_s` 一起使用，不能与 `decision` 同时出现。
- 命中后运行这个命令，按 Claude Code 的 PreToolUse 协议得出结论。
- 只能写在全局 `~/.winhand/guards.toml` 里。项目里的 `.winhand/guards.toml` 写了 `command` 时，该文件按“写错了”处理，失败即关闭。理由：仓库里的文件不能让 winhand 执行命令。

**开关与可见性**
- winhand `config.toml` 新增 `[hooks]` 段：
  - `sources`：要运行哪些来源的 hooks，默认 `["codex", "claude-code"]`，`[]` 表示全部关闭；
  - `timeout_s`：默认 30 秒。
- `context(path)` 新增 `hooks` 一项：对该目录生效的每个 hook 的来源、声明文件、matcher、命令（经过脱敏）、状态（`managed`、`trusted`、`untrusted`、`modified`、`disabled`、`unsupported`）、是否会运行，以及不运行的原因。

## Source coverage

来源：
- `docs/workspace-layer.md`（c8b652c，complete）；
- `docs/comet/changes/client-hooks/research.md`：Codex 源码和 Claude Code 文档中的事实，逐条注明出处（complete）；
- 用户原始需求：“本地的 hooks……在云端也能用”。

| 来源条目与位置 | 读取状态 | 需要保留的内容 | Spec 位置 | 验收 ID | 覆盖状态 | 理由或替代关系 |
|---|---|---|---|---|---|---|
| S1：§1 规则来源 1“或交给一个命令去判断” | complete | `guards.toml` 的 command 规则 | action-guard R2、R4、R13 | A8、A9 | covered | 只允许写在全局文件中，见 D4 |
| S2：§1 规则来源 2（Claude Code、Codex hooks 翻译） | complete | 动作到工具名的对应、协议 | hooks R1–R6 | A1–A6 | covered | 按 research 修正：Codex 写入为 `apply_patch`，读文件不交给 Codex |
| S3：§1“外部 hooks 只运行已信任的” | complete | Codex `trusted_hash`、Claude Code 工作区信任 | hooks R2、R3 | A3、A4 | covered | Claude Code 没有逐个 hook 的信任，按工作区信任处理 |
| S4：§1 “未信任的只在 context() 里列出” | complete | 可见性 | workspace-context R13 | A7 | covered | |
| S5：待定“外部 hooks 默认运行还是默认关闭” | complete | 默认值 | hooks R7 | A10 | covered | 默认运行来源工具已信任的，见 D2 |
| S6：§2 来源表“工具”列 `.winhand/profiles/` | complete | | — | — | non-goal | 仓库自带的终端配置等于让仓库决定运行什么，另开 change 设计信任机制 |
| S7：§3 生命周期桥（UserPromptSubmit、Stop、PreCompact 等） | complete | | — | — | non-goal | P2 |
| S8：用户原始需求“本地的 hooks” | complete | 本机 hooks 在云端经 winhand 生效 | hooks R1–R6 | A1–A6 | covered | 只限 PreToolUse；其余事件属于 P2 |

# Non-goals

- PostToolUse、UserPromptSubmit、Stop、SessionStart、PreCompact 等其他事件：P2 生命周期桥。
- `http`、`mcp_tool`、`prompt`、`agent` 类型的 hook；hook 的 `if` 条件、`async`、`once`：列为不支持，不运行。
- 插件（Codex plugins、Claude Code plugins）里的 hooks，技能和子代理 frontmatter 里的 hooks。
- 按 `updatedInput` 改写调用参数。
- 项目自带的终端 profile（`.winhand/profiles/`）。
- 桌面类工具（截图、ui、input）交给 hooks。

# Acceptance examples

- **A1** Codex 项目 hook 拦下写入：
  - 项目已信任，`.codex/hooks.json` 中 PreToolUse 的 matcher 为 `Write|Edit`，脚本对路径含 `src/` 的补丁以退出码 2 加 stderr 拒绝；
  - 用户 `config.toml` 中这个 hook 的 `trusted_hash` 与当前哈希一致；
  - 对该项目调用 `fs_write("src/a.c")`：文件没有被写，工具错误中带上 stderr 原文和 hook 的声明位置；
  - 调用 `fs_write("docs/a.md")`：照常写入。
- **A2** Claude Code hook 的几种结论：在已信任项目的 `.claude/settings.json` 中，matcher 为 `Bash` 的 hook 按命令内容输出：
  - `permissionDecision: deny`：`run` 不执行；
  - `ask`：弹出确认窗口，选“否”时不执行；
  - `additionalContext`：照常执行，结果里带上附注；
  - `updatedInput`：不执行，理由说明要按 hook 的意思改参数后重发。
- **A3** 未信任的不运行：
  - Codex：`trusted_hash` 缺失或与当前不一致（hook 被改过），或项目未标记为 trusted；
  - Claude Code：项目没有 `hasTrustDialogAccepted`。

  以上情况 hook 都不运行，调用照常执行；`context()` 列出状态（`untrusted`、`modified`）和原因。
- **A4** 哈希算法：winhand 对一个 hook 算出的哈希，等于 Codex 仓库测试里给出的值。
- **A5** 失败照常放行：hook 超时（超过 `[hooks] timeout_s`）、退出码 1、输出无效 JSON 时，调用照常执行，附注说明 hook 失败；超时的 hook 连同它的子进程都被结束。
- **A6** shell 会话输入：在 shell 会话中提交 `rm -rf build` 这一行，交给 matcher 为 `Bash` 的 hook。hook 拒绝时，这一行不会发出。
- **A7** `context(path)` 列出对该目录生效的 hooks：来源、声明文件、matcher、命令（经过脱敏）、状态和是否会运行。
- **A8** `guards.toml` 的 command 规则：全局规则 `on = "command"`、`command = "<脚本>"`，脚本输出 `permissionDecision: deny` 时，`run` 被拒绝，理由来自脚本。
- **A9** 项目里的 `.winhand/guards.toml` 写了 `command`：该项目内的动作被拒绝，错误说明 command 规则只能写在全局文件中。
- **A10** 开关与兼容：
  - `[hooks] sources = []` 时，任何 hook 都不运行；
  - 没有任何 hooks 配置时，行为与 0.4.5 完全一致，原有测试全部通过。

# Constraints and invariants

- 只运行来源工具在同样条件下也会运行的 hooks，绝不运行未信任的。
- 守卫的结论只会更严：hook 的 `allow` 不能放过 `guards.toml` 拒绝的动作。
- hook 的输出和错误经过 `[redact]` 脱敏后才交给模型。
- 运行 hook 不弹出可见窗口；超时后结束整个进程树。
- Linux 和 Windows CI 都要通过。依赖真实 pwsh、Git Bash 的测试只在 Windows 上运行。

# Decisions

- D1：只做 PreToolUse 和 `command` 类型。理由：
  - 其余事件需要客户端在发消息、回答结束时通知 winhand，属于 P2；
  - 其余类型要连 MCP 或模型，超出终端执行 MCP 的范围。
- D2：默认运行来源工具已信任的 hooks，`[hooks] sources = []` 可以全部关闭。理由：
  - 用户的目标是“尽可能接近本地体验”；
  - 这些 hooks 在本机用 Codex 或 Claude Code 时本来就会运行；
  - 信任门槛与来源工具相同，不额外放宽。
- D3：Codex hook 在 PreToolUse 返回 `ask`，按 Codex 的做法放行并附注，不弹窗；Claude Code 的 `ask` 则弹窗。理由：与来源工具一致。
- D4：`command` 规则只允许写在全局 `guards.toml` 中。理由：项目文件来自仓库，不能借此执行命令。
- D5：`updatedInput` 按拒绝处理，并说明原因。理由：
  - winhand 不替模型改参数，以免模型以为执行的是自己发的内容；
  - 让模型按 hook 的意思重发，结果同样符合 hook 的要求。
- D6：项目自带的终端 profile 不放在本 change，以后单独设计信任机制。理由：它让仓库决定 winhand 运行什么，风险等级与 hooks 不同。
- D7：在 Windows 上用 PowerShell 运行 hook 时，把程序的退出码传出来（命令后补 `if ($LASTEXITCODE) { exit $LASTEXITCODE }`）。理由：
  - `pwsh -Command` 会把退出码 2 变成 1，Codex 因此把“拒绝”当成“hook 出错”而放行；
  - 用户的 comet hooks 正是靠退出码 2 拒绝的，在 Codex 的 Windows 环境里实际拦不住；
  - winhand 让拒绝生效，比 Codex 严格，符合 hook 的本意，也符合“守卫的结论只会更严”。（第 1 轮 CI 和真机核对中发现）
- 决定 D1–D7 由 Claude 代定：用户把推进交给 Claude（“那你继续帮我推进吧，我不会写这些，交给你了”），汇报时说明，尤其是 D2 改变了默认行为。

# Open questions

无。

# Verification expectations

- 正式检查（在 `agent/` 下）：`uv sync --locked`、`uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run pytest -q`。
- 对分支手动触发 `agent.yml`，ubuntu 和 windows 都要通过；Windows 上用真实的 pwsh 运行 hook。
- 本机冒烟：在 winhand 连接器重新授权后进行。在临时克隆中运行，使用临时 `WINHAND_HOME`：
  - 对用户一个带 comet `.codex/hooks.json` 的真实项目调用 `context()`，核对 hooks 的信任状态与 Codex `/hooks` 显示的一致；
  - 在该项目的临时副本中，确认 hook 能拦下不该有的写入。
- 每轮由新的只读 Verifier 独立判定 A1–A10。
