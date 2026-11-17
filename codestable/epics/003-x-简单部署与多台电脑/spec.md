---
kind: epic
title: "简单部署与多台电脑"
status: closed
created: 2026-11-18
---

# 简单部署与多台电脑

> **读者：** 接手这条改进线的人——相对 0.11.0 要改什么、现在怎么理解方案、哪些能推、哪些还悬着、关了合回哪里。

---

## 这条线要改变什么

11-17 主人对照朋友的 pi-bridge（Cloudflare 一键部署，加上 `npm install -g` 装命令行），要 winhand 也这么好装；11-18 又提出两件事：中转首页平时用不到，要改得实用；要支持多台电脑连同一个中转，地址一律用 `https://<域名>/pc/<名称>/mcp`，各台电脑互不牵连（主人原话「解耦处理最好」）。

相对现在：

| | 现在（0.11.0） | 这条线做完 |
|---|---|---|
| 部署中转 | 本地跑 6 条命令，手改 wrangler.jsonc 三处，要两个密钥 | Cloudflare 点按钮，只填主人口令；国内再绑一次域名。自己的服务器一条命令 |
| 连电脑 | 浏览器下载安装包（可能弹 SmartScreen），在设置里填地址和令牌 | 在中转页面生成一条命令，在电脑的 PowerShell 里运行 |
| 电脑数量 | 一个中转一台；第二台连上来会把第一台顶掉 | 一个中转多台，每台一个名字 |
| 中转首页 | 在线状态、连接器地址、登录数、退出所有登录，混在一起 | 平时一行状态；接入时按步骤给命令和地址；安全操作收起来 |

关联 spec：
- `codestable/spec/index.md`：三个部分怎样连起来、统一语言（中转、一次性链接）、边界；
- `codestable/spec/files.md`：一次性链接的地址；
- `codestable/spec/waiting.md`：离线时看 `status.json`、等待链接。

## 当前怎么理解（活规格）

### 每台电脑一块独立的地址

一台电脑的所有东西都在 `https://<域名>/pc/<名称>/` 下面：

| 地址 | 用途 | 凭什么进 |
|---|---|---|
| `/pc/<名称>/mcp`、`/pc/<名称>/mcp/<服务>` | claude.ai 连接器，以及这台电脑上其他 MCP 服务 | OAuth，令牌只对这台电脑有效 |
| `/pc/<名称>/agent` | 电脑上的 winhand 用 WebSocket 连进来 | 这台电脑的设备令牌 |
| `/pc/<名称>/t/<令牌>` | 一次性文件链接、等待链接 | 链接里的令牌 |
| `/pc/<名称>/hook/<事件>` | 云端会话开场取规矩 | `HOOK_TOKEN` |
| `/pc/<名称>/status.json` | 这台在不在线，给脚本用 | 公开 |

- 中转里每台电脑一个 Bridge（Durable Object，名字 `pc:<名称>`）。断线续传、离线提示本来就装在 Bridge 里，每台一份，互不影响。
- 电脑名称只用小写字母、数字和连字符，最长 32 个字符，例如 `home`、`office`。中文名放进网址要编码，连接器地址会很难看，所以不收。
- 电脑上的 winhand 不用知道自己叫什么：配对时存下的地址就是 `wss://<域名>/pc/<名称>/agent`，链接的前缀取这个地址去掉末尾的 `/agent`。
- **不选**一个连接器加「用哪台电脑」参数（pi-bridge 的做法）：40 多个工具都要加参数，模型每次都要带对，漏了就在错的电脑上执行。分开的代价只是每台电脑在 claude.ai 里各授权一次。

### OAuth：每台电脑一个资源

- 每台电脑的 `/pc/<名称>/mcp` 是一个独立的 OAuth 资源（RFC 8707 / RFC 9728）。给 `home` 签的令牌拿去调 `office` 会被拒；授权页写明是哪台电脑。
- 现在用的 `OAuthProvider` 只能有一个资源，所以改用同一个库的 `OAuthAuthorizationServer` 加每台电脑一个 `OAuthResourceServer`。授权服务器的资源清单在构造时固定，所以每个请求按当前的电脑清单构造（清单存在 KV 里）。
- 退出登录：「退出所有登录」作废全部；移除一台电脑时，作废它的设备令牌和发给它的登录。
- CIMD 默认关，claude.ai 走动态注册。11-13 Cloudflare 上的 Worker 取 Claude 的公开元数据，被转到 `claude.com/app-unavailable-in-region`（地区限制），这就是生产搬到 LAX 的原因；关掉 CIMD，中转就不用访问 Claude。用变量 `CIMD=on` 可以打开。

