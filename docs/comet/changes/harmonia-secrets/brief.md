# harmonia-secrets：用 Harmonia 管理密钥，AI 用得到、看不到

## Outcome

Cloudflare、GitHub 这类密钥只在 Harmonia 里存一份，同步到这台电脑和其他设备。云端的 AI 经 winhand 在电脑上用 `harmonia exec` 运行需要密钥的程序：程序拿到密钥，AI 看不到；换密钥时在手机上改一次，所有设备都跟着变，不用每次重新建。

## 依据

- 用户的要求（2026-10-18）：经常要做 Cloudflare、GitHub 自动化，每次重建密钥太麻烦，希望用 [harmonia-vault/harmonia](https://github.com/harmonia-vault/harmonia) 的机制，同时保障隐私。
- 读了 Harmonia 的代码（724411f）：
  - `harmonia exec` 只把变量注入子进程的环境，子进程的输出原样透传，不做遮挡（`cli/cmd/harmonia/platform_windows.go` 的 `replaceProcess`）。
  - 电脑上的设备密钥以明文 JSON 存在 `~/.harmonia/keys.json`，只靠文件权限保护（`cli/internal/state`），同一用户的任何程序都能读，经 winhand 的 AI 也能。
  - `harmonia export --all` 只输出每个变量名生效的那一个值；同名变量在别的环境里的值要按环境逐个导出（`--env <id>`）。环境的 ID 和名称以明文存在 `cache.json` 里，值是密文。
  - 它给 AI 的说明（`harmonia skill`）要求：用 `harmonia exec`，不要打印值，`export`、`--show`、删除变量、卸载等要用户同意，不要运行 `harmonia manager`，不要读 `~/.harmonia`。
- 用户的电脑上还没装 Harmonia（2026-10-19 核实）。

## Scope

- 遮挡：Harmonia 给这台电脑的每个值，在所有工具结果里换成 `«redacted:harmonia:变量名»`。winhand 自己调用 `harmonia export --env <id> --format json`，按环境逐个读，值只留在进程里；`cache.json` 或 `overrides.json` 的内容变了才重读，在结果发出前完成。`[redact] harmonia = false` 可以关掉。
- 遮挡器：所有字面值合成一个正则，长的优先，各带标签。原来按种类分几遍替换，短值会把另一种类的长值切成两半。
- 内置守卫规则（不写进 guards.toml，`context()` 里能看到）：
  - 拒绝读写、删除、移动 Harmonia 的数据目录；
  - 命令里出现这个目录时拒绝；
  - 拒绝 `harmonia manager`；
  - `export`、`var list --show`、`var rm`、`logout`、`uninstall`、`shell uninstall|system-vars`、`service uninstall`、`update channel|auto`、`env activate|deactivate|order` 要人同意。
  - 这些规则不拒绝看不清内容的输入行，否则所有会话里用编辑键输入的行都会被拒。
- `secret_prompt(message, harmonia='<环境>/<变量名>')`：值从掩码对话框直接经标准输入交给 `harmonia var set`，再同步到其他设备。
- `context()` 增加 `harmonia` 一节：是否接入、可用的环境、怎么用；完整指南里加一条。
- README 一节：winhand 做了什么、怎么接入 Harmonia、建议。

## Non-goals

- 在 winhand 里实现 Harmonia 的协议或加密：只用它的命令行。
- 云端容器接入 Harmonia：每个会话都是新容器，每次都要在手机上批准，设备密钥还在 AI 手里。云端直接调 API 时，用云环境的 Network secrets。
- 替用户部署 Harmonia 服务端、安装 App。

## Acceptance

- **H1**：两个环境里有同名变量时，两个值都被遮挡，标签带变量名。
- **H2**：手机上新加的值同步到电脑后，下一个结果就被遮挡；只是重写了缓存、内容没变时不重读。
- **H3**：没接入账号或关掉之后，不调用 Harmonia。
- **H4**：AI 读不到 `keys.json`：`fs_read` 拒绝，`fs_search` 的结果里去掉；命令行里读它、`harmonia manager` 都拒绝；`export` 等要人同意；`harmonia exec`、`status`、`env list`、`var list` 照常。
- **H5**：`secret_prompt` 存进 Harmonia：值经标准输入交给 `var set`，结果里没有值，转告“注意：……”提示。
- **H6**：原有检查全部通过。

## Constraints

- Harmonia 是 v0.x，1.0 之前命令和本机数据都可能变。winhand 依赖的只有 `export --env <id> --format json`、`var set`（读标准输入）、数据目录里的 `config.json`（`deviceId`）和 `cache.json`（`environments`）。读不到时只记下原因（`context()` 的 `masking_problem`），不影响其他功能。
