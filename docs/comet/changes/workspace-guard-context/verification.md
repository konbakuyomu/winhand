# 验收报告：workspace-guard-context

## 结论

第 5 轮（提交 f9db783）：14 项验收中 13 项通过，**A3 未通过**。原因是第 4 轮为降低搜索开销加入的目录预检查引入了回归，见下文“遗留问题”。

验收失败已累计 5 次，达到 `max_verify_failures: 5` 的上限。按 Comet 规则，停在 Verify 阶段，等用户决定下一步。

## 第 5 轮逐项结论（f9db783）

| ID | 结论 | 依据 |
|---|---|---|
| A1 | passed | instructions 3116 字符，含 `context(` 和 `help()`；`help()` 含桌面、`job_start`、`sys_info(checks=true)`、`call()`、本机 MCP 各段；`test_instructions_fit_and_help_has_the_rest` |
| A2 | passed | 两条批量删除命令被拒、未执行，错误写明规则文件、编号、理由，`history()` 记为 error；`test_batch_delete_command_is_refused_and_not_run` |
| A3 | **failed** | 例子本身（`fs_read`、`fs_send`、`fs_download_url` 读 `.env`）通过；但 A3 背后的 R1（`fs_search` 行）、R3（按真实路径判断）在一种情况下不成立，见“遗留问题 1” |
| A4 | passed | 项目规则只管自己的目录；`test_project_rules_cover_only_their_folder` |
| A5 | passed | 只有“是”会执行；否、超时、无法弹窗都不执行并说明原因；默认按钮为“否”；本机冒烟看到真实弹窗 |
| A6 | passed | 附注出现在文字和 `guard_notes` 中 |
| A7 | passed | 经 `call()` 转发的写入同样被拒 |
| A8 | passed | 场景各步在 shell 会话中被拒，只输入不执行时放行，非 shell 会话不检查；Verifier 用真实的客户端取消复现，确认上一轮的异常缺口已补上 |
| A9 | passed | 规则文件写错时失败即关闭，写明文件和“line 3, column 17”，改好后恢复 |
| A10 | passed | 全局规矩、AGENTS.md 链、技能（项目优先）、守卫摘要、本机 MCP 名都在；不传路径只有全局部分 |
| A11 | passed | 脱敏生效，截断时给出 `fs_read(path, offset=)` 提示（转义字符很多时总长会超出预算，见“遗留问题 2”） |
| A12 | passed | `[context]` 经过 load → save → load 不丢 |
| A13 | passed | 原有测试未改动且全部通过；无规则时结果不变、不排队 |
| A14 | passed | stdio 默认不含 `context(`，`stdio_hint` 和 http/中转含；都不超过 4096 字符 |

## 检查记录

| 检查 | 结果 |
|---|---|
| `uv run ruff check src tests` | 通过 |
| `uv run ruff format --check src tests` | 通过（57 个文件） |
| `uv run pytest -q`（云端 Linux） | 293 通过，39 跳过（仅 Windows 的场景） |
| GitHub Actions `agent.yml`（ubuntu-latest + windows-latest） | 各轮均通过：af71ae1 run 38059014910、435804e run 38059293912、67f1ebc run 38060834245、3d5e606 run 38062091337、31e1433 run 38063630243、f9db783 run 38064503723 |
| 本机冒烟（用户的 Windows 11，临时克隆和临时 `WINHAND_HOME`，真实 pwsh 会话） | 各轮脚本在 f9db783 上全部重跑通过，见下 |

本机冒烟覆盖的内容：
- **instructions 长度**：stdio 2775 字符，不含提示；远程 3116 字符，含提示；
- **`context()`**：对真实项目返回全局 AGENTS.md、项目 AGENTS.md 和 53 个技能；
- **拒绝**：`Remove-Item -Recurse` 的命令、shell 会话里的同一命令都被拒，目录保留；
- **确认框**：置顶显示，焦点在“否”，无人应答时按超时拒绝；
- **shell 输入的各种绕过**：Enter 键、先输入后回车、`run` 的 stdin、输入后 `session_stop`、Home 键改过的行、按键名写错后回车、反引号续行，全部被拒；
- **文件**：复制或移动 `a.txt`、`srcdir\.` 到受保护目录被拒；日期命名目录里的 `.pem` 在搜索结果中被略去；对 `.env` 打补丁被拒，且不泄露内容。

