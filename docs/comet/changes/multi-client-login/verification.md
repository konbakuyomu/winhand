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
