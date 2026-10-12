# 验收记录：session-bridge

## 第 1 轮（f2e43b9）

**正式检查**
- 电脑：`ruff check`、`ruff format --check` 通过；`pytest` 459 passed，40 skipped（新增 12 个）。
- 中转：`npm run typecheck` 通过；`npm test` 28 passed（新增 hook 的 9 个和隧道的 1 个）；`e2e.py` 对 `wrangler dev` 全部通过，含新增的 hook 检查。
- CI：`relay.yml`（run 38255420511）、`agent.yml`（run 38255416481，ubuntu、windows）都通过。

**独立 Verifier 结论**：A1–A7 全部通过，没有阻断问题。

核对方法：
- 用自己起的 `wrangler dev` 和真实隧道实测；
- 在 dash 和 bash 下对模拟中转跑生成的命令；
- 内容和预算的压力测试；
- `merge_settings` 的各种文件形状。

以下几项核对没有问题：
- hook 令牌到不了 MCP 工具、文件链接、`/agent`；
- `x-winhand-user` 无法伪造；
- `X-Winhand-Repo` 不能穿越路径；
- 令牌里有空格、引号、`$(…)` 时原样到达。

| # | 问题 | 级别 | 处理（第 2 轮） |
|---|---|---|---|
| 1 | `@` 引用进来的文件没有参与“是否被 git 跟踪”的判断，已跟踪的引用文件被重复发送 | 次要（实测） | 判断范围改为全部规则（含引用） |
| 2 | 中转等电脑回答最多 120 秒，hook 的 curl 25 秒就放弃：电脑在线但慢时，会话开场什么也没有，也没有说明；电脑关机时每次开场多等约 20 秒 | 次要 | hook 请求等电脑最多 5 秒、等回答最多 15 秒，超时由中转回说明；电脑端 `git ls-files` 最多 5 秒 |
| 3 | 失败计数在核对令牌之前：别人（例如公开仓库的克隆在云端开会话）可以把主人的 IP 锁住；每次 401 都写一次 KV | 疑似 | 没带令牌的不计数；只有带错的计数，锁定也只拦带错的；带对的令牌永远放行。README 写明域名会出现在仓库里，建议只用于私有仓库 |
| 4 | 技能名单固定最多约 2,500 字符，规矩很少时也被截断 | 小（实测） | 先留 2,500，规矩放完后的剩余也给技能名单 |
| 5 | 3xx 的正文会被打印进会话（例如登录跳转页） | 小（实测） | 回答先写临时文件，2xx 才输出 |
| 6 | 用环境变量放令牌时，令牌出现在 curl 的命令行参数里（同一容器内 `ps` 可见） | 小 | 不改：环境变量方式本来就对使用这个环境的人可见，README 已推荐 Network secrets |
| 7 | 开头说明可能很长：仓库地址最多 500 字符，出现两次，带凭据的地址也会被回显 | 小（实测） | 只以 `owner/name` 出现；命令发送前去掉 `@` 之前的凭据 |
| 8 | “(not read: not read: …)” 重复 | 小 | 修正 |
| 9 | 电脑回 500 时说明写“不在线或没有回答” | 小 | 改为“不在线、没有及时回答或出错” |
| 10 | Windows 上没有 Git Bash 时，本机 Claude Code 用 PowerShell 跑这条命令会出错 | 小（只读推断） | 不改：Claude Code 在 Windows 上需要 Git Bash；规格写明 |
| 11 | 9,000 字符按 Python 字符数计，Claude Code 若按 UTF-16 计，表情符号会多算 | 疑似 | 按 UTF-16 长度计数 |
| 12 | 没有 `content-length` 的分块正文要整个读完才判断 413 | 小 | 边读边数，超出即停 |
| e2e | 端到端没有覆盖 hook 令牌访问 `/mcp/<name>`、不带令牌 | 小 | 补上 |

**Verifier 指出的含糊处**
- 放不下的规则清单排在技能名单之前：可以接受。
- 全局规矩很长时会挤掉项目里没提交的规矩（只列路径）：与 R5 的写法一致，保留。
- 离线说明是英文：与 brief 的意思相同，保留（给模型看的提示统一用英文）。

## 第 2 轮

**正式检查**
- 电脑：`ruff check` 通过；`pytest tests/test_session_bridge.py` 17 passed（新增 5 个；命令测试补了 3xx 和带凭据的远端）。
- 中转：`npm test` 31 passed；`e2e.py` 对 `wrangler dev` 49 项全部通过。
- CI（5de5d3a）：`agent.yml`（run 38257687214，ubuntu、windows）、`relay.yml`（run 38257690764）都通过。

**独立 Verifier 结论**：A1–A7 全部通过，没有阻断发布的问题。第 1 轮的各项都已按“处理”一栏修好（第 6、10 项按决定不改）。

实测项目：
- 令牌：
  - 不带令牌 30 次都不计数；
  - 带错 20 次后才 429；
  - 锁定后带对的照常通过；
  - 重复的令牌头算错；
