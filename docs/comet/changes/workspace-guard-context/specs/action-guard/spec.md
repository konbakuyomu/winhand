# action-guard

## Purpose

winhand 在执行会读写这台机器的工具调用前，先把调用描述成“动作”，再按这台电脑上写好的规则决定：放行、拒绝、请人确认，或者放行并附一句提醒。这一层对所有 MCP 客户端一视同仁，没有规则文件时不产生任何拦截。

## Requirements

### R1 动作描述

每个会读写这台机器的工具调用，都被描述成一个或多个动作。动作包含以下字段：

- `kind`：动作类型；
- `paths`：涉及的路径；
- `text`：命令行或输入的文字；
- `cwd`：工作目录；
- `host`：远程主机；
- `tool`：发起调用的工具名。

| 工具 | 动作 |
|---|---|
| `run` | `command`：`text` 为命令行或脚本内容，加上 `stdin`（交给 shell 或解释器的内容同样会执行）；`cwd` 为调用的 `cwd` |
| `job_start` | `command`：`text` 为命令，`cwd` 为调用的 `cwd` |
| `session_start` | `command`：`text` 为 profile 与参数拼出的命令行 |
| `remote_run` | `command`：`text` 为脚本加参数，`host` 为目标主机 |
| `session_send` | `input`（见 R9） |
| `fs_write`、`fs_write_bytes`、`fs_upload_url` | `write` |
| `secret_prompt` 带 `save_to` | `write`，路径为 `save_to` |
| `fs_op` 的 `copy` | 写入路径为 `write`，源路径为 `read`。`to` 是已有文件夹时，写入路径是 `to/<源文件名>`，与 `fs_op` 实际写入的位置一致 |
| `fs_op` 的 `mkdir` | `write` |
| `fs_op` 的 `delete` | `delete` |
| `fs_op` 的 `move` | `move`，源和写入路径都在 `paths` 中；另有源路径的 `read`（移走文件等于交出它的内容）和写入路径的 `write`。写入路径的确定同 `copy` |
| `fs_edit` | `edit` |
| `fs_apply_patch` | `edit`，`paths` 为补丁涉及的全部文件（相对 `root` 解析）；补丁重命名的源文件另有 `read` |
| `session_stop`（不带 `force`） | `input`（见 R9）：会话里已输入的内容加上退出命令，作为被执行的一行 |
| `session_prompt_user` | `input`（见 R9）：已输入的内容不为空时，它加上口令作为被执行的一行 |
| `fs_read`、`fs_send`、`fs_download_url` | `read` |
| `fs_search` | `read`，路径为搜索的目录或文件；搜索文件夹时，结果里读取规则拒绝或要求确认的文件被略去，`guard_hidden` 写明略去了哪些 |

其他工具（查询类，以及桌面类工具）不产生动作，不受守卫影响。参数格式不对、无法描述成动作的调用，交给工具自己的参数校验处理，不会执行。

路径按工具自己的方式解析：`fs_*` 工具展开 `~`、相对路径以 winhand 的工作目录为基准、不展开环境变量；`fs_upload_url` 和 `fs_download_url` 另外展开 `%VAR%` 和 `$VAR`。守卫判断的路径与工具实际操作的路径一致。

### R2 规则文件

- 全局规则：`<WINHAND_HOME>/guards.toml`。默认是 `~/.winhand/guards.toml`。
- 项目规则：从动作路径向上，逐级收集所有 `.winhand/guards.toml`。命令类动作从 `cwd` 开始向上找；没有 `cwd` 时只用全局规则。
- 文件内容是若干条 `[[guard]]`，每条的字段如下：

  | 字段 | 必填 | 含义 |
  |---|---|---|
  | `on` | 是 | 动作类型，或类型列表 |
  | `match` | 否 | 正则，匹配 `text` |
  | `path` | 否 | glob 列表，匹配 `paths` 中任一路径 |
  | `decision` | 是 | `deny`、`ask` 或 `note` |
  | `reason` | 否 | 给模型和人看的说明 |
  | `timeout_s` | 否 | 仅 ask 规则，等待确认的秒数，默认 40，超过 50 按 50 计（客户端约 60 秒放弃一次工具调用） |

- 规则编号从 1 开始，按文件里出现的顺序计算。
- 文件修改后，下一次调用即生效，不需要重启 winhand；即使修改前后大小和修改时间都相同也一样。

