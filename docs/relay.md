# 中转：部署、多台电脑与安全设计

> 部署和接入的最短路径见 [README](../README.md)。其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [开发](development.md)。

`relay/` 是一个 Cloudflare Worker，也能用 workerd 跑在自己的服务器上（`relay/selfhost/`，代码是同一份）。它做三件事：

- 给 claude.ai 提供标准的 MCP 远程端点和 OAuth 2.1，授权页用主人口令校验；
- 电脑上的 winhand 主动用 WebSocket 连上它，**电脑本身不开任何入站端口**；
- 只透明转发 HTTP 请求（包括流式 SSE 回答），不解析 MCP。以后 MCP 协议升级，中转不用改。

## 用命令行部署到 Cloudflare

不用 README 里的按钮时：

```powershell
cd relay
npm ci
npx wrangler login                                 # 浏览器里授权
npx wrangler kv namespace create OAUTH_KV          # 把输出的 id 填进 wrangler.jsonc
npx wrangler secret put OWNER_PASSPHRASE           # 主人口令，至少 24 个随机字符
npx wrangler deploy
```

- 国内连不上 `*.workers.dev`，要给 Worker 加一个托管在 Cloudflare 上的自定义域名：可以在控制台里加，也可以取消 `wrangler.jsonc` 里 `routes` 那行的注释。
- 以后升级，重新运行 `npx wrangler deploy`。

自己的服务器见 [relay/selfhost/README.md](../relay/selfhost/README.md)，那里有一条命令的部署。

## 多台电脑

一个中转可以连多台电脑。每台电脑有一个名字（小写字母、数字和中间的连字符，最长 32 个字符），它的东西都在自己名下：

| 地址 | 用途 | 凭什么进 |
| --- | --- | --- |
| `/pc/<名称>/mcp` | claude.ai 连接器 | OAuth，令牌只对这台电脑有效 |
| `/pc/<名称>/mcp/<服务>` | 这台电脑上其他的 MCP 服务，见 [本机的其他 MCP 服务](local-mcp.md) | 同一个登录 |
| `/pc/<名称>/agent` | 电脑上的 winhand 连进来 | 这台电脑的设备令牌 |
| `/pc/<名称>/t/<令牌>` | 一次性文件链接、等待链接 | 链接里的令牌 |
| `/pc/<名称>/hook/<事件>` | 云端会话开场取规矩 | `HOOK_TOKEN` |
| `/pc/<名称>/status.json` | 这台在不在线，给脚本用 | 公开 |

`/status.json` 列出全部电脑。每台电脑在中转里有自己的连接、断线续传和离线状态，互不影响。

**配对。**
1. 在首页填电脑名称和主人口令，点「生成安装命令」，得到 `irm https://<域名>/i/<一次性码> | iex`。
2. 在那台电脑的 PowerShell 里运行它。
3. 脚本向中转换取这台电脑的设备令牌，用 `winhand login` 存进 `~/.winhand/config.toml`，然后装好、启动 winhand。

关于一次性码和令牌：
- 一次性码 10 分钟有效，只能用一次。
- 打开链接（GET）只返回脚本，不消耗一次性码；脚本再 POST 一次才消耗。所以聊天软件预览这个链接，不会把码用掉。
- 设备令牌由中转生成，中转只存它的 SHA-256。用已有的名字再配对一次，就是换掉那台电脑的令牌，别的电脑不受影响。

