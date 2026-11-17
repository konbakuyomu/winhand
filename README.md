# winhand

winhand 让 claude.ai 和 Claude Code 通过 MCP 操作你的 Windows 电脑：运行命令和后台任务，驱动 gdb、串口、REPL、安装程序这类交互程序，读写文件并在 AI 的沙箱和电脑之间传文件，按名字操作桌面程序。你在电脑上写给 AI 的规矩、技能和守卫规则，每次调用都照样生效。

它由两部分组成：一个中转（部署在 Cloudflare Workers 或你自己的服务器上），给 claude.ai 提供带登录授权的 MCP 地址；每台电脑上运行的 winhand，主动用 WebSocket 连上中转，在本机执行。电脑不开任何入站端口。一个中转可以连多台电脑，每台一个地址。

## 部署中转

只需要一个主人口令：授权 Claude、在中转首页配对电脑时输入。用一串足够长的随机字符（至少 24 个）。

**Cloudflare（免费套餐即可）**

[![Deploy to Cloudflare](https://deploy.workers.cloudflare.com/button)](https://deploy.workers.cloudflare.com/?url=https://github.com/konbakuyomu/winhand/tree/main/relay)

按页面提示连接 GitHub，填写 `OWNER_PASSPHRASE`。Cloudflare 会在你的 GitHub 下建一个仓库并完成部署。

`*.workers.dev` 在国内常连不上：部署后在 Worker 的 **Settings → Domains & Routes** 里加一个托管在 Cloudflare 上的自定义域名。也可以不用按钮，用命令行部署，见 [中转](docs/relay.md#用命令行部署到-cloudflare)。

**自己的服务器（Linux 加 Docker）**

先把域名解析到这台服务器，放行 80 和 443 端口，然后运行：

```bash
curl -fsSL https://raw.githubusercontent.com/konbakuyomu/winhand/main/relay/selfhost/install.sh | sudo bash -s -- mcp.example.com
```

它会构建并启动中转，由 Caddy 自动申请证书，最后打印首页地址和主人口令。再运行一次就是升级，状态和登录都保留。

| 配置项 | 作用 | 默认值 |
| --- | --- | --- |
| `OWNER_PASSPHRASE` | 主人口令（secret） | 无，必须设置 |
| `PUBLIC_ORIGIN` | 中转的公网地址，例如 `https://mcp.example.com` | 取请求的地址 |
| `CIMD` | 设为 `on` 时，接受用元数据地址标识的 OAuth 客户端 | 关 |
| `AGENT_GRACE_S` | 电脑短暂掉线时，请求等它重连的秒数 | `20` |

## 连接电脑

打开 `https://<你的域名>/`，填电脑名称（例如 `home`）和主人口令，点「生成安装命令」，在那台电脑的 PowerShell 里运行它给的一行：

```powershell
irm https://<你的域名>/i/<一次性码> | iex
```

这条命令会：
- 下载最新安装包并核对 SHA-256，静默安装；
- 存好这台电脑的设备令牌，打开开机自启，启动托盘程序；
- 最后打印这台电脑的连接器地址。

命令 10 分钟内有效，只能用一次。多台电脑各起一个名字，各运行一次。以后的新版本在托盘程序的设置里点更新即可。

## 接入 Claude

在 claude.ai 的「设置 → 连接器 → 添加自定义连接器」里，每台电脑加一个：

```text
https://<你的域名>/pc/<电脑名称>/mcp
```

用主人口令授权，新开一个对话就能用。

中转首页列出每台电脑的地址，以及它在不在线。Claude Code 云端会话还要在环境的网络设置里放行这个域名，一次性文件链接和任务结束叫醒才能用。

本机的 Claude Code、Claude Desktop、Codex 不经中转，直接用 stdio：`uv --directory <winhand>\agent run winhand stdio`。

> [!WARNING]
> 连上的 AI 能在这台电脑上执行命令、读写文件、操作桌面。主人口令要足够长，不要外传。
> 设备令牌由中转生成，只在安装时出现一次。怀疑泄露时：
> - 设备令牌：在首页移除那台电脑，再重新配对；
> - 登录：在首页「退出所有登录」。
>
> 守卫规则和程序授权只防误操作，不是安全边界。

## 文档

- [使用指南](docs/guide.md)：工具一览、会话、长任务与叫醒、文件进出电脑、桌面操作
- [规矩与守卫](docs/rules-and-guards.md)：`context`、守卫规则、本机 hooks、云端会话开场带上规矩
- [本机的其他 MCP 服务](docs/local-mcp.md)
- [中转：部署、多台电脑与安全设计](docs/relay.md)
- [桌面端](desktop/README.md)、[开发与命令行](docs/development.md)

## 许可证

[MIT](LICENSE)
