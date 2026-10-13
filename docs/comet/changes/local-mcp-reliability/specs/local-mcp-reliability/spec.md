# local-mcp-reliability：工具段每个工具都有说明；uv 项目的虚拟环境坏了时自动修好

依据见 `research.md`。R1 替换 `docs/comet/specs/local-skills-tools/spec.md` R4 中“工具段”的长度规则，其余条款不变。

### R1 工具段的长度

- 每个启用的服务一块：
  - `## <server>`；
  - 有缓存时：服务说明的第一行，然后每个工具一行 `- <tool>: <说明的第一个非空行>`；
  - 没有缓存时：一行“not listed yet”。
- 先对全部文字脱敏，再计算长度（UTF-16 单位）。
- 全部放得下时，每条说明最长 150 字符，服务说明最长 300 字符。
- 放不下时：
  1. 求出能放下全部内容的最长统一长度 `cap`（40 ≤ cap ≤ 150）。每条说明截到 `cap`，截断处加 `…`；服务说明截到 `min(300, 2 × cap)`；
  2. `cap` = 40 仍放不下时，从最后一个服务开始，把它改为只列工具名（逗号分隔）；
  3. 仍放不下时，从最后一个服务开始跳过，末尾说明跳过了几个服务；
  4. 最后仍超出时截断正文。
- 总长不超过预算（开场为 9,000）。

Scenario: (A1) 每个工具都有说明
- 设置：5 个服务，分别有 33、2、14、55、5 个工具，说明长度不一，大多超过 60 字符。
- 结果：
  - 总长不超过 9,000；
  - 每个工具一行，都有说明；
  - 截断后的说明长度相同且不少于 40 字符（本来更短的照原样）。

Scenario: (A1) 放得下时不截短
- 设置：两个服务，共 10 个工具。
- 结果：说明与 0.4.8 相同（第一行，最长 150 字符）。

Scenario: (A2) 工具极多
- 设置：10 个服务，每个 60 个工具。
- 结果：总长不超过 9,000；前面的服务每个工具都有说明；后面的服务只有工具名，或被跳过并在末尾计数。

### R2 哪些服务要检查虚拟环境

- 只检查 stdio 服务，且启动命令的文件名（去掉路径，不区分大小写）是 `uv` 或 `uv.exe`。
- 参数里第一个不是选项的词是 `run`。`run` 之前的选项按 uv 的全局选项读取：
  - `--directory`、`--project`、`--config-file`、`--cache-dir` 带一个值；
  - 也接受 `--opt=value` 的写法；
  - 其他以 `-` 开头的视为不带值的开关。
- `run` 之后、第一个不是选项的词之前，出现 `--no-project`、`--isolated`、`--script` 或 `--with-requirements` 时不检查。
- 项目目录：
  - `--directory` 的值；没有时用 `--project` 的值；
  - 相对路径以服务的 `cwd` 为准（没有 `cwd` 时以 winhand 的当前目录为准）；
  - 都没有时用服务的 `cwd`；
  - 仍没有时不检查。
- 虚拟环境目录：
  - `UV_PROJECT_ENVIRONMENT`（先看服务的 env，再看 winhand 的环境变量），相对路径以项目目录为准；
  - 没有设置时是 `<项目>/.venv`。

### R3 判断和修复

- 坏掉的判断：
  - 虚拟环境目录里有 `pyvenv.cfg`，其中有 `home = <目录>`；
  - 而 `<目录>` 里没有 Python 可执行文件（Windows：`python.exe`；其他系统：`python3` 或 `python`）。
  - `pyvenv.cfg` 不存在、读不了或没有 `home` 时，视为不需要修复。
- 修复（服务的 `repair_env` 为 true，默认）：
  1. 在项目目录里运行 `uv venv --clear`，带上 `run` 参数里的 `--python <值>`、`--managed-python`、`--no-managed-python`；
  2. 再运行 `uv sync`，带上同样的参数，以及 `run` 参数里的 `--locked` 或 `--frozen`；
  3. uv 用服务的启动命令（同一个 uv 可执行文件），环境变量与启动服务时相同；
  4. 两步合计最多 600 秒，不计入 `startup_timeout_s`；超时或退出码不为 0 时，修复失败。
