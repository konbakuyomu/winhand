# Verification: client-hooks

## 第 1 轮（519b980）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 382 passed，39 skipped。

**本机冒烟**（中文 Windows 11，519b980 的临时克隆，只读：只找 hooks、判断状态，不运行任何 hook）：
- 用户 Codex 记录的项目里，9 个有 comet 的 `.codex/hooks.json`：
  - 7 个判为 `trusted`，与用户在 Codex 里审核过的 `trusted_hash` 一致，没有一个误判为 `modified`。这说明哈希算法、`commandWindows` 的处理和键的写法在真机上与 Codex 一致；
  - 2 个判为 `untrusted`。核对用户的 `config.toml`：这两个项目只信任过 `user_prompt_submit` 的 hook，没有 `pre_tool_use` 的记录，判断正确。
- 9 个项目的 `.claude/settings.json` 都判为 `untrusted`：用户的 `~/.claude.json` 没有项目记录，即没有在 Claude Code 里接受过工作区信任，判断正确。
- Codex 写的键是 `D:\Dev\...`，项目列表里是 `d:\dev\...`，按文件系统的方式不区分大小写比较，才能对上。

**独立 Verifier 结论**：A1–A10 全部通过，但有 2 个阻断问题：

| # | 问题 | 级别 | 处理（第 2 轮） |
|---|---|---|---|
| 1 | Codex 的项目信任没有逐个 `.codex` 文件夹判断：项目根被标为 untrusted、子目录被信任时，项目根的 hook 仍然运行；被标为 untrusted 的子目录的 hook 也运行 | 阻断（安全） | 按 Codex 的方式逐个文件夹判断，`untrusted` 优先 |
| 2 | hook 的 `timeout` 不是数字（如 `"5"`）时，这个项目里每个受守卫的调用和 `context()` 都报内部错误 | 阻断 | 字段类型不对的 hook 判为 `unsupported`，写明原因；每个 hook 单独处理，出错不影响其他 |
| 3 | `.codex/hooks.json` 链接到 `/dev/zero` 时无限读取直到内存耗尽（仅 Linux/macOS） | 次要 | 只读普通文件，最多 4 MB |
| 4 | hook 以退出码 2 拒绝、但留下一个占着输出的子进程时，拒绝变成超时放行，子进程也没被结束 | 次要 | hook 已退出就按它的结论；超时时结束整个进程组 |
| 5 | 最多 8 个 hook 并行 | 次要 | 提高到 32 |
| 6 | matcher 不是有效正则时静默跳过 | 次要 | 判为 `unsupported` 并写明原因 |
| 7 | `CODEX_HOME` 恰好是项目的 `.codex` 时，同一个 hook 运行两次 | 次要 | 不再作为项目层重复读取 |

规格中含糊之处的决定：
- Claude Code 的工作区信任不覆盖嵌套的仓库；
- 托管设置里的 `disableAllHooks` 关掉所有 hooks，包括托管的；
- Codex 来源的输出按 Codex 的严格方式读：不认识的字段、`stopReason` 都算失败；
- Claude Code 退出码 2 时仍以 stderr 为理由（与规格一致）；
- 另加一条测试：hooks 开启、但没有任何 hook 文件时，`fs_write`、`fs_read`、`run` 的结果与以前相同。

**CI**（run 38195189706，519b980）：ubuntu 通过；windows 有 1 个失败：`test_a_trusted_codex_project_hook_refuses_a_write`。
- 原因：Codex 和 winhand 都用 `pwsh -NoProfile -Command <命令>` 运行 hook，PowerShell 会把程序的退出码 2 变成 1。在用户真机（PowerShell 7.6.5）上核对过：不加 `; exit $LASTEXITCODE` 时得到 1，加上后得到 2。
- 进一步核对用户真机上 comet 的 `comet-hook-router.mjs`：对 codex、claude-code 平台，拒绝时用退出码 2 加 stderr。所以在 Codex 的 Windows 环境里，comet 的写入阶段守卫会被 PowerShell 变成退出码 1，被当成 hook 出错而放行。这是根据 PowerShell 的行为和 comet 源码推断的，没有实际运行 Codex 验证。
- 处理：
  - 新决定 D7：winhand 经 PowerShell 运行 hook 时，把程序的退出码传出来，让这类拒绝生效；
  - 补了 Windows 上的测试（不加 `; exit $LASTEXITCODE` 的 hook，靠退出码 2 也能拒绝）；
  - README 写明 Codex 里的这个坑。