**不用托盘程序时**，用命令行连接（需要 [uv](https://docs.astral.sh/uv/)）。令牌从 stdin 读，不出现在命令行里：

```powershell
cd agent
'<设备令牌>' | uv run winhand login --url wss://<域名>/pc/<名称>/agent --token-stdin
uv run winhand connect
```

**移除一台电脑。** 在首页「安全」里移除，它的设备令牌、发给它的登录和它的连接随即作废。

## 从单台电脑的旧部署迁移

旧部署设有 `AGENT_TOKEN`，只连一台电脑，地址是 `/mcp`、`/agent`、`/t/…`、`/hook/…`。新代码部署上去以后：

1. 只要还设着 `AGENT_TOKEN`，旧地址照常通。它们指向一台叫 `default` 的电脑，旧连接器的登录也照常可用，不用马上改。
   - 旧中转开着 CIMD，claude.ai 的登录就是 CIMD 客户端。所以升级时要设 `CIMD=on`：自托管用 `install.sh --cimd on`，Cloudflare 加变量 `CIMD=on`。
   - 不设的话，旧连接器的访问令牌最多一小时后过期，刷新会失败，要重新授权。
2. 在首页给这台电脑配对一个名字，在 claude.ai 加上 `/pc/<名称>/mcp` 连接器。各个会话都换到新连接器以后，删掉 `AGENT_TOKEN`，旧地址随之消失。

## 登录

- 每次在授权页输入口令，都是一个独立的登录。多个 Claude 账号、多个会话可以同时登录同一台电脑，新登录不会让别处掉线。
- 30 天没用过的登录自动失效，再用时重新输一次口令；一直在用的登录不会过期。
- 每台电脑最多保留最新的 20 个登录，超出时作废最早的。
- 首页能看到一共有几个登录。在「安全」里输入主人口令，可以退出所有电脑的所有登录。

## 安全设计

- **口令。** 主人口令用常量时间比较。授权页、配对、移除电脑、退出登录共用一个错误计数：同一 IP 连续输错 5 次，锁定 15 分钟。
- **令牌只存哈希。** 设备令牌和一次性码，中转都只存 SHA-256。电脑一侧的令牌存在 `~/.winhand/config.toml`；winhand 返回给模型的任何内容里，它都会被遮挡（见[输出脱敏](guide.md#输出脱敏)）。
- **每台电脑一个 OAuth 资源。**
  - 每台电脑的 `/pc/<名称>/mcp` 是一个独立的受保护资源（RFC 8707、RFC 9728）。给一台电脑签的令牌，在别的电脑会被拒绝。
  - 授权页写明是哪台电脑。
  - OAuth 令牌由中转校验，不会转发给电脑。
- **CIMD 默认关，客户端走动态注册。** 原因：2026-11-13 部署在 Cloudflare 上的 Worker 去取 Claude 的元数据文档时，被转到了「该地区不可用」的页面。设 `CIMD=on` 才接受用元数据地址标识的客户端。
- **授权页**有防 CSRF 的一次性 handle 和绑定 cookie，并禁止被嵌入 iframe。
- **首页公开。** 谁都能打开首页，所以它只显示电脑名称、在不在线和 winhand 版本，不显示 Windows 名称和系统版本。
- **隧道端口。** 电脑在 127.0.0.1 上为隧道开的端口只认隧道：
  - 每次启动随机生成一个口令，隧道转发的请求都带着它，本机其他程序或网页（DNS rebinding）连上这个端口也调不了工具；
  - Host、Origin 不对的请求也会被拒绝。
- **短暂掉线。** 电脑短暂掉线时（winhand 重启或升级、网络切换），请求先等它重连，默认最多 20 秒（变量 `AGENT_GRACE_S`），之后才报错。
  - 离线超过 2 分钟（睡着了、关机了）就不再等，立刻报错。
  - 报错是带同一请求 id 的 JSON-RPC 错误，写明离线多久、可能的原因，以及那台电脑的 `status.json` 地址。
- **断线续传。** 电脑和中转之间的连接断一下（winhand 没重启）时，正在执行的调用不会丢：
  - 中转按帧记着已经收到多少，电脑重连后从断开处接着发，客户端看到的是一次完整的回答；
  - 电脑最多替断开的调用保留 60 秒，单个调用最多 8 MB 的回答。
- **一次性文件链接** `/pc/<名称>/t/<令牌>` 不走 OAuth，靠令牌本身授权：
  - 令牌是 256 位随机数，只能用一次，默认 10 分钟内有效，最长 30 分钟；
  - 目标路径和大小上限在签发时就固定了，上传可以要求 sha256 一致；
  - 电脑上只在内存里保存令牌的哈希，winhand 重启后所有链接作废；
  - 同一 IP 反复用无效令牌，会被暂时拒绝。

## Claude Code 云端会话的网络设置

要在 Claude Code 云端会话里用一次性文件链接和任务结束叫醒，得在云环境的网络设置里放行中转域名：

1. 打开会话标题栏的云环境菜单，选 Edit。
2. Network access 选 Custom，在 Allowed domains 里加上中转的域名。
3. 勾上 Also include default list of common package managers；不勾就只剩你填的域名。

改完约一分钟生效，正在运行的会话也会跟着生效。

如果靠[云端会话开场就带上本机规矩](rules-and-guards.md#云端会话开场就带上本机规矩)里的 Network secret 放行，它的 Path prefixes 要留空：填了 `/hook/` 的话，一次性链接会被沙箱的代理拦下，curl 报 403。
