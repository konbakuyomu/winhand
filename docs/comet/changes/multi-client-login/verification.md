# 验收记录：multi-client-login

## 第 1 轮

**改动**
- `relay/src/index.ts`：
  - 登录时传 `revokeExistingGrants: false`，新登录不再作废已有的登录；
  - 每次登录后只保留最新的 20 个，超出的作废。这一步失败不影响本次登录，下次登录时再清理；
  - provider 设置 `refreshTokenTTL` 和 `refreshTokenIdleTTL` 为 30 天；
  - 新增 `/signout`：校验主人口令，与授权页共用失败计数，通过后作废全部登录；
  - 首页显示登录数，读不到时如实说明。
- `relay/src/pages.ts`：状态页加“登录”一节和“退出所有登录”表单。
- `relay/test/e2e.py`：新增 `many_sign_ins`，最后执行，因为它会退出所有登录。
- `README.md`：说明多个登录可以同时存在、30 天失效、最多 20 个，以及如何一键退出。

**检查**（云端，本地 `wrangler dev`）
- `npm run typecheck`（tsc）通过；`npm test`（bridge、hook 单元测试）31 passed。
- `relay/test/e2e.py` 全部通过，57 项 PASS。
- 对照：把新测试跑在改动前的中转代码上，失败在“two sign-ins of the same client both stay valid”。说明这项测试能复现用户遇到的掉线。
- CI：`relay.yml`（run 39025433203，9f6d429）通过，包括类型检查、单元测试和 `wrangler dev` 上的 e2e。

**逐项结论**

| 验收项 | 结论 | 依据 |
|---|---|---|
| M1 | 通过 | 同一客户端（相同 client_id 和回调地址）登录两次，两份令牌都能调用 `/mcp`；旧代码上这一项失败 |
| M2 | 通过 | 再登录 20 次后，最早的三份令牌（包括刷新得到的那份）得到 401，最新 20 份仍可用，状态页显示“现在有 20 个登录” |
| M3 | 通过 | 口令错误：401，“口令不对”，登录仍有效；口令正确：“已退出 20 个登录”“现在有 0 个登录”，之后旧令牌得到 401 |
| M4 | 通过 | provider 配置了 30 天的 `refreshTokenIdleTTL`；e2e 里刷新令牌成功，新令牌可用。顺延到期时间是库的行为（见 `oauth-provider.js` 刷新时的 `grantData.expiresAt = now + refreshTokenIdleTTL`） |
| M5 | 通过 | 类型检查、单元测试、e2e 都通过 |

**没做的**
- 没有请独立 Verifier：按用户要求，再审一轮要用用户电脑上的 Codex，而电脑现在连不上。可以在部署前补一轮。
- 没有部署：要在用户电脑上用 `relay/wrangler.deploy.jsonc` 执行 `npx wrangler deploy`。

**风险**
- 状态页每次打开都要列一次 KV（列出登录）。Cloudflare 免费额度是每天 1000 次列表操作；个人用的状态页一般用不到。即使超出，也只是状态页读不到登录数，登录本身不受影响。

## Codex 审核与部署

**Codex 审核**（用户电脑上的 Codex CLI 0.163.0，只读沙箱，范围 `git diff 17b0658 9f6d429 -- relay` 加 README 相关段落，对照 `@cloudflare/workers-oauth-provider` 1.1.0 源码）

通过的几项，已逐条对照库源码核实：
- `revokeExistingGrants: false` 后，同一 client 的授权互不作废；刷新令牌轮换和撤销只作用于当前 grant（`oauth-provider.js` 中 `completeAuthorization` 只在该选项不为 false 时收集要撤销的 grant）。
- `listUserGrants` 分页取全；`createdAt` 是秒级时间戳，按降序保留最新的 20 个。
- `/signout` 只接受 POST；先查锁定再验口令；与授权页共用 `winhand:fail:<ip>` 和 15 分钟窗口；口令比较是定长摘要加 `timingSafeEqual`。
- 状态页只显示数量，文本经过转义；读不到登录数时页面照常显示。
- 两个 TTL 的单位都是秒：`refreshTokenTTL` 是首次签发的有效期，`refreshTokenIdleTTL` 在每次刷新时把到期时间设为 `now + 30 天`，即 30 天不用才失效。

发现与处理：

| Codex 定级 | 发现 | 核实 | 处理 |
|---|---|---|---|
| 阻塞 | `revokeOldGrants` 用 `Promise.allSettled` 吞掉单个作废失败，`/signout` 仍显示“已退出 N 个登录” | 成立 | 6cf82e0：分别统计成功和失败；有失败时状态页报错（500）并提示再试；登录后清理旧登录的失败记日志 |
| 非阻塞 | 同一秒内的授权排序不分先后，新授权可能被剪掉 | 成立，但要同一秒有 21 个以上登录才会触发 | 不改 |
| 非阻塞 | 失败计数先读后写，并发可能多试几次；口令正确时不清零 | 成立，改动前 `/authorize` 就是这样 | 不在本次改 |
| 非阻塞 | 跨站 POST `/signout` 可以累加失败计数，把这个 IP 锁 15 分钟（不能作废登录） | 成立，改动前 `/authorize` 的 POST 也在 `approveConsent` 前先查计数，单给 `/signout` 加 Origin 校验挡不住 | 不在本次改 |
| 提示 | 登录数包括授权码还没换成令牌的授权（最多 10 分钟） | 成立 | 不改 |

修复后：云端 `npm run typecheck` 通过，`npm test` 31 passed，`wrangler dev` 上 `relay/test/e2e.py` ALL PASSED。Codex 复审 6cf82e0：修复正确，对其余几条的判断没有事实错误。另提一点：状态页查询电脑在线状态失败时整页报错，这是改动前就有的行为。

**部署**（用户电脑，临时克隆 6cf82e0，用户的 `wrangler.deploy.jsonc`）
- `npm ci`、`npx tsc` 通过；`npx wrangler deploy -c wrangler.deploy.jsonc` 上传成功，当前版本 `42479f84-1b3e-46fc-a5d4-b8d30a05aaa6`（100%）。
- “绑定自定义域名”一步照旧报 `Could not find zone`，域名本来就绑着，不影响访问。

**线上验证**（从用户电脑请求）
- `GET https://winhand.konbakuyomu.us/`：200，页面有“现在有 7 个登录”和“退出所有登录”表单（`action="/signout"`）。
- 不带令牌 `POST /mcp`：401。
- `GET /signout`：303（跳回首页，不执行退出）。
