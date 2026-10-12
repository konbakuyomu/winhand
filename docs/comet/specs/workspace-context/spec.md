# workspace-context

## Purpose

`context(path, repo)` 回答一个目录“有什么规矩、有什么技能、有什么工具”，让任何 MCP 客户端都能拿到这台电脑上最新的本机规则。

规矩按来源工具各自的读法读取，返回的内容和这些工具在本机实际读到的一致：
- Codex 和通用的 AGENTS.md；
- Claude Code 的 CLAUDE.md、`.claude/rules`、`@import`。

## Requirements

### R1 工具

- 工具签名：`context(path: str | None = None, repo: str | None = None)`。
- 传 `path` 时，返回全局部分和项目部分；`path` 和 `repo` 都不传时，只返回全局部分，并提示传入工作目录或仓库名。
- `path` 可以是文件或目录，也可以不存在（包括名字长到文件系统不接受）：这时从最近的已存在上级目录开始。
- `repo` 见 R8。
- 项目根：从 path 向上，最近一个含 `.git` 的目录；找不到时，项目根就是 path 所在目录。

### R2 Codex 格式（`format=codex`）

- 全局：`$CODEX_HOME`（未设置时为 `~/.codex`）下的 `AGENTS.override.md`；不存在时读 `AGENTS.md`。`scope=global`。
- 项目：从项目根到 path 所在目录，每一级各取一份：`AGENTS.override.md` 优先，没有时取 `AGENTS.md`。`scope=project`。
- 不越过项目根，不展开 `@import`。

### R3 Claude Code 格式（`format=claude-code`）

- `~/.claude` 指 `$CLAUDE_CONFIG_DIR`（若已设置），否则指用户主目录下的 `.claude`。
- 全局部分，按以下顺序：
  1. 管理员策略文件（`scope=managed`）：
     - Windows：`C:\Program Files\ClaudeCode\CLAUDE.md`；
     - macOS：`/Library/Application Support/ClaudeCode/CLAUDE.md`；
     - 其他系统：`/etc/claude-code/CLAUDE.md`。
  2. `~/.claude/CLAUDE.md`（`scope=global`）。
  3. `~/.claude/rules/` 下所有 `*.md`，递归，按相对路径排序（`scope=global`）。按 R4 判断是否生效。
- 目录部分（`scope=project`）：从文件系统根到 path 所在目录，每一级依次取存在的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`，按从上到下的顺序。用户自己的 `~/.claude/CLAUDE.md` 是全局文件，不算作主目录这一级的 `.claude/CLAUDE.md`。
- 项目规则（`scope=project`）：项目根 `.claude/rules/` 下所有 `*.md`，递归，按相对路径排序，排在项目根这一级的 CLAUDE 文件之后。按 R4 判断是否生效。
- 读取规则文件（包括展开引用、判断 `paths`）最多用 10 秒；超时就停下，结果带 `incomplete` 说明，已读到的照常返回。这样仓库里再多、再大的规则文件也不会让 `context()` 卡住。
- 一个规则文件夹最多看 500 个文件，按遍历顺序（每个文件夹先看自己的文件，再看子文件夹）；超过时，在这些文件之前加一条 `skipped` 说明。
- AGENTS.md 兜底：只启用 Claude Code 格式时，若 path 所在目录及其所有上级目录都没有 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`（用户自己的 `~/.claude/CLAUDE.md` 不算），就按 R2 的项目部分读取 `AGENTS.md`（不含 `AGENTS.override.md`）。这样读到的文件标 `format=claude-code`。
- 每份 Claude Code 格式的文件都按 R5 展开 `@import`。

### R4 按路径生效的规则

