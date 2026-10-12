# hooks

## Purpose

经 winhand 的调用，在执行前也会运行本机已有的 Codex、Claude Code PreToolUse hooks，并照它们的结论拒绝、请人确认或附注。只运行来源工具自己会运行的 hooks。

事实依据见 `docs/comet/changes/client-hooks/research.md`。

## Requirements

### R1 找到 hooks

- 对一个动作，winhand 按它的锚点目录（命令的 `cwd`，文件动作的文件所在目录；会话输入用会话的工作目录）找出对它生效的 hooks。
- 项目根的定义与 `context()` 相同：最近一个含 `.git` 的上级目录；找不到时为锚点目录本身。（Codex 可以用 `project_root_markers` 换掉 `.git` 这个标记，判断仓库主检出目录时还会跳过没有 `HEAD` 的 `.git` 文件夹；这两处 winhand 不跟随。）
- Codex（`[hooks] sources` 含 `codex` 时）：
  - 用户层：`$CODEX_HOME`（默认 `~/.codex`）下的 `hooks.json`，以及 `config.toml` 的 `[hooks]`；
  - 项目层：项目根到锚点目录沿途每一级的 `.codex/hooks.json` 和 `.codex/config.toml` 的 `[hooks]`；
  - 系统层（托管）：Windows 为 `%ProgramData%\OpenAI\Codex\`，其他系统为 `/etc/codex/`，读法同用户层；
  - 托管层：系统层 `requirements.toml` 的 `[hooks]`；旧式托管文件 `managed_config.toml` 的 `[hooks]`（其他系统为 `/etc/codex/managed_config.toml`，Windows 为 `$CODEX_HOME\managed_config.toml`，与 Codex 相同）。
  - 项目层只看存在 `.codex` 文件夹的那几级（Codex 也只为这些目录建立项目层）。
- Claude Code（`[hooks] sources` 含 `claude-code` 时）：
  - 用户层：`~/.claude/settings.json`（设置了 `CLAUDE_CONFIG_DIR` 时，从该目录读）；
  - 项目层：项目根的 `.claude/settings.json`、`.claude/settings.local.json`；
  - 托管层：`managed-settings.json` 和 `managed-settings.d/*.json`。位置：
    - Windows：`C:\Program Files\ClaudeCode\`；
    - macOS：`/Library/Application Support/ClaudeCode/`；
    - 其他系统：`/etc/claude-code/`。
- 读取格式：
  - `hooks.json`：`{"hooks": {"PreToolUse": [{"matcher": …, "hooks": [{"type": "command", …}]}]}}`；
  - Codex `config.toml`：`[[hooks.PreToolUse]]`、`[[hooks.PreToolUse.hooks]]`；
  - Claude Code `settings.json`：`"hooks"` 键，结构同上。
- 文件不存在时跳过。文件无法解析时，这个文件的 hooks 全部不运行，并在 `context()` 中报出错误；不影响其他文件。

### R2 信任

每个 hook 得出一个状态，只有 `managed` 和 `trusted` 的会运行。

Codex：
- Codex 的 `hooks` 功能（旧名 `codex_hooks`）关闭时，所有 Codex hooks 都是 `disabled`。按 Codex 的做法得出开关：默认开；依次合并系统层、用户层、受信任的项目层（项目根到锚点目录）、旧式托管文件的 `[features]`，后面的覆盖前面的；同一张表里 `hooks` 优先于 `codex_hooks`；最后 `requirements.toml` 的 `[features]` 可以强制开或关。
  - 命令行参数（`-c features.hooks=…`、`--profile`）winhand 看不到，不计入。
- 来自系统层和托管层的：`managed`。
- `requirements.toml` 中 `allow_managed_hooks_only = true` 时，其余来源都是 `disabled`。
- 其余每个 hook：
  - 键为 `<声明文件的绝对路径>:pre_tool_use:<组序号>:<组内序号>`，序号从 0 开始；
  - 在用户 `config.toml` 的 `[hooks.state."<键>"]` 中查找：
    - `enabled = false`：`disabled`；
    - `trusted_hash` 等于当前哈希（R3）：`trusted`；
    - `trusted_hash` 存在但不相等：`modified`；
    - 没有 `trusted_hash`：`untrusted`。
  - 经符号链接到达的项目：键先按写法查，再把装着 `.codex` 的项目文件夹（用户层为 Codex 主目录）换成真实路径查一次。`.codex` 文件夹和 hooks 文件本身不跟随链接：否则仓库可以把 `.codex` 链到另一个已审核过的项目，借用那里的信任。
- 项目层的 hook 还要求它所在的 `.codex` 文件夹被 Codex 信任，按 Codex 的方式逐个文件夹判断：依次看这一级目录、项目根、仓库的主检出目录在系统层、用户 `config.toml` 和旧式托管文件的 `[projects]` 里的 `trust_level`（后者覆盖前者），取第一个有记录的；每个目录先按真实路径（跟随符号链接）查，再按写法查；它是 `trusted` 才信任。不满足时为 `untrusted`，原因写明这个文件夹未被信任。
- `CODEX_HOME` 恰好是某个项目的 `.codex` 时，它只算用户层，不再作为项目层读第二次。
- 链接的 git 工作树（`.git` 是文件）：只有 git 自己的记录都对得上时才算（与 Codex 相同；这些记录只读不超过 64 KiB 的普通文件，不跟随链接，命名管道之类不会让读取卡住）：工作树的 git 文件夹在 `<公共目录>/worktrees/` 下，它的 `gitdir` 指回这个工作树，`commondir` 指向那个公共目录，主检出目录的 `.git` 就是这个公共目录。算作工作树时，按 Codex 的做法，工作树里存在 `.codex` 文件夹的那几级，hooks 从主检出目录里对应的 `.codex` 读取；信任按上一条，依次看工作树的这一级、工作树根、主检出目录。
- Codex 会整份拒绝的 hooks 文件，其中的 hooks 一个都不运行，在 `context()` 中报出原因。按 Codex 读文件的方式（serde）判断：
  - 整份拒绝：`hooks.json` 顶层有 `description`、`hooks` 以外的键；以 UTF-8 字节顺序标记开头；任一已知事件（不只是 PreToolUse）下的组或 handler 类型不对，例如 handler 没有 `type`、`type` 不认识（如 `http`）、command handler 没有 `command`、`timeout` 不是非负整数、`async` 不是布尔值、mcp_tool handler 没有 `server` 或 `tool`；
  - 不影响：组和 handler 里 Codex 不认识的键（例如 `description`、`shell`、`args`、`if`）、不认识的事件名、`matcher` 为 null，Codex 都忽略，winhand 也照常读取；可选字段（`commandWindows`、`statusMessage`、`timeout` 等）设为 null，等于没设；
  - 数字：Codex 编译时开启了 serde_json 的 `arbitrary_precision`，不按类型读取的数按原样保存，所以超出范围的数（`1e400`、几百位的整数）放在哪里都不会让文件被拒绝。只有要求整数的字段（`timeout`、`additionalContextLimit`）必须是 0 到 2^64−1 的整数，`-0`、小数、超出范围的数都会让整份文件被拒绝；mcp_tool 的 `input` 写在 TOML 里时不能有 `inf`、`nan`；
  - 只跳过一个 handler：在 Windows 上 `commandWindows` 设为空字符串，或命令只有空白（Codex 跳过这一个）。

Claude Code：
- 托管层的：`managed`。
- 链接的 git 工作树（判断方法同 Codex 一节）：工作区信任也看主检出目录的记录（Claude Code 按主检出目录记工作区信任）。经符号链接到达的项目，按写法和真实路径各查一次。
- 托管设置中 `allowManagedHooksOnly = true` 时，其余来源都是 `disabled`。
- 按 Claude Code 的设置优先级（托管 > 项目本地 > 项目 > 用户），`disableAllHooks` 为 true 时，非托管来源都是 `disabled`。
- 其余：`.claude.json` 的 `projects` 中，项目根的 `hasTrustDialogAccepted` 为 true 时为 `trusted`。项目根不是独立的仓库（没有 `.git`）时，上级目录的信任也算；是独立仓库时，上级目录的信任不覆盖它（工作区信任不覆盖嵌套的仓库）。否则为 `untrusted`。
- 托管设置里 `disableAllHooks = true` 时，所有 hooks（包括托管的）都是 `disabled`；其他设置文件里的 `disableAllHooks` 只关掉非托管的。

两种来源都适用：
- 可选字段设为 null 等于没设。（Claude Code 据其代码会因一个写错的 hook 整份跳过设置文件；winhand 不复刻 Claude Code 的设置校验，这样的文件里的 hooks 照常读取，只会更严。例外：这样的文件里的 `disableAllHooks` 在 winhand 中仍生效，仓库用一份写对的文件同样能做到，不带来新的绕过；托管设置文件里的 `disableAllHooks`、`allowManagedHooksOnly` 同样仍生效，这些文件只有管理员能写。）
- `type` 不是 `command` 的 hook、带 `async` 的 hook：`unsupported`，不运行。Claude Code 的 hook 带 `if`、`once`、`asyncRewake` 时同样不运行；这些键 Codex 不认识、直接忽略，所以 Codex 的 hook 照常运行。
- 字段类型不对的 hook（`command` 不是字符串、`timeout` 不是秒数，Codex 要求整数；`args` 不是列表）、matcher 不是有效正则的 hook：`unsupported`，写明原因，不运行，也不影响其他 hook 和这次调用。
- 配置文件只读普通文件，最多 4 MB；设备文件、过大的文件按“无法读取”报告。JSON 里的 `NaN`、`Infinity` 不是合法 JSON，整份文件按“无法读取”处理；`timeout` 不能是非有限数。

### R3 Codex 的哈希

- 当前哈希按 Codex 的算法计算：
  - 输入是 `{"event_name": "pre_tool_use", "matcher": <matcher，没有就省略>, "hooks": [<规范化的 handler>]}`；
  - 规范化的 handler 是 `{"type": "command", "command": <在 Windows 上取 commandWindows，没有时取 command>, "timeout": <timeout，默认 600>, "async": <默认 false>}`，再加上设置了的 `statusMessage`，以及设置了且不等于 2500 的 `additionalContextLimit`。
- 序列化为键排序、紧凑分隔（`,`、`:`）、不转义非 ASCII 的 JSON，取 SHA-256，写成 `sha256:<十六进制>`。

Scenario: (A4) 与 Codex 的测试值一致
- 设置：Codex 仓库 `config/src/fingerprint_tests.rs` 中的测试输入。
- 操作：用 winhand 的哈希函数计算。
- 结果：与该测试中的期望值相同。

### R4 交给 hooks 的输入

每个动作按来源转成该来源工具的工具名和输入：

| 动作 | Claude Code | Codex |
|---|---|---|
| `command`、`input` | `Bash`：`{"command": <命令行>}`；matcher 同时按 `Bash` 和 `PowerShell` 匹配 | `Bash`：`{"command": <命令行>}` |
| `write` | `Write`：`{"file_path", "content"}`，拿不到内容时为空字符串 | `apply_patch`：`{"command": <补丁>}`，matcher 同时按 `apply_patch`、`Write`、`Edit` 匹配 |
| `edit` | `Edit`：`{"file_path", "old_string", "new_string", "replace_all"}`；`fs_apply_patch` 对每个文件各发一份，`old_string`、`new_string` 为空字符串 | `apply_patch`：`{"command": <补丁>}`；`fs_apply_patch` 的补丁只列出涉及的文件（`*** Update File:`），不带内容 |
| `delete` | `Bash`：`Remove-Item -LiteralPath '<路径>'` | 同左 |
| `move` | `Bash`：`Move-Item -LiteralPath '<源>' -Destination '<目标>'` | 同左 |
| `read` | `Read`：`{"file_path"}` | 不交给 hooks |

- 补丁的格式（路径在项目根之内时写成相对路径，与 Codex 一致）：
  - `fs_write` 写新文件时，为 `*** Begin Patch` / `*** Add File: <路径>` / 每行加 `+` / `*** End Patch`；
  - 覆盖已有文件时，为 `*** Update File: <路径>`、`@@` 和新内容的 `+` 行；
  - `fs_edit` 时，为 `*** Update File: <路径>`，每处修改一个 `@@`，带 `-` / `+` 行。
- 一个调用产生多个动作时，每个动作分别交给 hooks；任何一个被拒绝，整个调用被拒绝。
- stdin 的 JSON：
  - 两种来源都有：`session_id`（winhand 本次连接的标识）、`cwd`（R5 的工作目录）、`hook_event_name`（`"PreToolUse"`）、`permission_mode`（`"default"`）、`tool_name`、`tool_input`、`tool_use_id`（每次调用一个新值）；
  - Codex 另有：`turn_id`、`model`（`"winhand"`）、`transcript_path`（null）。
- matcher：
  - 空、`*` 或没有（null）：匹配全部；
  - Codex：按写的原样比较，不去掉空白（只含空格的 matcher 什么也不匹配）；只含字母、数字、`_`、`|` 时，按 `|` 分隔的名字精确匹配；
  - Claude Code：只含字母、数字、`_`、`-`、空格、`,`、`|` 时，按 `|` 或 `,` 分隔的名字精确匹配；
  - 其余按正则匹配，不锚定。正则无效时，这一组跳过，在 `context()` 中报出。

### R5 运行

- 工作目录：动作所在项目的根目录；没有项目时，为锚点目录。
- Codex：
  - Windows 上依次尝试 `pwsh.exe -NoProfile -Command <命令>`、`powershell.exe -NoProfile -Command <命令>`、`cmd.exe /C <命令>`，用找到的第一个；
  - 经 PowerShell 运行时，命令后补一行 `if (-not $?) { if ($LASTEXITCODE) { exit $LASTEXITCODE } else { exit 1 } }`：只有最后一条命令失败（`$?` 为假，pwsh 本来会以 1 退出）时，才改为传出 `$LASTEXITCODE`，即最近一个程序的退出码（例如 2），没有时仍按 1；成功结束或自己 `exit` 的命令不受影响。Codex 本身不这样做，退出码 2 会变成 1（D7）。Claude Code 的 `shell = "powershell"` 和 `guards.toml` 的 `command` 规则同样处理；
  - Windows 上 hook 以挂起状态启动，放进作业对象后再运行，它启动的任何进程都在同一个作业里；
  - 其他系统用 `$SHELL -c <命令>`，没有 `$SHELL` 时用 `/bin/sh`。
- Claude Code：
  - 有 `args` 时，把 `command` 作为程序、`args` 作为参数直接运行；
  - `shell = "powershell"` 时，用 pwsh（找不到时用 powershell.exe）的 `-NoProfile -Command`；
  - 其余情况，Windows 上用 Git Bash 的 `bash -c`，找不到时用 PowerShell；其他系统用 `sh -c`；
  - 环境变量加上 `CLAUDE_PROJECT_DIR=<项目根>`。
- 环境变量继承 winhand 的环境。
- 同一个动作匹配到的 hooks 并行运行。
- 超时：取 hook 自己的 `timeout`（秒，默认 600；Codex 的 0 按 1 秒）与 `[hooks] timeout_s`（默认 30）中较小的一个。超时后结束整个进程树；在 Linux、macOS 上连同它的整个进程组。
- hook 的输入输出经临时文件传递。hook 退出后按它的退出码和输出判断，不受它留下的进程影响；hook 结束（或超时）后，它启动的进程一并结束。
- 运行时不弹出窗口。

### R6 结论

每个 hook 的输出按来源翻译：

| 输出 | Claude Code 来源 | Codex 来源 |
|---|---|---|
| 退出码 2，且 stderr 非空 | 拒绝，理由为 stderr | 拒绝，理由为 stderr |
| 退出码 2，stderr 为空 | 拒绝，理由写明 hook 拒绝但没说原因 | 失败（放行并附注） |
| 退出码 0，stdout 是一个 JSON 对象 | 按下面各行解析 | 按下面各行解析 |
| `permissionDecision: "deny"` | 拒绝，理由为 `permissionDecisionReason` | 理由非空时拒绝，否则为失败 |
| `permissionDecision: "ask"` | 请人确认，理由为 `permissionDecisionReason` | 失败 |
| `permissionDecision: "allow"` 或 `"defer"` | 不改变结论 | 不带 `updatedInput` 时为失败 |
| `updatedInput` | 拒绝，理由写明 hook 想改写这次调用的参数，请按它的意思改后重发，并附上 `updatedInput` 的内容 | 同左 |
| 顶层 `decision: "block"` | 拒绝，理由为 `reason` | `reason` 非空时拒绝，否则为失败 |
| `continue: false` | 拒绝，理由为 `stopReason` | 失败 |
| `additionalContext`、`systemMessage` | 附注 | 附注 |
| 其他退出码、超时、无法启动、stdout 以 `{` 开头但不是有效的 JSON 对象 | 失败；`onFailure = "block"` 时改为拒绝 | 失败 |

- Codex 来源的输出按 Codex 的严格方式读：有 Codex 不认识的字段，或出现 `continue: false`、`stopReason`、`suppressOutput: true`，都算失败。
- “失败”表示放行，并在结果里附注哪个 hook 失败及原因。
- winhand 自己在找 hooks、运行 hooks 时出错（不是 hook 本身失败）：这次调用被拒绝，并写明原因。宁可不做，也不跳过房主的 hooks。
- 一个动作的结论按以下顺序取最严的一个：拒绝 > 请人确认 > 附注 > 无。
- 与 `guards.toml` 的结论合在一起，也按这个顺序取最严的。hooks 不能放过 `guards.toml` 拒绝的动作。
- 拒绝时，工具错误写明：
  - 来源；
  - 声明文件与键（Codex）或设置文件与组序号（Claude Code）；
  - hook 给出的理由。
- 请人确认与 `guards.toml` 的 `ask` 用同一个窗口和超时；理由里写明是哪个 hook 要求确认。
- hook 的 stdout、stderr 经过 `[redact]` 脱敏后才放进结果，每段最多 2,000 字符。

Scenario: (A1) Codex 项目 hook 拦下写入
- 设置：
  - 项目已在用户 `config.toml` 中标为 trusted；
  - 项目 `.codex/hooks.json` 的 PreToolUse 组 matcher 为 `Write|Edit`，脚本读 stdin，补丁中含 `src/` 时向 stderr 写理由并以 2 退出；
  - 用户 `config.toml` 中这个 hook 的 `trusted_hash` 是当前哈希。
- 操作：调用 `fs_write("<项目>/src/a.c", …)`，再调用 `fs_write("<项目>/docs/a.md", …)`。
- 结果：
  - 第一次不写文件，工具错误含 stderr 原文和 hook 的键；
  - 第二次照常写入。

Scenario: (A2) Claude Code hook 的几种结论
- 设置：项目已接受工作区信任；`.claude/settings.json` 的 PreToolUse 组 matcher 为 `Bash`，脚本按命令内容分别输出 deny、ask、`additionalContext`、`updatedInput`。
- 操作：用 `run` 依次发出对应的四条命令；ask 时确认窗口选“否”。
- 结果：
  - deny、ask 两条不执行；
  - additionalContext 那条照常执行，结果带附注；
  - updatedInput 那条不执行，理由中有改写后的参数。

Scenario: (A3) 未信任的不运行
- 设置：
  - Codex hook 没有 `trusted_hash`，或 `trusted_hash` 与当前哈希不同，或项目未标为 trusted；
  - Claude Code 项目没有 `hasTrustDialogAccepted`。
- 操作：发出会被这些 hook 拒绝的调用。
- 结果：调用照常执行，hook 没有运行（以脚本留下的标记文件为证）。

Scenario: (A5) 失败照常放行
- 设置：hook 分别为：睡眠超过 `[hooks] timeout_s`，且先启动一个子进程；以退出码 1 退出；输出 `{not json`。
- 操作：发出匹配的调用。
- 结果：
  - 调用照常执行，附注写明 hook 失败；
  - 超时的 hook 及其子进程都已结束。

Scenario: (A6) shell 会话的输入
- 设置：一个 shell 会话；matcher 为 `Bash` 的 hook，命令含 `rm -rf` 时拒绝。
- 操作：`session_send` 提交 `rm -rf build`。
- 结果：这一行没有发到会话里，工具错误带上 hook 的理由。

### R7 开关

- winhand `config.toml` 的 `[hooks]` 段：
  - `sources`：默认 `["codex", "claude-code"]`；
  - `timeout_s`：默认 30，最小 1。
- `sources = []` 时不读任何 Codex、Claude Code 的 hooks 配置，不运行它们的 hook；`guards.toml` 的 command 规则是 winhand 自己的规则，照常生效。
- `[hooks]` 段在 `config.load()` 后完整保留，`config.save()` 时原样写回。

Scenario: (A10) 关掉与兼容
- 设置：`[hooks] sources = []`，存在会拒绝一切的已信任 hook。
- 操作：发出调用。
- 结果：
  - 照常执行；
  - 没有任何 hooks 配置时，原有测试全部通过。
