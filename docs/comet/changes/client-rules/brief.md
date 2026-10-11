# Outcome

`context()` 按 Claude Code 和 Codex 各自的读法，返回这台电脑上的规矩，内容和这两个工具在本机实际读到的一致：
- Codex：`AGENTS.override.md` 优先于 `AGENTS.md`。
- Claude Code：
  - CLAUDE.md 的几种位置；
  - `@import`；
  - `.claude/rules` 按路径生效；
  - 没有 CLAUDE.md 时改读 AGENTS.md。

云端会话可以用仓库名（`owner/name`）找到这个仓库在本机的检出目录。

同时修掉 P0 遗留的两处超出约 6 万字符预算的问题。

对应设计文档 `docs/workspace-layer.md` 中 P1 的“规矩”部分。P1 的 hooks 和信任部分是后续 change `client-hooks`。

# Scope

**Codex 格式**（`format=codex`）
- 全局：`$CODEX_HOME`（默认 `~/.codex`）下的 `AGENTS.override.md`，不存在时读 `AGENTS.md`。
- 项目：从项目根到 path 沿途每一级目录，各取一份：`AGENTS.override.md` 优先，没有时取 `AGENTS.md`。
- 以上与 Codex 源码 `codex-rs/core/src/agents_md.rs` 一致。

**Claude Code 格式**（`format=claude-code`）
- 全局：
  - 管理员策略文件（Windows `C:\Program Files\ClaudeCode\CLAUDE.md`、macOS `/Library/Application Support/ClaudeCode/CLAUDE.md`、Linux `/etc/claude-code/CLAUDE.md`）；
  - 用户级 `~/.claude/CLAUDE.md`；
  - 用户级规则 `~/.claude/rules/**/*.md`；
  - 设了 `CLAUDE_CONFIG_DIR` 时，用它代替 `~/.claude`。