## 第 2 轮（b9f1635）

**Build 期间的发现**（在用户真机上跑 hook 测试，4 次提交）：
- 2aa75ed：Windows 上，hook 以退出码 2 拒绝但留下一个子进程时，子进程占着管道，拒绝的理由丢失（拒绝本身仍生效）。hook 的输入输出改为经临时文件传递；Windows 上用作业对象、其他系统用进程组，结束 hook 留下的进程。
- 2aa75ed 起：守卫向上找项目规则时，不再把全局规则文件当成项目文件。这一处的写法在第 3 轮又改了，见下。
- b9f1635：用户真机上，shell 输入测试被用户自己的全局 `guards.toml`（禁止批量删除）先拦下了。测试改用中性的词，不再与运行测试的机器上的规则冲突。

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 399 passed，40 skipped。
- `agent.yml`（run 38210596957，b9f1635）：ubuntu、windows 都通过。
- 用户真机（中文 Windows 11）：`tests/test_hooks.py` 52 passed，1 skipped。

**独立 Verifier 结论**：A1–A10 全部通过，第 1 轮的问题 1–7 都已修好。新发现：

| # | 问题 | 级别 | 处理（第 3 轮） |
|---|---|---|---|
| 1 | Codex 的 `[features] hooks = false` 被忽略，hooks 仍然运行 | 阻断 | 读取这个开关（旧名 `codex_hooks`），关掉时所有 Codex hooks 为 `disabled` |
| 2 | `timeout` 为 `NaN` 时绕过上限（JSON 解析接受 NaN）；`guards.toml` 的 `timeout_s = nan` 同样 | 次要 | JSON 不接受 NaN/Infinity，整份文件报错；`timeout`、`timeout_s` 必须是有限数 |
| 3 | 主目录经符号链接到达时，全局 `guards.toml` 被当成项目文件；里面有 command 规则时，主目录下的动作全被拒绝 | 次要 | 按真实路径识别全局文件 |
| 4 | 第 2 轮加的“跳过 `~/.winhand/guards.toml`”在 `WINHAND_HOME` 指向别处时违背 R2 | 次要 | 撤销这个跳过，改为第 3 项的真实路径判断 |
| 5 | Codex 的 `timeout = 0` 被当成默认值，Codex 按 1 秒 | 次要 | 按 Codex：至少 1 秒 |
| 6 | `fs_write` 内容里有孤立代理字符时，走了“hooks 无法运行”的拒绝路径 | 小 | 交给 hook 的 JSON 用替换字符编码 |
| 7 | D7 的 PowerShell 尾巴在两种情况下会让 hook 变宽松（沿用旧的退出码；最后一个 cmdlet 失败时退出码变 0） | 次要（只读代码推断） | 收窄为只在最后一条命令失败时传出退出码 |
| 8 | Windows 上 hook 启动后才放进作业对象，期间启动的孙进程可能逃出 | 疑似 | 挂起启动，放进作业后再恢复运行 |
| 含糊 | Codex 会整份拒绝的 hooks 文件（不认识的键、缺 `type`、类型不对），winhand 仍运行其中别的 hook | — | 按 Codex：整份不用，报出原因 |
| 含糊 | 链接的 git 工作树，Codex 从主检出目录读 hooks 和信任 | — | 按 Codex 处理 |
| 含糊 | hook 正常结束后，它启动的后台进程是否结束 | — | 结束；写进 R5 |

