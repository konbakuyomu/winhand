# 长任务与等待

> **读者：** 要改 `run`、后台任务，或任何一处“等”的逻辑的人：AI 怎样等一个跑很久的命令，每次能等多久，什么时候提前回来。

---

## 先说结论

AI 不该守着一个命令一轮轮地问，也不该自己睡一会儿再看。WinHand 给的路是：

1. `run` 约 40 秒内一定回复。命令还没完时，回 `still_running` 和一个编号（`run-N`），命令继续跑。
2. 更长的工作用 `job_start` 起后台任务。它独立于 winhand 进程，winhand 重启、升级都不影响它。
3. 两种都用 `job_status(id, since=游标, wait_s=…)` 等。等到下面任一情况就返回：任务结束、新输出匹配 `patterns`、出现了要人处理的情况、到了这次调用的上限。到上限不算失败，带着游标再问一次就行。

## 每次调用最多等多久

| 调用方 | 一次最多等 | 等待期间 |
|---|---|---|
| 经中转来的（claude.ai 连接器、云端会话） | 40 秒 | — |
| 本机客户端，而且请求了进度 | 1800 秒 | 开始时立刻发一次进度，之后每 15 秒一次 |
| 其他 | 40 秒 | — |

要的比上限长时，回复里的 `wait_note` 说明为什么被缩短。`job_status` 和 `session_wait` 都按这条规则。

**为什么经中转只给 40 秒。**
- Claude Code 的文档说：远程 MCP 只限首字节 60 秒，之后只要有进度就一直等。
- 但 2026-11-01 实测，云端会话经 claude.ai 连接器的调用，到 60 秒就被切断，进度通知发了也没用。
- 从电脑这边看不出是哪一层切断的，所以按“是不是经中转”来划线：中转给每个请求加了 `x-winhand-user` 头，agent 一看就知道。
- 40 秒比 60 秒少，是给中转和客户端返回留出余量。

**为什么本机客户端还能等 1800 秒。** 本机客户端不经过中转和连接器这两层，那次的证据不涉及它。代价是：本机 stdio 的长等待还没在真实客户端上实测过。以后哪个经中转的客户端真能长等，要先实测，再放开。

**几个调用方同时等。** 参数相同的并发等待共用一次等待，但每个调用方各收各的进度心跳。否则共用等待的那几个拿不到首字节，到 60 秒就会超时。

## 叫醒：等的时候不花回合

经中转的调用每次最多等 40 秒，所以云端会话以前要么每 40 秒问一次，要么用 `send_later` 定闹钟回来看。醒一次要把几十万 token 的上下文重读一遍，闹钟还常晚到：调用日志里，任务结束到会话看到，p90 的上界是 15.7 分钟。现在任务一结束就能叫醒会话：

- **哪里给。** 只给经中转的调用，本机客户端能长等，不给。
  - `job_start` 的回复、`run` 回 `still_running` 时，带 `wake` 和 `wake_note`。
  - `job_status(wake=true)` 也给，条件按参数定：
    - 带 id：这个任务结束。这次给了 `patterns` 的，带上它和这次的游标，新输出匹配也算；一组 id 取第一个。
    - 只给 `files`：第一个文件出现或有改动。
    - 什么都不带：电脑在线。
- **怎么用。** `wake` 是一行 bash。会话把它放进沙箱后台运行：Claude Code 的 Bash `run_in_background`，最长 2 小时。
  - 条件满足时命令结束，沙箱叫醒会话，带回最后一行结果；等的时候一个回合也不花。
  - 2 小时到了还没满足，就再跑一次。
- **底下是等待链接 `/pc/<电脑名称>/t/<令牌>`（单台电脑的旧部署是 `/t/<令牌>`）。** `wake` 一遍遍 GET 它：
  - 电脑每次最多挂 50 秒（`WAIT_HOLD_S`），每秒查一次条件。满足了回 `{"done":true,"state":…,"exit_code":…,"matched":…}`，没满足回 `{"done":false,…}`，命令接着问。
  - 电脑不在线时，命令隔几秒再问，电脑回来就接上。
  - 满足以后再问，照样回 `done:true`。
- **链接的规矩。** 它和一次性文件链接一样，靠令牌本身授权：
  - 令牌 256 位，电脑上只存哈希；
  - 只回状态，从不回输出；
  - 默认 12 小时有效。过期一天之内回 410，再往后和不认识的一样回 404；
  - 存在 `~/.winhand/wait-links.json`，winhand 重启、更新后照样认得；
  - 中转一行不改：`/t/` 本来就把 GET 转给电脑，响应头最多等 120 秒。

