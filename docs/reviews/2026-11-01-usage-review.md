# WinHand 使用复盘与下一轮建议（2026-11-01）

状态：待讨论。这次只做调查，没有改 winhand 的代码。

## 一、结论

1. **等后台任务最慢，也最费钱。**
   - 0.7.0 之后的 13 天里，`job_status` 占全部调用的 16.6%，累计在里面等了 30.9 小时。
   - 其中 63% 的等待等满 40 秒，任务还没跑完。
   - 你提过两次方向相反的要求：
     - 10-22 为了省钱，让调度会话“别再每 40 s 轮询”；
     - 10-31 又因为任务 23:20 就跑完、AI 到 0:22 才发现，要求“周期性检查的更频繁”。

   靠轮询，两条只能顾一头；只有“任务结束时叫醒 AI”能两条都满足。Claude Code 现在已经提供了做这件事需要的机制（见第四节）。
2. **`fs_read` 读 PDF、Word、PPT、Excel 时，正文没交到模型手里。**
   - 这是 bug，2026-11-01 已复现，从 0.3.2 起就存在。
   - 77 次读文档里，有 24 次紧接着就是 AI 自己写脚本抽文字。
3. **电脑睡着、重启或离线，长任务就停。**
   - 6 个对话因为电脑睡眠中断，最长停了 72 分钟。
   - 一次生产发布期间离线 3 小时 12 分。
   - 一次掉线后，会话闲置了约 50 小时。
   - AI 为防休眠自己写了 7 个看门狗脚本。

建议的顺序：
- **第 1 轮**：先修文档 bug、精简轮询回复（小修，0.7.4）。
- **第 2 轮**：解决“长任务不用守着”（0.8.0）。
- **之后**：文件与服务器一轮，桌面一轮。详见第六节；需要你定的事在第七节。

## 二、看了哪些材料

- **电脑上的调用日志**（`~/.winhand/activity/*.jsonl`）
  - 2026-09-26 至 11-01 共 35,611 次调用，报错 316 次。
  - 0.7.0 之后（10-19 至 11-01，13 天）共 21,972 次，报错 134 次（0.6%）。
- **云端对话**：10-19 至 10-31 的 66 个 Claude Code 云端会话。
  - 6 个子代理分线阅读：RK3588 总调度、RK3588 各票、串口转接板各票、桌面与文档、运维、工作流与看板。
  - 每条结论都带会话 id 和时间（UTC）。
- **以前的记录**：
  - 看板的 winhand 卡；
  - 仓库 `docs/comet/`；
  - 两次改进会话：`session_01WQyEGGHL3rqYLUGGxCgBwm`（10-10 至 10-17）和 `session_01VqExYkgDcXQ3v5PJkV1fZt`（10-18 至 10-24，产出 0.7.0 至 0.7.3）。
- **同类项目和规范**：在电脑上用 Smart Search 0.1.32（`search`、`exa-search`、`fetch`）查了当前版本。
  - 原始网页存在电脑的 `%TEMP%\wh-research`（桌面类）和 `%TEMP%\wh-research2`（终端、文件、SSH、协议类）。
  - 涉及的几条 Claude Code 行为，是对照 `cc-mcp-docs.md`（code.claude.com/docs/en/mcp）的原文逐条核对的。

0.7.0 之后的调用分布：

| 类别 | 调用次数 | 占比 |
|---|---:|---:|
| `run` / `job_*` / `remote_run` / `proc_*` | 11,792 | 53.7% |
| 文件读写搜（`fs_read`、`fs_edit`、`fs_write`、`fs_search` 等） | 8,413 | 38.3% |
| 文件进出（`fs_upload_url`、`fs_download_url`、`fs_send`） | 620 | 2.8% |
| 桌面（`screenshot`、`ui`、`window`、`input`、`ocr`） | 563 | 2.6% |
| 规矩与辅助（`context`、`skill`、`help`、`history`） | 339 | 1.5% |
| 本机 MCP（`mcp_call`、`mcp_tools`） | 236 | 1.1% |
| 交互会话（`session_*`） | 9 | 0.04% |

