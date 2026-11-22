# Agent Note: 云端操控电脑继续用 winhand，不换成 pi-bridge 加 Windows 插件

Status: implemented

## Problem

2026-11-17，用户的一位朋友做好了 pi-bridge（github.com/cr-zhichen/pi-bridge）：
- 一个 Cloudflare Worker 服务端，给 claude.ai、ChatGPT 网页版提供 MCP 地址；
- 一个在电脑上跑的命令行，加载本机安装的 pi（pi 的 SDK），把 pi 的内置工具、插件和 MCP 服务转发给网页版 AI。

另一个对话建议整体换过去：传输、鉴权、长任务交给他和 pi 去维护，Windows 特有的部分（截屏、键鼠、OCR、串口、守卫、后台作业）做成一个 pi 插件。理由是 winhand 全靠自己维护，pi 有生态，以后新插件自动可用。

用户拿不定主意：要不要完全转向这种调用方式。要回答这个问题，先得核实「守卫和任务存活都能放进插件，不用改 pi、不用改桥」这句话。

## Decision

- 云端 AI 操控这台电脑，继续用 winhand，不迁移到 pi-bridge。用户 11-17 23:5x 拍板：「就走 winhand」。
- 下面这些是读代码核实过的，依据是 pi-bridge 提交 add85db（11-17 23:50）和 pi 1.5.2 的文档：
  - pi-bridge 只给 AI 6 个工具：open_session、list_capabilities、call、run、wait、cancel。`run` 和 `call` 最多等 25 秒，没完就交回一个任务号，之后靠 `wait` 轮询。
  - `call` 直接执行 pi 的工具，代码里写明「不经过扩展的 tool_call、tool_result 钩子」。`run` 不走工具，直接调 pi 的 shell；在 Windows 上默认是 Git Bash（`bash -c`）。
  - 任务只存在命令行进程的内存里，输出日志写在磁盘上。命令行退出时 `shutdown()` 会结束所有在跑的任务，所以 `pi-bridge update`、服务重启都会杀掉作业。
  - 命令输出一律按 UTF-8 解码。没有任何传文件的路径：搜不到 upload、download、transfer、base64。
  - 鉴权是路径里的一把密钥：`/<密钥>/mcp` 给网页版 AI，`/<密钥>/connect` 给电脑，两处是同一把。网址漏了，别人既能用 AI 控制电脑，也能冒充一台电脑连上来。
  - pi 的文档写明：插件注册同名工具，可以覆盖内置的 read、bash、powershell、edit、write、grep、find、ls。pi 设置里有 shellPath、shellCommandPrefix。
- 所以那句话只对一半：
  - pi 工具这条路的守卫可以在插件里做（覆盖同名工具）。长任务可以由插件自己提供落盘的作业工具。
  - `run` 的守卫只能把 shellPath 换成自己写的包装程序。
  - 任务结束叫醒、一次性文件链接、OAuth 都要在服务端加路由，插件做不到。
- 借鉴的两点，以后单独开票，不在这次做：
  - 默认只给 AI 少数几个工具，其余经 `call`；
  - 输出太长时只留头尾，并给出完整输出的文件路径。
- 真想用某个 pi 插件时：写一个几十行的小转换，把 pi 的工具作为本机 MCP 服务挂到 winhand 下面，经 `mcp_call` 调。调用仍走 winhand 的守卫和后台作业，传输不换。

## Alternatives considered

- **整体迁移：pi-bridge 加一个 Windows 的 pi 插件。**
  - 最强的理由：传输、鉴权、长任务的底子有人维护；pi 的插件、技能、MCP 生态直接能用；跨平台；一键部署；ChatGPT 也能连。
  - 否掉的原因：
    - 马上会丢掉今天刚实测通过的叫醒。他的等法是每 25 秒 `wait` 一次，正是 2026-11-17 复盘里最贵的那种。
    - 长作业跨重启：RK3588 那些跑几小时的排队作业，在他那边每次更新都会被杀。
    - 守卫和审批、GBK、一次性文件链接、OAuth 都没有。
    - Windows 部分（截屏、键鼠、OCR、串口和 gdb 会话、审批弹窗、托盘程序）要从 Python 改写成 TypeScript。
    - 总调度和子会话的派单、技能，都是按 winhand 的工具名写的。
    - 会变成依赖两个跟不上的上游：pi-bridge 是一个人的项目，11-17 当晚还在改基础结构；桥直接调 pi 的内部接口（createAgentSession、runToolCall）。