### 配对和一行安装

- 中转首页填电脑名称和主人口令，点「生成安装命令」，得到：

  ```powershell
  irm https://<域名>/i/<一次性码> | iex
  ```

  一次性码 10 分钟有效，用一次就作废。口令输错的次数和授权页共用一个计数。
- 打开这个链接不算用掉：GET 只返回安装脚本，脚本再 POST 一次才换到设备令牌。聊天软件预览链接时不会把它用掉。
- 设备令牌由中转生成（256 位随机数），只在那次 POST 的回复里出现一次；中转只存哈希。同名重新配对，就只换这台电脑的令牌；在新命令运行之前，原来的电脑照常在线。
- 安装脚本：
  1. 从 GitHub Releases 的 `SHA256SUMS.txt` 找到最新安装包的文件名，下载后核对 SHA256；
  2. Inno Setup 静默安装（`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /MERGETASKS=autostart`）；
  3. 用 `winhand login` 把地址和令牌写进 `~/.winhand/config.toml`，令牌经 stdin 传，不上命令行；
  4. 经 explorer.exe 启动托盘，和安装向导一样，避免继承安装程序的 RedirectionGuard。
- PowerShell 下载的文件不带「来自网络」标记，不弹 SmartScreen：11-18 在 PowerShell 7.6 上实测过，5.1 待测。
- **不选** npm：winhand 在电脑上是 100 MB 的托盘程序，自带 Python，套 npm 得先装 Node，步骤反而多。

### 首页

- 平时：每台电脑一行，写明在线、版本，或离线了多久（不再显示 UTC 时间串）；另有 Claude 登录数。其余收起来。
- 接入：一台电脑都没有时自动展开三步：
  1. 生成安装命令；
  2. 连接器地址，带复制按钮；
  3. 云端 Claude Code 放行域名（可选）。
- 安全，收起：退出所有登录；移除一台电脑。

### 部署

- **Cloudflare 一键部署**：README 放「Deploy to Cloudflare」按钮，指向 `relay/` 子目录。
  - KV 和 Durable Object 由按钮自动创建。
  - 主人口令写在 `.dev.vars.example` 里，部署页逐个要求填写；`package.json` 的 `cloudflare.bindings` 写说明。
  - `PUBLIC_ORIGIN` 不用填，取请求的地址。
  - 国内连不上 `*.workers.dev`，一键部署又不管域名，README 写明在控制台加一次自定义域名。
  - 升级：Fork 后部署的，同步 Fork 就自动重新部署。
- **自己的服务器**：`relay/selfhost/` 用 workerd 跑同一份中转代码，前面是 Caddy，用 Docker Compose 起，再给一条命令装好。

### 兼容：现在这台电脑不断线

- 中转有 `AGENT_TOKEN` 时，旧地址 `/mcp`、`/agent`、`/t/`、`/hook/`、`/status.json` 照旧通，都指向一台叫 `default` 的电脑，等同于 `/pc/default/`。旧连接器的登录照常能用（旧资源 `https://<域名>/mcp` 留在清单里）。
- 主人在页面上重新配对这台电脑、在 claude.ai 加上新的连接器以后，删掉 `AGENT_TOKEN`，旧地址随之消失。新部署不设 `AGENT_TOKEN`，也就没有旧地址。

### 必须守住

- 信息安全性：设备令牌和一次性码只存哈希；各台电脑的令牌、OAuth 令牌、链接互不通用；首页不显示电脑的 Windows 名称和系统版本（和现在一样）；仓库公开，个人部署细节不进仓库。
- 兼容性：0.11.0 的 agent 不升级也能按旧地址连；旧连接器在迁移期间照常可用。
- 可靠性：断线续传、离线等待按台各自生效。

## 现在推什么、先搁什么

**Issues：**（均在本目录 `issues/`；编号只在本 Epic 内有效）

- [x] `issues/001-x-收进自托管中转.md`：电脑主检出里 11-13 起没提交的自托管中转和 CIMD 诊断，去掉个人部署细节后收进仓库。其余几张都要改同一批文件，先做。
- [x] `issues/002-x-中转支持多台电脑.md`：`/pc/<名称>/` 地址、每台的令牌和 Bridge、按电脑的 OAuth 资源、旧地址兼容。依赖 001。
- [x] `issues/003-x-配对和首页.md`：首页重做、生成安装命令、换取令牌、移除电脑。依赖 002。
- [x] `issues/004-x-一行安装.md`：安装脚本、`winhand login`、在 GitHub 的 Windows 机器上跑通。依赖 003。
- [x] `issues/005-x-一键部署.md`：Deploy 按钮、CIMD 默认关、文档；自己的服务器一条命令。依赖 002。