- 目录：从文件系统根到 path 沿途每一级目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`，按从上到下的顺序。
- 项目规则：项目根 `.claude/rules/**/*.md`。
  - frontmatter 没有 `paths` 的，总是生效；
  - 有 `paths` 的，只有 path 匹配其中一个 glob 时才生效。`paths` 可以是 YAML 列表，也可以是逗号分隔的字符串，支持 `{a,b}`；
  - 不生效的规则列在 `rules_not_applied`，带上它们的 `paths`。
- `@import`：
  - 路径相对于所在文件，支持 `~` 和绝对路径，`\ ` 表示空格；
  - 最多展开 4 跳；
  - 同一文件只出现一次；
  - Markdown 行内代码和代码块里的 `@` 不算引用。
- AGENTS.md 兜底：只启用 Claude Code 格式时，path 及其上级目录都没有 CLAUDE.md、`.claude/CLAUDE.md`、`CLAUDE.local.md`，才读项目里的 AGENTS.md。这是 Claude Code 的默认行为。

**合并与开关**
- 每份规则标出 `format`、`scope`（`managed`、`global`、`project`）；被引用的文件另标 `imported_by`。
- 同一文件经两条路线都被读到时只返回一次，例如 CLAUDE.md 里写了 `@AGENTS.md`。
- 新配置项 `[context] formats` 选择启用哪些格式，默认 `["codex", "claude-code"]`。
- `[context] rules` 一旦配置，就代替两种格式的全局部分，与 P0 一致；文件的格式按文件名判断。

**安全**
- `context()` 读的每个文件（规则、被引用的文件、SKILL.md）都先过守卫的 `read` 规则。被拒绝或需要确认的，不读，并写明原因。
- 项目里的文件引用项目外的文件（外部引用）时，只有下面两种情况才展开：
  - Claude Code 已为这个项目批准过外部引用；
  - 配置了 `[context] external_imports = true`。

  其余列在 `imports_skipped`。这对应 Claude Code 首次遇到外部引用时弹出的批准框。

**`context(repo="owner/name")`**
- 按 git 远端在本机找到检出目录。查找范围：
  - Codex 的项目记录（`config.toml` 的 `[projects]`）；
  - Claude Code 的项目记录（`~/.claude.json` 的 `projects`）；
  - `[context] repo_roots` 列出的文件夹本身及其直接子文件夹。
- 远端 URL 的 https、ssh、`git@` 写法都能匹配，不区分大小写，有没有 `.git` 后缀都行；`repo` 也可以直接写 URL。
- 同一仓库在本机有多个检出目录时：取最近有 git 活动的一个，其余列在 `other_checkouts`。
- 同时给了相对 `path` 时，path 相对于检出目录解析。
- 找不到时报错，说明查过哪些来源，以及怎样配置 `repo_roots`。

**P0 遗留**
- 技能列表单独限额约 20,000 字符：限额内列完整条目，超出部分只列名称并写明总数。规矩正文不再被技能列表挤掉。
- 先脱敏，再按脱敏后的长度计算预算。

## Source coverage

来源：
- `docs/workspace-layer.md`（7a7c135，complete）；
- P0 归档 `docs/comet/archive/2026-10-11-workspace-guard-context/verification.md` 中的遗留问题（complete）；
- 外部事实：Claude Code 官方文档 memory 页面（code.claude.com/docs/en/memory，经 claude-code-guide 查询）、Codex 源码 `agents_md.rs` 与 `config/mod.rs`（main 分支）。

| 来源条目与位置 | 读取状态 | 需要保留的内容 | Spec 位置 | 验收 ID | 覆盖状态 | 理由或替代关系 |
|---|---|---|---|---|---|---|
| S1：§2 来源表·Claude Code 行“规矩”列 | complete | `~/.claude/CLAUDE.md`、`CLAUDE.md`、`CLAUDE.local.md`、`.claude/rules/` | workspace-context R3、R4 | A1、A3 | covered | 按官方文档补上管理员策略文件、`.claude/CLAUDE.md`、用户级 `~/.claude/rules`、上级目录 |
| S2：§2 合并规则（没有 CLAUDE.md 时才读 AGENTS.md；`@import` 4 层） | complete | 兜底规则、`@import` 4 跳 | workspace-context R3、R5 | A2、A5 | covered | 两种格式都启用时两边都返回，见 D3 |
| S3：§2 来源表·Codex 行“规矩”列 | complete | `~/.codex/AGENTS.md`、项目 AGENTS.md | workspace-context R2 | A4 | covered | 按源码补上 `AGENTS.override.md` 和 `CODEX_HOME` |
| S4：§2 “当前目录”第二种定法（仓库名 → 本机检出目录） | complete | `context(repo=…)` | workspace-context R8 | A6 | covered | 查找范围见 D4 |
| S5：§2 来源表“技能”“工具”列 | complete | 技能目录、本机 MCP | — | — | background | 技能目录 P0 已覆盖；Claude Code 插件技能和 MCP 配置跟随属于 P3 |
| S6：§2 “所有输出都经过脱敏，并按大小预算分页” | complete | 脱敏、预算 | workspace-context R9 | A7、A8 | covered | |
| S7：实施顺序 P1“规矩、技能” | complete | 按客户端语义读取 | workspace-context R2–R6 | A1–A5、A9 | covered | |
| S8：实施顺序 P1“hooks、信任”；§1 第 2 种来源；待定“外部 hooks 默认运行还是关闭” | complete | | — | — | non-goal | 由 change `client-hooks` 负责 |
| S9：P0 遗留 1（技能多时超出预算） | complete | 技能单独限额 | workspace-context R9 | A7 | covered | |
| S10：P0 遗留 2（脱敏占位符超出预算） | complete | 先脱敏再计预算 | workspace-context R9 | A8 | covered | |
| S11：Claude Code 外部引用需批准（官方文档） | complete | 不擅自读项目外文件 | workspace-context R6 | A10 | covered | 加上守卫 `read` 规则 |

# Non-goals

- hooks、信任机制、`guards.toml` 交给外部命令判断、项目自带 profile：change `client-hooks`。
- Claude Code 在读到子目录文件时才加载的、path 以下的子目录 CLAUDE.md：`context()` 只给到 path 为止的这条链。
- Claude Code 的 `claudeMdExcludes`、“Project instructions”选项（`claude-md-and-agents-md` 等）、`--add-dir`；Codex 的 `project_doc_fallback_filenames`、`project_root_markers`、32 KiB 截断。只按默认设置读，按 winhand 自己的预算截断。
- Claude Code 插件里的技能和规则；resources / prompts 出口（P3）；生命周期桥（P2）。

# Acceptance examples

- **A1** Claude Code 规矩链：存在以下文件：
  - 用户级 `CLAUDE.md` 和 `rules/style.md`；
  - 项目上一级目录的 `CLAUDE.md`；
  - 项目根的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`；
  - 项目 `.claude/rules/general.md`（无 `paths`）；
  - 子目录 `sub/CLAUDE.md`。

  `context(path=<root>/sub)` 按以下顺序返回，每份标 `format=claude-code`：
  1. 用户级（`scope=global`）；
  2. 上一级目录；
  3. 根目录三份；
  4. 项目规则；
  5. `sub`。
- **A2** `@import`：
  - CLAUDE.md 中的 `@docs/style.md` 和 `@~/notes/me.md` 各返回一份，标出 `imported_by`；`~/notes/me.md` 要在外部引用已批准时才返回；
  - 第 5 跳不再展开，记在 `imports_skipped` 中；
  - 互相引用的两份文件各只返回一次；
  - 代码块和行内代码里的 `@x`、邮箱地址 `a@b.com` 都不当作引用。
- **A3** `.claude/rules`：
  - 没有 `paths` 的规则总是返回；
  - `paths: ["src/**/*.{c,h}"]` 的规则：path 为 `src/a/x.h` 时返回，为 `docs/a.md` 时不返回，并列在 `rules_not_applied`；
  - 逗号字符串写法 `paths: src/**, lib/*.c` 同样生效；
  - 子文件夹里的规则也会被读到。
- **A4** Codex 覆盖文件：
  - `$CODEX_HOME/AGENTS.override.md` 存在时，返回它而不是同目录的 `AGENTS.md`；
  - 某一级目录同时有 `AGENTS.override.md` 和 `AGENTS.md` 时，只返回前者，标 `format=codex`。