## 历轮记录

| 轮次 | 提交 | 结果 | 主要问题与处理 |
|---|---|---|---|
| 本机冒烟 | af71ae1 | 发现问题 | 确认框默认焦点在“是”，误按回车会放行 → 435804e 改为默认“否” |
| 1 | 435804e | A8 未通过 | `keys=["Enter"]`、先输入后回车可绕过 shell 输入检查；`stdin`、`fs_search`、`move`、路径变量等多处绕过；确认等待 120 秒超过客户端时限 → 67f1ebc |
| 2 | 67f1ebc | A8 未通过 | `session_stop` 会执行已输入的行；并发输入；复制进已有目录按目录判断；搜索目录泄露 → 3d5e606 |
| 3 | 3d5e606 | A8 未通过 | 口令框取消后遗忘已输入内容；按键名写错；词中续行；搜索结果路径解析；补丁失败回报泄露内容 → 31e1433 改为“能确定就判断，不能确定就按命中处理” |
| 4 | 31e1433 | A8 未通过（窄） | 输入调用以异常结束时未标记“无法推断”；无规则时搜索过滤开销约 0.4 秒 → f9db783 |
| 5 | f9db783 | A3 未通过（窄） | 第 4 轮的目录预检查没有考虑“文件本身是符号链接”的情况 |

## 遗留问题

1. **（中低，第 4 轮引入的回归）**
   - **违反**：R1 的 `fs_search` 行、R3 的“按真实路径判断”。
   - **原因**：`_filter_search` 只预检查结果文件所在目录。如果结果文件本身是符号链接，它真实路径所在目录的读取规则不会被查看，文件直接放行。
   - **复现**：
     1. 没装 ripgrep，使用 Python 搜索引擎；
     2. `work/linked.txt` 指向 `vault/prod.key`；
     3. `vault/.winhand/guards.toml` 禁止读取 `**/*.key`；
     4. 搜索 `work` 时，结果里出现了密钥内容。
   - **范围**：ripgrep 默认不跟随链接，不受影响；`fs_read` 经链接读取仍被拒。31e1433 没有这个问题。
   - **修复思路**：对结果文件的每种写法（含真实路径）所在目录都做预检查，约一行改动。
2. **（低）**
   - **违反**：workspace-context R6 的“不超过约 60,000 字符”。
   - **原因**：预算按原始字符数计算，转成 JSON 后反斜杠、引号、换行会变长。
   - **复现**：一份全是 Windows 路径和引号的 AGENTS.md，结果约 79,674 字符；截断和续读提示仍正确。
   - **修复思路**：按 JSON 编码后的长度计算预算。
3. **（最低）**
   - **违反**：R12 措辞“每次调用沿目录逐级查看一次”。
   - **原因**：`fs_edit`、`fs_op copy`、`session_send` 每次调用会查两次。开销可忽略，行为不变。
   - **修复思路**：改措辞，或在一次调用内复用查找结果。

## 已声明的限制（不计为缺陷）

- 守卫按工具参数判断，不是沙箱：`run` 启动的程序自己读写什么、正则没覆盖到的写法（会话里先定义的别名或变量等）、`proc_kill` 和桌面工具都不在范围内。
- 用过编辑键、历史展开，或守卫跟不上的输入，按“无法推断”处理：所有带 `match` 的 `input` 规则都视为命中，需先用 Ctrl+C 清行。

## 待用户决定

- 验收已达失败上限：继续修复遗留问题并再验一轮，还是接受当前结果并在文档中列明遗留问题。
- D15：确认框默认等待从 120 秒改为 40 秒、`timeout_s` 最多 50 秒（客户端约 60 秒放弃一次调用），是否同意。
