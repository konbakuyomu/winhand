# 调研：Claude Code 怎样列技能和 MCP 工具，开场 hook 的容量

来源：code.claude.com 的 hooks、skills、mcp、context-window、env-vars、settings-reference 页面（2026-10-12）；[推断] 表示文档没有直说。另有对用户电脑的实测，标 [实测]。

## 多个 SessionStart hook

- “多个 hook 为同一事件返回 `additionalContext` 时，Claude 收到全部的值。”（hooks › Add context for Claude）
- 上限按每个 hook 分别计：一个 hook 的 `additionalContext`、`systemMessage`、`initialUserMessage` 和纯文本 stdout，各自最多 10,000 字符，“即使同一事件有多个 hook，也分别计算”。（hooks › JSON output）
- 所有匹配的 hook 并行运行；文档没有说明合并后的顺序。[推断] 不要依赖顺序。（hooks › Hook handler fields）
- 交互式启动时 hook 在后台运行，但 Claude 的第一次回答会等它们结束。（hooks › SessionStart）

## 技能怎样列出

- 会话开始时载入每个技能的名字和描述，正文在调用时才载入。（skills › Control who invokes a skill；context-window › Skill descriptions）
- 列表的预算是模型上下文窗口的 1%，可用 `skillListingBudgetFraction` 或 `SLASH_COMMAND_TOOL_CHAR_BUDGET` 调整。超出时名字都保留，从最少用的技能开始去掉描述。（skills › Skill descriptions are cut short）
- 每条的 `description` 加 `when_to_use` 合计最多 1,536 字符。（skills › frontmatter reference）
- `disable-model-invocation: true` 的技能不进列表；`user-invocable: false` 只是不进 `/` 菜单，描述仍在上下文里。

## MCP 工具怎样列出

- 默认延迟载入：会话开始时只载入工具名和服务说明（instructions），完整 schema 用工具搜索时再载入。（mcp › Scale with MCP tool search）
- 每个工具描述和每个服务说明默认截断在 2,048 字符。（mcp › Tool search for MCP server authors）

## 用户电脑 [实测]

- 技能 40 个，`name` 加 `description` 共约 9,700 字符（winhand 现在把描述截在 300 字符）。
- 本机 MCP 服务 5 个，都是 stdio，用到时才启动：eshop-mcp（npx，启动超时 180 秒）、fast-context（npx）、freecad-mcp（uv，180 秒）、pyocd-debug（uv）、usb-camera-mcp（uv）。
- 本会话的工具里没有这 5 个服务：它们要在 claude.ai 里单独加成连接器才能用。