## 第 3 轮（3769fed，文档 f9e9e9f）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 406 passed，40 skipped。
- `agent.yml`（run 38212031588，3769fed）：ubuntu、windows 都通过。

**独立 Verifier 结论**：A1–A10 全部通过。Verifier 从 openai/codex 的 main 分支（2026-10-12）取了源码，把 Codex 的 serde 类型放进一个小 Rust 程序，逐个核对文件是否被接受、哈希是否一致（6 个 handler 的哈希与 Codex 算出的完全相同）。

第 2 轮问题的核对：2、3、4、5、6 已修好；7（PowerShell 尾巴）、8（挂起启动加作业对象）只读代码判断为已修好；1（features 开关）、工作树部分修好，整份拒绝修错了，见下表。

| # | 问题 | 级别 | 处理（第 4 轮） |
|---|---|---|---|
| N1 | 整份拒绝比 Codex 严：Codex 只有 `hooks.json` 顶层拒绝不认识的键，组和 handler 里不认识的键（`description`、`shell`、`args`）、`matcher: null` 都照常读。winhand 却整份不用，已信任、会拒绝的 hook 被跳过（Verifier 实际跑出：写进 `src/` 没被拦下） | 阻断 | 按 Codex 的 serde 定义重写整份拒绝的判断：只有顶层拒绝不认识的键；组、handler、事件名里不认识的都忽略；`matcher` 可为 null（研究笔记 §1 本来就这样写，第 3 轮的实现与它相反） |
| N1b | 反过来，Codex 会整份拒绝、winhand 却部分使用的：不认识的 `type`（如 `http`）、command 没有 `command`、`description` 或 `additionalContextLimit` 类型不对、别的事件下的组写错、mcp_tool 缺 `server`、UTF-8 字节顺序标记 | 次要（更严） | 对 12 个已知事件全部按 serde 规则检查；Codex 的 `hooks.json` 不接受字节顺序标记 |
| N2 | `.codex` 文件夹的信任：Codex 取第一个有记录的（这一级、项目根、主检出目录），winhand 是“有一个 untrusted 就不信任” | 次要（更松） | 按 Codex：取第一个有记录的；`[projects]` 也读系统层 `config.toml` |
| N3 | `[features]` 只读用户文件；Codex 合并系统层、用户层、受信任的项目层，再由 `requirements.toml` 强制 | 次要 | 按 Codex 合并（含旧式托管文件），同一张表里 `hooks` 优先于 `codex_hooks`，`requirements.toml` 最后强制；命令行参数看不到，写进规格 |
| N4 | 工作树：Codex 的信任先看工作树自己；Codex 要求工作树这一级有 `.codex` 文件夹；Claude Code 的工作区信任记在主检出目录上 | 次要 | 三处都按来源工具处理 |
| N5 | `context()` 里 hooks 的错误部分没有大小上限（8000 个顶层键时返回 23 万字符，挤掉了其他内容） | 次要 | 每条错误最多 300 字符，错误也计入 hooks 部分的大小预算 |
| N6 | 守卫找项目规则时，每一级都算一次真实路径，即使缓存里已有（`fs_search` 过滤 2000 个文件夹时慢了约一倍） | 疑似（性能） | 只在候选文件存在、且缓存里没有时才算真实路径 |
| 小 | Claude Code hook 的超时提示里出现“Codex wants a whole number” | 小 | 只对 Codex 的 hook 这样提示 |
| 小 | `[hooks] timeout_s = inf` 被接受 | 小 | 必须是有限数 |
| 小 | action-guard 规格里 command 规则的超时与代码不一致 | 小 | 规格改为：没写时取 `[hooks] timeout_s`，写了也不超过它 |
| 小 | Codex 的 matcher 被去掉空白后比较（`" "` 在 winhand 里匹配全部，在 Codex 里什么也不匹配） | 小（更严） | 按写的原样比较 |
| 小 | 旧式托管文件 `managed_config.toml` 里的 hooks 没有读 | 小 | 按托管来源读取（路径与 Codex 相同） |
| 小 | `SetInformationJobObject` 的返回值没检查；失败时结束不了 hook 留下的进程 | 小（只读代码） | 失败时不用作业对象，退回按进程树结束 |
| 小 | A6 的测试仍断言 `rm -rf` 不在输出里，第 2 轮换词后它什么也证明不了 | 小 | 改为断言被拒的那一行没有发出 |
| 另 | Codex 不认识的 `if`、`once`、`asyncRewake` 让 Codex 的 hook 被列为不支持（修 N1 时发现） | 次要（更松） | 这几个键只对 Claude Code 的 hook 生效 |
| 另 | Windows 上 `commandWindows` 为空字符串时，Codex 跳过这个 hook，winhand 却改用 `command`（修 N1 时发现） | 小 | 按 Codex：设了就用它，为空时这个 hook 不运行 |