取舍见 `.agents/notes/implemented/feature/2026-11-17-wake-through-wait-links.md`。

## 等待什么时候提前回来

除了任务结束和 `patterns` 匹配，下面几种情况也会提前返回，`matched` 写明是哪一种：

- `window`：任务的进程弹出了新窗口，常常是要人点的对话框。
- `stalled`：5 分钟没有新输出，也几乎不占 CPU，多半卡住了。一开始就卡着的不算，免得每次都马上返回、空转。
- `prompt`：命令已经 5 秒没有输出，期间又冒出一个像在要人回应的窗口（对话框、登录框，或 1Password 这类签名、口令程序）。WinHand 同时让那个窗口的任务栏按钮闪烁，提醒主人。
- `secure_screen`：出现了 UAC 这类安全桌面，只有主人能处理。

这些情况 AI 都处理不了，早点回来告诉 AI，比等满上限有用。

“像在要人回应”是靠窗口类、窗口有没有所有者，加一份程序名单来判断的。所以名单外的程序会漏报，无关程序的对话框也可能被报出来；回复里说的是“可能在等它”。

## 还在跑的任务是什么样子

`job_status` 对还在跑的任务给出 `activity`：进程数、CPU、内存、多久没有输出、是否卡住，以及进程树弹出的窗口。`files=[…]` 可以顺便给出别的日志文件的末尾，守卫把它当作读这些文件来判断。

从同一个游标接着问时，只回新的东西：
- 启动命令和日志路径只在第一次给；
- stderr 只给上次之后新写的部分；
- 任务结束时再给全部字段；
- 要看 stderr 的末尾，就给 `tail`。

## 编号和退出码不会丢

- `run-N` 的编号存在电脑上，winhand 重启后接着往下编，不会重复。每个 `run` 一开始就有编号：回复在路上丢了时，`history()` 里能看到它，`job_status` 也能接着等。
- 后台任务的命令单独存成一个脚本来运行，所以命令里写了 `exit N`，也照常记下退出码 N。
- 完成的 `run` 至少留最近 20 条。回过 `still_running`、把编号交出去的，结束后再留 1 小时，这期间别的命令跑完多少条都挤不掉它。
  - 以前只留最近 20 条：几个会话同时跑命令时，`run-8211` 在 `job_status` 等它的时候被挤掉，退出码丢了。
  - 不是全部留一小时：输出都在内存里，命令多的时候占得太多。没交出编号的，调用方当场已经拿到结果。
- `job_start` 的 `timeout_s` 到时，结束整棵进程树，退出码记为 -1，标 `timed_out`，并给 `reason`（`timeout_s reached (N s)`），任务列表里也有。
- `after=[任务 id]` 让任务等前面的任务结束再开始，可以把几步串起来。

## 列表、停止和常驻任务

- **列表只给状态行。** `job_status()` 不带 id 时列最近 20 个任务，每个只给 `id`、`name`、`state`、`exit_code`、`started`、`finished`、`seconds`（超时的加 `timed_out`、`reason`），不带输出。
  - 以前每个直接运行的程序（比如 Codex）都带着最多 6 KB 的 stderr 尾巴，20 个就一两万字，一个会话看列表就把别的会话的输出读进了自己的上下文。
  - `prefix=` 按 id 或名字的开头筛，先筛再取 20 个。
  - `files=[…]` 不带 id 也照样给出那几个文件的末尾。
- **停止只回结果。** `job_stop` 回 `id`、`state`、`exit_code`、`stopped` 和最后 20 行（最多 2,000 字）。后台任务和 `run-N` 都一样。
- **回复带电脑的时间。** `run` 和 `job_status` 的回复有 `now`：电脑本地时间，带时区。以前 199 次 `run` 只为看时间。
- **`stdin_file`。** `job_start(stdin_file=路径)` 把电脑上的一个 UTF-8 文件当 stdin（开头的 BOM 去掉）。它和 `stdin=` 二选一，只能配 `args=`。守卫把它当作读这个文件来判断。
- **常驻任务。** `job_start(keep=true)` 的任务，进程没留下退出码就没了（电脑重启、被结束）时，托盘程序在一分钟内照原样再起一个：
  - 沿用原来的命令、参数、目录、名字、超时和密钥名；
  - 旧任务记 `revived_as`，新任务记 `revived_from`，托盘的时间线上记一条；
  - 每个任务只拉起一次；拉不起来（程序或目录没了、密钥删了）也只记一次，原因写在 `revive_error`；
  - `job_stop` 停掉的不再拉起；
  - 不能配 `stdin`、`stdin_file`、`elevated`：重启时电脑前可能没人点 UAC；
  - 密钥只记名字，拉起时再从密钥库取，任务目录里没有值；
  - 只有托盘程序会拉起。本机 stdio 服务和 `winhand connect` 不拉，免得几个进程各拉一份。