- 规则文件开头的 YAML frontmatter 里，`paths` 可以写成列表（多行 `- x` 或单行 `[x, y]`），也可以写成逗号分隔的字符串。
- 没有 `paths`，或 frontmatter 无法解析时，规则总是生效。
- `paths` 的值和列表项后面的 `# 注释`（引号外、前面有空白的 `#`）不算 glob，列表中的注释行和空行跳过。
- 有 `paths` 时，只有传入了 path，且 path 相对于项目根的路径（`/` 分隔）匹配其中一个 glob，规则才生效：
  - glob 支持 `*`、`**`、`?`、`[a-z]` / `[!x]` 和 `{a,b}`（Windows 上不区分大小写）；
  - 反斜杠转义下一个字符（例如 `\[` 表示字面的 `[`，`\{`、`\}`、`\,` 不参与 `{}` 展开），与 Claude Code 文档一致；
  - 匹配用不回溯的算法：单个模式的耗时与路径长度和模式长度的乘积成正比；
  - 一个规则文件的 glob 最多展开成 1,000 个模式（与 Claude Code 的上限相同），超过的部分不再尝试；超过 1,000 字符的 glob 跳过；`{}` 最多展开 16 层（连续或嵌套）；空的选项保留，例如 `docs/{,internal/}*.md` 同时匹配 `docs/a.md` 和 `docs/internal/a.md`；
  - 一次调用最多尝试 20,000 个模式；用完后，剩下有 `paths` 的规则列在 `rules_not_applied`，并标 `unchecked`，说明可以用该文件的路径再调用一次；
  - 不带目录的 `*.md` 只匹配项目根下的文件。
- 不生效的规则不读正文，列在 `rules_not_applied` 中，每项包含 `path`、`paths`、`format`、`scope`。

### R5 `@import`

- 引用的写法是 `@路径`：`@` 要么在行首，要么前面是空白字符。路径到下一个空白字符为止，`\ ` 表示路径中的空格。
- 以下不算引用：
  - 行内代码和围栏代码块里的内容；
  - 前面紧挨字母或数字的 `@`，例如邮箱地址；
  - 解析后不是已存在文件的路径。
- 引用路径的解析：
  - 相对路径相对于所在文件的目录；
  - 支持 `~` 和绝对路径；
  - 路径末尾的 `.,;:!?)]}` 若导致找不到文件，去掉后再试一次。
- 被引用的文件紧跟在引用它的文件之后返回（多份时按引用顺序，各自的引用又紧跟在各自之后），标 `imported_by`（引用它的文件路径），格式和 scope 与引用它的文件相同。被引用的文件可以继续引用其他文件。
- 被引用的文件本身有 `paths` 时，仍按引用读取，不按 `paths` 判断（引用即表示要读）。
- 最多 4 跳：顶层文件引用的文件是第 1 跳。第 5 跳不再展开，记在引用者的 `imports_skipped` 中，原因写明超过 4 跳。
- 每份文件的 `imports` 列出它引用的文件，同一文件只列一次；`imports` 和 `imports_skipped` 各最多列 30 项，其余只计数（`imports_more`、`imports_skipped_more`）。已经返回过的文件不再重复返回。
- 启用了 Claude Code 格式时，一份先按 Codex 格式读到的文件（例如 AGENTS.md）被 Claude Code 格式的文件引用后，它自己的 `@import` 也照样展开，因为 Claude Code 读它时会展开；展开的结果紧跟在它之后。只有这次引用本身是允许的（没有超过 4 跳、不是未经批准的外部引用）时才展开，展开时按引用者的 scope 判断外部引用。

### R6 安全

- `context()` 读取任何会把内容返回给模型的文件（规则文件、被引用的文件、SKILL.md）之前，都先以 `read` 动作过守卫。只为找位置而读、内容不返回的文件（`.claude.json`、Codex 的 `config.toml`、`.git/config`）不在此列：
  - 被拒绝、需要确认，或守卫规则文件损坏时，不读这个文件，也不弹窗；
  - 规则文件记 `skipped`，被引用的文件记在引用者的 `imports_skipped`，都写明原因；
  - 技能的 SKILL.md 被拒绝时，这个技能不列出。
- 外部引用：`scope=project` 的文件引用项目根以外的文件。这类引用只有满足以下任一条件才展开，否则记在 `imports_skipped`，原因写明“项目外，Claude Code 未批准”：
  - `~/.claude.json`（设置了 `CLAUDE_CONFIG_DIR` 时为该目录下的 `.claude.json`）的 `projects` 里，这个项目根对应条目的 `hasClaudeMdExternalIncludesApproved` 为 true；
  - 配置了 `[context] external_imports = true`。
