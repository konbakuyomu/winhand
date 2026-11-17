# 自托管中转

在自己的 Linux 服务器上跑和 Cloudflare 同一份中转代码：workerd 运行 `src/index.ts`（OAuth、Bridge、一次性链接都不改），前面用 Caddy 接 HTTPS。Cloudflare 部署照旧用 `src/index.ts`，不经过这里。

## 一条命令

服务器装好 Docker（带 Compose 插件），把域名解析到这台服务器，放行 80 和 443 端口，然后：

```bash
curl -fsSL https://raw.githubusercontent.com/konbakuyomu/winhand/main/relay/selfhost/install.sh | sudo bash -s -- mcp.example.com
```

`install.sh` 做这些事：
1. 下载仓库的 `relay/` 到 `/opt/winhand-relay/code`；
2. 第一次运行时生成主人口令和 Caddy 与中转之间的口令，放进只有 root 能读的 `secrets/`；
3. 构建镜像，`docker compose up -d`，Caddy 自动申请证书；
4. 等首页能打开，打印地址；第一次运行还会打印主人口令，只显示这一次。

再运行一次就是升级：换上新代码、重建镜像，`secrets/`、`state/` 和所有登录都保留。

可选参数：
- `--dir` 换安装目录；
- `--ref` 指定分支或标签；
- `--source` 用本地检出代替下载；
- `--no-build` 用已有的 `winhand-relay:local` 镜像。

```text
Claude → HTTPS（Caddy）→ 127.0.0.1:8787 上的 workerd → OAuth + Bridge → 电脑上的 winhand
```

## 组成

- `entry.ts`：自托管入口。只认 Caddy 转来的请求：要带 `PROXY_TOKEN`，Host 要是自己的域名；客户端 IP 只取 Caddy 写的头，不信客户端自己带的 `CF-Connecting-IP`。
- `config.mjs`、`prepare.mjs`：校验配置，把入口打包成 `dist/selfhost/worker.mjs`，生成带密钥的 `dist/selfhost/config.capnp`（权限 600）。
- `start.mjs`：启动 `WORKERD_BIN` 指向的 workerd，只监听 `127.0.0.1:8787`。不在启动时下载运行时；版本早于 2026-11-08 的拒绝启动（内建 KV 从那以后才有）。
- `runtime/`：单独的锁文件，固定 workerd `1.20261113.1`。
- `Dockerfile`、`compose.yaml`、`Caddyfile`：容器化部署。Caddy 不记访问日志，免得 OAuth 的 code、state 和一次性链接进日志。

## 配置

| 变量 | 作用 |
| --- | --- |
| `WINHAND_HOST` | 中转的域名，例如 `mcp.example.com`。`PUBLIC_ORIGIN` 由它得出 |
| `WINHAND_STATE_DIR` | 持久目录，下面的 `bridge/` 和 `kv/` 是全部状态 |
| `OWNER_PASSPHRASE_FILE` | 主人口令所在的文件（至少 24 个字符） |
| `AGENT_TOKEN_FILE` | 可选：单台电脑旧部署的设备令牌（至少 32 个字符）。新部署不用，电脑在首页配对 |
| `PROXY_TOKEN_FILE` | Caddy 和 workerd 之间的口令（至少 32 个字符） |
| `HOOK_TOKEN_FILE` | 可选，云端会话开场取规矩用 |

密钥只放在文件里，不进命令行参数和日志；读取时去掉 UTF-8 BOM 和行尾空白（PowerShell 管道写的文件常带 BOM）。各个密钥要互不相同。

## 运行

```bash
cd relay
npm ci
npm ci --prefix selfhost/runtime --ignore-scripts
export WORKERD_BIN="$PWD/selfhost/runtime/node_modules/@cloudflare/workerd-linux-64/bin/workerd"
node selfhost/serve.mjs     # prepare + start
```

手动用 Docker 时，照 `install.sh` 做：在 `relay/` 里用 `selfhost/Dockerfile` 构建镜像 `winhand-relay:local`，在放 `compose.yaml` 的目录里写好 `secrets/owner-passphrase`、`secrets/proxy-token` 和 `.env`（`WINHAND_HOST`、`PROXY_TOKEN`），再运行 `docker compose up -d`。

## 验证

- `npm test`、`npm run selfhost:typecheck`；
- 设好 `WORKERD_BIN` 后运行 `node test/selfhost-runtime.mjs`：在 `127.0.0.1:18787` 起一个只用假凭据的 workerd，验证 KV、OAuth（动态注册、授权、换令牌、刷新、撤销）、WebSocket 和 SSE 转发、整进程重启后授权仍在，约一分钟。
- `node test/multi-pc-runtime.mjs`：多台电脑、配对、按电脑的 OAuth 资源，以及升级前签的登录在新代码上照常可用。
- 部署好以后，`WINHAND_RELAY=https://<域名> OWNER_PASSPHRASE_FILE=<文件> uv run --project agent python relay/test/live_check.py`：配对一台临时电脑，用真实的 winhand 连上、授权、调 `help()`，最后把它移除。

## 备份和升级

- 先停服务，再备份整个 `WINHAND_STATE_DIR`（包括 SQLite 的 WAL、SHM 文件）；不要只拷正在写的单个 `.sqlite`。
- 升级 workerd 前同样停机备份；失败时连同对应的整套状态一起回退。
- 不要改配置里的 `uniqueKey` 和 KV 的 namespace，改了等于换一个新库，所有授权失效。