- **A5** 两种格式同时存在：
  - 项目根同时有 `CLAUDE.md`（内容为 `@AGENTS.md` 加一行补充）和 `AGENTS.md` 时，默认设置下两份都返回，`AGENTS.md` 只出现一次，`CLAUDE.md` 的 `imports` 里列出它；
  - `formats = ["claude-code"]` 时，有 CLAUDE.md 的项目不单独返回 AGENTS.md；没有任何 CLAUDE 文件的项目返回 AGENTS.md。
- **A6** 仓库映射：
  - Codex 项目记录里有一个检出目录，远端为 `https://github.com/konbakuyomu/winhand.git`，`context(repo="Konbakuyomu/Winhand")` 返回的 `path` 为该目录，并带回它的规矩；
  - 远端写成 `git@github.com:konbakuyomu/winhand` 同样能匹配；
  - `repo_roots` 下有第二个检出时，它列在 `other_checkouts`；
  - `context(repo=…, path="sub")` 解析为检出目录下的 `sub`；
  - 找不到时返回工具错误，说明查过哪些来源和 `repo_roots` 的配置方法。
- **A7** 300 个技能（每个描述约 300 字符）加一份 20,000 字符的 AGENTS.md：
  - 结果按 JSON 长度不超过 60,000 字符；
  - AGENTS.md 完整返回；
  - 技能列表在约 20,000 字符内给出完整条目，其余只列名称，并写明总数。
- **A8** 规矩中有 3,000 处会被脱敏的短值，占位符比原值长：结果在脱敏之后仍不超过 60,000 字符，并保留 `redacted` 计数和说明。
- **A9** `formats = ["codex"]` 时，不读任何 Claude Code 格式的文件；P0 的 `context()` 验收（A10、A11、A12）和原有测试全部仍然通过。
- **A10** 安全：
  - 项目 CLAUDE.md 写了 `@../outside/secret.md`、`@~/.ssh/id_rsa`，Claude Code 未批准外部引用时，这两份都不读，列在 `imports_skipped` 并写明原因；
  - Claude Code 批准后（`~/.claude.json` 里该项目的 `hasClaudeMdExternalIncludesApproved` 为 true），项目外文件照常展开；
  - 守卫 `read` 规则拒绝 `**/.env` 时，`@.env` 不读，标明被守卫拒绝。

# Constraints and invariants

- 只读文件，不执行任何东西，不弹任何窗口。
- 返回的路径都是绝对路径。
- 输出经过 `[redact]` 脱敏。
- 不修改 P0 已发布规格中与本次无关的行为。P0 的字段（`path`、`project_root`、`rules[].path/scope/content/lines/truncated`、`skills`、`guards`、`local_mcp_servers`、`next`）继续保留。
- Linux 和 Windows CI 都要通过。测试不依赖运行环境的真实 HOME。

# Decisions

- D1：P1 拆成两个先后进行的 change：先 `client-rules`，再 `client-hooks`。理由：两部分互不依赖，分开验收收敛更快。
- D2：Codex 格式以源码为准：项目里每级目录取一份，`AGENTS.override.md` 优先；全局同理；不越过项目根。
- D3：两种格式都启用时，同一目录的 CLAUDE 文件和 AGENTS 文件都返回，同一文件只返回一次。理由：
  - winhand 面向任何客户端，房主在哪个文件里写的规矩都应该让模型看到；
  - 常见的 `CLAUDE.md` 写 `@AGENTS.md`，通过去重不会重复；
  - 只想要 Claude Code 默认行为时，设 `formats = ["claude-code"]`。
- D4：仓库映射只查 Codex 项目记录、Claude Code 项目记录和 `[context] repo_roots`，不全盘扫描。理由：快、可预期；记录里没有的仓库，配置一行 `repo_roots` 即可找到。
- D5：外部引用默认沿用 Claude Code 的批准状态，所有读取都过守卫的 `read` 规则。理由：仓库可能来自别人，否则一行 `@~/.ssh/id_rsa` 就能把私钥交给模型。
- 决定 D3–D5 由 Claude 代定：用户把推进交给 Claude（“那你继续帮我推进吧，我不会写这些，交给你了”），汇报时会说明，用户可以随时改。

# Open questions

无。

# Verification expectations

- 正式检查（在 `agent/` 下）：`uv sync --locked`、`uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run pytest -q`。
- 对分支手动触发 `agent.yml`，ubuntu 和 windows 都要通过。
- 本机冒烟：在 winhand 连接器重新授权后进行。在临时克隆中运行，使用临时 `WINHAND_HOME`：
  - 对用户的真实项目调用 `context()` 和 `context(repo=…)`；
  - 核对返回的规矩与 Codex、Claude Code 实际读取的文件一致；
  - 核对用户真实的 `~/.claude.json` 是否有 `hasClaudeMdExternalIncludesApproved` 字段。
- 每轮由新的只读 Verifier 独立判定 A1–A10。