- 链接到项目外：项目里找到的规则文件、`.claude/rules` 文件夹和其中的子文件夹，真实路径（解开符号链接后）不在项目根之内时，与外部引用适用同样的条件；不满足时不读，记 `skipped`。项目根以上各级目录里的文件，以它所在的那一级目录为界。链接到项目内其他位置的照常读取；`.claude/rules` 里链接进来的子文件夹照常递归。
- 网络路径（`\\server\share`、`//server/share`，以及 `\\?\` 开头但不是盘符的路径，如 `\\?\UNC\…`、`\\?\GLOBALROOT\…`）形式的外部引用，以及指向这类路径的符号链接，在判断之前不访问它，以免向网络发起连接。
- Claude Code 的批准记录可以在项目根，也可以在项目根到 path 之间任何一级目录（Claude Code 启动的目录）。
- 名字不是有效 UTF-8 的文件（Linux 允许）跳过，不让整个结果无法发送。
- `managed`、`global` 文件的引用不受“外部引用”限制，但同样要过守卫。

### R7 合并、格式开关与全局规则配置

- `[context] formats`：启用的格式列表，取值为 `codex`、`claude-code`，默认 `["codex", "claude-code"]`。
- 返回顺序：
  1. 全局部分，按 `formats` 的顺序排列各格式的全局文件；
  2. 目录部分，按目录从上到下排列；同一级目录内，按 `formats` 的顺序排列。
- 同一个文件（按真实路径，Windows 上不区分大小写）只返回一次，以先出现的位置为准。
- `[context] rules`：配置后（包括 `[]`），代替两种格式的全局部分，只读列出的文件，支持 `~` 和环境变量，不存在的文件静默跳过。这些文件是用户明确列出的，不受 `formats` 限制。格式按文件名判断：
  - `CLAUDE.md`、`CLAUDE.local.md`，以及位于 `rules` 文件夹中的文件，为 `claude-code`，并展开 `@import`；
  - 其他文件为 `codex`。
- 每份规则返回：`path`、`format`、`scope`，以及 `content` 或 `truncated` 或 `error` 或 `skipped`；读到了正文的还有 `lines`。按需附带 `imported_by`、`imports`、`imports_skipped`。`content` 是文件全文（包括开头的 frontmatter）。

### R8 按仓库名找到本机目录

- `repo` 可以写成 `owner/name`，也可以是任意 git 远端 URL，最长 500 字符。
- 匹配方式：取远端 URL 路径的最后两段（去掉 `.git` 后缀和末尾的 `/`），不区分大小写，与 `repo` 的最后两段比较。https、ssh、`git@host:` 和不带用户名的 `host:`（`~/.ssh/config` 里的主机别名）写法都支持。
- 候选目录：
  - Codex `config.toml` 中 `[projects]` 的键；
  - Claude Code `.claude.json` 中 `projects` 的键；
  - `[context] repo_roots` 列出的每个文件夹本身，以及它的直接子文件夹。
- 每个候选目录先找它的项目根，再读 git 配置里所有 remote 的 `url`：
  - `.git` 是文件夹时，读其中的 `config`；
  - `.git` 是文件（worktree）时，按 `gitdir:` 找到 git 目录，若有 `commondir`，读共用目录里的 `config`。
- 同一项目根只算一次。多个检出目录匹配时，取 git 目录中 `index`、`HEAD`、`FETCH_HEAD` 修改时间最新的那个，其余按时间列在 `other_checkouts`，最多 30 个、约 4,000 字符，再多只计数（`other_checkouts_more`）。
- 结果：
  - 返回中带 `repo`：`name`、`checkout`、`source`、`other_checkouts`；
  - 没给 path 时，path 为检出目录；
  - 给了相对 path 时（先展开 `~` 和环境变量），path 相对于检出目录解析；
  - 给了绝对 path 时，path 照用。
- 找不到时，返回工具错误，写明查过哪些来源、各有多少个候选目录，以及配置 `[context] repo_roots` 或改传 `path` 的方法。

Scenario: (A6) 仓库名映射到本机检出目录
- 设置：
  - Codex `config.toml` 的 `[projects]` 记录了检出目录 A，A 的远端是 `https://github.com/konbakuyomu/winhand.git`；
  - `repo_roots` 下另有一个较早的检出目录 B，远端是 `git@github.com:konbakuyomu/winhand`。