- 修复成功：
  - 服务日志（`logs/mcp/<name>.log`）记下修复前的 `home`、运行的命令和 uv 输出的最后几行；
  - 照常启动；
  - 服务状态的 `note` 写“repaired its Python environment (<时间>)”。
- 修复失败：
  - 不启动；
  - 错误里写明虚拟环境、它指向的不存在的 Python、失败的命令和 uv 输出的最后 5 行。
- `repair_env = false` 时不修复也不启动。错误里写明：
  - 虚拟环境和它指向的不存在的 Python；
  - 修法：在项目目录里运行 `uv venv --clear`，再运行 `uv sync`，或者打开自动修复。
- 同一个虚拟环境目录同时只修复一次，其他启动等它修完再检查。
- 检查和修复适用于所有启动 stdio 服务的地方：
  - 经网关的启动；
  - `mcp_tools`、`mcp_call` 的启动；
  - 健康检查之后的重启；
  - 应用的“测试”按钮和 `winhand mcp test`（`probe`）。

Scenario: (A3) 自动修复
- 设置：一个没有依赖的 uv 项目，内含一个最小的 MCP 服务脚本，用 `uv --directory <项目> run --locked python server.py` 启动；`.venv/pyvenv.cfg` 的 `home` 改成不存在的目录。
- 操作：经 `mcp_tools("broken")` 启动。
- 结果：
  - 修复后 `pyvenv.cfg` 的 `home` 指向存在的 Python；
  - 列出了服务的工具；
  - 服务日志里有修复记录；
  - 修复时 `uv sync` 带了 `--locked`。

Scenario: (A4) 关掉自动修复
- 设置：同上，`repair_env = false`。
- 结果：
  - 工具错误，写明虚拟环境、不存在的 Python 和修法；
  - `pyvenv.cfg` 没有被改动，服务没有启动。

Scenario: (A5) 不检查的情况
- 结果：以下情况不读取任何 `pyvenv.cfg`，启动流程与 0.4.8 相同：
  - 命令是 `npx`、`uvx`；
  - `uv run --no-project …`；
  - 没有项目目录也没有 `cwd`。

### R4 说清原因

- stdio 服务启动失败（初始化前退出或拒绝初始化），而它最后的输出里有 `No Python at '`：
  - 错误信息末尾加一句：这个服务的 Python 虚拟环境指向的 Python 已不存在（取 `No Python at` 后的路径，去掉多余的引号）；
  - 再附上修法：R2 适用的服务说明运行哪两条 uv 命令（自动修复已开启却仍失败时，说明这一点）；其他服务说明重建它的虚拟环境。
- `probe` 的失败结果同样带这句说明。

Scenario: (A6) 启动失败的说明
- 设置：一个启动命令打印 `No Python at '"C:\gone\python.exe'` 后以 103 退出。
- 结果：错误信息里有 `C:\gone\python.exe` 和“no longer exists”的说明。

### R5 `sys_info(checks=true)`

- 对每个启用的、R2 适用的 stdio 服务按 R3 判断。坏掉的列在检查结果里，包括：
  - 服务名、虚拟环境、它指向的不存在的 Python；
  - 修法（自动修复是否开启，以及手动修复的命令）。
- 不运行 uv，不修复。

Scenario: (A7) 检查和测试按钮
- 结果：
  - `sys_info(checks=true)` 列出坏掉的环境；
  - `probe` 遇到坏掉的环境时先修复再测试，`ok` 为 true。

### R6 配置

- `[mcp_servers.<name>]` 的 `repair_env`：布尔值，默认 true；只在为 false 时写入 config.toml。
- 桌面端的服务列表带上它。桌面端保存服务时，请求里没有这个字段就保留原值。

Scenario: (A8) 配置往返
- 结果：
  - `repair_env = false` 的服务经 `config.save` / `config.load` 和桌面端保存后仍为 false；
  - 原有测试全部通过；
  - instructions 不超过 4,096 字符。