- **`quiet_ok_s`。** `job_start(quiet_ok_s=秒数)`：这么久没有输出算正常，卡住的门槛取它和 5 分钟中大的那个。
- **负载。** `sys_info(load=true)` 给可用内存、总内存（GB，一位小数）、CPU 占用（采样 0.5 秒）、在跑的后台任务数和 `run` 数。查进程的命令行仍用 `proc_list(name=…)`：它本来就按名字或命令行筛，并给出命令行的前 300 字。

## 任务运行期间电脑不睡

`run` 的命令和后台任务运行期间，电脑不会进入睡眠（显示器照常可以关），做完就撤销：

- `run` 由 winhand 自己向系统声明；后台任务由它自己的包装脚本声明，所以 winhand 重启也不影响。交互会话不声明。
- 接电源和用电池一样处理。主人的笔记本接电源时本来就不睡，只在用电池时 10 分钟后才睡；电量到临界值时 Windows 仍会休眠，不会把电耗光。
- 要关掉：`~/.winhand/config.toml` 里写 `[power] keep_awake = false`；单个任务用 `job_start(keep_awake=false)`。

## 电脑不在线时

- 短暂掉线（winhand 重启或升级、网络切换）：中转先等电脑重连，最多 20 秒。
- 离线超过 2 分钟（睡着了、关机了）：不再等，立刻报错，错误里给出那台电脑的 status.json 地址。
- 每台电脑的 `/pc/<名称>/status.json` 公开 `online`、`version`、`offline_since`、`offline_for_s`，不含 Windows 名称。云端会话可以在后台循环查它，电脑一回来就接着干活。顶层的 `/status.json` 列出全部电脑；设着 `AGENT_TOKEN` 时，顶层还带着 `default` 的这几个字段，单台电脑时代的写法照常可用。
- 电脑和中转之间的连接断一下（winhand 没重启）时，正在执行的调用不会丢：电脑重连后，从断开的地方接着发。每个调用最多保留 60 秒、8 MB。
- 桌面端重新连接、换中转地址时，会停掉本机的 MCP 服务再起一台。之后经中转的回答照常完整：sse_starlette 有个全局退出标志，见到一台 uvicorn 服务关停就永久置上，置上后所有 SSE 回答一开头就断。所以起本机服务前，先关掉它的自动排空并复位（`relay_client.serve_local`，见 `codestable/issues/004-x-ff-隧道重启后经中转的回答被截断.md`）。

## 证据

- 取舍：
  - `.agents/notes/implemented/feature/2026-11-01-relayed-calls-wait-40s.md`
  - `.agents/notes/implemented/feature/2026-11-17-wake-through-wait-links.md`
- 实现和实测：
  - `codestable/epics/001-x-使用复盘四轮强化/issues/002-x-第2轮长任务不用守着.md`：「实测」「0.10.1 装上后复测」两节；
  - `codestable/epics/001-x-使用复盘四轮强化/issues/001-x-第1轮小修.md`：轮询只回新内容、编号跨重启；
  - `codestable/epics/001-x-使用复盘四轮强化/issues/006-x-ff-任务命令里的exit照常记下退出码.md`。
- 代码落点：
  - `agent/src/winhand/waiting.py`：上限和心跳；
  - `agent/src/winhand/transfer.py` 的 `issue_wait`、`endpoint`：等待链接；
  - `agent/src/winhand/jobs.py`：包装脚本；
  - `agent/src/winhand/jobwatch.py`：`activity`、卡住判断、弹窗；
  - `agent/src/winhand/proc.py`：`run`、完成命令的保留（`_forget_finished`）、`sys_info(load)`；
  - `agent/src/winhand/desktop_backend.py` 的 `upkeep()`：每分钟拉起常驻任务；
  - `agent/src/winhand/power.py`：防休眠；
  - `relay/src/bridge.ts`：离线报错、`x-winhand-user` 头。
- 测试：`agent/tests/test_review2_wait.py`、`agent/tests/test_review2_jobs.py`、`agent/tests/test_review_round1.py`、`agent/tests/test_review_round2.py`、`agent/tests/test_review_round4.py`