**穿刺顺序**（按不确定 × 失败代价）：
1. 授权服务器按电脑清单动态构造，多个资源同时能授权、能调用，旧资源的登录不失效（002）；
2. claude.ai 在 CIMD 关掉时走动态注册（005；主人加新连接器时顺带验证）；
3. 在真 Windows 上静默安装、写配置、启动托盘、连上中转（004）。

**暂不推进：** 每台电脑单独的 `HOOK_TOKEN`；winget 安装源；给托盘程序做配对界面。

**关闭时要满足：**
- 两台电脑同时在线，各自的连接器只能调到自己；旧连接器在迁移期间可用；
- 新部署从按钮到 claude.ai 能用，主人实际走一遍；
- 安装命令在一台干净的 Windows 上一次跑通；
- LAX 换上新中转、这台电脑重新配对以后，删掉 `AGENT_TOKEN` 仍正常。

**合并回 project spec 的候选：** `spec/index.md` 的「三个部分怎样连起来」和统一语言（电脑名称、配对、一次性码）；`spec/files.md` 的链接地址；`spec/waiting.md` 的 `status.json` 地址；部署和配对的取舍放进 `.agents/notes/`。

## 相关材料

- `.agents/notes/implemented/architecture/2026-11-17-stay-on-winhand-not-pi-bridge.md`：为什么不迁到 pi-bridge，以及借鉴的部分。
- `codestable/notes/001-发版与部署.md`：发版和部署中转的步骤，这条线做完要更新。
- `docs/relay.md`：中转的安全设计，这条线要改写地址部分。

## 关闭记录（2026-11-18）

主人 11-18 确认以下两步已经做完，并说「3 暂时不做，继续」，按现状关闭：
- 在 claude.ai 加了 `/pc/default/mcp` 连接器；
- 电脑更新到 0.12.0。

**关闭条件逐项：**

| 条件 | 结果 |
| --- | --- |
| 两台电脑互不通用；旧连接器在迁移期间可用 | 测试里两台电脑互不通用，已验证（真实 workerd、真 Cloudflare）。生产上旧连接器和新的 `/pc/default/mcp` 都能调到这台电脑；首页 2 个登录，电脑是 0.12.0 |
| 新部署从按钮到 claude.ai 能用，主人走一遍 | 未做：主人暂不做。同一份配置的 Cloudflare 测试 Worker 已用真实 agent 跑通 |
| 安装命令在干净的 Windows 上一次跑通 | CI 的 windows-latest 上通过：签名安装包、Windows PowerShell 5.1 |
| LAX 换上新中转，删掉 `AGENT_TOKEN` 仍正常 | 换上了，且旧地址保留。删掉 `AGENT_TOKEN` 要等这台电脑以正式名字重新配对、各会话换到新连接器，主人尚未决定；测试里去掉 `AGENT_TOKEN` 的情形已验证 |

**合并进 Project Spec 的章节：**
- `codestable/spec/index.md`：
  - 「这个项目是什么」里经中转的客户端；
  - 「三个部分怎样连起来」的图和中转一节；
  - 统一语言：一次性链接、电脑名称、配对、设备令牌、旧地址 / default；
  - 边界与考量：各台电脑互不牵连、CIMD 默认关、首页公开；
  - 想深入时。
- `codestable/spec/files.md`：一次性链接的地址。
- `codestable/spec/waiting.md`：等待链接的地址、电脑不在线时的 `status.json`。

**Vision**：`codestable/vision/index.md` 还是骨架，没有相关条目，未改。

**遗留（未建新票，主人要时再建）：**
- 在浏览器里点一次 Deploy 按钮，验证 CIMD 关掉时 claude.ai 是否改走动态注册。
- 在第二台真机上跑一行安装。
- 迁移的最后一步：这台电脑以正式名字重新配对，各会话换到新连接器，再删掉 `AGENT_TOKEN`。要注意，这台电脑经 SSH 隧道连 LAX，靠「WinHand LAX」快捷方式启动；一行安装脚本会直接启动托盘，也会写开机自启，两者要先理顺。
- 原自托管票里的生产定期备份、隧道看门狗。