最近的工作以 KiCad 脚本、长时间布线、运维和文档为主，交互会话几乎没用。

## 三、AI 想要但 WinHand 没有、只好绕路的地方

| AI 想要的 | 现在怎么绕 | 代价与出处 |
|---|---|---|
| 任务结束时叫醒它 | 一轮连发 3 个 `job_status(wait_s=40)`（顺序执行，等于睡 2 分钟）；沙箱里 `sleep N` 定时器；`send_later` 回访；自写 `wait-final.ps1`；多任务等待时传 `patterns:["NEVERMATCH"]` 强行等满 | 见第四节 1 |
| 任务运行期间电脑别睡 | 7 个自写看门狗脚本，调用 `SetThreadExecutionState`；还常叮嘱你“电脑保持插电、别睡眠” | 6 个对话中断，最长 72 分钟 |
| 后台任务超时、结束整棵进程树 | `job_start(timeout=…)` 被拒（10-29）；自写 4 小时 tree-kill | RK3588 票 06–10 |
| 看出任务卡死 | 用 `Get-Process` 采样 CPU（`proc_list` 没有 CPU 列） | 票 23 空轮询 29 次 |
| 看到任务弹出的对话框 | 你在电脑前手动点“確定” | Freerouting 每次启动都弹警告，冒烟测试卡满 600 s |
| 读 PDF、Word 的文字 | 写 PyMuPDF、python-docx、拆 zip 的脚本 | 77 次中 24 次 |
| 一次传多个文件或整个文件夹 | 打 zip，再 `Expand-Archive`，再逐个核哈希（已成“惯例”） | 一次连传 4 个以上的有 57 批，共 349 个文件 |
| 传 10 MB 以上的文件 | 切成 4 MB 分片，拼回，再逐个删除分片 | 35 次上传、34 次删除，约 17 分钟 |
| 在服务器上带密钥跑脚本 | `run` 直接调 ssh，把密钥从管道塞进去 | 0.7.0 之后 ssh 走 `run` 124 次，`remote_run` 0 次 |
| 往服务器拷文件、开端口转发 | 用 scp、heredoc，或把 base64 塞进 stdin；`job_start(ssh -N -L …)` | 运维会话 |
| 一次改多个文件 | 自写替换脚本，还发明了一种补丁格式 | 一次改 15 张卡、38 处；这些自写脚本首次运行出错 6 次 |
| 一次读或搜多个文件 | `run` 里 `Get-Content` / `Select-String` | 连读多个文件的批次有 884 批 |
| 截图存文件、按窗口坐标点、后台任务带脚本 | 参数被拒后改走别的路 | `screenshot(path=)`、`input(window=)`、`job_start(script=)` |
| 截最小化的窗口 | 先 `window restore`（要授权），再截 | 11 次 |
| 用电脑的网络抓网页 | `Invoke-WebRequest` 加正则；`Edge --headless --dump-dom` | 沙箱代理 403 时 |
| 知道电脑现在几点 | 每次 `run` 里调 `Get-Date` | 597 个脚本 |

## 四、问题细节和证据

### 1. 等后台任务

数字（调用日志，0.7.0 之后）：
- `job_status` 共 3,653 次，占 16.6%，累计 30.9 小时；
- 2,298 次（63%）等满 40 秒还没结束；
- 单个任务最多被问 230 次（`t22-run-reva`，153 分钟）。

对话里的情况：
- **一轮连发 3 个、但顺序执行。**
  - 几个 winhand 调用同时发出时会一个接一个执行，因为工具没有只读标记，见第 9 条。
  - AI 就利用这一点：一轮连发 3 个 `job_status(wait_s=40)`，每轮睡 2 分钟。
  - 例：RK3588 总调度（`session_01HfZLfLGU1dTxUr3nRVnXdo`），一个 17.6 分钟的任务用了 25 次调用、约 10 轮。
