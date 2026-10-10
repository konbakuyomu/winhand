# mcp-guide

## Purpose

MCP 客户端在连接时拿到的 instructions 应当完整可见。Claude Code 只保留 instructions 的前 4096 个字符（实测），现在的指南有 5730 个字符，末尾几段一直被截掉。本能力把 instructions 控制在限制以内，完整指南改由 `help()` 提供，并告诉模型这台电脑可能有本机规则和守卫。

## Requirements

### R1 instructions 不超过 4096 字符

- 服务端的 instructions 不超过 4096 个字符，无论开关怎么设置。
- 内容保留最常用的使用规则（`run`、会话、等待、文件、历史、桌面），并告诉模型两件事：
  - `help()` 有完整指南；
  - 开始在某个目录工作前，以及上下文被压缩后，调用 `context(path)` 读这台电脑的规矩和技能。被守卫拒绝或要求确认的操作来自这台电脑的主人，不要换方式绕过。

### R2 help() 返回完整指南

`help()` 返回完整指南：instructions 的全部内容，加上被移出的细节、本机工具清单和 `call()` 的说明。

Scenario: (A1) instructions 不被截断，help() 完整
- 操作：建立 MCP 连接，读取 initialize 结果里的 instructions；调用 `help()`；
- 结果：
  - instructions 不超过 4096 字符，包含 `context(` 和 `help()`；
  - `help()` 的结果包含以下各段：桌面操作、`job_start`、`sys_info(checks=true)`、`call(tool, arguments)`、本机 MCP 服务是独立地址。

### R3 本机 stdio 客户端

- `winhand stdio` 入口默认不显示 R1 中关于 `context()` 的那一句，因为本机客户端自己会读规则。
- 配置 `[context] stdio_hint = true` 时显示。
- `winhand http`、`winhand connect`（中转）入口始终显示。
- `context()` 工具在所有入口都可用。

Scenario: (A14) stdio 入口默认不显示规则指引
- 操作：分别以 stdio 默认配置、stdio 加 `stdio_hint = true`、http 三种方式构建服务端，读取 instructions；
- 结果：只有第一种不含 `context(`，三种都不超过 4096 字符。
