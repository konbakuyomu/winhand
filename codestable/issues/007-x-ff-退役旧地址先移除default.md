---
kind: issue
title: "迁移文档补上顺序：先移除 default，再删 AGENT_TOKEN"
type: ff
status: closed
created: 2026-11-25
---

# 迁移文档补上顺序：先移除 default，再删 AGENT_TOKEN

主人的电脑重新配对、换上 `/pc/<名称>/mcp` 连接器以后，要删掉 LAX 上的旧令牌。照 `docs/relay.md` 原来的写法直接删 `AGENT_TOKEN` 会碰到问题：

- 单台电脑时代的登录没有绑定地址。中转删了 `AGENT_TOKEN` 就不再配 `legacyGrantResource`，`workers-oauth-provider` 这时取唯一的那个资源（`getLegacyGrantResource`）。
- 结果是，只剩一台电脑时，这些登录会改成指向那台电脑，不会作废。
- `default` 也会从首页消失，没法再单独移除，只能「退出所有登录」。

中转移除 `default` 时，提示本来就是「删掉这个密钥才算完全移除」，文档里没写这个顺序。

- 改动：
  - `docs/relay.md`「从单台电脑的旧部署迁移」：拆出第 3 步，先在首页移除 `default`，再删 `AGENT_TOKEN`；写明顺序为什么不能反，以及自托管和 Cloudflare 各怎么删。
  - `relay/selfhost/README.md`：升级一节补一句，不再要旧地址时怎么办。
  - `codestable/notes/001-发版与部署.md`：记下 LAX 不再设 `AGENT_TOKEN`。
- 验证（LAX，2026-11-25）：
  - 主人在首页移除 `default` 以后，「Claude 登录」从 3 个变成 1 个，剩下的是新连接器的。
  - 删掉 `.env` 里的 `AGENT_TOKEN_FILE` 和 `secrets/agent-token`，再 `docker compose up -d`，只重建了 relay 容器。13 秒后状态页只列出那台电脑，在线。
  - `/mcp`、`/agent`、`/t/…`、`/hook/…`、`/pc/default/…` 和不带路径的资源元数据都返回 404。`/pc/<名称>/status.json` 返回 200，`/pc/<名称>/mcp` 不带令牌返回 401。
  - 经新连接器调 `sys_info` 正常，一次性链接 `/pc/<名称>/t/…` 返回 `done:true`。
  - 新生成的 `config.capnp` 里没有 `AGENT_TOKEN`，`CIMD` 还开着。
- codestable：已同步 `notes/001-发版与部署.md`。现状规格不用改：讲旧地址的那一条本来就以设着 AGENT_TOKEN 为前提。

顺手发现：删 `AGENT_TOKEN` 以后，没绑定地址的登录可以由 `relay/src/auth.ts` 自动作废，就不用靠文档记顺序了。这次没做，要做另开票。