**Verifier 指出的含糊处**
- `[hooks] sources = []` 时 `guards.toml` 的 command 规则是否还运行：照常运行，它是 winhand 自己的规则。brief A10 和 hooks 规格已写明。
- 会话输入交给 Codex hook 时作为 `Bash`：按 brief 的表，有意为之（Codex 的 `write_stdin` 本身没有 PreToolUse）。
- “Codex 会运行什么”以 openai/codex main 分支 2026-10-12 的源码为准。

**留下的、不处理的差异**（都是 winhand 更严，或者极少见）
- 同一张表里重复的键：Codex（serde）整份拒绝，Python 取最后一个。
- 组或 handler 写成 JSON 数组的位置形式：serde 接受，winhand 整份不用。
- Codex 的正则语法与 Python 的个别差异。

## 第 4 轮（1313e3e）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 428 passed，40 skipped。
- `agent.yml`（run 38214141158，1313e3e）：ubuntu、windows 都通过。

**独立 Verifier 结论**：A1–A10 全部通过。第 3 轮的 N1–N6、所有“小”和“另”的条目都已修好（Windows 专属的两条只读代码判断）。

Verifier 的核对方法：
- 用 Codex 实际使用的 serde 类型和 toml 0.9.5 重建对照程序，对比了 94 个 `hooks.json` 用例、21 个 `config.toml`/`requirements.toml` 用例；
- 另外 5 个 handler 的哈希与 Codex 的 Rust 代码算出的完全一致。

新发现：

| # | 问题 | 级别 | 处理（第 5 轮） |
|---|---|---|---|
| P1 | 可选字段设为 null（`"statusMessage": null`、`"commandWindows": null`）时 hook 被列为不支持，已信任、会拒绝的 hook 不运行（Codex 按“没设”照常运行，哈希也不变） | 次要（更松，实测） | null 等于没设；Claude Code 的 `args: null` 同样处理 |
| P2 | 经符号链接到达的项目（例如 macOS 的 `/tmp`、链接过去的代码目录）：Codex 先按真实路径查信任，`hooks.state` 的键也可能是真实路径；winhand 只按写法查，于是显示未信任，拒绝的 hook 不运行 | 次要（更松，实测） | 信任按真实路径、再按写法查；`hooks.state` 的键按写法和真实路径各查一次；Claude Code 的工作区信任同样 |
| P3 | 判断链接的工作树时只看 `commondir`，没有核对 git 的其他记录；一个解压出来的文件夹可以自称是某个已信任仓库的工作树，让它的 `.claude/settings.json` hooks 被当成已信任 | 疑似 | 按 Codex（git-utils trust.rs）核对全部记录：git 文件夹在 `<公共目录>/worktrees/` 下、`gitdir` 指回本工作树、`commondir` 指向公共目录、主检出目录的 `.git` 就是公共目录；有一处不符就不算工作树 |
| 小 | 旧式托管文件的 `[projects]` 没有参与信任判断 | 小 | 参与，且优先于用户层 |
| 小 | mcp_tool 的 `input` 里超出 u64 的整数：serde_json 读成浮点数，Codex 接受；winhand 整份拒绝 | 小（更松） | 按 serde 判断：只有 2^63 到 2^64 之间的整数 TOML 装不下 |
| 小 | `context()` 读 hooks 出异常时，错误文字没有截短 | 小 | 截短到 300 字符 |
| 规格 | workspace-context R13 的 hooks 大小与代码不一致；hooks R1 没提 Codex 的 `project_root_markers` 和没有 `HEAD` 的 `.git` | 小 | 规格写明：列表约 6,000 字符，连同错误不超过约 10,000；这两处 Codex 的细节 winhand 不跟随 |

