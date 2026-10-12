# local-skills-tools：技能带描述，经 winhand 用本机 MCP 工具

云端会话像本机 Claude Code 一样知道并能用这台电脑的技能和本机 MCP 工具。事实依据见 `research.md`。

### R1 开场分三段

- `/hook/session-start` 读请求头 `X-Winhand-Part`（不区分大小写，去掉首尾空白）：
  - `rules`：规矩（0.4.7 的内容，不含技能名单）；
  - `skills`：技能段（R2）；
  - `tools`：本机 MCP 工具段（R4）；
  - 没有这个请求头：规矩，末尾附技能名单（与 0.4.7 相同，已部署的单个 hook 不变差）；
  - 其他值：404。
- 每段各自不超过 9,000 字符（按 UTF-16 单位计），是一个单独 hook 的 `additionalContext`。
- 中转原样转发这个请求头，中转代码不变。
- `winhand bridge init` 生成一个 matcher 组（`startup|resume|clear|compact`），内含三个 command handler，依次带 `X-Winhand-Part: rules`、`skills`、`tools`，各自 `timeout: 30`。
- `--write` 去掉已有的、命令里含 `/hook/session-start` 的 handler（包括 0.4.7 的单个 hook），再加上这一组；重复执行结果相同。

Scenario: (A7) 按请求头取段
- 操作：分别带 `X-Winhand-Part: rules`、`skills`、`tools`、不带、带 `everything` POST `/hook/session-start`。
- 结果：
  - `rules` 只有规矩；`skills` 只有技能；`tools` 只有工具段；
  - 不带时有规矩和技能名单；
  - `everything` 返回 404。

Scenario: (A7) 生成和替换
- 设置：`.claude/settings.json` 里有 0.4.7 生成的单个 hook。
- 操作：执行两次 `winhand bridge init --write`。
- 结果：只有一个 SessionStart 组，内含三个 handler，依次是 `rules`、`skills`、`tools`；其他设置不变。

### R2 技能段

- 第一行说明：这些技能在电脑上（附电脑名）；合适时调用 `skill(name)` 取 SKILL.md 和文件，照做，脚本用 `run` 在电脑上运行。
- 之后每个技能一行：`- <name>: <description>`。
  - 描述是 frontmatter 的 `description`，有 `when_to_use` 时用空格接在后面，空白折叠成单个空格，合计最多 1,536 字符（Claude Code 的截断长度）。
  - 没有描述的技能只写 `- <name>`。
- 顺序：项目技能（本机检出目录的 `.agents/skills`、`.claude/skills`）在前，然后是用户技能；各自按名字排序；同名时项目的优先。
- `disable-model-invocation: true` 的技能不进列表，只在末尾一行 `Used only when asked for by name: <名字…>` 里列名字。
- 守卫的 `read` 规则拒绝（deny 或 ask）的 SKILL.md 不列出。
- 放不下时：
  1. 求出能放下全部的最长统一截断长度，每条描述截到这个长度（截断处加 `…`），最短 80 字符；
  2. 每条 80 字符仍放不下时，从最后一个开始改为只列名字，直到放下；
  3. 仍然超出时截断正文。
- 正文经过脱敏。

Scenario: (A1) 描述原样
- 设置：几个技能，描述合计远小于预算；一个技能带 `when_to_use`；一个技能 `disable-model-invocation: true`；项目里有一个技能。
- 结果：项目技能在第一行说明之后；`when_to_use` 接在描述后；不能自动调用的技能只出现在末尾那一行。

Scenario: (A1) 均匀缩短
- 设置：40 个技能，每个描述约 300 字符。
- 结果：总长不超过 9,000；40 个都有描述，截断后长度相同且不少于 80 字符。

Scenario: (A1) 名字都在
- 设置：150 个技能，每个描述约 300 字符。
- 结果：总长不超过 9,000；150 个名字都在；前面的有描述，最后的只有名字。

### R3 `skill(name, path=None, repo=None)`

- 按名字找技能（不区分大小写），找不到时按技能文件夹名找。有 `path` 或 `repo` 时先找那个项目的技能（找项目根的方法同 `context`）。
- 返回：
  - `name`、`scope`（project 或 user）、`folder`、`skill_md`、`description`；
  - `content`：SKILL.md 全文；超过 60,000 字符时截到整行，并在 `truncated` 里说明用 `fs_read` 从哪一行接着读；
  - `files`：技能文件夹里的文件（相对路径和字节数），跳过以点开头的目录和 `node_modules` 之类，最多 50 个；更多时 `files_more` 提示用 `fs_list`；
  - `note`：照 content 做，路径相对 folder，脚本在这台电脑上运行。
- 守卫的 `read` 规则拒绝（deny 或 ask）这个 SKILL.md 时：`content` 为 null，`refused` 写明哪个规则文件、第几条和理由；其余字段照常。
- 找不到时返回工具错误，列出最多 8 个相近的名字和技能总数。
- 结果经过脱敏。

