# Outcome

云端会话像本机 Claude Code 一样知道并能用这台电脑的技能和本机 MCP 工具：

- 开场就有每个技能的名字和描述；要用时调用 `skill(name)` 拿到 SKILL.md 正文和技能文件夹里的文件，脚本在电脑上运行。
- 开场就有每个本机 MCP 服务的工具清单（工具名和一句说明）；`mcp_tools(server)` 取完整参数，`mcp_call(server, tool, arguments)` 经 winhand 直接调用，不必在 claude.ai 里把每个服务单独加成连接器。

用户原话：“起码你要让这边 winhand 获取后知道 skills 文件里面的那句描述……mcp 也是要拿到本地的工具描述”。

# Scope

**开场分段**
- `/hook/session-start` 按请求头 `X-Winhand-Part` 分三段：`rules`（缺省，即 0.4.7 的内容去掉技能名单）、`skills`、`tools`。每段是一个单独的 SessionStart hook，各自不超过 9,000 字符。依据：多个 hook 的 `additionalContext` 都会送到，10,000 字符的上限按每个 hook 分别计（见 `research.md`）。
- 中转不变：它原样转发这个请求头。
- `winhand bridge init` 生成三个 hook（同一 matcher 组里三个 handler，并行运行）；`--write` 替换旧的那一个。
- 旧配置（只有一个 hook、没有 `X-Winhand-Part`）仍然得到规矩，并在末尾附技能名单（与 0.4.7 相同），所以已部署的仓库不会变差。

**技能段**
- 每个技能一行：`- <name>: <description>`。描述取 SKILL.md frontmatter 的 `description`，有 `when_to_use` 时接在后面，合计最多 1,536 字符（与 Claude Code 相同）。
- 放不下时，均匀压缩每条描述的长度，最短 80 字符；仍放不下时，其余技能只列名字。
- 项目技能（本机检出目录里的 `.agents/skills`、`.claude/skills`）排在前面，同名时项目的优先。
- 段首一句说明：这些技能在电脑上，用 `skill(name)` 取正文。

**MCP 工具段**
- 每个启用的本机 MCP 服务：服务说明（初始化时给的 `instructions`，最多 300 字符）和每个工具的一行：`- <tool>: <描述第一行，最多 150 字符>`。
- 数据来自电脑上的工具清单缓存，**不为了开场去启动任何服务**（有的服务启动要几分钟，或会动到 FreeCAD、摄像头）。还没有缓存的服务写“尚未列出，`mcp_tools("<server>")` 会启动并列出它”。
- 段首一句说明：用 `mcp_call(server, tool, arguments)` 调用，完整参数用 `mcp_tools(server)`。

**工具清单缓存**
- 存在 `<WINHAND_HOME>/cache/mcp-tools.json`，按服务名记录：配置指纹（启动命令、参数、URL、工作目录、环境变量的 SHA-256）、列出时间、服务说明、工具（名字、描述、参数 schema）。配置变了的服务视为没有缓存。
- 三种来源会更新它：
  1. 任何客户端经 winhand 的本机 MCP 网关列出工具时（`tools/list` 的回答，第一页且没有下一页）；
  2. `mcp_tools(server)`；
  3. 应用里的“测试”按钮和 `winhand mcp test`（`probe`）。

**新工具**（在 winhand 主连接上）
- `skill(name)`：
  - 返回技能的 SKILL.md 正文（与 `fs_read` 一样过守卫的 `read` 规则、脱敏、过长截断）、技能文件夹路径、文件夹里的文件清单（最多 50 个）；
  - 找不到时列出相近的名字。
- `mcp_tools(server=None)`：
  - 不给 server：列出所有启用的服务、状态和缓存里的工具名；
  - 给 server：必要时启动它，列出工具的名字、描述和参数 schema，并更新缓存。
- `mcp_call(server, tool, arguments={}, timeout_s=120, call_id=None)`：
  - 必要时启动服务，调用工具，原样返回内容（文字、图片）；
  - 服务报错（`isError`）时作为工具错误返回；
  - 约 40 秒内没有结果时先返回 `call_id`，`mcp_call(call_id=...)` 接着等（客户端约 60 秒就放弃一个工具调用，与 `run` 的做法相同）；`mcp_tools(server)` 启动慢时同样先返回，再次调用接着等。

**守卫和 hooks**
- `mcp_call` 描述成新的动作类型 `mcp`，文字为 `<server>.<tool> <参数 JSON>`。
  - `guards.toml` 可以写 `on = "mcp"`，用 `match` 匹配，例如 `match = '^pyocd-debug\.(flash|erase)'` 配 `decision = "ask"`。
  - 现有规则写的 `on` 不含 `mcp`，所以不受影响。
- 本机 PreToolUse hooks 看到的工具名是 `mcp__<server>__<tool>`（Claude Code 和 Codex 都是这个命名），`tool_input` 是参数。本机 hooks 里针对 MCP 工具的 matcher 因此同样生效。

**说明文字**
- MCP instructions（不超过 4,096 字符）加一句：技能用 `skill`，本机 MCP 用 `mcp_tools`/`mcp_call`。
- `help()` 里写全。

## Source coverage

