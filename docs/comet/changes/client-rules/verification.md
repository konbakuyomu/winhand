# Verification: client-rules

## 第 1 轮（d0e67d3）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 315 passed，39 skipped（Verifier 运行）。
- `agent.yml`（run 38162757846）：ubuntu 通过；windows 有 2 个失败（`test_many_skills_leave_the_rules_whole`、`test_both_formats_and_claude_codes_agents_md_fallback`）。原因是测试用 `write_text` 写文件，Windows 上换行被转成 `\r\n`，与期望的正文不一致。属于测试的问题，第 2 轮已修。

**独立 Verifier 结论**：A1–A10 在 Verifier 自己的样例上都通过，但发现以下问题：

| # | 问题 | 级别 | 处理 |
|---|---|---|---|
| 1 | 文件很多时，结果远超 6 万字符：放不下的文件仍逐个列出；`rules_not_applied` 和 `imports` 没有上限 | 阻断 | 放不下的文件改进 `rules_omitted`；`rules_not_applied` 限约 8,000 字符；`imports`、`imports_skipped` 每份最多 30 项 |
| 2 | 项目里指向项目外的符号链接（`CLAUDE.md` → `~/.ssh/id_rsa`、`.claude/rules` → 任意目录）未经批准就被读取 | 阻断（安全） | 新决定 D6：与外部引用同样需要批准；规格 R6 补充 |
| 3 | 项目在主目录下且 `~/.claude/CLAUDE.md` 存在时，AGENTS.md 兜底永远不生效；设了 `CLAUDE_CONFIG_DIR` 时 `~/.claude/CLAUDE.md` 被当成目录文件 | 次要 | 用户自己的 CLAUDE.md 不算目录文件 |
| 4 | 默认格式下，AGENTS.md 先按 Codex 读到，它自己的 `@import` 丢失 | 次要 | 被 Claude Code 格式引用后照样展开 |
| 5 | `github-work:org/repo.git`（主机别名）匹配不到 | 次要 | 支持不带用户名的写法 |
| 6 | `repo` 加 `path="$MYDIR/x"` 时路径拼错 | 次要 | 先展开变量再判断是否绝对路径 |
| 7 | `imports` 里同一文件重复 | 次要 | 去重 |
| 8 | `paths` 的行尾注释被当成 glob；列表里的注释行让规则变成总是生效 | 次要 | 去掉注释 |
| 9 | 规则文件夹超过 500 个文件时静默丢弃 | 次要 | 加一条说明 |
| 10 | 测试受运行机器的 `CODEX_HOME`、`CLAUDE_CONFIG_DIR` 影响 | 次要 | `conftest.py` 自动隔离 |
| 11 | 疑似：Windows 上 `@\\host\share\x` 在判断“项目外”之前就访问网络路径 | 次要（安全） | 网络路径先判断，不访问 |
| 12 | 项目根上面还有 `.git` 时项目范围变大 | 与规格一致 | 记为已知限制（D7） |

规格中含糊之处已在 `specs/workspace-context/spec.md` 中写明：
- 用户自己的 CLAUDE.md 不算目录文件；
- `[context] rules` 列出的文件不受 `formats` 限制；
- 只有读到正文的规则才带 `lines`；
- 守卫只管内容会返回的文件；
- `redacted` 计数包括被截掉的部分；
- `content` 包含 frontmatter。

## 第 2 轮（f2929ab）

**正式检查**
- 云端 Linux（Verifier 运行）：`ruff check`、`ruff format --check` 通过；`pytest` 323 passed，39 skipped。
- 把 HOME、CODEX_HOME、CLAUDE_CONFIG_DIR 指向放满规则文件的目录后再跑 context 和 guard 的测试：107 passed，2 skipped。说明测试不会读到运行机器自己的配置。
- `agent.yml`（run 38164324954，5149d0a，代码与 f2929ab 相同）：ubuntu、windows 都通过。

**独立 Verifier 结论**：A1–A10 全部通过；第 1 轮的问题 1–11 已修，12 按 D7 接受。没有阻断问题，新发现 10 个次要问题：