- 操作：调用 `context(repo="Konbakuyomu/Winhand")`，再调用 `context(repo="konbakuyomu/winhand", path="sub")`，再调用 `context(repo="nobody/none")`。
- 结果：
  - 第一次返回的 `path` 为 A，`other_checkouts` 含 B，并带回 A 的规矩；
  - 第二次返回的 `path` 为 A 下的 `sub`；
  - 第三次返回工具错误，写明查过的来源和 `repo_roots` 的配置方法。

### R9 技能

- 配置项 `[context] skills`：用户级技能目录列表。没有配置时，默认为 `~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`。
- 项目级技能目录固定为项目根下的 `.agents/skills`、`.claude/skills`。
- 每个技能是一个含 `SKILL.md` 的子目录。读取 frontmatter 中的 `name` 和 `description`；没有 `name` 时用目录名。
- 跳过以 `.` 开头的目录和非目录项。
- 同名技能只保留一个：项目级优先；同一级内，按目录列表的顺序，先出现的优先。
- 每个技能返回名称、描述（超过 300 字符时截断）、`SKILL.md` 路径和来源（`user`、`project`）。
- 返回中附一句说明：需要时用 `fs_read` 读 SKILL.md，脚本用 `run` 在这台电脑上执行。

### R10 大小与脱敏

- 所有文字先按 `[redact]` 脱敏，再计算长度。整个结果按 JSON 编码后的长度，不超过约 60,000 字符。
- 技能列表单独限额约 20,000 字符：
  - 限额内按顺序给出完整条目；
  - 超出部分放进 `skills_more`：`count` 为未完整列出的技能总数，`names` 列出名称，名称也受限额约束，放不下的只计数。
- `rules_not_applied` 按脱敏后的长度最多占约 8,000 字符，其余只在说明里计数。
- 守卫规则摘要最多占约 8,000 字符，其余只计数（`guards.rules_more`）。守卫规则文件的错误信息每条最多 500 字符，合计约 3,000 字符，其余只计数（`guards.errors_more`）。
- 名字不是有效 UTF-8 的路径，在计算长度之前就换成可显示的形式。
- 规矩正文使用剩余的预算，按返回顺序依次放入。放不下时，这份文件截断，注明总行数，以及用 `fs_read(path, offset=…)` 继续读的方法。连一条截断说明都放不下的文件不再单独列出，放进 `rules_omitted`：`count` 为文件数，`paths` 在约 2,000 字符内列出路径。
- 结果中有被脱敏的内容时，带上 `redacted` 计数和 `redaction_note` 说明，与其他工具一致。计数包括被截掉的部分。

Scenario: (A7) 技能很多时规矩不被挤掉
- 设置：300 个用户级技能，每个描述约 300 字符；项目 AGENTS.md 约 20,000 字符。
- 操作：调用 `context(path=<项目>)`。
- 结果：
  - JSON 长度不超过 60,000 字符；
  - AGENTS.md 完整返回；
  - `skills` 的条目在约 20,000 字符内；
  - `skills_more.count` 加上 `skills` 的条目数等于 300。

Scenario: (A8) 脱敏后的长度也在预算内
- 设置：`[redact] values` 含一个 6 字符的值；AGENTS.md 中这个值出现 3,000 次，总长约 55,000 字符。
- 操作：调用 `context(path=<项目>)`。
- 结果：
  - 返回内容中没有原值；
  - 整个结果的 JSON 长度不超过 60,000 字符；
  - 带有 `redacted` 计数。

### R11 守卫与工具

- 返回对该路径生效的守卫规则摘要：文件、编号、`on`、`decision`、`reason`。规则文件出错时，写明错误。
- 返回本机 MCP 服务名（winhand 配置里启用的 `mcp_servers`），并说明每个服务是独立的 MCP 地址。

### R12 配置保存

`config.toml` 的 `[context]` 段（`rules`、`skills`、`formats`、`external_imports`、`repo_roots`、`stdio_hint`）在 `config.load()` 后完整保留，`config.save()` 时原样写回。

## 验收场景