| 来源条目 | 读取状态 | Spec 位置 | 验收 ID | 覆盖状态 |
|---|---|---|---|---|
| 用户：技能要带描述 | complete | R1、R2 | A1、A2 | covered |
| 用户：MCP 要拿到本地工具描述 | complete | R3、R4、R5 | A3、A4、A5 | covered |
| 设计文档 P3“跟随本机 MCP 配置，加 mcp() 元工具” | complete | R5 | A5 | covered |
| 设计文档 P3 resources/prompts 出口 | complete | — | — | non-goal |

# Non-goals

- 本机 MCP 的 resources、prompts，以及服务向客户端发起的请求（sampling、elicitation）：`mcp_call` 遇到这类请求时回答“不支持”。
- 跟随 Codex、Claude Code 的 MCP 配置文件（`~/.codex/config.toml`、`.mcp.json`）自动导入服务：已有 `winhand mcp import`，不变。
- 为了列出工具在后台自动启动服务。
- Claude Code 的 `disable-model-invocation`、`allowed-tools` 等字段的完整语义：只用于决定是否列出（`disable-model-invocation: true` 的技能不列出描述，只列名字）。

# Acceptance examples

- **A1** 技能段：40 个技能、描述共约 9,700 字符时，开场技能段含每个技能的名字和描述；总长不超过 9,000 字符时描述原样，超出时均匀压缩、每条至少 80 字符；项目技能在前。
- **A2** `skill("officecli")` 返回 SKILL.md 正文、文件夹路径和文件清单；读规则拒绝的 SKILL.md 不返回正文；不存在的名字给出相近的名字。
- **A3** 工具段：缓存里有 pyocd-debug 的工具时列出工具名和一句说明；没有缓存的服务标“尚未列出”；配置改了的服务也标“尚未列出”；开场不启动任何服务。
- **A4** 缓存的来源：经网关的 `tools/list`、`mcp_tools(server)`、`probe` 都会更新缓存；缓存文件损坏时当作没有缓存，不报错。
- **A5** `mcp_tools` 和 `mcp_call`：
  - 用测试用的假 MCP 服务，`mcp_tools("fake")` 列出工具和 schema；
  - `mcp_call("fake", "echo", {"text": "x"})` 返回 `echo: x`；
  - 图片内容原样返回；
  - 不存在的服务或工具给出清楚的错误；
  - 服务报错时作为工具错误返回。
- **A6** 守卫：
  - `on = "mcp"`、`match = '^fake\.echo'`、`decision = "deny"` 时 `mcp_call` 被拒绝，服务没有被调用；
  - 本机 PreToolUse hook（matcher `mcp__fake__.*`）拒绝时同样被拒绝。
- **A7** 开场分三段：
  - 带 `X-Winhand-Part: skills`、`tools` 时分别只给技能、工具；
  - 不带时给规矩加技能名单（与 0.4.7 相同）；
  - `winhand bridge init` 生成三个 hook，`--write` 替换旧的一个，重复执行不重复添加。
- **A8** 兼容：原有测试全部通过；instructions 仍不超过 4,096 字符。

# Constraints and invariants

- 开场段只读文件和缓存，不启动服务，不运行命令（`git ls-files` 除外）。
- `mcp_call` 和其他工具一样受守卫约束；守卫拒绝时不启动、不调用服务。
- 缓存里只有工具清单，不存调用参数和结果。

# Decisions

- **D1 分段而不是压缩**：技能描述约 9,700 字符、规矩约 6,000 字符，一个 hook 的 10,000 字符上限放不下。分成三个 hook 后，各自都有完整的空间，也互不挤占。
- **D2 不为开场启动服务**：开场只读缓存。代价是从没用过的服务开场只有名字，但不会因为开场意外拉起 FreeCAD、摄像头或长时间下载。
- **D3 经主连接调用本机 MCP**：一个 winhand 连接就能用所有本机 MCP，不必为每个服务单独授权；原有的 `/mcp/<name>` 单独连接器保留。
- **D4 动作类型 `mcp` 和 hooks 的工具名**：沿用 Claude Code 的 `mcp__server__tool` 命名，本机已有的 hooks 不用改就能匹配。
- **D5 长调用先返回**（Build 时补充）：客户端约 60 秒放弃一个工具调用，有的服务启动就要几分钟。`mcp_call` 和 `mcp_tools(server)` 约 40 秒内先回答，未完成的在后台继续，凭 `call_id` 或再次调用接着等。`timeout_s` 默认 120 秒，只计调用本身，不计启动。
- **D6 列出不算动作**（Build 时补充）：`mcp_tools` 只取工具清单，不经守卫判断；等待 `call_id` 也不再判断。

# Open questions

- 首次填充缓存：需要把用户电脑上的 5 个服务各启动一次（用应用的测试按钮或 `winhand mcp test`），等用户同意——不阻塞开发。

# Verification expectations

- `ruff`、`pytest`（新增测试覆盖 A1–A8，假 MCP 服务用 `tests/fake_mcp_server.py`）、CI 两个工作流。
- 独立 Verifier 判定 A1–A8。
- 真机：用户同意后填充缓存，新开云端会话看到三段开场内容，并实际 `mcp_call` 一个只读的工具。
