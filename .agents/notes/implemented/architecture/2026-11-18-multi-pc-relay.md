# Agent Note: 一个中转连多台电脑，每台一块地址、一个 OAuth 资源，从首页配对

Status: implemented

## Problem

2026-11-18，主人要求：
- 一个中转连多台电脑；
- 地址统一用 `https://<域名>/pc/<名称>/mcp`；
- 各台电脑互不牵连。原话是「解耦处理最好」。

同一天还要求：
- 中转首页改得实用；
- 部署和安装像 pi-bridge 一样简单。

改之前的状况：
- 中转只有一个 Bridge（Durable Object 名 `default`），只认一个 `AGENT_TOKEN`；
- 第二台电脑连上来，会以关闭码 4000 把第一台顶掉，被顶掉的不再重连；
- OAuth 用库里的 `OAuthProvider`，它只能有一个受保护资源 `${origin}/mcp`。

## Decision

- **每台电脑一块地址**，都在 `/pc/<名称>/` 下：`mcp`、`mcp/<服务>`、`agent`、`t/<令牌>`、`hook/<事件>`、`status.json`。
  - 每台电脑一个 Bridge，名字是 `pc:<名称>`。断线续传、离线等待本来就装在 Bridge 里，所以按台各自生效。
  - 名称只用小写字母、数字和中间的连字符，最长 32 个字符。
  - agent 不需要知道自己的名字：链接和 hook 的前缀取自它配对时存下的连接地址去掉 `/agent`，见 `agent/src/winhand/config.py` 的 `relay_base`。
- **每台电脑一个 OAuth 资源** `${origin}/pc/<名称>/mcp`。
  - 用同一个库的 `OAuthAuthorizationServer` 加每台一个 `OAuthResourceServer`，给一台签的令牌拿到另一台会被拒，授权页也写明是哪台电脑。
  - 授权服务器的资源清单在构造时固定，所以每个请求按 KV 里的电脑清单构造一次。
- **配对由中转生成令牌**：
  - 首页填电脑名称和主人口令，得到 `irm https://<域名>/i/<码> | iex`。码是 128 位，只存哈希，10 分钟内有效。
  - GET 只返回安装脚本、不消耗码，所以聊天软件预览链接不会把码用掉；脚本 POST 一次才消耗。
  - 消耗时生成 256 位设备令牌，只存 SHA-256。同名重新配对只换那一台的令牌。
- **兼容**：只要还设着 `AGENT_TOKEN`，旧地址 `/mcp`、`/agent`、`/t/`、`/hook/` 就指向叫 `default` 的电脑。
  - 旧资源 `${origin}/mcp` 留在清单里，并设为 `defaultResource` 和 `legacyGrantResource`。
  - 顶层 `/status.json` 在列出全部电脑之外，保留 `default` 的旧字段。
- **CIMD 默认关**，变量 `CIMD=on` 才开。11-13 Cloudflare 上的 Worker 去取 Claude 的元数据，被转到 `app-unavailable-in-region`。

## Alternatives considered

- **一个连接器，每个工具加一个「用哪台电脑」参数（pi-bridge 的做法）。**
  - 最强的理由：claude.ai 里只加一个连接器。
  - 否掉的原因：
    - 40 多个工具都要加参数；
    - 模型每次都得带对，漏了就在错的电脑上执行；
    - 叫醒和文件链接还是得按电脑分路由。
- **所有电脑共用一个 OAuth 资源（例如 `${origin}/pc`），继续用 `OAuthProvider`。**
  - 最强的理由：改动小，不用每个请求构造授权服务器。
  - 否掉的原因：
    - 一台的令牌能调所有电脑，违背「各台互不牵连」；
    - `OAuthProvider` 只有一个资源，换资源会让旧连接器的登录失效，而总调度和子会话全天候在用旧连接器。
- **电脑清单放 Durable Object，不放 KV。**
  - 最强的理由：强一致，新配对的电脑立即在各地可授权。
  - 否掉的原因：
    - 要加一个 DO 类，Cloudflare 和自托管两边的配置都得改；
    - KV 在写入的地方立即可见。配对之后先要下载安装百来 MB，再去 claude.ai 加连接器，KV 最多一分钟的传播已经过去了。
- **设备令牌仍由主人在部署时设成密钥（每台一个变量）。**
  - 否掉的原因：每加一台电脑都要改 Worker 的密钥，一键部署也就不止填一个口令了。

## Consequences

- 收益：
  - 多台电脑互不影响；
  - 旧连接器和 `AGENT_TOKEN` 在迁移期间照常可用；
  - 新部署只要一个主人口令；
  - 首页成了接入入口。
- 代价：
  - 每台电脑在 claude.ai 里要各授权一次；
  - 每个请求都要读一次 KV 里的清单，并构造一次授权服务器；
  - 新配对的电脑，在别的地区最多一分钟后才能授权。
- 迁移：在页面上给现在这台电脑配对一个名字，在 claude.ai 加上 `/pc/<名称>/mcp` 连接器，等各会话都换过来，再删掉 `AGENT_TOKEN`。

## Verification

- `relay/test/multi-pc-runtime.mjs`，用真实 workerd 运行时跑：
  - 先用改动前的中转（提交 ea67358）在 `/mcp` 签一个登录；换上新代码后，这个登录的访问令牌和刷新令牌都照常可用。
  - 配对两台电脑：码只能用一次，GET 不消耗。
  - 每台只认自己的令牌；电脑看到的路径不带 `/pc/<名称>`；路径插入的资源元数据指向那台电脑。
  - 移除一台电脑：它的连接以 1008 关闭，令牌失效，地址 404。
  - 去掉 `AGENT_TOKEN` 后，旧地址 404，已配对的电脑照常可用。
- 单元测试：
  - `relay/test/pcs.test.mjs`：名称、路由、令牌只存哈希、配对码；
  - `agent/tests/test_multi_pc.py`：链接前缀、hook 地址、`winhand login`。
- 原有的 `relay/test/selfhost-runtime.mjs`（单台电脑的完整 OAuth 流程）在新代码上照样通过。