### R3 匹配

- 一条规则命中，需要同时满足：
  - 动作类型在 `on` 里；
  - 写了 `match` 时，`match` 用 `re.search` 能在 `text` 中找到；
  - 写了 `path` 时，`paths` 中至少一个路径匹配其中一个 glob。
- `path` 的 glob 支持 `**`。Windows 上不区分大小写，`/` 与 `\` 等价。
- glob 相对路径的基准：项目规则相对于该 `.winhand` 所在目录；全局规则的相对模式匹配任意位置（`*.pem` 等同 `**/*.pem`）。
- 每个路径同时按以下写法判断，任一写法命中即命中：去掉 `\\?\`（及 `\\?\UNC\`）前缀和 NTFS 数据流后缀（如 `::$DATA`）的写法；解析符号链接、目录联接和短文件名后的真实路径。查找项目规则文件时也使用真实路径。
- 正则编译失败按 R10 处理。

### R4 裁决

- 所有命中的规则合并裁决：有 deny 则拒绝；否则有 ask 则请人确认；否则有 note 则放行并附注；都没有则放行。
- 规则没有 allow，项目规则不能放宽全局规则。

### R5 拒绝

- 工具不执行，不产生任何副作用。
- 以工具错误返回，内容包括：
  - 动作类型；
  - 规则文件路径和规则编号；
  - `reason`；
  - 一句指引：这条限制来自这台电脑的主人，不要换一种方式绕过。
- 这次调用照常出现在 `history()` 和桌面端时间线中，状态为错误；内容经过 `[redact]` 脱敏。

Scenario: (A2) 拒绝批量删除命令
- 设置：全局 `guards.toml` 有 `on = "command"`、`match` 能匹配 `Remove-Item … -Recurse` 和 `rm -rf` 的 deny 规则；
- 操作：调用 `run(command="Remove-Item -Recurse C:\\tmp\\x")`，再调用 `run(command="rm -rf /tmp/x")`；
- 结果：两次都没有执行，都返回包含规则文件、规则编号和理由的工具错误；`history()` 的最近两条是这两次调用，状态为错误。

Scenario: (A3) 拒绝读取 .env
- 设置：全局规则 `on = "read"`、`path = ["**/.env"]`、`decision = "deny"`；
- 操作：分别用 `fs_read`、`fs_send`、`fs_download_url` 访问某目录下的 `.env`；
- 结果：三次都被拒绝；读取同目录下的 `README.md` 不受影响。

Scenario: (A4) 项目规则只管自己的目录
- 设置：`<proj>/.winhand/guards.toml` 有 `on = "write"`、`path = ["build/**"]` 的 deny 规则；
- 操作：分别写 `<proj>/build/a.txt` 和 `<other>/build/a.txt`；
- 结果：前者被拒绝，后者写入成功。

### R6 请人确认

- 在这台电脑上弹出确认框，显示：动作类型、命令或路径、规则的 `reason`、发起的工具名。
- 默认按钮是“否”：确认框会盖在人正在操作的窗口上，误按回车或空格不能放行。
- 选“是”：照常执行。
- 选“否”：按 R5 拒绝，理由注明“电脑前的人拒绝了”。
- 超时或无法弹窗：同样按 R5 拒绝，理由注明是超时还是无法弹窗。

Scenario: (A5) 确认后执行，拒绝或超时不执行
- 设置：规则 `on = "delete"`、`decision = "ask"`；
- 操作：调用 `fs_op(action="delete", ...)` 三次，确认框分别回答“是”、“否”、超时；
- 结果：第一次删除成功；后两次都没有删除，并分别注明“被拒绝”和“超时”。

### R7 附注

- 动作照常执行。
- 结果的文字内容末尾追加一段附注，列出每条命中的 note 规则的 `reason`。
- 结构化结果是对象时，另加字段 `guard_notes`。

Scenario: (A6) 附注随结果返回
- 设置：规则 `on = "edit"`、`path = ["**/*.c"]`、`decision = "note"`、`reason = "改完记得跑单元测试"`；
- 操作：用 `fs_edit` 修改一个 `.c` 文件；
- 结果：修改成功，结果中能看到“改完记得跑单元测试”。

### R8 转发的调用

