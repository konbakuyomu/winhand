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

## 第 2 轮

（待填）
