# Agent Note: 任务结束叫醒云端会话，走电脑签发的等待链接

Status: implemented

## Problem

经中转的调用每次最多等 40 秒（[relayed-calls-wait-40s](2026-11-01-relayed-calls-wait-40s.md)），所以云端会话等长任务，只能每 40 秒问一次，或者用 `send_later` 定闹钟回来看。2026-11-17 的使用复盘（`docs/reviews/2026-11-17-usage-review.md` 第四节第 1 条）量到：

- 子会话最多每 10 分钟醒一次。H5 2.8 小时醒 16 次，N17 4.5 小时设了 25 次 `send_later`。
- 醒一次约等于把 30–60 万 token 的上下文重读一遍。一个考官会话 11.5 小时花了 44.5 美元、1.56 亿缓存读。11-07 主人的额度用完，各线停了约四天。
- 闹钟还常晚到。822 个被问到结束的任务，从结束到会话看到，上界的中位是 0.4 分钟，p75 是 9.0 分钟，p90 是 15.7 分钟，累计 141.7 小时。

要的是任务一结束就叫醒会话，等的时候不花回合。

## Decision

- 云端沙箱的后台命令结束时会叫醒会话（Bash `run_in_background`，最长 2 小时）。所以要叫醒，就让沙箱里跑一条命令：条件满足它就结束。
- 这条命令要问的地方，是电脑签发的**等待链接** `https://<中转>/t/<令牌>`：
  - `transfer.issue_wait(cond, ttl_s=43200)` 签发，返回 `url`、`expires` 和 `wake`；
  - 条件只有四种：
    - `{"job": id}`：后台任务或 `run-N` 不再在跑；
    - `{"job": id, "patterns": […], "cursor": n}`：从游标起的新输出匹配，或任务结束；
    - `{"file": path}`：文件出现，或发票后改动过；
    - `{"online": true}`：电脑在答。
  - 令牌 256 位，电脑上只存哈希，存在 `~/.winhand/wait-links.json`，winhand 重启、更新后照样认得。默认 12 小时有效；过期一天内回 410，再往后和不认识的一样回 404。
  - `transfer.endpoint` 收到对它的 GET：最多挂 50 秒（`WAIT_HOLD_S`），每秒查一次条件。满足就回紧凑的 `{"done":true,"state":…,"exit_code":…,"matched":…}`，否则回 `{"done":false,…}`。从不回输出；PUT、POST 回 405。
- `wake` 是一行 bash：循环 `curl -m 75` 那个链接，见到 `"done":true`、`expired` 或 `unknown` 就打印并退出；没回应睡 15 秒，否则睡 5 秒。
- 只给经中转的调用（`waiting.relayed()`）：`job_start` 的回复、`run` 的 `still_running`、`job_status(wake=true)` 带 `wake` 和 `wake_note`。本机客户端能长等，不给。
- 中转不改。`/t/<43 位令牌>` 原本就是一次性文件链接的路，GET 原样转给电脑，响应头最多等 120 秒；只有电脑回 404 才给来源 IP 记失败，`wake` 见到 `unknown` 就停。

## Alternatives considered

- **中转加 `/hook/wait`，沙箱 POST 过去挂着。** 最强的理由：复盘时以为这是现成的路，中转已有 `/hook/<事件>`（`relay/src/hook.ts`），云端沙箱的代理也放行 `/hook/` 并自动带令牌。否掉的原因：11-13 起生产用的是 LAX 上自建的中转，它没配 HOOK_TOKEN；沙箱代理自动带令牌的那个域名，是 11-13 起已经下线的旧 Worker。要走这条路，得同时改 LAX 的配置、重新部署，还要改云端环境的密钥，三处都在仓外。等待链接用的是已经在跑的 `/t/`，一处都不用动，靠令牌本身授权，也不用另一把共享密钥。
- **GitHub 信箱（作业结束在 PR 上发评论叫醒）。** 最强的理由：实测 14 秒叫醒。否掉的原因：会话要挂上那个仓，后订阅的会悄悄顶掉先订阅的，开 PR、发评论都要主人本人点头，38 张票全带了 `--no-inbox`。
- **电脑上跑 `claude -p --cloud` 往会话推消息。** 最强的理由：不用沙箱轮询。否掉的原因：要在电脑上登录 Claude，主人 11-01、11-07 两次说过只在网页端用，电脑上不登录。
- **把经中转的等待放长到几分钟。** 最强的理由：AI 什么都不用改。否掉的原因：连接器 60 秒就切断，0.10.1 实测过；而且等的这几分钟仍算一个回合，上下文照样重读。
- **只把 `send_later` 的闹钟调密。** 最强的理由：零开发。否掉的原因：醒得越勤越贵，正是复盘里最贵的那一项。

## Consequences

- **收益**：
  - 任务结束到会话醒来，约等于沙箱下一次 GET 的间隔，加上叫醒本身：条件满足时挂着的那次 GET 当场返回；
  - 等的时候不花回合，也不用每次醒来先 ToolSearch、再设闹钟；
  - 电脑离线再回来，`{"online": true}` 的链接也能叫醒，不用主人说一句「电脑回来了」。
- **代价与已知上限**：
  - 沙箱一直在轮询：每 50 秒一次 GET，电脑上每秒查一次条件。几十个会话同时等时要量一下电脑的负载；
  - 沙箱要能连到中转的域名。各个云端环境的网络设置不一样，派生的子会话所在的环境要实测；
  - 后台命令最长 2 小时，更长的任务要会话重跑一次 `wake`，那一次要花一个回合；
  - 一组 id 只等第一个；条件只有这四种，等窗口、等进程这类要另加；
  - 叫醒延迟和花费的量化，等发版后跑满一周，用 `docs/reviews/tools/activity_stats.py` 和 `get_session` 量，写回 Epic 002。叫醒不了或延迟超过 1 分钟（p90），回到这里重看。

## Verification

- `agent/tests/test_review2_wait.py`：签发，没满足回 `done:false`，任务结束、匹配、文件、在线回 `done:true`，过期 410，不认识 404，重启后照样认得，只有经中转时回复才带 `wake`。
- 代码：`agent/src/winhand/transfer.py` 的 `issue_wait`、`endpoint`；`agent/src/winhand/tools_desktop.py` 的 `job_start`、`job_status`；`agent/src/winhand/server.py` 的 `run`。
