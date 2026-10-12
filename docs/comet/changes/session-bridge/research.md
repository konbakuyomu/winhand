# 调研：云端 Claude Code 的 SessionStart hook 和网络

来源：code.claude.com 的 hooks、settings、settings-reference、managed-settings、cloud-environments、claude-code-on-the-web、network-config 页面（2026-10-12）；[推断] 表示文档没有直说。另有本会话的实测，标 [实测]。

## SessionStart

- matcher：`startup`、`resume`、`clear`、`compact`、`fork`。输入除通用字段（`session_id`、`transcript_path`、`cwd`、`permission_mode`、`hook_event_name`）外有 `source`，可能有 `model`、`agent_type`、`session_title`。（hooks › SessionStart）
- 输出：`hookSpecificOutput.additionalContext` 在第一条消息之前加入上下文；退出码 0 时纯文本 stdout 也加入上下文。（hooks › SessionStart）
- **只支持 `command` 和 `mcp_tool` 两种 hook，不支持 `http`**。（hooks › Prompt-based hooks 的事件表）
- `additionalContext`、纯文本 stdout 各自上限 10,000 字符；超出时存成文件，模型只拿到路径和 2,000 字符预览，不会被要求去读那个文件。没有设置能提高上限。（hooks › JSON output）
- 压缩上下文后，SessionStart 以 `source = "compact"` 再触发一次，所以“压缩后重新带上规矩”由它覆盖。PostCompact 不能注入上下文。（hooks › SessionStart、PostCompact）

## 云端会话读哪些设置

- 单仓库会话读仓库里的 `.claude/settings.json`，包括 hooks；`~/.claude/settings.json`、`.claude/settings.local.json` 不读。（settings › Settings in cloud sessions）
- 多仓库会话只读各仓库的 `enabledPlugins`、`extraKnownMarketplaces`，不读 hooks。（同上）
- 云端用 `CLAUDE_CODE_REMOTE=true` 区分。（cloud-environments › Setup scripts vs. SessionStart hooks）[实测] 本会话里 `CLAUDE_CODE_REMOTE=true`，`git remote get-url origin` 是 `https://github.com/konbakuyomu/winhand`。
- SessionStart 的命令 hook 在云端最多 600 秒。（cloud-environments › Time limits）

## 网络和 Network secrets

- 云端会话的所有出站流量经安全代理。访问级别：None、Trusted（包管理器、GitHub 等）、Full、Custom（自定义域名清单）。（cloud-environments › Security proxy、Access levels）
- [实测] 本会话的环境不放行中转域名：`curl https://winhand.konbakuyomu.us/` 得到代理的 403。MCP 工具能用，是因为它们经 Anthropic 的 MCP 代理，不经容器。
- Network secrets：按环境保存；“代理在请求离开会话的虚拟机后，为你列出的域名加上密钥”，密钥不出现在环境变量和任何文件里。（cloud-environments › Add network secrets）
  - 只对 Pro/Max 开放，需要组织管理员角色；Team、Enterprise、自托管环境暂不支持。
  - 填写：允许的域名（可用 `*.` 开头）和凭据类型。默认是 `Authorization: Bearer`；自定义请求头可设名称、前缀、值，例如 `X-Api-Key` 并清空前缀。
  - 保存后看不到值，只能删除重加。
  - 列出的域名即使访问级别不放行也能访问。
- 普通环境变量对使用这个环境的所有人可见。（cloud-environments › Set environment variables）

## http hook（本次不用，记下以备以后）

- 字段：`url`、`headers`、`allowedEnvVars`、`onFailure`、`timeout`；总是 POST JSON，正文是事件输入。请求头里的 `$VAR` 只替换 `allowedEnvVars` 列出的变量。（hooks › HTTP hook fields）
- 2xx 的 JSON 按命令 hook 的输出解读；非 2xx、连不上都不阻断。（hooks › HTTP response handling）
- 托管设置 `allowedHttpHookUrls`、`httpHookAllowedEnvVars` 可以限制它。（settings-reference）
