# 云端会话与本机体验一致（设计草案）

目标：Claude 在云端会话（claude.ai/code）里通过 winhand 操作这台电脑时，全局和项目提示词、hooks、skills、本机 MCP 的效果尽量和在本机直接用 Claude Code / Codex 时一样，并且不用手动上传任何东西。

## 先说结论

- 以 winhand 为中心的实时方案为主（方向 2 的改进版），GitHub 镜像只当可选的离线备份（方向 1）。
- “注入”拆成三件事，各用合适的机制：
  1. **读规则**：winhand 的 MCP `instructions` 加上新工具 `context()`，零配置，每个会话都拿到最新内容；
  2. **执行 hooks**：由 winhand 在电脑上执行 hooks，挂在它自己的文件和命令工具上。云端改的是电脑上的文件，守卫也必须在电脑上触发；
  3. **新会话和压缩后自动注入**：可选。在常用仓库里提交一个云端 SessionStart hook（`startup|resume|compact`），通过中转向电脑取上下文。

## 依据：云端会话能读到什么

来自 Claude Code 官方文档（cloud-environments、settings、memory、hooks 几页），以及在本会话里的实测：

| 事实 | 对设计的影响 |
|---|---|
| 云端只加载**仓库里提交的**配置：`CLAUDE.md`、`.claude/settings.json` 的 hooks、`.claude/skills|agents|commands|rules`、`.mcp.json`。`~/.claude/settings.json`、`.claude/settings.local.json`、用户级 skills 和插件都不读 | 本机的用户级配置只能靠 winhand 或 setup script 带过去 |
| **多仓库会话**不读任何仓库的 hooks、权限、`env`，也不加载 `.mcp.json` | 把“配置镜像仓库”作为第二个仓库挂进会话，会让项目仓库自己的 hooks 全部失效 |
| setup script 在 Claude Code 启动前运行，可以写 `~/.claude/CLAUDE.md`，但结果会被缓存：只有改脚本、改网络白名单或约 7 天过期才重跑；它发出的请求也拿不到 network secret | 适合装工具，不适合同步实时状态 |
| SessionStart hook 每次启动、resume、压缩后（matcher `compact`）都会运行，输出直接进上下文 | 想要压缩后一定重新注入，只有这条路 |
| MCP 服务的 `instructions` 每个会话连接时重新获取，进入系统提示词，压缩后仍在。**Claude Code 只保留前 4096 个字符**（实测：winhand 的 GUIDE 有 5730 字符，云端看到的正好截在第 4096 个字符） | 这是唯一零配置、每次都最新的通道，但只放得下一个指引，放不下整份规则。另外，现在 GUIDE 末尾约 1600 字符（桌面、`job_start`、`sys_info`、`call()`、本机 MCP 几段）在云端是看不到的 |
| 云端 VM 是 Ubuntu。winhand 改的是电脑上的文件 | Windows hooks（`node "D:/..."`、`.cmd`）在 VM 里跑不了，就算能跑，守卫的也只是 VM 里的克隆 |

## 这台电脑的实际情况（2026-10-10 读取）

- 全局提示词在 `~/.codex/AGENTS.md`（3.7 KB）。`~/.claude/CLAUDE.md` 不存在，Claude Code 只在项目里没有 `CLAUDE.md` 时才读项目的 `AGENTS.md`，不会读 Codex 的全局 `AGENTS.md`。所以就算在本机用 Claude Code，它现在也看不到这份全局规则。
- Codex 记录的 24 个项目中：10 个有 `AGENTS.md`，10 个有 `.codex/hooks.json`，10 个有项目级 `.agents/skills`，只有 2 个有 `CLAUDE.md`，9 个没有 GitHub 远端（客户项目、资料工作区）。
- 项目 hooks 基本都是 comet 的阶段守卫：`PreToolUse`，matcher 为 `Write|Edit`，执行 `comet-hook-router.mjs`。它从 stdin 读 `tool_name` 和 `tool_input.file_path`；拒绝时退出码 2，原因写到 stderr；放行时可以输出 `hookSpecificOutput.additionalContext`，和 Claude Code 的 hook 协议完全一致。
- 其他 hooks：`C:\.codex\hooks.json`（对 C 盘下所有目录生效）里 impeccable 的 `PostToolUse`（`Edit|Write|apply_patch`）和 `Stop`；ponytail 插件的 `SessionStart`、`UserPromptSubmit`、`SubagentStart`。Codex 对每个 hook 记录 `trusted_hash`，项目记录 `trust_level`。
- skills 由 cc-switch 统一管理，约 50 个，共 38 MB，其中 impeccable.exe 一个文件就有 14 MB。多数 skill 要在 Windows 上跑脚本（eide、freecad、quicker、pyocd、kicad……）。
- 本机 MCP（Codex 配置）：eshop-mcp、fast-context、freecad-mcp、node_repl、pyocd-debug、usb-camera-mcp、amap-maps。