- hook 请求：电脑慢时 15 秒得到说明；离线时等 min(宽限期, 5 秒)；
- MCP 路径：宽限期和 120 秒不变；
- 超时之后：待处理项清掉，电脑迟到的回答被丢弃、不报错；
- 1,000 个技能时名单用到 8,587 字符；250 组随机内容（含表情符号）都不超过 9,000；
- 输出：只有 200 会输出；204、302、401、500、连不上都不输出，也不留临时文件；mktemp、curl 缺失时安静退出；
- 凭据：密码里含 `@` 也能去掉；
- 正文大小：分块正文 65,537 字节即 413。

| # | 问题 | 级别 | 处理（第 3 轮） |
|---|---|---|---|
| 1 | BMP 以外的字符组成的长规矩被截成空：截断时每轮按 UTF-16 的超出量删字符，删多了一倍 | 小（实测） | 每轮删超出量的一半 |
| 2 | `repo_key` 认不出的地址按原样显示，带凭据时会回显（只有手写或旧的 hook 会这样发） | 小（实测） | 电脑端也去掉 `@` 之前的部分，并限长 100 字符 |
| 3 | 仓库名没有长度上限，病态的请求头会让开头说明超过约 600 字符 | 小（实测） | 同上，限长 100 字符 |
| 4 | 规格里 R1 的“错误的令牌”场景、R6 的“bash”与现在的行为不符 | 小 | 改正 |
| 5 | wrangler dev 下，声明超过 64 KiB 的请求得到 413 后，同一连接上的下一个请求 500 | 疑似（只在 wrangler dev） | 不改：部署后的中转大概率不受影响，真机验收时留意 |

## 第 3 轮

**正式检查**
- 电脑：`ruff check`、`ruff format --check` 通过；`pytest tests/test_session_bridge.py` 19 passed（新增 2 个）。
- 中转：`npm test` 31 passed。
- CI（b56a8c9，PR konbakuyomu/winhand#9）：ubuntu、windows 的 `agent.yml` 和 `relay.yml`（含 wrangler dev 端到端）共 6 项都通过；合并为 f964b43，发布 0.4.7（release run 38260454037）。

## 真机验收（0.4.7）

**部署**
- 在用户电脑上，用用户自己的部署配置（`relay/wrangler.deploy.jsonc`，被 git 忽略）执行 `npx wrangler deploy`：
  - 新版本已上线，占 100%；
  - 绑定自定义域名那一步报 “Could not find zone”，但域名早已绑定，访问照常。记下：用这个登录账号重新绑定域名会失败。
- `HOOK_TOKEN` 由脚本生成，经 stdin 交给 `wrangler secret put`，聊天和日志里都没出现。
- 第一次放进剪贴板的令牌，在用户填表时被别的内容覆盖，存进 claude.ai 的值不对：中转回 “bad hook token”。改为生成新令牌写到桌面文件，用户复制后，文件由 Claude 删除。

**线上中转**
- 从电脑请求：不带令牌 401（no hook token），带错 401（bad hook token），`/hook/stop` 404。

**云端环境**
- 用户在环境里加了 Network secret：
  - 网站 `winhand.konbakuyomu.us`，路径前缀 `/hook/`；
  - 自定义请求头 `X-Winhand-Hook-Token`，前缀留空。
- 加上之后，本会话（访问级别不放行这个域名）也能访问中转，代理自动带上了令牌头。

**电脑**
- 发现用户电脑上运行的 WinHand 还是 0.4.4，0.4.5、0.4.6 都没装上，用户随后更新到 0.4.7。
- 这说明此前 P0、P1 的功能（守卫、`guards.toml`、本机 hooks）在用户电脑上到这时才真正生效。

**端到端**
- 从本云端会话 POST `/hook/session-start`（`X-Winhand-Repo: https://github.com/konbakuyomu/winhand`），HTTP 200。回答约 5,977 字符，含：
  - 电脑 “MSI” 上该仓库的检出目录 `D:\Dev\20_个人项目\winhand`；
  - 全局规矩 `~/.codex/AGENTS.md`、`~/.claude/rules/comet-workflow-guard.md`；
  - 技能名单。
- 在测试仓库 konbakuyomu/winhand-cloud-test（私有，`.claude/settings.json` 由 `winhand bridge init --write` 生成）新开云端会话，用户问“开场收到了哪些规矩和技能”。会话答出了：
  - 电脑送来的两份规矩，并正确理解 comet 规则只在仓库有 `.comet` 时适用；
  - 39 个技能的名单；
  - 说明这台电脑没有该仓库的检出目录（测试仓库只克隆在临时文件夹里，不在 Codex、Claude 记录的项目中），符合 R5。

## 结论

- A1–A7 全部通过。
- 2 轮独立 Verifier 加 1 次针对性复核，没有阻断问题，发现的问题都已处理或按决定保留。
- 真机端到端已在真实的云端会话里看到效果。

**已知限制**（不影响验收）：
- 多仓库的云端会话不读仓库的 hooks，这个功能不生效（Claude Code 的规定）。
- 用户发消息、回答结束这两个时机没有做（D1）。
- 用环境变量放令牌时，令牌在容器里可见；推荐用 Network secret。
- 用这个 wrangler 登录重新绑定自定义域名会失败，以后需要时在 Cloudflare 后台操作。
- 测试仓库 konbakuyomu/winhand-cloud-test 和电脑上的临时克隆（`%TEMP%\winhand-cloud-test`）留给用户决定是否删除。

**用户决定**：用户同意部署中转、用新建的私有仓库测试，并确认了云端会话里的效果。