**留下的、不处理的差异**（第 3 轮已列的之外；都是 winhand 更严，或者极少见）
- TOML 里用日期时间当 `statusMessage`（toml 0.9 会转成字符串，winhand 整份拒绝）。
- `timeout` 写成 `-0`、字符串里有孤立代理字符、`requirements.toml` 的 `managed_dir` 不是路径：Codex 整份拒绝，winhand 照常读。

## 第 5 轮（6b31917）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 433 passed，40 skipped。
- `agent.yml`（run 38215834162，6b31917）：ubuntu、windows 都通过。

**独立 Verifier 结论**：A1–A10 全部通过。第 4 轮的 P1、P2、P3 和各“小”条目都已修好，没有回退：第 4 轮的 94 个 JSON、21 个 TOML 用例重跑，只剩已知的差异。

P3 的核对方法：Verifier 把 `_main_checkout` 与 Codex 的 `resolve_root` 逐步对照，并用真实的 `git worktree add` 实测了这些情况，都与 Codex 一致：
- 绝对路径；
- git 2.48 起的相对路径写法；
- 经符号链接到达；
- `--separate-git-dir`；
- `.git` 文件本身是链接。

新发现：

| # | 问题 | 级别 | 处理（第 6 轮） |
|---|---|---|---|
| N1 | git 的记录文件（工作树的 `gitdir`、`commondir`）是命名管道时，查找 hooks 永远卡住；调用不会执行（失败即关闭），但每次占住一个工作线程。第 4 轮的 `commondir` 读取已有这个问题，第 5 轮又多了两处 | 次要（实测） | 这些记录只读不超过 64 KiB 的普通文件、不跟随链接（与 Codex 的 `read_metadata_file` 相同）；client-rules 的仓库识别也改用同一个读法 |
| N2 | 第 5 轮的链接处理走得太远：`.codex` 文件夹或 `hooks.json` 本身是指向另一个已审核项目的链接时，借用了那边的 `trusted_hash`，Codex 却会把它当成未审核 | 次要（实测；多运行了 Codex 不运行的 hook） | 只把装着 `.codex` 的项目文件夹（用户层为 Codex 主目录）换成真实路径；`.codex` 和文件本身不跟随链接 |
| N3 | Claude Code 据其代码（2.1.296 的打包代码，Verifier 只读未运行）会因一个写错的 hook（null、`timeout: 0` 等）整份跳过设置文件，winhand 仍读取其余 hooks；这样的文件里的 `disableAllHooks` 在 winhand 中仍生效 | 次要 | 不复刻 Claude Code 的设置校验：差异几乎都是 winhand 更严；`disableAllHooks` 那一处，仓库用一份写对的文件同样能做到，不带来新的绕过。写进规格 |
| N4 | Codex 的 hooks 文件里有 `1e400`（超出范围）或 `-0`（serde_json 读成浮点数）时 Codex 整份拒绝，winhand 照常读 | 小 | 按 serde_json 读数：`1e400` 无法读取，`-0` 是浮点数 |
| 规格 | 链接那句话的范围；null 规则对两种来源都适用 | 小 | 规格改正 |

## 第 6 轮（cde2e9f）

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 438 passed，40 skipped。
- `agent.yml`（run 38217738867，cde2e9f）：ubuntu、windows 都通过。