经 `call(tool, arguments)` 转发的调用，与直接调用该工具受同样的规则约束。

Scenario: (A7) call 转发同样受约束
- 设置：一条拒绝写入 `**/locked/**` 的规则；
- 操作：调用 `call("fs_write", {"path": ".../locked/a.txt", "content": "x"})`；
- 结果：被拒绝，文件没有被创建。

### R9 交互式会话的输入

- 只对 shell 类会话生效。判定条件：profile 为 `shell` 或 `ssh`，或程序名（去掉路径和扩展名）是 pwsh、powershell、cmd、bash、sh、zsh 之一，或没有指定程序（默认 shell）。gdb、串口、python 等会话不产生动作。
- 守卫为每个 shell 会话记住“已输入、还没执行”的内容。一次 `session_send` 按“文字 → 按键 → submit”的顺序处理：
  - 换行、Enter 键（以及 Ctrl+M、Ctrl+J）和 `submit` 都会执行当前这一行；以 `\` 或反引号结尾的行不单独执行，与下一行合起来；
  - 每个被执行的行（含之前输入、还没执行的部分）产生一个 `input` 动作，`text` 为这一行，`cwd` 为会话的工作目录；
  - Backspace（文字中的 DEL、BS 字符同样）删掉最后一个字符；Ctrl+C 在终端会话中清掉正在输入的一行，在管道会话中只是一个字符；
  - 行中出现光标移动、历史记录、Tab 补全等编辑键或其他控制字符时，最终执行的内容无法从输入推断：这一行标为“经过编辑”，任何带 `match` 的 `input` 规则都视为命中，拒绝信息提示先用 Ctrl+C 清行，再直接输入并提交。
- `session_stop`（不带 `force`）会输入退出命令并回车，`session_prompt_user` 会输入口令并回车，两者同样按被执行的行判断；`session_stop` 被拒绝时，提示可以用 `force=true` 结束会话而不输入任何内容。
- 只输入、不执行的文字不检查。
- 同一会话的输入调用按顺序判断和发送：后一个调用等前一个结束后才判断，判断用的已输入内容总是最新的。只发送 Ctrl+C 的调用不排队。
- 被拒绝时这次调用的文字和按键都不发给程序，记住的内容不变。

Scenario: (A8) shell 会话里的命令也受约束
- 设置：一条 `on = "input"` 的规则，`match` 能匹配 `rm -rf`；
- 操作：在 shell 类会话中依次：
  - `session_send(text="rm -rf /tmp/x", submit=true)`；
  - 发送不带 `submit`、但以换行结尾的 `"rm -rf /tmp/y\n"`；
  - `session_send(text="rm -rf /tmp/z", keys=["Enter"])`；
  - 先只输入 `"rm -rf /tmp/w"`（不执行），再分别用 `keys=["Enter"]` 和 `text="", submit=true` 执行；

  在非 shell 会话中发送同样的文字。
- 结果：只输入的那一次放行；shell 会话里其余各次都被拒绝，程序没有执行这些行；非 shell 会话照常发送。

### R10 规则文件出错

- 出错的情况：TOML 解析失败、字段缺失或类型不对、`decision` 不在三种之内、正则编译失败、文件存在但读不了。
- 不算出错、视为没有规则文件：路径不存在、路径中某一级是文件而不是目录、winhand 没有权限查看的目录。
- 出错文件管辖的范围：全局文件出错时是全部动作，项目文件出错时是该目录下的动作。这个范围内的动作一律拒绝，错误里写明文件路径和具体错误。
- 文件修正后下一次调用即恢复。

Scenario: (A9) 规则文件写错时说明原因
- 设置：全局 `guards.toml` 内容不是合法 TOML；
- 操作：调用 `run(command="echo hi")`；
- 结果：被拒绝，结果写明 `guards.toml` 的路径和解析错误的位置；把文件改成合法内容后，再次调用照常执行。

### R11 默认规则

winhand 不自带任何规则。README 给出示例规则，包括“禁止批量删除”和“禁止读取 .env”。

### R12 没有规则时不变

没有任何规则文件时，所有工具的行为、返回内容和性能与之前一致。

Scenario: (A13) 没有规则时行为不变
- 设置：没有 `guards.toml`，也没有 `[context]` 配置；
- 操作：运行原有的全部测试和 lint、格式检查；
- 结果：全部通过。