Scenario: (A1) Claude Code 规矩链
- 设置：用临时目录作为 HOME。存在以下文件：
  - 用户级 `~/.claude/CLAUDE.md` 和 `~/.claude/rules/style.md`；
  - 项目上一级目录的 `CLAUDE.md`；
  - 项目根的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`；
  - `.claude/rules/general.md`（无 `paths`）；
  - `sub/CLAUDE.md`。
- 操作：`context(path=<root>/sub)`，`formats = ["claude-code"]`。
- 结果：返回顺序为：
  1. 用户级 `CLAUDE.md`、`style.md`（`scope=global`）；
  2. 上一级目录的 `CLAUDE.md`；
  3. 根目录的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`；
  4. `general.md`；
  5. `sub/CLAUDE.md`。

  每份都标 `format=claude-code`。

Scenario: (A2) @import
- 设置：
  - 根 `CLAUDE.md` 引用 `@docs/style.md`，`docs/style.md` 又依次引用 4 跳深的链；
  - 另有两份文件互相引用；
  - 代码块和行内代码里写了 `@docs/hidden.md`，正文里写了邮箱 `me@example.com`。
- 操作：调用 `context(path=<root>)`。
- 结果：
  - `docs/style.md` 紧跟在 `CLAUDE.md` 之后，标 `imported_by`；
  - 第 5 跳在第 4 跳文件的 `imports_skipped` 中；
  - 互相引用的两份文件各出现一次；
  - `hidden.md` 和邮箱都没有被当作引用。

Scenario: (A3) 按路径生效
- 设置：`.claude/rules/c.md` 的 frontmatter 是 `paths: ["src/**/*.{c,h}"]`；`.claude/rules/sub/lib.md` 的 frontmatter 是 `paths: src/**, lib/*.c`；`.claude/rules/all.md` 没有 frontmatter。
- 操作：分别以 `src/a/x.h` 和 `docs/a.md` 调用 `context`。
- 结果：
  - 第一次返回 `c.md`、`lib.md`、`all.md`；
  - 第二次只返回 `all.md`，`c.md`、`lib.md` 列在 `rules_not_applied`，带上它们的 `paths`。

Scenario: (A4) Codex 覆盖文件
- 设置：`CODEX_HOME` 指向临时目录，其中同时有 `AGENTS.override.md` 和 `AGENTS.md`；项目根同时有两份，子目录只有 `AGENTS.md`。
- 操作：`context(path=<root>/sub)`，`formats = ["codex"]`。
- 结果：依次返回全局 override、根 override、子目录 AGENTS.md，都标 `format=codex`。

Scenario: (A5) 两种格式同时存在
- 设置：项目根有 `CLAUDE.md`（内容为 `@AGENTS.md` 和一行补充）和 `AGENTS.md`；另一个项目只有 `AGENTS.md`。
- 操作：用默认 `formats` 和 `formats = ["claude-code"]` 各调用一次。
- 结果：
  - 默认时，`AGENTS.md` 只出现一次，`CLAUDE.md` 的 `imports` 里列出它；
  - `["claude-code"]` 时，第一个项目的 `AGENTS.md` 只作为 `CLAUDE.md` 的引用出现；第二个项目返回 `AGENTS.md`，标 `format=claude-code`。

Scenario: (A9) 只开 Codex 格式时与 P0 一致
- 设置：`formats = ["codex"]`，项目里同时有 CLAUDE 系列文件和 AGENTS.md。
- 操作：调用 `context(path=...)`。
- 结果：
  - 不返回任何 CLAUDE 系列文件；
  - P0 的 A10、A11、A12 场景和原有测试全部通过。

Scenario: (A10) 不擅自读项目外和受保护的文件
- 设置：项目 `CLAUDE.md` 写了 `@../outside/secret.md`、`@~/.ssh/id_rsa`、`@.env`；全局守卫规则对 `**/.env` 的 `read` 动作为 deny。
- 操作：
  1. 不批准外部引用时调用 `context`；
  2. 在 `.claude.json` 中把该项目的 `hasClaudeMdExternalIncludesApproved` 设为 true，再调用一次。
- 结果：
  - 第一次，三份都不读，列在 `imports_skipped`：两份标明“项目外，未批准”，`.env` 标明被守卫拒绝；
  - 第二次，项目外的两份照常返回，`.env` 仍然被拒绝。
