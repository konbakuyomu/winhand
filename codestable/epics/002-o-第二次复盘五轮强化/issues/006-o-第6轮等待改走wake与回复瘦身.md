---
kind: issue
title: "第 6 轮：等待改走 wake，回复再瘦身"
type: feature
status: open
created: 2026-11-24
---

# 第 6 轮：等待改走 wake，回复再瘦身

## 做成以后是什么样

1. **经中转的会话等后台任务，先用 `wake`。**
   - 开场说明（`agent/src/winhand/server.py` 的 `GUIDE`）和 `help()` 里，等任务的头一种写法是：后台跑回复里的 `wake`，醒来再用一次 `job_status` 取结果。
   - `job_status(wait_s=40)` 写成第二种：本机客户端用，或者 40 秒内就要结果的短任务用。
   - 经中转，同一个任务第二次问 `job_status` 还在跑，回复里带一句提示和这个任务的 `wake`。
2. **`job_status()` 的列表**，20 个任务在 2,000 字以内。现在 3,545 字，每个任务 177 字：
   - id 本身以名字开头，`name` 又写一遍；
   - `finished` 带七位小数和时区，而且有了 `started` 和 `seconds` 就能推出来。
3. **brief 的 `context()`**：守卫一节每条只给哪个文件第几条、管哪些动作、原因，不再原样列正则和路径清单（现在 11 条 3.9 千字）。全文照旧在不带 brief 的回答里。目标：rk3588 一次 brief 从 26,843 字降到 2 万字以内。
4. **`job_start` 收 `env`**，和 `run` 一样。这周有 11 次调用带了它被拒。
5. **守卫拒绝时写明命中的那一段**（截短），放在回复第一行，调用日志里也就有了。这周 14 次拒绝里有 9 次，日志截掉了删除那一段，事后判断不了是不是误伤（见第 5 张票的一周实测）。

**范围：**
- 不改等待链接、`wake` 那行命令、经中转每次最多等 40 秒。
- 不替主人改规矩文件。每次整份给的还有两份：
  - Codex 的 AGENTS.md，3.2 千字，里面的子代理派发细则只对 Codex 有用；
  - Comet 阶段规则，3.5 千字，只对开了 Comet 的仓有用。
  要不要拆、要不要只在某些仓给，由主人定。这张票只把数报给主人。
- `fs_write` 的 `file_text`（5 次）不加别名：是模型带过来的别家工具的参数名，报错里已经列出正确的参数。

## 为什么现在做

Epic 002 的一周实测（2026-11-24，Epic spec 里有表），四个目标只达到一个：
- 没在回合里等的任务，从结束到会话来看，p90 从 16.9 分钟降到 2.9 分钟；可 73% 的任务仍在回合里用 `job_status` 等，和之前一样。整周带 `wake=true` 的 `job_status` 只有 34 次。
- hwforge 的规矩 11-22 加了「等作业用 wake」，之后每起一个后台任务仍要问 1.7–2.2 次 `job_status`。会话照开场说明做，说明里头一种就是 `job_status(wait_s=40)`。
- 每次等待都是一个回合，要把会话上下文从缓存重读一遍；这周看到的子会话上下文到了 53 万 token。
- 规矩：只有 5% 的 `context()` 带 `since`，增量很少用得上；每次整份给的机器级部分有 1.6 万字。

## 方案与实现安排

- 等待：叫醒这条路不改，只改说明和提示。为什么 p90 没进 1 分钟、为什么改说明而不是改路，见 `.agents/notes/implemented/feature/2026-11-17-wake-through-wait-links.md` 的 Consequences。
- 规矩：`since` 的增量不再加码（缓存落盘、中间修改给差异都省不下多少），改为压短每次都给的部分。理由见 `.agents/notes/implemented/feature/2026-11-17-context-per-file-delta.md` 的 Consequences。
- 列表和守卫一节改字段，要先查有没有地方按现在的字段读：总调度和 dev-board 的脚本读不读 `name`、`finished`。

## 验证

- 单测：
  - 20 个任务的列表 ≤ 2,000 字；
  - brief 的守卫一节变短、规则条数不变，拒绝时的提示不变；
  - `job_start(env=...)` 生效；
  - 经中转第二次问同一个任务，回复带 `wake`；本机客户端不带。
- 装上后按 `docs/reviews/tools/wake_stats.py` 再量一周，和这次比：在回合里等的任务占比降到一半以下，没在回合里等的任务 p90 ≤ 1 分钟。

## 关闭时

- 回写候选：`codestable/spec/waiting.md`「叫醒」一节（说明里的等法）、`codestable/spec/index.md`（`context` 的 brief 怎么给守卫）。