- **混合：传输用 pi-bridge，winhand 作为 MCP 服务挂进 pi。**
  - 最强的理由：winhand 的功能全保留，还省掉自己的中转。
  - 否掉的原因：中转不是维护的大头，0.11.0 发版时中转一行没改。叫醒和一次性链接要靠中转的 `/t/` 路由，pi-bridge 的服务端没有。中间多一层 pi，鉴权反而退回到网址里的密钥。
- **照另一个对话的提法：守卫放进插件、shell 换成包装程序、作业由插件提供。**
  - 最强的理由：不改 pi，也不改桥。
  - 否掉的原因：守卫和作业存活确实能这样做，但叫醒、文件链接、OAuth 还是要改服务端，相当于长期维护一个分叉。

## Consequences

- **收益**：在用的东西一样不丢，总调度和各个子会话不用迁移。今天发的 0.11.0（叫醒、规矩增量、常驻任务、守卫）直接生效。
- **代价与已知上限**：
  - 中转和传输层继续自己维护；
  - pi 的生态不能直接拿来用，用到时要写那个小转换；
  - 只支持 Windows，也没有一键部署。
- **什么时候重看**：pi-bridge 做到下面几条，迁移就越来越划算，到时 Windows 部分做成 pi 插件才值得：
  - `call` 和 `run` 经过 pi 的工具钩子，守卫能用 pi 插件写；
  - 任务记在磁盘上，更新、重启时不杀；
  - 有任务结束的通知出口，能接叫醒；
  - 电脑的凭证和连接器密钥分开，或者改成 OAuth。
- **2026-11-18 复查**：pi-bridge v0.5.0（提交 9adacdd）做到了第一条。提交 da7ab3a 让 `call` 触发 `tool_call`、`tool_result` 等钩子，`run` 按 bash 工具触发同样的钩子，扩展可以阻止调用、改参数和结果。
  - 其余三条还没有：`packages/cli/src/bridge.ts` 里任务仍放在内存的 Map 里，`shutdown()` 仍会结束全部任务；没有任务结束的通知；连接器地址仍是 `/<密钥>/mcp`。
  - 结论不变。
- **2026-11-22 复查**：pi-bridge v0.11.0（提交 df822c7）。
  - 第 4 条做到了：有 OAuth，每台电脑一把独立密钥。
  - 另外新增：R2 临时文件链接（50 MB，24 小时）、多用户、委派给本机 Pi 的模型。
  - 第 2、3 条还没有：任务在内存 Map 里，`shutdown()` 全部结束；没有任务结束的通知，只能每 25 秒 `wait`。
  - 这次也比了「技能获取」：机制和 winhand 一样，都是先列名字和描述，再读 SKILL.md；Pi 不读 Claude 和 Codex 的插件目录。所以没有迁移，而是在 winhand 里补齐来源。见 [skill-sources-map-and-search](../feature/2026-11-23-skill-sources-map-and-search.md)。
  - 结论不变。

## Verification

- 依据的代码，在 pi-bridge add85db 里：
  - packages/cli/src/bridge.ts：`call` 不经钩子；`run` 用 createLocalBashOperations；`shutdown()` 结束所有任务；任务存在内存的 Map 里；
  - packages/cli/src/output.ts：按 utf8 解码；
  - packages/server/src/protocol.ts：WAIT_MS = 25_000；
  - packages/server/src/index.ts：`/<密钥>/mcp`、`/<密钥>/connect`；
  - packages/server/src/mcp.ts：注册了 6 个工具。
- 依据的文档：pi 1.5.2 的 docs/extensions.md「Overriding Built-in Tools」，docs/shell-aliases.md（Windows 上先找 Git Bash）。
- 对照的 winhand 实测：`codestable/epics/002-o-第二次复盘五轮强化/issues/002-x-第2轮任务结束叫醒会话.md` 和 `codestable/epics/002-o-第二次复盘五轮强化/issues/004-x-第4轮多会话与离线.md` 的「实测」一节。