- **大部分回复是空的或重复的。**
  - 同一会话的 141 个回复里，55% 是“running”且没有新输出。
  - 每次都回传的启动命令，占 `job_status` 回复字节的 49%；工作流会话里是 35%（27K/78K 字符）。
  - 直接运行的程序（例如 Codex），每次还回传 stderr 末尾 6 KB，有没有新内容都传（`jobs.py` `status()`）。
- **沙箱定时器。** 一个会话用了 45 个 `sleep N` 定时器，共 8.6 小时；另有 11 次 `send_later` 回访。
- **用户的两次要求。**
  - 10-22T15:17（`session_019RdXhRqVskhghEhqZGLZsV` 转给票 24）：“省钱的改法（用户要求降低花费）……别再每 40 s 轮询 job_status 了”。
  - 10-31T16:32:48（RK3588 总调度）：“刚刚那个23点20分就跑完了，你这个等到了第二天 0点22分才等待结束，浪费了一个小时，我需要你跑长时间任务的时候周期性检查的更频繁才行”。

Claude Code 现在的规则（`code.claude.com/docs/en/mcp`，已核对原文）：
- **60 秒只限首字节。** HTTP 和 claude.ai 连接器的单次请求计时只到服务器回第一个字节为止（至少 60 秒）。
- **空闲窗口 5 分钟。** 收到回复或进度通知就重新计时。
- **主对话里的调用超过 2 分钟会自动转后台。** 结果出来时以任务通知送回，期间不消耗回合。子代理里的调用不转后台，会一直等。
- **只读工具。** 标了 `readOnlyHint` 的工具能和其他只读调用并行；断线时如果它的调用还没收到回复，会在新连接上补发一次。
- **幂等工具。** 标了 `idempotentHint` 的工具同样会补发一次。

所以 winhand 的 40 秒上限当初是为了避开首字节计时。如果改成“一开始就发进度、之后每 20 秒发一次”，`job_status` 可以一直等到任务结束：
- 主对话里，2 分钟后自动转后台，任务一结束 AI 就醒；
- 子代理里一直等，不耗回合。

这条路要先实测一次，确认经过中转和 claude.ai 连接器的路径不会把流截断。中转本身只给响应头设了 120 秒上限，响应体不限时（`relay/src/bridge.ts:31`）。

### 2. 读文档的 bug

- **复现**（2026-11-01）：
  - `fs_read("RK3588 Hardware Design Guide-V1.0.pdf", pages="5-6")` 只返回 `{"type":"pdf","pages":289,"shown_pages":[5,6],…}`；
  - 读一个 `说明.docx` 只返回 `{"type":"docx","path":…}`。
- **原因。** 返回结果里同时带了“概况”（`structured_content=facts`）和正文（`tools_desktop.py` `media_result()`）。Claude Code 只把结构化的概况交给模型，正文没交过去。图片不受影响，照常显示。这个写法从 `203103e`（2026-09-26，0.3.2）起就有。
- **影响。**
  - 研究 A 会话（10-24T14:56:33）：“The text layer didn't come through fs_read”，随后写了 7 个 PyMuPDF 脚本。
  - 读 xlsx 改成拆 zip；读 docx 改写 `dump_docx.py`。
- **修法。** 正文也放进结构化部分，或者文档结果不带结构化部分。改动很小。

### 3. 电脑睡眠、重启和离线

- **电脑会睡。** 接电源时空闲 900 秒就睡眠。
  - 票 09 的一次布线没有防休眠：本地 07:09–08:16 电脑睡着了。
  - 看门狗盯错了进程（mise 的 uv shim 先退出了），防休眠在半路被释放；你以为“电脑关机中断了”。
  - 出处：`session_01Fji1iYSHjoXyQ4MbJ4iQbh`、`session_01Vn7advxKSTPY6A7FVFQuiP`。
