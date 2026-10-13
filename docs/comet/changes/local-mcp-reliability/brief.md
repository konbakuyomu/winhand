# Outcome

两件事，都来自 0.4.8 的真机验收：

1. 开场的本机 MCP 工具段里，**每个工具都有一句说明**。0.4.8 按服务顺序放整块，pyocd-debug 的 55 个工具只剩名字。
2. 本机 MCP 服务因为**虚拟环境指向的 Python 被删掉**而启动不了时，winhand 自己发现并修好，然后照常启动。修不好时，说清原因和修法。

用户原话：“做这个改进，我还想彻底解决刚刚 winhand 没法启动本地 mcp 的 bug”。

# Scope

**工具段均匀缩短**（改 `local-skills-tools` 规格的 R4）
- 每个工具一行：`- <tool>: <说明第一行>`。
- 全部放得下时，说明最长 150 字符（与 0.4.8 相同）。
- 放不下时，求出能放下全部工具的最长统一长度，每条说明截到这个长度，最短 40 字符。服务说明（instructions）也随之缩短，最长 300 字符。
- 每条 40 字符仍放不下时，从最后一个服务开始改为只列工具名；还放不下就跳过，末尾说明跳过了几个。
- 先脱敏再计算长度，不变。

**启动前检查和修复虚拟环境**
- 适用范围：stdio 服务，启动命令是 uv（`uv` 或 `uv.exe`，可带路径），子命令是 `run`。
- 项目目录：
  - 取 `--directory` 或 `--project` 的值（相对路径以服务的 `cwd` 为准）；
  - 没有时用服务的 `cwd`；
  - 都没有时不检查。
  - 虚拟环境是项目目录下的 `.venv`；设了 `UV_PROJECT_ENVIRONMENT`（服务的 env 优先，其次 winhand 自己的环境）时用它。
- 判断：`pyvenv.cfg` 存在、有 `home`，而 `home` 里没有 Python 可执行文件。
- 修复：
  - 在项目目录里运行 `uv venv --clear`，再运行 `uv sync`；
  - 服务启动参数里有 `--locked`、`--frozen`、`--python <值>`、`--managed-python`、`--no-managed-python` 时，原样带上；
  - 用服务自己的环境变量运行，最多 10 分钟，不计入 `startup_timeout_s`；
  - 输出写进服务日志，结果显示在服务状态里。
- `run` 后面有 `--no-project`、`--isolated`、`--script` 时不检查：这些不用项目的虚拟环境。
- 每个服务可以用 `repair_env = false` 关掉自动修复。关掉时只检查，启动前就报出原因和修法，不启动坏掉的服务。
- 应用里的“测试”按钮（`probe`）、经网关的启动、`mcp_tools` / `mcp_call` 的启动都走同一个检查。同一个虚拟环境同时只修一次。

**说清原因**
- stdio 服务启动失败，而它的输出里有 `No Python at '…'` 时，错误信息里加一句：这个服务的 Python 虚拟环境指向的 Python 已不存在，以及修法（`uv venv --clear` 和 `uv sync`，或在 winhand 里打开自动修复）。
- `sys_info(checks=true)` 列出虚拟环境坏掉的本机 MCP 服务和修法。

**配置**
- `[mcp_servers.<name>]` 新增 `repair_env`（默认 true，只在为 false 时写入 config.toml）。
- 桌面端保存服务时保留它，与 `call_timeout_s` 的做法相同。

## Source coverage

| 来源条目 | 读取状态 | Spec 位置 | 验收 ID | 覆盖状态 |
|---|---|---|---|---|
| 用户：所有工具都带一句说明 | complete | R1 | A1、A2 | covered |
| 用户：彻底解决本机 MCP 起不来 | complete | R2–R5 | A3–A7 | covered |
| 真机验收的遗留项（pyocd-debug 只有名字） | complete | R1 | A1 | covered |

# Non-goals

- uvx（`uv tool run`）和 npx 启动的服务：它们的环境在 uv / npm 的缓存里，由各自的工具管理。
- 直接用某个虚拟环境里的 python.exe 启动的服务：只在启动失败时说清原因（R4），不自动修复。
- 把用户项目改用 uv 自己的 Python：修复时按项目原来的设置选 Python。
- 桌面端新增“修复”按钮或 `repair_env` 开关的界面：先在 config.toml 里设置。

# Acceptance examples

- **A1** 工具段：
  - 5 个服务共约 110 个工具（名字和说明的长度与用户电脑相近）时，每个工具都有说明，每条至少 40 字符（说明本来更短的除外）；
  - 总长不超过 9,000；
  - 放得下时说明不被截短。
- **A2** 工具很多（400 个以上）时，后面的服务只列工具名或被跳过并计数，总长不超过 9,000。
- **A3** 自动修复：
  - 用 `uv --directory <项目> run …` 启动、`.venv` 的 `home` 不存在的服务，启动前被修复（`uv venv --clear` 和 `uv sync`，带上原来的 `--locked`），然后正常启动；
  - 服务日志里有修复记录。
- **A4** `repair_env = false` 时不修复；启动前报错，错误里写明虚拟环境、它指向的不存在的 Python，以及修法。
- **A5** 不在范围内的命令（npx、uvx、`run --no-project`、没有项目目录）不检查，行为与 0.4.8 相同。
- **A6** 启动失败的输出里有 `No Python at '…'` 时，错误信息说明原因和修法。
- **A7** 测试按钮（`probe`）同样先修复再测试；`sys_info(checks=true)` 列出坏掉的虚拟环境。
- **A8** 兼容：
  - 原有测试全部通过；
  - instructions 不超过 4,096 字符；
  - 桌面端保存服务后 `repair_env` 不丢。

# Constraints and invariants

- 只在确认基础 Python 不存在时修复；环境完好的服务启动流程不变。
- 修复只动项目的虚拟环境目录（由 uv 重建），不改项目里的其他文件。不带 `--locked` / `--frozen` 时，`uv sync` 可能像 `uv run` 一样更新 `uv.lock`。
- 修复失败时不启动服务，错误里带上 uv 输出的最后几行。

# Decisions

- **D1 自动修复默认开启**：这类损坏只会在基础 Python 被删掉后出现，`uv run` 每次都失败，而重建虚拟环境就是修法。默认开启才能做到“彻底解决”，不想要时可以按服务关掉。自动修复会由 uv 删除并重建 `.venv` 目录，用户已确认（2026-10-13）。
- **D2 统一长度而不是按服务分配**：与技能段的做法一致。每个工具都有说明，比部分工具有长说明、部分工具只有名字更有用。
- **D3 修复用 `uv venv --clear` 再 `uv sync`**：单独 `uv sync` 实测不修（见 research.md）。

# Open questions

- （已解决）D1：用户 2026-10-13 确认自动修复默认开启，并确认了整个范围。

# Verification expectations

- `ruff`、`pytest`：
  - 修复的测试用真实的 uv（CI 里有）、一个没有依赖的小项目，以及一个手写的最小 MCP 服务；
  - 坏掉的环境通过改写 `pyvenv.cfg` 的 `home` 模拟。
- CI 两个工作流；独立 Verifier。
- 真机：在用户电脑的临时目录里做一个虚拟环境坏掉的服务，确认 winhand 自动修好并启动；再开一次云端会话，看 pyocd-debug 的工具都带说明。
