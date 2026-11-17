# winhand

winhand 让 claude.ai 和 Claude Code 通过一个 MCP 地址操作你的 Windows 电脑：运行命令和后台任务，驱动 gdb、串口、REPL、安装程序这类交互程序，读写文件并在 AI 的沙箱和电脑之间传文件，按名字操作桌面程序。你在电脑上写给 AI 的规矩、技能和守卫规则，每次调用都照样生效。

它由两部分组成：部署在 Cloudflare Workers 上的中转，给 claude.ai 提供带登录授权的 MCP 地址；运行在电脑上的 winhand，主动用 WebSocket 连上中转，在本机执行。电脑不开任何入站端口。

## 部署中转

需要一个 Cloudflare 账号，和一个托管在 Cloudflare 上的域名（`*.workers.dev` 在国内常连不上）。

```powershell
cd relay
npm ci
npx wrangler login
npx wrangler kv namespace create OAUTH_KV   # 把输出的 id 填进 wrangler.jsonc
npx wrangler secret put OWNER_PASSPHRASE
npx wrangler secret put AGENT_TOKEN
npx wrangler deploy
```

部署前在 `relay/wrangler.jsonc` 里填好 `PUBLIC_ORIGIN`，并取消 `routes` 那一行的注释，换成你的域名。

| 配置项 | 作用 | 默认值 |
| --- | --- | --- |
| `PUBLIC_ORIGIN` | 中转的公网地址，例如 `https://mcp.example.com` | 无，必须设置 |
| `OWNER_PASSPHRASE` | 在 claude.ai 授权时输入的主人口令（secret） | 无，必须设置 |
| `AGENT_TOKEN` | 电脑连中转用的设备令牌（secret） | 无，必须设置 |
| `AGENT_GRACE_S` | 电脑短暂掉线时，请求等它重连的秒数 | `20` |

## 连接电脑

下载 [Releases](https://github.com/konbakuyomu/winhand/releases) 里的 `winhand-win-Setup.exe` 安装，它在通知区域运行，不需要 Python 或 .NET。在设置里填中转地址 `wss://<你的域名>/agent` 和设备令牌，打开「登录 Windows 后自动启动」。新版本在后台下载，在设置里点更新即可。安装包用自签名证书，信任方法见 [Windows 签名与信任](docs/windows-signing.md)。

不装桌面端时，也可以用命令行连接（需要 [uv](https://docs.astral.sh/uv/)），`--save` 会把地址和令牌记下来：

```powershell
cd agent
uv run winhand connect --url wss://<你的域名>/agent --token <AGENT_TOKEN> --save
```

## 接入 Claude

在 claude.ai 的「设置 → 连接器 → 添加自定义连接器」里填：

```text
https://<你的域名>/mcp
```

用主人口令完成授权，新开一个对话就能用。打开 `https://<你的域名>/` 能看到电脑在不在线。Claude Code 云端会话还要在环境的网络设置里放行这个域名，一次性文件链接和任务结束叫醒才能用。

本机的 Claude Code、Claude Desktop、Codex 不经中转，直接用 stdio：`uv --directory <winhand>\agent run winhand stdio`。

> [!WARNING]
> 连上的 AI 能在这台电脑上执行命令、读写文件、操作桌面。主人口令和设备令牌都要足够长，不要外传；怀疑泄露时，在状态页输入主人口令退出所有登录，再换掉设备令牌。守卫规则和程序授权只防误操作，不是安全边界。

## 文档

- [使用指南](docs/guide.md)：工具一览、会话、长任务与叫醒、文件进出电脑、桌面操作
- [规矩与守卫](docs/rules-and-guards.md)：`context`、守卫规则、本机 hooks、云端会话开场带上规矩
- [本机的其他 MCP 服务](docs/local-mcp.md)
- [中转：部署与安全设计](docs/relay.md)
- [桌面端](desktop/README.md)、[开发与命令行](docs/development.md)

## 许可证

[MIT](LICENSE)
