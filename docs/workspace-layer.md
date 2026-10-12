# 本机规则与守卫层（设计草案）

## 定位

winhand 的核心是终端执行 MCP：终端会话、一次性命令、文件、进程和桌面。本文在核心之上加一层**可选的、和客户端无关**的能力：

- **通用**：只用标准 MCP（tools、instructions、resources、prompts），Claude Code（本地或云端）、claude.ai、Codex、Cursor、ChatGPT 等任何 MCP 客户端都能用。
- **Claude Code 一致性只是其中一种用法**：Claude Code、Codex 的配置只是“来源”，由适配器读取后翻译成通用概念；不用它们的人可以只用 winhand 自己的格式。
- **同时是终端能力的扩展点**：守卫也能检查发到交互式会话里的命令，这一点连 Claude Code 自带的 hooks 都做不到。

```
 任意 MCP 客户端（Claude Code 本地/云端、claude.ai、Codex、Cursor、ChatGPT ……）
                │ 标准 MCP：tools · instructions · resources · prompts
┌───────────────┴─────────────────────────────────────────────────────┐
│ 工作区层（可选）                                                      │
│   context：这里有什么规矩、技能、工具     guard：动作前后放行/拒绝/确认/附注 │
│        ▲ 来源适配器：通用标准（AGENTS.md、SKILL.md）· winhand 自有格式      │
│          · Claude Code · Codex · 以后 Cursor、Gemini CLI ……              │
├──────────────────────────────────────────────────────────────────────┤
│ 执行核心：终端会话 · run/job · 文件 · 进程 · 桌面   （每次调用先描述成“动作”）│
└──────────────────────────────────────────────────────────────────────┘
        ▲ 生命周期桥（可选，按客户端）：发消息、回答结束、上下文压缩时，经中转通知 winhand
```

## 1. 动作与守卫（guard）

核心里每次会读写这台机器的调用，先统一描述成一个**动作**：

| 动作 | 来自哪些工具 | 主要字段 |
|---|---|---|
| `command` | `run`、`job_start`、`session_start` | 命令行、`cwd`、shell |
| `input` | `session_send`（发到交互式会话的一行） | 会话、profile、文字 |
| `write` | `fs_write`、`fs_write_bytes`、`fs_upload_url` | 路径 |
| `edit` | `fs_edit`、`fs_apply_patch` | 路径、改动内容 |
| `delete` / `move` | `fs_op` | 路径、目标 |
| `read` | `fs_read`、`fs_send`、`fs_download_url` | 路径 |

守卫在动作前后运行，结论只有四种：**放行**、**拒绝**（附理由，原样交给模型）、**请人确认**（在电脑上弹窗）、**附注**（附在结果里给模型看）。

实现上是一个新的中间件，和现有的 `RedactOutput`、`RecentCalls` 并列。所有客户端的所有调用都经过它，`call(tool, arguments)` 转发的调用也一样。

守卫规则有三种来源，最后都翻译成上面四种结论：

1. **winhand 自有规则**：`~/.winhand/guards.toml` 和项目里的 `.winhand/guards.toml`，按动作类型、路径通配符、命令正则匹配，或交给一个命令去判断。比如把 AGENTS.md 里“禁止批量删除”这条软规则变成真正生效的拦截：

   ```toml
   [[guard]]
   on = ["command", "input"]
   match = '(?i)(\brm\s+-\w*r\w*f|Remove-Item\b.*-Recurse|\brd\s+/s|\brmdir\s+/s|\bdel\s+/s)'
   decision = "deny"
   reason = "禁止批量删除：一次只删一个明确路径的文件，或请用户手动删除"

   [[guard]]
   on = "read"
   path = ["**/.env", "**/.env.*", "**/secrets/**"]
   decision = "deny"
   ```

2. **Claude Code hooks**（`settings.json`）和 **Codex hooks**（`hooks.json`）。两者格式相同，动作按下面的对应关系翻译成它们认识的工具名，再按它们的协议运行：stdin 传 JSON，退出码 2 表示拒绝，可输出 `permissionDecision` 和 `additionalContext`。

   | 动作 | `tool_name` |
   |---|---|
   | `command`、`delete`、`move` | `Bash` |
   | `write` | `Write` |
   | `edit` | `Edit`（Codex 来源另发 `apply_patch`） |
   | `read` | `Read` |