- **离线 3 小时 12 分。** 10-28 09:32–12:44Z，正值一次生产发布；重启之后，Codex 任务显示为 `vanished`（`session_01RXVvkkyXKU1QPrYVMszQm5`）。
- **掉线后会话闲置约 50 小时。**
  - 10-28 23:12Z 起，每次重连都“connection timed out after 30000ms”，winhand 工具从会话里消失。
  - AI 用 curl 打开中转首页得到 200，误以为是网络抖动；重试 47 分钟后放弃。
  - 电脑其实在它放弃 14 分钟后就连回来了，可会话一直闲到你 10-31 回来说“winhand 好了”。
- **断线仍然频繁。** 每天 15–47 次（“no close frame”），根因仍未查明（上一轮怀疑 mihomo/Sparkle 的 TUN）。0.7.0 的续传在兜底：0.7.0 之后因断线被取消的调用每天最多 4 个。

### 4. 后台任务本身不够用

- 没有 `timeout_s`、进程树结束（Job Object）、CPU 和“多久没输出”。票 23 卡住后，又空轮询了 29 次才被发现。
- 子进程弹出的对话框看不到：Freerouting 的“Non-ansi character”警告，让 6 个变体分别多等了 8–95 秒，冒烟测试卡满 600 秒。
- 进度常写在别的文件里：RK3588 总调度 329 次 `run` 里，约 50 次只是去读布线器的日志。
- 串联任务要自己写 `chain.ps1`；有一个任务的内容是 `while ($true)` 等另一个任务的文件出现。
- 默认用 Windows PowerShell 5.1：`*>` 写出 UTF-16 文件，只好改用 `shell=pwsh` 重跑。
- 同时开几个 Codex：要自写 `launch-audit.ps1` 加 6 次手工“偷看”进度。
- `run` 超时后会留下孤儿进程：一次 `WINWORD.EXE`（631 MB）没退出；一次核对任务卡了 6 分钟，最后靠 `job_stop` 加 `proc_kill` 收场。
- `run(script)` 失败却返回退出码 0 的几种情况：
  - .NET 的相对路径不跟 `Set-Location` 走；
  - pwsh 脚本里 Python 报了 traceback，退出码仍是 0。

### 5. 文件进出电脑

- 一次性链接传输 916 次（上传 666、下载 250），中位数 18.5 KB；上传的多数是 `.py`、`.sh`、`.md`。AI 习惯在沙箱里写好、测好，再传到电脑上。
- 超过约 8–10 MB 的上传大约 30 秒就返回 502，但文件其实传到了。AI 改成切片上传，后来干脆一律用 `max_mb=10`（视频会话、CPA 会话）。
- `%TEMP%` 被手动清理过两次，暂存在里面的文件跟着丢了（“could not open …\Temp\t20src\rk-review21.zip”）。
- `fs_send` 一次调用就能把 ≤20 MB 的文件交给 Claude Code，它会存进沙箱（已验证），但一共只用了 3 次。看一张图，AI 用的是“裁剪脚本 + `fs_download_url` + curl + Read”，4 次调用。

### 6. 服务器（SSH）

- **`remote_run` 被冷落。**
  - 开场说明（`server.py` 的 GUIDE）完全没提它，只有 `help()` 里有。
  - CPA 会话调过一次 `help` 之后，仍然又用 `run` 调了约 20 次 ssh。
  - 一次 here-string 带 CR 导致 `set: -\r: invalid option` 之后，“`run` 调 ssh”这个习惯就被写进了压缩摘要，一直沿用。
- **缺的东西**：
  - 密钥：`$env:CPAMP_ADMIN_KEY | ssh …`；
  - 拷文件；
  - 端口转发；
  - 遮挡网址里的 `user:password`：一个会话里有 11 个脚本带着手写的 `mask()`。

### 7. 改多个文件、读多个文件、context 太大