| # | 问题 | 处理 |
|---|---|---|
| 1 | 仓库里的 `paths` glob 能让 `context()` 卡住几分钟到几小时（`PurePath.full_match` 背后的正则回溯） | 改用不回溯的 glob 匹配；超长 glob、过深的 `{}` 不展开 |
| 2 | `.claude/rules` 里链接进来的子文件夹被静默忽略 | 递归时跟随链接子文件夹，规则与链接文件相同；链出项目且未批准的记 `skipped` |
| 3 | 链接到项目内其他目录的文件被当成“项目外” | 以项目根为界；项目根以上各级以自身所在目录为界 |
| 4 | 项目文件未经批准引用 `~/.codex/AGENTS.md` 时，那份文件自己的引用被带出来 | 只有引用本身被允许时才展开已读文件，并按引用者的 scope 判断 |
| 5 | `rules_not_applied` 在脱敏之前计长度 | 先脱敏再计 |
| 6 | 项目 `guards.toml` 规则很多、或 `other_checkouts` 很多时无上限 | 守卫摘要限约 8,000 字符；`other_checkouts` 最多 30 个 |
| 7 | 第 2 轮的 imports 去重变成平方复杂度 | 用集合 |
| 8 | 嵌套极深的 `{}` 导致 RecursionError | 限制嵌套层数 |
| 9 | 先按 Codex 读到的文件，其引用排在错误的位置 | 引用的文件挂在引用者之下，输出时紧跟其后 |
| 10 | 名字不是 UTF-8 的文件让整个回复无法发送 | 跳过这类文件，输出前再做一次清理 |
| 疑似 | `import_refs` 的去重平方复杂度；列表分隔符少算 1 字符；指向网络路径的符号链接会先被访问 | 都已处理 |

规格中含糊之处的决定：
- 链接以项目根为界；
- 批准记录可在项目根到 path 之间任何一级；
- `formats` 里不认识的值忽略，结果的 `formats` 字段显示实际生效的格式。

## 第 3 轮（33aa976，4d6d62c）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 331 passed，39 skipped（33aa976）。
- `agent.yml`（run 38167139121，33aa976）：ubuntu 通过；windows 有 1 个失败：`test_linked_rule_folders_follow_the_same_rules`。
  - 原因：Windows 上 `os.readlink` 把本机目标写成 `\\?\C:\...`，被当成网络路径，项目内的链接全被拒绝。这是真实缺陷，只在 Windows 上出现。
  - 4d6d62c 已修：只有 `\\server\...`、`//server/...`、`\\?\UNC\...` 算网络路径。补了回归测试。

**本机冒烟**（中文 Windows 11，4d6d62c 的临时克隆 `%TEMP%\winhand-p1-smoke`，只读，用真实的 HOME 和 winhand 配置）：
- 全局：返回 `~/.codex/AGENTS.md`（codex）和 `~/.claude/rules/comet-workflow-guard.md`（claude-code，用户级规则）。没有 `~/.claude/CLAUDE.md`，与设计文档的观察一致。
- 6 个带规则文件的 Codex 项目：
  - 每个都返回项目根的 AGENTS.md；
  - kicad-tools、easyeda-agent 同时有 CLAUDE.md，两份都返回（D3）；
  - 每次调用 0.05–0.08 秒，结果 2.1 万–3.6 万字符。
- `context(repo="konbakuyomu/winhand")` 从 Codex 的项目记录找到 `d:\dev\20_个人项目\winhand`；`nobody/none` 返回的错误写明查过 24 个 Codex 项目、0 个 Claude Code 项目。
- 用户的 `~/.claude.json` 没有 `projects` 记录，因此 `hasClaudeMdExternalIncludesApproved` 无法在真机上核对。它的效果由测试覆盖（A10）。
- 在真机上跑 `tests/test_workspace_rules.py`、`tests/test_workspace_context.py`：39 passed，3 skipped。跳过的是需要建符号链接权限的两个测试和只在 Linux 上跑的非 UTF-8 文件名测试，这几项由 GitHub 的 windows、ubuntu runner 覆盖。
- 临时目录 `%TEMP%\winhand-p1-smoke` 留给用户手动删除（用户规则：不批量删除）。

**独立 Verifier**：（待填）