Scenario: (A2) 取技能
- 设置：`~/.agents/skills/officecli/SKILL.md` 和 `scripts/office.py`。
- 操作：`skill("OfficeCLI")`。
- 结果：`content` 是 SKILL.md 正文；`files` 是 `SKILL.md` 和 `scripts/office.py`；`scope` 是 user。

Scenario: (A2) 读规则拒绝
- 设置：全局 `read` 规则 `path = ["**/officecli/**"]`，`decision = "deny"`。
- 结果：`content` 为 null，`refused` 含 `guards.toml` 和理由。

Scenario: (A2) 找不到
- 操作：`skill("officecl")`。
- 结果：工具错误，提到 `officecli`。

Scenario: (A2) 项目优先
- 设置：项目 `.claude/skills/officecli` 和用户的同名技能。
- 操作：`skill("officecli", path=<项目>)`。
- 结果：`scope` 是 project，内容是项目那份。

### R4 本机 MCP 工具段和工具清单缓存

- 缓存文件：`<WINHAND_HOME>/cache/mcp-tools.json`，按服务名记录：
  - `fingerprint`：服务的 kind、command、args、url、cwd、env 的 SHA-256；
  - `listed_at`、`instructions`（服务初始化时给的说明，最多 2,000 字符）；
  - `tools`：每个工具的 `name`、`description`、`inputSchema`。
- 缓存写入是原子的（先写临时文件再替换）；读不了、不是 JSON、结构不对或指纹不符的条目当作没有缓存，不报错。
- 更新缓存的时机：
  1. 客户端经 `/mcp/<name>` 列出一个 stdio 服务的工具：`tools/list` 请求没有 cursor，回答里也没有 `nextCursor` 时（HTTP 服务由 winhand 原样转发，不在此列）；
  2. `mcp_tools(server)`；
  3. `probe`（应用的“测试”按钮、`winhand mcp test`）。
- 工具段：
  - 第一行：`Local MCP servers on this PC. Call a tool with mcp_call(server, tool, arguments); mcp_tools(server) gives each tool's full parameters.`
  - 每个启用的服务一块：`## <server>`；有缓存时，服务说明的第一行（最多 300 字符），然后每个工具 `- <tool>: <描述第一个非空行，最多 150 字符>`；没有缓存时 `(not listed yet: mcp_tools("<server>") starts it and lists its tools)`。
  - 放不下一整块时，这个服务只列工具名；还放不下就跳过，末尾说明跳过了几个服务。
  - 没有启用的服务时说明“none are enabled”。
  - 正文经过脱敏。
- 生成工具段只读缓存和配置，不启动任何服务。

Scenario: (A3) 来自缓存
- 设置：缓存里有 fake 的 echo（描述两行）和服务说明；还有一个停用的服务。
- 结果：有 `## fake`、服务说明和 `- echo: Say it back.`；没有描述的第二行；没有停用的服务；fake 没有被启动。

Scenario: (A3) 没列过或配置改了
- 设置：缓存里 fake 的指纹来自另一套参数。
- 结果：fake 写“not listed yet”。

Scenario: (A4) 缓存的来源
- 操作：客户端经 `/mcp/fake` 列出工具；`probe(fake)`；`mcp_tools("fake")`。
- 结果：每次之后缓存里都有 fake 的工具、说明和参数 schema。

Scenario: (A4) 缓存损坏
- 设置：缓存文件不是 JSON，或条目结构不对。
- 结果：当作没有缓存，工具段照常生成。

### R5 `mcp_tools(server=None)` 和 `mcp_call(...)`

- 两个工具都用正在运行的 agent 的本机服务（与 `/mcp/<name>` 共用进程）；没有 agent 时（stdio、`winhand http`）按 `config.toml` 自己管理，进程退出时停掉自己启动的服务。
- `mcp_tools()`：每个启用的服务的 `server`、`state`、缓存里的工具（名字和一行说明）或“尚未列出”的提示；不启动任何服务。
- `mcp_tools(server)`：
  - 必要时启动服务，列出全部工具（跟随 `nextCursor`，最多 50 页），更新缓存，返回 `tools`（名字、描述、`inputSchema`）和 `instructions`；
  - 约 40 秒内没列完时返回 `still_running` 和 `next`，列出在后台继续；再次调用 `mcp_tools(server)` 接着等同一次列出。