- `fs_edit` 在 0.7.0 之后失败 32 次。主要是“old text not found”，有几次是模型把引号写成了 `\"`。
- 有一次一个文件写出了 106 个字面上的 `\"`，后来只好用脚本清掉。
- `fs_edit` 失败按普通结果返回（`{"error":…}`，没有标成错误）。
- 每次 `context()` 返回 25–45 KB，其中技能清单 14–17 KB。每个子会话开场都调一次，压缩后再调；一个会话 30 分钟里调了 4 次。

### 8. 桌面

桌面工具在 0.7.0 之后只占 2.6%，但出错率高：`ui` 13/95，`ocr` 11/97，`screenshot` 12/219，`window` 8/82。

- **最小化窗口截不了**（11 次）：只能先还原再截，而还原要授权。
- **控件变了**：“control is gone”（7 次），只好改用 OCR。
- **OCR 认不出带标点的字**：`未选串口 · 115200` 这类带 “·” 的字串找不到。
- **前台切换和 `filter`**：
  - `window(focus)` 被 Windows 拒绝（7 次）。
  - `ui` inspect 的 `filter` 不匹配 automation id，结果 0 个命中。
- **等“画面稳定”被干扰**：浏览器里在放视频，4 次全部超时，共约 96 秒。
- **签名弹窗卡住命令**：`git commit` 被 1Password 的签名弹窗卡住，没人点。10-25 你已经关掉提交签名，之后再没出现过。但“命令被一个弹窗卡住”这类问题还在，比如 Freerouting。

### 9. 小毛病

- `run-N` 编号在 winhand 重启后从 1 重新开始，出现过同一个 `run-92` 跨 5 天的情况。掉线后 `history()` 只显示“running”，不给可以等待的 id。
- 几个只读调用一起发出时会排队：
  - `git status` 在一个 pytest 后面等了 41 秒；
  - `proc_list` 在 `job_status` 后面等了 58 秒；
  - `window(list)` 在“等画面稳定”后面等了 23–32 秒。
- `"expected_sha256": }` 这种只写了键名、没有值的参数（模型自己写错的），0.7.3 之后又出现过一次。每次整条调用作废，丢掉的内容最大 19.8 KB，只能重写或改成先在沙箱写好再上传。
- 小错误：
  - `fs_list` 对文件调用（12 次）；
  - `fs_search` 的 `context` 超过 20（6 次）；
  - `job_status` 的 id 写成了 JSON 字符串；
  - `old/new` 和 `edits` 同时给、但内容一致，也被拒；
  - `ocr(area=)` 不认。
- 用 `run` 启动的 `daily.ps1` 写出的日志乱码：UTF-8 被当成 GBK 读了。

### 用得好的地方（保留）

- 断线后用 `history()` 判断调用跑没跑，几乎每次都对，没出现重复执行。
- `job_status` 的 `patterns`、多 id 同时等，以及 `run` → `still_running` → `job_status` 这条接力，都好用。
- `run(command='git', args=['commit','-F','-'], stdin=…)` 写中文提交信息不会出引号问题。
- `run` / `job_start` 的 `secrets=…` 从没泄露过密钥。
- `fs_upload_url` / `fs_download_url` 加 sha256 校验：抽查 29 次，0 次失败。
- `screenshot(wait=…)` 加 `window(wait)` 加 `ui(invoke)` 测 App，13 张截图 0 次出错；OCR 驱动多步测试流程，在 UI Automation 认不出控件时也能用。
- `fs_search` 用 ripgrep 加 gb18030，`fs_find` 11 秒扫完 4.7 万个目录。

## 五、同类项目和规范，最近新增了什么