3. **以后**：其他客户端的 hooks 格式（Cursor、Gemini CLI 等），各加一个适配器即可。

外部 hooks 只运行**已信任**的：Codex 里 `trusted_hash` 与当前命令一致的，Claude Code 里已通过信任确认的项目，或者在 winhand 设置里明确勾选的。未信任的只在 `context()` 里列出，不运行。

**终端特有**：交互式会话里敲的命令也是动作（`input`），会过守卫。只检查 shell 类会话（pwsh、cmd、bash、ssh）的命令行，不检查密码输入，也不检查 gdb、串口这类程序的输入。

## 2. 工作区上下文（context）

给定一个目录，回答三个问题：这里**有什么规矩**、**有什么技能**、**有什么工具**。

“当前目录”有两种定法：一是工具调用的目标路径，向上找到项目根；二是客户端给出的仓库名（`owner/name`），按 git 远端在本机找到对应的检出目录。

各来源由适配器读取，可以在设置里分别开关：

| 来源 | 规矩 | 技能 | 工具 |
|---|---|---|---|
| 通用标准 | `AGENTS.md`（多家工具通用） | `SKILL.md`（多家工具通用的技能格式） | — |
| winhand 自有 | `.winhand/guards.toml` 的说明 | — | `.winhand/profiles/`（项目自带的终端 profile） |
| Claude Code | `~/.claude/CLAUDE.md`、`CLAUDE.md`、`CLAUDE.local.md`、`.claude/rules/` | `~/.claude/skills`、`.claude/skills`、插件 | `~/.claude.json`、`.mcp.json` |
| Codex | `~/.codex/AGENTS.md` | `~/.codex/skills`、`~/.agents/skills`、`.agents/skills` | `config.toml` 的 `mcp_servers` |
| 以后 | Cursor `.cursor/rules`、`GEMINI.md` …… | | |

规矩按各自工具的规则读取：比如 Claude Code 在没有 CLAUDE.md 时才读 AGENTS.md，`@import` 最多展开 4 跳；Codex 每级目录只取一份，`AGENTS.override.md` 优先。两种格式都启用时两边都返回，同一文件只返回一次。技能同名时项目优先。技能的说明交给模型读，脚本始终用 `run` 在本机执行。

**出口**都是标准 MCP，客户端支持哪种就用哪种：

| 出口 | 作用 | 支持情况 |
|---|---|---|
| 工具 `context(path, repo)` | 主通道 | 所有客户端都支持工具 |
| instructions | 一句指引：“这台电脑有本机规则，开始工作前和上下文被压缩后调用 context()” | Claude Code 只保留前 4096 字符，有的客户端不显示，所以不能只靠它 |
| resources | `winhand://context/<路径>`、`winhand://skills/<名称>` | 支持资源的客户端可以直接引用 |
| prompts | 每个技能一个 prompt | 支持 prompts 的客户端里就是斜杠命令 |

所有输出都经过 `RedactOutput` 脱敏，并按大小预算分页。

本机客户端（stdio 方式的 Claude Code、Codex）本来就会读自己的配置，可以关掉规则指引，避免重复。

## 3. 生命周期桥（可选，按客户端）

MCP 看不到三个时机：**用户发消息**、**回答结束**、**上下文压缩**。有 hooks 的客户端可以加一小段配置，在这些时机经中转通知 winhand：

- 中转新增 `/bridge/<事件>` 入口，用一个**只读令牌**认证。它只能取上下文、运行本机对应事件的 hooks（比如“回答结束”时的检查），不能调用任何工具。
- `winhand bridge init --client claude-code|codex|...` 生成对应客户端的配置片段。云端 Claude Code 的令牌放进环境的 Network secrets，令牌本身不进容器。
- 没有 hooks 的客户端退回到“instructions 指引 + `context()`”。