- `mcp_call(server, tool, arguments={}, timeout_s=120, call_id=None)`：
  - 必要时启动服务（启动时间按服务的 `startup_timeout_s`，不计入 `timeout_s`），发送 `tools/call`；
  - 结果原样返回：`content`（文字、图片等）和 `structuredContent`；服务的 `isError` 为真时作为工具错误返回，内容照样带上；
  - 约 40 秒内没有结果时返回 `still_running`、`call_id` 和 `next`；`mcp_call(call_id=...)` 再等最多约 40 秒；结果只交出一次；没人取的结果 10 分钟后丢弃；
  - 超过 `timeout_s` 仍没有回答时返回工具错误（`did not answer tools/call within … s`），并给服务发 `notifications/cancelled`；服务继续可用；
  - 服务不存在或已停用：工具错误，列出启用的服务；没给 `server`/`tool` 也没给 `call_id`：工具错误；`call_id` 不存在：工具错误；`timeout_s` 不大于 0：工具错误。
- 服务在 `mcp_call` 期间向客户端发起的请求（sampling、elicitation、roots）：没有客户端经 `/mcp/<name>` 连着这个服务时回答“没有客户端可以回答”；有时交给那个客户端（与独立地址原有的行为相同）。
- HTTP 服务：列出工具时用一个连接跟随全部分页并取服务说明；每次调用单独连接。

Scenario: (A5) 列出和调用
- 操作：`mcp_tools()`、`mcp_tools("fake")`、`mcp_call("fake", "echo", {"text": "你好"})`、`mcp_call("fake", "picture")`、`mcp_call("fake", "fail", {"why": "no luck"})`。
- 结果：
  - 列出前 fake 的 `tools` 为 null；之后有 echo 的描述和 schema，状态 running；
  - echo 返回 `echo: 你好`；picture 返回 PNG 图片；fail 是工具错误，含 `no luck`。

Scenario: (A5) 清楚的错误
- 操作：不存在的服务、停用的服务、不存在的工具、什么都不给、不存在的 `call_id`。
- 结果：都是工具错误，写明原因；服务名错误时列出启用的服务。

Scenario: (A5) 长调用
- 操作：调用一个约 1.5 秒的工具，等待时间设成 0.05 秒；再用 `call_id` 等。
- 结果：第一次返回 `still_running` 和 `call_id`；第二次返回结果；第三次用同一个 `call_id` 是工具错误。

Scenario: (A5) 超时
- 操作：`timeout_s=0.3` 调用一个要 5 秒的工具；再调用 echo。
- 结果：第一次是超时的工具错误；echo 照常返回。

### R6 守卫和本机 hooks

- `mcp_call`（不带 `call_id`）是一个动作：类型 `mcp`，文字 `<server>.<tool> <arguments JSON>`（键排序，非 ASCII 原样），没有路径和工作目录。
  - 因此只有全局 `guards.toml` 的规则适用；`path` 规则不匹配它；
  - 动作说明（确认框、拒绝信息）为 `mcp_call → mcp: <文字>`，最长 300 字符。
- 带 `call_id` 的 `mcp_call` 和 `mcp_tools` 不是动作（等待和列出不执行新的工具）。
- `guards.toml` 的 `on` 接受 `mcp`；没写 `mcp` 的已有规则不受影响。
- 本机 PreToolUse hooks（Claude Code 和 Codex）看到的 `tool_name` 是 `mcp__<server>__<tool>`，`tool_input` 是参数对象，matcher 按这个名字匹配。hook 的工作目录按没有路径的动作处理（winhand 的当前目录）。
- 守卫或 hook 拒绝时不启动服务，不发送调用。

Scenario: (A6) 守卫拒绝
- 设置：全局规则 `on = "mcp"`、`match = '^fake\.echo '`、`decision = "deny"`。
- 结果：`mcp_call("fake", "echo", …)` 是工具错误，写明规则文件和第几条；fake 没有启动；`mcp_call("fake", "pid")` 照常执行。

Scenario: (A6) hook 拒绝
- 设置：Claude Code PreToolUse hook，matcher `mcp__fake__.*`，参数里有 DENY 时拒绝。
- 结果：带 DENY 的调用被拒绝，fake 没有启动；其他调用照常；hook 收到的 `tool_name` 是 `mcp__fake__echo`，`tool_input` 是参数。

### R7 说明文字

- MCP instructions（含 context 提醒）不超过 4,096 字符，含一句：技能用 `skill(name)`；本机 MCP 用 `mcp_tools()` 和 `mcp_call(server, tool, arguments)`。
- `help()` 的完整指南写全两者，包括 `call_id` 的用法；不再说本机 MCP 只能单独加连接器。
- `context()` 和 `sys_info` 里本机 MCP 服务的说明改为指向 `mcp_tools` / `mcp_call`；`context()` 的技能说明改为指向 `skill(name)`。

### R8 兼容

- 原有测试全部通过。
- 0.4.7 生成的单个 hook 仍然得到规矩和技能名单。
- `/mcp/<name>` 独立地址不变。

Scenario: (A8) 兼容
- 结果：全部测试、`ruff check`、`ruff format --check` 通过；instructions 不超过 4,096 字符。
