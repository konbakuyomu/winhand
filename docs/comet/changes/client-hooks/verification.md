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

## 第 3 轮

（待填）