| 项目 | 看到的版本 | 新东西 | WinHand | 建议 |
|---|---|---|---|---|
| Windows-MCP（CursorTouch） | v0.8.7（09-30），主线到 10-31 | 主人接管：一动真鼠标或按 Ctrl+Alt+Shift+Backspace 就收回控制，空闲 10 秒再交还；AI 操作时屏幕边缘发光；`ControlStatus`；按开始菜单名启动程序；显示器 DPI 和工作区；带编号标注的截图 | 都没有 | 接管、启动程序：第 4 轮 |
| FlaUI-MCP（shanselman） | v0.2.0（07-08） | 控件 `ref`、`windows_batch` 批量动作、`send_keys` | `ref` 和批量没有 | 第 4 轮 |
| Microsoft UFO | v3.0.10（09-22） | 7 月后只有安全修复：命令白名单被绕过、参数注入、敏感动作在确认之前就执行了；UFO² 推测式多步动作 | 守卫同样只是正则 | 守卫改为按解析后的程序和参数判断 |
| Anthropic computer use | `computer_toolset_20260801` | 按住修饰键点击、`hold_key`、`key` 重复、光标位置；同一回合多个动作顺序执行，遇错即停 | 有拖动、区域放大、按下/松开（但说明里没写） | 第 4 轮 |
| Microsoft MCP on Windows | Learn 06-04，Build 2026 | ODR 注册表；代理在隔离会话里运行（MXC SDK 预览，只支持非交互） | 没有 | 观察 |
| DesktopCommanderMCP | v0.3.0（10-13） | 所有工具加注解（#828）；编辑没找到时给相似度和字符级差异 | 都没有 | 第 2、3 轮 |
| ssh MCP（tufantunc v2.22.0、classfang v1.10.0、bvisible v4.0.1） | 10 月 | sftp 文件不经过模型；sudo 口令自动遮挡；多主机；rsync 同步；隧道；只读/危险标记 | 没有 | 第 3 轮 |
| Playwright MCP | v0.0.83（09-28） | `browser_find`；把操作录成代码；按需开启工具组；工具带只读标记；README 推荐编码代理用 CLI 加技能 | 只能经网关接入，还没配 | 观察（电脑上已有 playwright-cli 技能） |
| MCP 规范 + Claude Code | 2026-07-28 | 只读/幂等标记（并行、断线补发）；`anthropic/alwaysLoad`（开场就加载工具）；`anthropic/maxResultSizeChars`；超过 2 分钟转后台；进度重置 5 分钟空闲计时；tasks 扩展（公测，只在终端 CLI 等） | 都没用上 | 第 2 轮 |

## 六、以前参考过的项目

上一轮的调研（10-18，由子代理用 Smart Search 完成）：

| 项目 | 借鉴了什么 | 当时没做、记了下来的 |
|---|---|---|
| Anthropic computer use | 截图总像素上限 1.15 MP（93ef5c1） | 示教录制 |
| DesktopCommanderMCP | 跨重启的 `history()`（0085a23） | 编辑没找到时给差异；任务期间防休眠；流式搜索；Office 编辑 |
| Windows-MCP | — | 主人接管和边框；DPI 和工作区；按开始菜单名启动；注册表 |
| FlaUI-MCP、Playwright MCP | — | 控件树带 `ref` 和批量动作；工具分组 |
| UFO² | — | GUI 与 API 混用；推测式多步动作 |
| ssh-mcp v2 | 只读/危险标记，后来归到“工具注解” | 命令分级、临时授权、用 elicitation 确认、每日配额 |

调研还看过这些（多数只记了想法）：
- **终端类**：terminator（录制回放）、cua（不占光标的输入）、iterm-mcp、pty-mcp、PiloTY、tmux-mcp。可借鉴的想法：`session_read` 支持 tail 和 grep、`awaiting_input` 再细分子状态。
- **Windows 管理类**：winremote-mcp、windows-admin-mcp。它们为服务、计划任务、事件日志、注册表提供了专用工具。
- **其他**：MCPControl、wcgw（先读再改、只写空文件）、mcp-remote-ssh（env 文件存密钥加遮挡）。
- **命令行类**：win-cli-mcp-server、mcp-shell-server、sonirico/mcp-shell。其中有一个 CVE，教训是正则守卫会被绕过。