## 对终端能力的意义

- **安全底座**：不管哪个客户端，危险命令都会被拦下或需要确认，交互式会话里也一样。
- **项目自带终端配置**：仓库里放 `.winhand/profiles/`（比如这个项目的串口、gdb、OpenOCD 配置），连上就能用。
- **以后加的终端功能直接受益**：新的会话类型或工具，只要把调用描述成动作，就自动受守卫保护，也能出现在 context 里，不用为每个客户端单独适配。

## 各客户端能得到什么

| 客户端 | 终端核心 | 规矩与技能 | 守卫 | 生命周期桥 |
|---|---|---|---|---|
| Claude Code 云端 | ✓ | ✓ | ✓ | 可选（仓库里加配置） |
| Claude Code / Codex 本地 | ✓ | 自己会读，可关掉 | ✓（经 winhand 的操作也受守卫） | 一般不需要 |
| claude.ai 网页、手机对话 | ✓ | ✓ | ✓ | 不支持 hooks，靠指引 |
| ChatGPT、Cursor 等 | ✓ | ✓ | ✓ | 看客户端是否有 hooks |

## 依据

- **云端只读仓库里的配置**：云端 Claude Code 只加载仓库里提交的配置，用户级的 `~/.claude` 设置和技能都不读；多仓库会话不读任何仓库的 hooks 和 `.mcp.json`。所以“镜像仓库”不适合当主通道，最多导出一份快照做离线备份。
- **instructions 会被截断**：Claude Code 只保留 MCP instructions 的前 4096 字符。实测 winhand 现在的 GUIDE 有 5730 字符，末尾约 1600 字符在云端看不到，要先修。
- **本机规则在 Codex 那边**：全局规则在 `~/.codex/AGENTS.md`，没有 `~/.claude/CLAUDE.md`。Codex 记录的 24 个项目里，10 个有 `AGENTS.md`，10 个有 `.codex/hooks.json`（都是 comet 的写入阶段守卫），9 个没有 GitHub 远端。
- **comet 守卫的协议能直接复用**：它从 stdin 读 `tool_name` 和 `tool_input.file_path`，拒绝时退出码 2、理由写到 stderr，和上面第 1 节的翻译方式一致。
- **技能要在本机跑**：技能由 cc-switch 管理，约 50 个，多数要在 Windows 上跑脚本，所以由 winhand 在本机执行，而不是上传到云端。

## 实施顺序

1. **P0**（已实现并归档：`docs/comet/archive/2026-10-11-workspace-guard-context/`，现行规格在 `docs/comet/specs/`，用法见 README“本机规则与守卫”）：GUIDE 压到 4096 字符以内；守卫中间件和动作描述；`guards.toml`；`context()` 支持通用标准（`AGENTS.md`、`SKILL.md`）。
   P0 遗留，留给后续 change：
   - 技能很多时，`context()` 会超出约 6 万字符的上限，而且技能列表不截断；
   - 脱敏占位符比原值长，可能让结果超出预算。
2. **P1**：Claude Code 和 Codex 适配（规矩、技能、hooks、信任）。交互式会话的输入过守卫已在 P0 完成。分两个 change：
   - `client-rules`（已实现并归档：`docs/comet/archive/2026-10-12-client-rules/`，现行规格见 `docs/comet/specs/workspace-context/spec.md`）：按 Codex、Claude Code 的读法读规矩，`context(repo=…)`，以及 P0 的两处预算遗留；
   - `client-hooks`（进行中，`docs/comet/changes/client-hooks/`）：Claude Code、Codex hooks 翻译成守卫，信任机制。
3. **P2**：生命周期桥，以及各客户端的配置生成。
4. **P3**：resources 和 prompts 出口；跟随本机 MCP 配置；可选导出到 GitHub。

## 待定

- `guards.toml` 用这个格式行不行？
- 外部 hooks（Claude Code、Codex）默认运行还是默认关闭？（client-hooks 已决定：默认运行这两个工具自己信任的，`[hooks] sources = []` 可全部关闭）
- 本机 stdio 客户端是否默认关掉规则指引？