**独立 Verifier 结论**：A1–A10 全部通过，没有阻断问题。第 5 轮的 N1（命名管道）、N2（链接的范围）都已修好，前任 Verifier 的实验和新增的情况都与 Codex 一致：
- 经链接的上级目录到达的项目；
- 嵌套的 `sub/.codex`；
- 链接的 `CODEX_HOME`；
- 用户层 `hooks.json` 本身是链接；
- 直接到达或经链接到达的工作树。

N3 按决定写进规格。N4 修过了头，又有一处没修到：

| # | 问题 | 级别 | 处理（第 7 轮） |
|---|---|---|---|
| R6-1 | `1e400` 出现在 Codex 不读取的地方（组里不认识的键、不认识的事件名下），Codex 照常加载，winhand 却整份拒绝，已信任的 hook 不运行。第 6 轮引入，实测 | 次要（更松） | 读数时不再报错，而是标成“超出范围”；只有它出现在 handler 里（serde 会读 handler 的每个值）或 mcp_tool 的 `input` 里时才整份拒绝 |
| R6-2 | handler 里三百多位的整数：serde_json 读成浮点数后溢出，Codex 整份拒绝；winhand 照常读 | 小（更严） | 整数按 serde_json 的读法：超出 64 位整数范围的是浮点数，溢出的标成“超出范围”；超过 Python 位数上限的同样处理 |
| R6-3 | client-rules 的仓库识别把 git `config` 限在 1 MB，一个 1.26 MB 的 `config`（14000 个分支段）在第 5 轮能找到，第 6 轮找不到 | 小（回退） | 上限提到 64 MB；workspace-context 规格 R8 写明只读普通文件和各自的上限 |
| R6-4 | 规格没写数字的读法；“不认识的键照常读”与 R6-1、R6-2 的情况不符 | 小 | 规格写明数字的读法和例外 |
| R6-5 | 规格说 `disableAllHooks` 是“唯一的例外”，但托管设置文件里的 `disableAllHooks`、`allowManagedHooksOnly` 同样不随 Claude Code 的整份跳过 | 疑似 / 小 | 规格写明；这些文件只有管理员能写 |

**Verifier 对“多运行或少运行 hook”的回答**
- 少运行：R6-1（已修）；超过 4300 位的整数（已一并修）；N3 写明的例外和 R6-5（已写进规格）。
- 多运行：R6-2（已修）；第 4 轮已列的差异（重复键、孤立代理字符、`managed_dir` 不是路径、TOML 日期时间）。

## 第 7 轮

**正式检查**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 443 passed，40 skipped。
- 新增测试：
  - 组里和不认识的事件下的 `1e400` 不影响加载，已信任的 hook 照常运行；
  - handler 里和 mcp_tool `input` 里三百多位的整数让整份文件被拒绝；
  - 整数的各个边界；
  - 1 MB 以上的 git `config` 仍能读出远端。

**针对性复核（b40561f）**：R6-3 已修好（1.26 MB、5.4 MB 的 `config` 都能找到）；R6-1 成立。但 Verifier 发现，第 4 轮起用来对照的“Codex 的 serde”程序与 Codex 实际的编译方式不同：

- Codex 的 `exec-server-protocol` 依赖开启了 serde_json 的 `arbitrary_precision`（还有 `raw_value`），Cargo 把这些特性合并到整个程序，hooks 的读取也受影响。
- 证据有三处：main 分支和 rust-v0.162.1 的 `Cargo.toml`（Builder 另行核对过 main）；npm 发布的 0.162.1 程序里含 59 处只有开启这个特性才有的常量；按真实特性重建的对照程序。
- 开启后，serde_json 不对它不按类型读取的数做范围检查，超出范围的数按原样保存。

所以第 6 轮的 R6-2 是误报，第 7 轮为它加的拒绝反而让 winhand 跳过 Codex 会运行的 hook：
- handler 里不认识的键、mcp_tool `input` 里的 `1e400` 或几百位的整数，会让整份文件被拒绝；
- 第 4 轮起 `input` 里 2^63 到 2^64 的整数也会。