不在那次调研里、但也借鉴过的：
- LCU（amontlabs/lcu）：按程序授权（0.6.0）。
- Harmonia：你提的。
- Codex 与 Claude Code：规矩、钩子、技能的格式。
- 程序库方面，没选 pywinauto/uiautomation、pywinrt、Tesseract/PaddleOCR。

上一轮列的待做清单，按价值排是：
1. 工具注解；
2. 编辑没找到时给差异；
3. 任务期间防休眠；
4. 控件 `ref` 加批量；
5. 主人接管；
6. 服务、计划任务等专用工具。

这次的使用数据把第 1、3 条推到了最前面，第 2 条随文件一轮做。

## 七、分轮建议

### 第 1 轮：0.7.4 小修（约 1 天）

1. 修复 `fs_read` 读文档时丢正文；用 PDF、docx、pptx、xlsx 各测一次。
2. 精简 `job_status`：
   - 启动命令只在第一次回复里给；
   - stderr 只回新增的部分；
   - 没有新输出时只回一句短话。
3. 开场说明补三句：
   - 服务器用 `remote_run`；
   - 取回 ≤10 MB 的文件用 `fs_send`（一次调用）；
   - 写提交信息用 `run(command='git', args=['commit','-F','-'], stdin=…)`。
4. `run-N` 编号带上启动标识，重启后不再重复；`history()` 给在途调用一个可以等待的 id。
5. 参数小修：
   - `fs_list` 指向文件时返回该文件的信息；
   - `fs_search` 的 `context` 超过 20 自动截到 20；
   - 认 `ocr(area=)`；
   - 与 `edits` 一致的 `old/new` 不报错；
   - 什么都不改的编辑跳过并注明；
   - `job_status` 的 id 是 JSON 字符串时也能解析。

### 第 2 轮：0.8.0 长任务不用守着（2–3 天）

1. **先做 1 小时实测。** 用一个测试工具，经中转和 claude.ai 连接器等 5 分钟、每 20 秒发一次进度，看：
   - 流会不会被截断；
   - Claude Code 会不会转后台、结束时会不会通知。
2. **长等待。**
   - `job_status`、`session_wait`、`mcp_call` 可以一直等到任务结束或匹配到 `patterns`（上限比如 30 分钟），期间每 20 秒发一次进度。
   - 其他客户端保持 40 秒。
   - 实测不通的话，备选是“等待链接”：沙箱里用后台 curl 长轮询，命令一结束 Claude Code 就会被叫醒。
3. **工具注解。**
   - 读类工具标 `readOnlyHint`：可以并行，断线时还会补发一次。
   - 常用的 8–10 个工具标 `anthropic/alwaysLoad`：不用先 ToolSearch。
   - 真正的失败标成错误。
   - 这一项要和长等待一起上：只读标记会让“一轮 3 个等待”变成同时返回。
4. **防休眠。** 后台任务、`run`、会话运行期间电脑不睡；结束 10 分钟后恢复。App 设置里可以关掉。
5. **`job_start` 加强。**
   - 加 `timeout_s`；
   - 加 `script=`；
   - 装了 PowerShell 7 时默认用它；
   - 用 Job Object 管整棵进程树。
6. **`job_status` 多报一些信息。**
   - CPU 用量；
   - 多久没有输出；
   - 是否卡住（`stalled`）；
   - 任务进程弹出的窗口（标题、文字、按钮）；
   - 能顺便看几个日志文件（`files=[…]`）。
7. **中转。**
   - 提供 JSON 状态：是否在线、最后在线时间、版本；
   - 电脑离线超过几分钟时立即报错，不再每次等 20 秒。

### 第 3 轮：0.9.0 文件和服务器

1. **文件进出电脑。**
   - 多个文件或整个文件夹一次传：zip 上传后自动解开并逐个核对 sha256；下载时按目录或通配打包。
   - 大文件分片上传：每片在 30 秒内传完。
   - WinHand 自己的暂存目录，不受 `%TEMP%` 清理影响。
