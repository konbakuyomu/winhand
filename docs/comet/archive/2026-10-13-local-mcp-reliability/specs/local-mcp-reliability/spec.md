# local-mcp-reliability：工具段每个工具都有说明；uv 项目的虚拟环境坏了时自动修好

依据见 `research.md`。R1 替换 `docs/comet/specs/local-skills-tools/spec.md` R4 中“工具段”的长度规则，其余条款不变。

### R1 工具段的长度

- 每个启用的服务一块：
  - `## <server>`；
  - 有缓存时：服务说明的第一行，然后每个工具一行 `- <tool>: <说明的第一个非空行>`；
  - 没有缓存时：一行“not listed yet”；
  - 列出过但没有工具时：一行“(no tools)”（只列名字时也是这一行）。
- 先对全部文字脱敏，再计算长度（UTF-16 单位）。
- 全部放得下时，每条说明最长 150 字符，服务说明最长 300 字符。
- 放不下时：
  1. 求出能放下全部内容的最长统一长度 `cap`（40 ≤ cap ≤ 150）。每条说明截到 `cap`，截断处加 `…`；服务说明截到 `min(300, 2 × cap)`；
  2. `cap` = 40 仍放不下时，从最后一个服务开始，把它改为只列工具名（逗号分隔）；
  3. 仍放不下时，从最后一个服务开始跳过，末尾说明跳过了几个服务；跳过之后，剩下的服务从前往后尽量多地带说明（`cap` = 40）；
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

- 后备步骤按各块的长度累加计算，不为每种组合重新生成文字：上百个服务也在几秒内完成（中转等电脑回答最多 15 秒）。

Scenario: (A2) 工具很多
- 设置：3 个服务，各 60 个工具，名字 30 字符、说明 200 字符。
- 结果：总长不超过 9,000；第一个服务每个工具都有 40 字符的说明；后两个只有工具名。

Scenario: (A2) 服务很多
- 设置：10 个服务各 60 个工具；以及 100 个服务各 40 个工具。
- 结果：总长不超过 9,000；末尾说明跳过了几个服务；100 个服务的情况在 5 秒内算完。

### R2 哪些服务要检查虚拟环境

- 只检查 stdio 服务，且启动命令的文件名（去掉路径，不区分大小写）是 `uv` 或 `uv.exe`。
- 参数里第一个不是选项的词是 `run`；之后第一个不是选项、也不是选项值的词是要运行的命令，扫描到它或 `--` 为止。
  - 带值的选项支持 `--opt value`、`--opt=value`、`-p3.12` 和 `-p=3.12` 四种写法；
  - 已知带值的 uv 选项（全局的和 `uv run` 的）跳过其值，不会被当成命令。
- 不检查的情况：
  - `run` 的选项里有 `--no-project`、`--isolated`、`--script`、`-s`、`--gui-script`、`--active`；`run` 之前的全局选项里有 `--isolated` 或 `--no-project`；
  - 环境变量 `UV_NO_PROJECT` 或 `UV_ISOLATED` 为真（先看服务的 env，再看 winhand 的环境变量，下同）。
- 从哪里找项目：
  - `--directory` 的值（或 `UV_WORKING_DIR`）；
  - 再接上 `--project` 的值（或 `UV_PROJECT`），与 uv 相同；
  - 相对路径以服务的 `cwd` 为准（没有 `cwd` 时以 winhand 的当前目录为准）；
  - 都没有时用服务的 `cwd`，仍没有时不检查。
- 路径和 uv 一样处理：
  - 起点目录在 POSIX 上按真实路径（解析链接），因为 uv 的当前目录就是这样报告的；Windows 上按写下的路径；
  - `--project` 接在起点后面，不解析其中的链接。
- 项目：从那里向上找第一个有 `pyproject.toml` 的目录；直到根目录都没有时不检查（`uv run` 这时不建项目环境）。
- 虚拟环境所在的根：
  - 项目属于某个 uv workspace 时，是 workspace 的根；
    - 判断方法：从项目向上找 `pyproject.toml`：
      - 有 `[tool.uv.workspace]` 的，就是要判断的 workspace；
      - 先遇到一个有 `[project]`、没有 workspace 的，项目就是独立的；
    - 项目的相对路径匹配其 `members`、且不匹配 `exclude`，才算属于它；与 uv 相同，区分大小写；
    - `pyproject.toml` 读不了、不是 TOML 或这些键不是表时，视为没有 workspace，不报错；
  - 否则是项目自己。
- 虚拟环境目录：`UV_PROJECT_ENVIRONMENT`（相对路径以根为准），没有时是 `<根>/.venv`。
- 重建时原样带上的选项：
  - 全局选项 `--directory`、`--project`、`--python-preference`、`--config-file`、`--cache-dir`、`--allow-insecure-host`、`--offline`、`--no-config`、`--native-tls`、`--system-certs`、`--managed-python`、`--no-managed-python`、`--no-python-downloads`、`--allow-python-downloads`、`--no-cache`；
  - `--python`；
  - `--locked`、`--frozen`。

### R3 判断和修复

- 坏掉的判断（宁可判为完好：只有确实跑不起来的环境才重建）：
  - 虚拟环境目录里有 `pyvenv.cfg`，其中有 `home = <目录>`；
  - Windows：`<目录>` 里没有任何 `python*.exe`；
  - 只有 `<目录>` 不存在才算“没有”。以下情况视为完好：
    - 列不出内容（没有权限）；
    - 路径里有非法字符；
    - Windows 报告网络路径或驱动器不可达（错误 15、53、67）。
  - 其他系统：环境里的 `bin/python`、`bin/python3` 都指不到存在的文件，而且 `<目录>` 里没有 `python` 或 `python3*`；
  - `pyvenv.cfg` 不存在、读不了或没有 `home` 时，视为不需要修复。
- 修复（服务的 `repair_env` 为 true，默认）：
  1. 在项目目录里运行 `uv venv --clear`，带上 `run` 参数里的 `--python <值>`、`--managed-python`、`--no-managed-python`；
  2. 再运行 `uv sync`，带上同样的参数，以及 `run` 参数里的 `--locked` 或 `--frozen`；
  3. uv 用服务的启动命令（同一个 uv 可执行文件），在服务启动时的目录里运行（服务的 `cwd`，没有时是 winhand 的当前目录，与启动服务时相同），环境变量也与启动服务时相同，所以选项里的相对路径与服务启动时一样解析；手动修复的说明只在服务有 `cwd` 时写出目录；
  4. 两步合计最多 600 秒，不计入 `startup_timeout_s`。超时时结束 uv 的整个进程树，错误里带上已有的输出；退出码不为 0 或运行不了 uv 时，修复失败；每一步都记进服务日志。
  5. 修复在单独的线程里进行，不占 winhand 的事件循环和公用线程池。修复期间服务被删除或配置被改时，修复完不再启动它。
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
  - 再附上修法：
    - R2 适用、winhand 检查的环境确实坏了：写出两条 uv 命令，以及自动修复是否开启；
    - R2 适用、但 winhand 检查的环境是好的或不存在（服务实际用了别的环境）：说明这一点和自动修复是否开启，并说明要重建服务实际使用的那个环境，以及用哪两条命令；
    - 其他服务：说明重建它的虚拟环境。
- `probe` 的失败结果同样带这句说明，但只看这次测试新写入日志的内容（测试日志是追加写入的，旧的内容不算）。

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
