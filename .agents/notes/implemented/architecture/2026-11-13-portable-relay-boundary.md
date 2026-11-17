# Agent Note: 自托管中转保留 Workers 运行时契约

Status: implemented

## Problem

用户希望 WinHand 在自己的服务器快速部署，目标 LAX，Windows 经 DMIT 接入，并保留旧 Worker 可回退。原 relay 依赖 DurableObject WebSocket 休眠/附件/存储和 OAuth KV；直接换成 Node HTTP 服务并不能保持这些语义。现有 CIMD 诊断补丁尚未提交，不得覆盖。

## Decision

`relay/selfhost/` 是独立部署边界，bundle复用原 OAuthProvider/Bridge；原 Cloudflare 入口和发布不变。它生成 workerd 原生配置，使用官方内建 KV、两个独立持久化目录、固定 namespace/uniqueKey。没有自写 OAuth 或 KV 仿真。

原 workerd 1.20260925.1 缺少 2026-11-07 才合入的 bundled KV；独立 `selfhost/runtime` lockfile固定官方1.20261113.1。Windows和LAX/Linux真实KV/OAuth/WS与停机备份恢复穿刺通过，LAX公开服务及真实Windows隧道已部署，Claude网页仍需独立验收。启动器拒绝明显过旧版本，不在启动时下载或静默降级。localDisk为实验性，版本升级必须停机备份与恢复演练。

Caddy覆盖内部身份/IP头；运行时只监听 loopback，额外共享proxy secret与Host校验防止不受信任的本机/公网调用，避免自托管直接信任客户端伪造的 CF-Connecting-IP。OAuth公共origin固定HTTPS域名；秘密从私有文件加载，仅写私密构建配置，不进命令参数。Windows ACL仍需在部署时核实。

## Alternatives considered

- Node重写：生产运维常见，但需要重新实现DO、流式转发、重连和OAuth存储，显著扩大安全/回归范围，不作为此次捷径。
- Miniflare：本机已有5.20260925.0-alpha，便于本地模拟，但不把alpha模拟器包装为生产服务。
- 原 Worker：保留作为回退，但不能满足任意自有服务器部署。
- 硬编码/代理 CIMD或放宽大小限制：不能解决真实信任/地区边界，不采用。

## Consequences

优点是业务与安全协议不分叉；代价是依赖较新的实验性workerd能力，必须先实测再部署。新域名独立授权，不拷贝grants/token；Windows agent单relay，切换非双活。DMIT SSH管理链路与agent WSS数据链路分别验证。相关发布门槛见 `relay/selfhost/README.md`。

现有notes中 `2026-11-01-files-through-one-link.md`、`2026-11-01-relayed-calls-wait-40s.md` 仅规定原业务行为，本次保持不变，不替代它们。没有同主题自托管旧决定。