## 两个方向的评估

### 方向 1：GitHub 仓库映射

适合做备份，不适合当主通道：

- **挂载方式有矛盾**：作为第二个仓库挂进会话，会话就变成多仓库，项目仓库的 hooks、`.mcp.json` 都不加载；单独打开它，又没有项目代码。
- **hooks 放错了地方**：hooks 是 Windows 命令，而且要守卫的是电脑上被 winhand 改动的文件，放进云端仓库不起作用。
- **skills 的脚本在云端跑不了**：SKILL.md 的说明放在云端没问题，但脚本必须通过 winhand 在电脑上执行。
- **同步和泄露风险**：需要常驻进程不停提交。`settings.json` 的 `env`、MCP 的 `env`、skill 里的密钥都可能被推上去。14 MB 的 exe 也不适合放进 git。
- **9 个没有 GitHub 远端的项目**，它们的局部规则没有地方放。

### 方向 2：通过 winhand 实时注入

方向是对的。需要补上两点：一是 hooks 的作用是**执行**，光把内容注入上下文不够；二是“当前目录”可以从**工具调用的目标路径**和**仓库的 git 远端**推出来，不需要云端从某个目录启动。

## 推荐方案

### 第 1 层：上下文（零配置，所有会话生效）

**重排 instructions**：核心规则压缩到约 2.8 KB，详细说明移到 `help()`。末尾留约 1 KB 的动态段，由 winhand 在每次连接时生成，例如：

```
This machine has local agent rules: global AGENTS.md (3.7 KB), <n> skills, project hooks
in <m> folders (winhand runs them on fs_write/fs_edit/fs_apply_patch/run).
Before working on this machine, and again after the conversation is compacted (you see a
summary), call context(path=<the folder you work in>) and follow what it returns.
```

**新工具 `context(path=None, repo=None)`**，返回的规则链和本机一致：