按真实特性重新对照了 380 个新用例，加上之前各轮的 161 个。

| # | 问题 | 级别 | 处理 |
|---|---|---|---|
| R7-1 | 上述误拒：已信任的 hook 因旁边一个“超出范围”的数不运行 | 次要（更松，实测） | 去掉 handler 里超出范围就拒绝的检查和 `input` 的整数区间检查；要求整数的字段照旧检查；测试改为“这些数不影响加载，旁边的 hook 照常运行” |
| R7-2 | TOML 写的 mcp_tool `input` 里有 `inf`、`nan`，Codex 拒绝，winhand 照常读 | 小（更严） | 一并改为拒绝 |
| 规格 | hooks R2 关于数字的说法按错误的对照写成 | 小 | 按 `arbitrary_precision` 重写 |

**改后复核**（Builder 用 Verifier 留下的两个对照程序和比较脚本重跑）：
- 380 个新用例加 19 个其他用例：剩 6 处不一致，都是 Codex 拒绝、winhand 照常读（更严）：
  - 5 个在 `input` 里故意用 serde_json 内部键名（`$serde_json::private::Number`、`RawValue`）的用例；
  - 1 个 TOML 里超出 64 位的整数（Rust 的 toml 解析失败）。
- 之前各轮的 176 个用例：剩 7 处，都是已列出的差异（重复键、位置形式、孤立代理字符、顶层 null、`managed_dir`、TOML 日期时间）。

**正式检查（d30181a、c2741ea）**
- 云端 Linux：`ruff check`、`ruff format --check` 通过；`pytest` 447 passed，40 skipped。
- `agent.yml`：b40561f（run 38240456862）ubuntu、windows 都通过；c2741ea（run 38241999269）ubuntu、windows 都通过。

## 结论

- A1–A10 全部通过：第 1 轮起每轮的 Verifier 都判为通过，第 3 到 7 轮在不断收紧的对照下仍然通过。
- 7 轮独立验收：
  - 阻断问题 4 个：第 1 轮 2 个、第 2 轮 1 个、第 3 轮 1 个；
  - 第 4 轮起没有阻断问题，处理的是与 Codex 读文件、判信任方式的细小差异；
  - 第 7 轮发现第 4 轮起对照用的 Codex 程序少了 `arbitrary_precision`，据此撤回了一处误判（R6-2）。
- 核心约束“只运行来源工具会运行的 hook，结论只会更严”的核对方法：
  - 用 Codex 实际的 serde 类型和编译特性做对照，共 570 多个用例；
  - 剩下的差异都已列出，几乎都是 winhand 更严。

**已知限制**（不影响验收）：
- 与 Codex 的差异：重复键、组或 handler 写成数组的位置形式、孤立代理字符、顶层 null、`managed_dir` 不是路径、TOML 日期时间、`input` 里故意用 serde_json 内部键名、TOML 里超出 64 位的整数。
- Codex 的 `project_root_markers`、没有 `HEAD` 的 `.git`、命令行参数（`-c`、`--profile`）不跟随。
- 与 Claude Code 的差异：不复刻 Claude Code 的设置校验（N3），例外写在规格里。
- Windows 上的行为（PowerShell 退出码、挂起启动加作业对象、不弹窗）由 GitHub Actions 的 Windows 跑过测试；用户真机只跑到第 2 轮的代码。
- **待做**：经真实 winhand 服务在用户真机上冒烟。WinHand 连接器需要用户在 claude.ai 的连接器设置里重新授权。发布 0.4.6 前补做，结果记入发布说明；有问题另开 change。

**用户决定**：用户把推进交给 Claude（“交给你了”）。Claude 按验收结论归档，并在汇报中说明 D1–D7 这几项代定的决定，特别是 D2：已信任的 hook 默认运行，这改变了默认行为。