2. **文件工具一次处理多个目标。**
   - `fs_read(paths=[…])`、`fs_search` 多个查询、`fs_edit` 跨文件改：要么全成，要么全不改，可以先预演。
   - `fs_edit` 没找到原文时，给出最接近的原文和差异。
   - 新内容里多出 `\"` 时提醒。
3. `context()` 加简要模式：技能只列名字；内容没变时只说“没变”。
4. **`remote_run` 补齐。**
   - 加 `secrets=`；
   - 能拷文件；
   - 能开端口转发（后台运行）；
   - 自动遮挡网址里的 `user:password`；
   - `run` 里直接调 ssh/scp 时提示改用 `remote_run`。

### 第 4 轮：0.10.0 桌面

1. **`input` 补全。**
   - 按住修饰键点击和滚动、三击、按住键、光标位置；
   - 按窗口内坐标点；
   - 截图存成文件；
   - 最小化窗口自动还原。
2. **`ui` 和 OCR 的小改进。**
   - `ui` 的 `filter` 也匹配 automation id；
   - `invoke` 之后报告新弹出的对话框；
   - “画面稳定”只看目标窗口；
   - OCR 忽略 “·” 等标点。
3. **`ui` 的 `ref` 加批量动作。** inspect 时给每个控件一个编号，之后按编号操作；批量动作按顺序做，遇错就停，最后截一张图。
4. **主人在场。** 你在用电脑时，AI 的桌面操作自动暂停；AI 操作时屏幕加边框；设一个急停快捷键。
5. **命令被弹窗卡住时说出来。** 被签名、登录这类窗口卡住时，直接告诉 AI，并提醒你。

### 以后再看

- **MCP tasks 扩展**：Claude Code 公测中，目前只支持终端 CLI 等，等云端会话支持后再接。
- **本机 MCP 网关**：升级到 2026-07-28 规范。顺带修好 Playwright MCP 的接入：roots、通知、输出目录。
- **Windows 自带的 MCP（ODR）和代理隔离**：观察。
- **示教录制，以及服务、计划任务、注册表的专用工具**：用得少，暂不做。
- **断线根因**：继续等一次自然断线来取证。

## 八、需要你定的事

1. **下一轮做哪些。** 我的建议是先做第 1 轮（文档 bug 影响面大、改动小），紧接着做第 2 轮。
2. **防休眠的默认值。** 后台任务、命令、会话运行期间，默认不让电脑睡，可以吗？还是只在接电源时才这样？
3. **暂存目录的清理。** 能否让 WinHand 定期清理自己暂存目录里 7 天前的文件？你的规则禁止 AI 批量删除；这里是程序清理它自己的目录，所以需要你点头。
4. **守卫规则。** 上一轮发现 rule 1 有几种写法能绕过：`rd /q /s`、`del /f /s /q`、`[IO.Directory]::Delete(…,$true)`、`robocopy /MIR`。另外，`fs_op` 能把整个目录删进回收站。要不要我出一份新规则给你确认？

## 附：下次复盘怎么做

1. **统计调用日志**：把电脑上的 `~/.winhand/activity/*.jsonl` 打包，用 `fs_download_url` 取到沙箱，再运行：

   ```
   python3 docs/reviews/tools/activity_stats.py <目录> --since 2026-11-01
   ```

   它按工具输出调用次数、报错归类、等待时长、`run` 里在做什么、上传和读取的批次。
2. **读云端对话**：用 `list_events`（`kinds: ["assistant","user"]`，每页 100 条，从最新往前翻）。结果太大会被存成文件，用 `docs/reviews/tools/digest.py` 把每页压成“工具调用 / 报错 / AI 说的话 / 用户的话”的逐行摘要。按项目线派几个子代理并行读，每条结论都带会话 id 和时间。
3. **查同类项目**：在电脑上用 Smart Search（`exa-search`、`fetch`）看各项目的 releases 和 README，原始网页留在 `%TEMP%`，只写读到了的功能。