- 全局：`~/.claude/CLAUDE.md`，以及（可配置）`~/.codex/AGENTS.md`；
- 项目：从 `path` 向上找到项目根，依次读 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`、`.claude/rules/*.md`（按 `paths` frontmatter 过滤）。没有 CLAUDE.md 时改读 `AGENTS.md`，规则同 Claude Code；`@import` 最多展开 4 层；
- skills 索引：`~/.claude/skills`、`~/.codex/skills`、`~/.agents/skills`、项目的 `.claude/skills` 和 `.agents/skills`、已启用插件的 skills。同名的项目优先，每个给出名称、描述和 SKILL.md 路径。云端按描述挑中后用 `fs_read` 读 SKILL.md，脚本用 `run` 在电脑上执行，和本地一样按需加载，不必上传；
- 对这个路径生效的 hooks：哪些由 winhand 代为执行，哪些执行不了；
- 本机 SessionStart hooks 的输出（如 ponytail）：以 `source=startup` 运行一次，附上 `additionalContext`；
- 本机 MCP 服务，以及 winhand 是否已在托管。

`repo="owner/name"` 用来对应云端会话的仓库：winhand 按 git 远端找到电脑上的检出目录。索引来源包括 Codex 的 `[projects]`、`~/.claude.json` 的 `projects`，以及 winhand 设置里的根目录。例如 `konbakuyomu/winhand` 对应 `D:\Dev\20_个人项目\winhand`。

输出经过 `RedactOutput` 脱敏，按大小预算分页。

### 第 2 层：在电脑上执行 hooks

winhand 的写入和命令工具自带 hook 管线，项目根从目标路径向上查找：

| winhand 工具 | 传给 hook 的 `tool_name` / `tool_input` |
|---|---|
| `fs_write` | `Write`，`{file_path, content}` |
| `fs_edit` | `Edit`，`{file_path, old_string, new_string, replace_all}`（多处编辑时逐处展开，或用 `edits`） |
| `fs_apply_patch` | 每个文件一次 `Edit`；Codex 来源的 hook 另发一次 `apply_patch` |
| `fs_op`（delete/move） | 由 `Bash` 规则匹配，`command` 写成等价命令 |
| `run`、`job_start` | `Bash`，`{command}` |

- **规则来源**：Claude Code 的 `~/.claude/settings.json`、项目 `.claude/settings.json` 和 `.claude/settings.local.json`、已启用插件的 hooks；Codex 的全局和项目 `.codex/hooks.json`、插件 hooks。
- **协议和 Claude Code 一致**：stdin 传 JSON（`session_id`、`cwd`、`hook_event_name`、`tool_name`、`tool_input`）。PreToolUse 退出码为 2，或输出 `permissionDecision: "deny"`，winhand 就不执行这次调用，并把理由原样交给模型。`ask` 时在桌面弹窗请人确认。`additionalContext`，以及 PostToolUse 的 `decision: "block"` 和 `reason`，附在工具结果里。
- **只执行已信任的 hooks**：Codex 里 `trusted_hash` 与当前命令一致的，Claude Code 里已通过信任确认的项目，或在 winhand 设置里明确勾选的。未信任的只在 `context()` 里列出，不执行。
- **执行细节**：Windows 上优先用 `commandWindows`，否则和原客户端一样，有 Git Bash 用 Git Bash，没有就用 PowerShell。超时取 hook 的 `timeout`。每次 hook 的执行和结果显示在桌面端时间线里。
- **做不到的事件**：winhand 看不到用户输入，也不知道回合何时结束，所以 `UserPromptSubmit`、`Stop`、`SubagentStart`、`PreCompact` 没法照原样触发。静态部分放进 `context()`；`Stop` 类检查可以提供 `hooks_run(event="Stop", path)`，让模型收尾时调用，这只是软约束。

### 第 3 层：新会话和压缩后确定注入（可选，按仓库开启）

第 1 层要靠模型遵守 instructions。如果要求新会话、resume 和压缩后**一定**带上本机规则，就用云端 Claude Code 自己的 SessionStart hook：

```jsonc
// <常在云端打开的仓库>/.claude/settings.json
{ "hooks": { "SessionStart": [ { "matcher": "startup|resume|compact",
  "hooks": [ { "type": "command", "command": ".claude/hooks/winhand-context.sh", "timeout": 20 } ] } ] } }
```

- 脚本只在 `CLAUDE_CODE_REMOTE=true` 时运行，不影响本机。它执行 `curl https://mcp.你的域名/ctx?repo=<owner/name>&event=<source>`，输出直接进上下文。电脑离线时只输出一行说明，不报错。
- 中转新增 `/ctx` 入口。它和 `/t/` 一样不走 OAuth，由电脑上的 winhand 校验一个**只读的上下文令牌**：只能读规则和索引，不能调用任何工具。中转对反复失败的 IP 限流。
- 令牌放进云环境的 Network secrets（Pro/Max 可用），由代理在请求离开 VM 后附上，令牌本身不进 VM。Team/Enterprise 目前只能放环境变量。
- 只在单仓库会话生效。`winhand cloud init` 或桌面端的一个按钮负责生成这两个文件。

### MCP 一致

- winhand 持续跟随 Codex 和 Claude 的 MCP 配置（现在的 `mcp import` 是一次性的），本机新增的服务自动出现在 `/mcp/<名称>`。
- 主连接器里加一个元工具 `mcp(server, tool=None, arguments=None)`，用法和 `call` 类似：不带 `tool` 时列出该服务的工具，带上就直接调用。这样没单独加连接器的本机服务也能马上用。

### GitHub 镜像（可选）

`winhand export --to <私有仓库>`：把脱敏后的全局规则和 skills（不含二进制）导出成快照，供电脑离线时的云端会话和 routines 使用。只做导出，不当主通道。

## 另一种选择：Remote Control

如果目标就是“本地 Claude Code 本身”，可以在电脑上运行 `claude remote-control`，或用桌面版的本地会话，再从网页或手机操作。这样 CLAUDE.md、hooks、skills、插件、MCP 全部是原生的。代价是会话跑在电脑上，没有云端 VM、并行会话和 PR 自动修复这一套。两者可以并用：要走 GitHub 流程并操作电脑，用 winhand；要完全本地的语义，用 Remote Control。

另外，在 `~/.claude/CLAUDE.md` 里写一行 `@~/.codex/AGENTS.md`，本机 Claude Code 也能读到 Codex 的全局规则。

## 实施顺序

1. **P0**：GUIDE 压到 4096 字符以内（修复现在的截断）；加 `context()` 和 instructions 动态段。
2. **P1**：hook 管线（`PreToolUse`/`PostToolUse`，覆盖 `fs_write`、`fs_edit`、`fs_apply_patch`、`run`）、信任模型、桌面端显示。
3. **P2**：中转 `/ctx`、只读上下文令牌、`winhand cloud init`。
4. **P3**：MCP 跟随和 `mcp()` 元工具；可选的 GitHub 导出。

## 待定

- 全局规则：`~/.claude/CLAUDE.md` 和 `~/.codex/AGENTS.md` 都给，还是只给其中一个？
- hooks 管线默认开还是关？未信任的项目 hooks 是否需要在桌面端一键信任？
- P2 要在中转加新入口，是否需要？
