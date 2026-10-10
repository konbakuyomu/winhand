# workspace-context

## Purpose

`context(path)` 回答一个目录“有什么规矩、有什么技能、有什么工具”，让任何 MCP 客户端都能拿到这台电脑上最新的本机规则。P0 只读通用格式（AGENTS.md、SKILL.md），也把用户已有的其他规则文件当作普通文件读；各客户端自己的语义（例如 CLAUDE.md 的优先级、`@import`）不在本次范围。

## Requirements

### R1 工具

- 新工具 `context(path: str | None = None)`。
- 传 `path` 时返回全局部分和项目部分；不传时只返回全局部分，并提示传入工作目录。
- `path` 可以是文件或目录，可以不存在：不存在时从最近的已存在上级目录开始。

### R2 项目根与 AGENTS.md

- 项目根：从 path 向上，找到最近一个含 `.git` 的目录；找不到时，项目根就是 path 所在目录。
- 收集项目根到 path 所在目录沿途每一级的 `AGENTS.md`，按从根到深的顺序返回。每份带路径和正文。

### R3 全局规矩（待 Q6）

- 配置项 `[context] rules`：一个文件路径列表，支持 `~` 和环境变量。
- 没有配置时用默认列表（推荐）：`~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`。
- 不存在的文件静默跳过；配置了 `rules = []` 时不读任何全局规矩。
- 每份带路径和正文。

### R4 技能（待 Q7）

- 配置项 `[context] skills`：用户级技能目录列表。
- 没有配置时默认（推荐）：`~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`。
- 项目级目录固定为项目根下的 `.agents/skills`、`.claude/skills`。
- 每个技能是一个含 `SKILL.md` 的子目录。读取 frontmatter 中的 `name` 和 `description`；没有 `name` 时用目录名。
- 跳过以 `.` 开头的目录和非目录项（例如 zip）。
- 同名技能只保留一个：项目级优先于用户级；同一级里，按目录列表的顺序，先出现的优先。
- 每个技能返回名称、描述（过长时截断到 300 字符）、`SKILL.md` 路径和来源（user、project）。
- 返回中附一句说明：需要时用 `fs_read` 读 SKILL.md，脚本用 `run` 在这台电脑上执行。

### R5 守卫与工具

- 返回对该路径生效的守卫规则摘要：文件、编号、`on`、`decision`、`reason`。文件出错时写明错误。
- 返回本机 MCP 服务名（winhand 配置里启用的 `mcp_servers`），并说明每个服务是独立的 MCP 地址。

Scenario: (A10) 返回目录的规矩、技能和规则
- 设置：
  - 临时项目根有 `.git` 和 `AGENTS.md`，子目录 `sub/` 有另一份 `AGENTS.md`；
  - 项目 `.agents/skills/demo/SKILL.md` 的 frontmatter 带 `name: demo`；
  - 用户级技能目录有同名的 `demo` 和另一个 `other`；
  - 配置的全局规矩文件存在；
  - 项目有一条守卫规则；
  - 配置了一个本机 MCP 服务。
- 操作：调用 `context(path="<root>/sub/x.c")`，再调用不带参数的 `context()`。
- 结果：
  - 第一次返回全局规矩；按根到 `sub` 的顺序返回两份 AGENTS.md；技能 `demo` 来自项目、`other` 来自用户级；守卫规则摘要和本机 MCP 服务名都在。
  - 第二次只有全局规矩和用户级技能。

### R6 大小与脱敏

- 整个结果的文字不超过约 60,000 字符。
- 超出时，按顺序截断规矩正文：先截断最后加入的文件。被截断的文件注明总行数，以及用 `fs_read(path, offset=…)` 继续读的方法。
- 结果经过 `[redact]` 脱敏。

Scenario: (A11) 大文件截断，敏感值被遮盖
- 设置：`[redact] values` 包含某个值，一份 AGENTS.md 包含这个值，另一份 AGENTS.md 超过 60,000 字符；
- 操作：调用 `context(path=...)`；
- 结果：敏感值被遮盖；大文件被截断，并写明如何继续读。

### R7 配置保存

`config.toml` 的 `[context]` 段在 `config.load()` 后完整保留，`config.save()` 时原样写回。

Scenario: (A12) 保存配置不会丢掉 [context]
- 设置：`config.toml` 有 `[context] rules = [...]` 和 `skills = [...]`；
- 操作：`load()` 后修改中转地址，再 `save()`；
- 结果：重新 `load()` 后 `[context]` 的值不变。
