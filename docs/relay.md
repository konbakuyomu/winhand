# 中转：部署与安全设计

> 从原 README 挪来。部署和接入见 [README](../README.md)，其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [中转](relay.md) · [开发](development.md)。

`relay/` 是一个 Cloudflare Worker，职责如下：

- 对 claude.ai 提供标准的 MCP 远程端点，包括 OAuth 2.1（支持动态注册和 CIMD），授权页用“主人口令”校验；
- 电脑上的 `winhand connect` 主动用 WebSocket 连上它，**电脑本身不开任何入站端口**；
- 中转层不解析 MCP 协议，只透明转发 HTTP 请求（包括流式 SSE 响应），以后 MCP 协议升级也不用改它。

部署步骤（需要 Cloudflare 账号，以及一个托管在 Cloudflare 上的域名。`*.workers.dev` 在国内经常连不上）：

```powershell
cd relay
npm ci
npx wrangler login                                   # 浏览器里授权
npx wrangler kv namespace create OAUTH_KV            # 把输出的 id 填进 wrangler.jsonc
#   在 wrangler.jsonc 中填写 PUBLIC_ORIGIN = "https://mcp.你的域名"，并取消 routes 那一行的注释
npx wrangler secret put OWNER_PASSPHRASE             # 授权页口令（要足够长）
npx wrangler secret put AGENT_TOKEN                  # 电脑连中转用的设备令牌（随机长串）
npx wrangler deploy
```

在电脑上连接中转（加上 `--save` 会把地址和令牌写进 `~/.winhand/config.toml`，之后直接运行 `winhand connect` 即可）：

```powershell
cd agent
uv run winhand connect --url wss://mcp.你的域名/agent --token <AGENT_TOKEN> --save
```

最后在 claude.ai 的“设置 → 连接器 → 添加自定义连接器”里填入 `https://mcp.你的域名/mcp`，完成授权后，**新开一个会话**即可使用。打开 `https://mcp.你的域名/` 能看到电脑是否在线。

多个 Claude 账号、多个会话可以同时连接这台电脑。每次输口令授权都是一个独立的登录，新登录不会让别处掉线：

- 30 天没用过的登录会自动失效，再用时重新输一次口令；一直在用的登录不会过期。
- 最多保留最新的 20 个登录，超出时作废最早的。
- 打开 `https://mcp.你的域名/` 能看到现在有几个登录。在那里输入主人口令，可以一次退出所有登录（比如怀疑某个登录泄露了）；之后各处要重新授权。

安全设计：

- 授权页口令用常量时间比较；同一 IP 连续输错 5 次锁定 15 分钟。状态页“退出所有登录”的口令和授权页共用这个计数。
- 设备令牌在中转一侧只保存在 Worker secret 里，电脑一侧在 `~/.winhand/config.toml`；winhand 返回给模型的任何内容里它都会被遮挡（见[输出脱敏](guide.md#输出脱敏)）。
- 电脑为隧道在 127.0.0.1 上开的端口只认隧道：每次启动随机生成一个口令，隧道转发的请求都带着它，本机其他程序或网页（DNS rebinding）连上这个端口也调不了工具；Host/Origin 不对的请求也会被拒绝。
- 状态页谁都能打开，所以只显示电脑在不在线和 winhand 版本，不显示电脑名和 Windows 版本。
- OAuth token 由中转校验，不会转发给电脑。
- 授权页有防 CSRF 的一次性 handle 和绑定 cookie，并禁止被嵌入 iframe。
- 电脑短暂掉线（winhand 重启或升级、网络切换）时，请求会先等它重连，默认最多 20 秒（变量 `AGENT_GRACE_S`），之后才报错。离线超过 2 分钟（睡着了、关机了）就不再等，立刻报错。报错是带同一请求 id 的 JSON-RPC 错误，写明离线多久和可能原因，客户端会原样交给模型。
- `https://mcp.你的域名/status.json` 给脚本用：`online`、`version`、`offline_since`、`offline_for_s`，同样不含电脑名。云端会话可以在后台循环查它，电脑一回来就接着干活，不用等人提醒。
- 电脑和中转之间的连接断一下（winhand 没重启）时，正在执行的调用不会丢：中转按帧记着已经收到多少，电脑重连后从断开处接着发，客户端那边看到的是一次完整的回复。电脑最多替断开的调用保留 60 秒、单个调用 8 MB 的回复。
- 一次性文件链接 `https://mcp.你的域名/t/<令牌>` 不走 OAuth，靠令牌本身授权：
  - 令牌是 256 位随机数，只能用一次，默认 10 分钟内有效，最长 30 分钟；
  - 目标路径和大小上限在签发时就固定了，上传可以要求 sha256 一致；
  - 电脑上只在内存里保存令牌的哈希，winhand 重启后所有链接作废；
  - 同一 IP 反复用无效令牌会被暂时拒绝。

## Claude Code 云端会话的网络设置

要在 Claude Code 云端会话里用这个功能，得在云环境的网络设置里放行中转域名：打开会话标题栏的云环境菜单 → Edit → Network access 选 Custom，在 Allowed domains 里加上 `mcp.你的域名`，并勾上 Also include default list of common package managers（不勾就只剩你填的域名）。改完约一分钟生效，正在运行的会话也会跟着生效。只靠[云端会话开场就带上本机规矩](rules-and-guards.md#云端会话开场就带上本机规矩)里的 Network secret 放行时，它的 Path prefixes 要留空：填了 `/hook/` 的话，一次性链接（`/t/…`）会被沙箱的代理拦下，curl 报 403。中转更新后要重新执行 `npx wrangler deploy`。
