# Agent Note: 技能找全、摆少、要用时搜：来源表、能力地图和检索

Status: implemented

## Problem

2026-11-22 主人提出：skills 的获取是 winhand 的硬伤；朋友的 pi-bridge 借 Pi 框架，全局和项目里的技能都能检测到。主人要一个优雅的办法放进 winhand，而且不想让 AI 每次都看到八九十个平级的技能。

当时的状况（本机实测）：

- 项目层只扫 git 根下的 `.agents/skills` 和 `.claude/skills`，而且只看一级。没有逐级往上找，不认 `.codex/skills`、`.pi/skills`。
  - md2visio 的 `.codex/skills/openspec-flow`、Trellis 的 9 个 `.pi` 技能，`context(path)` 都列不出来。
- 不读插件的启用清单。已启用的 Claude 插件技能 31 个、Codex 插件技能 55 个，都看不到。
- `context()` 和云端开场 hook 把用户层的 81 个技能全部平铺，带描述，约 9–20 KB。
- `skill(name)` 不带 path 时只找用户层。另有三处 bug：
  - `skill(repo, path)` 丢掉子路径；
  - `skill(path=子目录)` 把起点压成了项目根；
  - `brief` 只留名字。

讨论在 parley 的 t18、t21、t22 三个线程里进行，参与者是主持人、astra（gpt-6.2-astra）和 agy-neon（gemini-4-neon-high）。公开资料用 smart-search 检索，摘要在 `C:\Users\dxt98\.winhand\scratch\skills-research\digest.md`。

## Decision

技能分三步处理：找全、摆少、要用时搜。代码在 `agent/src/winhand/skills.py`，`context()`、`skill`、云端开场 hook 和 `winhand skills` 都调用它。

### 找全：一张来源表，按各家自己的读法

| 层 | 位置 | 遍历 |
|---|---|---|
| 项目 | 从 start 到项目根，每一级的 `.agents/skills`、`.codex/skills` | 递归收集每一个 SKILL.md，深度 6，与 Codex 0.171.0 相同 |
| 项目 | 同一条链上的 `.claude/skills` | 只看一级，与 Claude Code 相同 |
| 项目 | 只看 start 的 `.pi/skills` | 遇到 SKILL.md 就停，与 Pi 1.6.0 相同 |
| 用户 | `~/.agents/skills`、`$CLAUDE_CONFIG_DIR/skills`、`$CODEX_HOME/skills`、`~/.pi/agent/skills`；配置了 `[context] skills` 时改用配置的目录 | 同上，按目录所属的工具 |
| 插件 | Claude：`installed_plugins.json`，加上 user → project → local 三层 `enabledPlugins`，后面的覆盖前面的 | `skills/`，加上 `plugin.json` / `marketplace.json` 声明的入口 |
| 插件 | Codex：`config.toml` 里 `enabled = true` 的；带 `.codex-remote-plugin-install.json` 且没被显式关掉的 | 版本选 `local`，没有就选最大的；只用声明的 skills 目录 |

几条约束：

- 没有 `.git` 时只看 start 本身，不像 Pi 那样一路爬到盘符根。
- 以点开头的目录和 `synced` 一律跳过。
- 一次调用最多走 4000 个目录、花 2 秒 CPU 时间（另有 10 秒总时间上限，见「后续修订」）；超了就标出没走完。
- 不起任何外部命令，不联网。t18 实测过两条在线路径：问 Codex app-server 要 1.2–1.4 秒并且要联网；`codex plugin list` 超过 20 秒。

### 名字：id 说明它在哪

- id 是「层:相对锚点的路径」：
  - `project:.codex/skills/openspec-flow`
  - `user:~/.codex/skills/officecli/officecli-docx`
  - `plugin:figma@claude-plugins-official/figma-use`
- 同一个真实文件只出一条，`also` 里列出其他入口。
- 只给名字时，按这个顺序找：离 start 近的项目层、用户层、插件层。插件技能的名字写成 `figma:figma-use`。
- 项目的 id 不带 path 时直接报错，不猜。

### 信任与宿主开关

- `[context] project_skills` 有三个值：
  - `warn`（默认）：照常列出，标 `trusted: false`；
  - `trusted`：只列受信项目的技能；
  - `off`：不列项目技能。
- 判断顺序：
  1. 先看 Codex 的 `trust_level`。显式写了 untrusted 时，任何模式下都排除，`trusted_roots` 也放不开。
  2. 再看 `[context] trusted_roots`。
  3. 最后看 Claude Code 是否接受过工作区信任。
- 未受信项目的技能不能顶替同名的用户技能，但能按 id 指定加载。加载结果的 note 写明「这是仓库作者的指示」。
- 宿主的开关不自动继承成隐藏，因为那是各家给自己的设置。下面两种情况只在技能上标 `host_off`：这个技能不进地图的例子，检索时分数打五折。
  - Claude 的 `skillOverrides` 是 off 或 user-invocable-only；
  - Codex 的 `[[skills.config]]` 写了 `enabled = false`。

### 摆少：一张不随路径变的地图

- 地图内容：
  - `pinned_skills`，带描述；
  - 8 个双语领域，每个带个数和最多 4 个例子；
  - 带专属技能的项目：Codex 和 Claude Code 记录的项目里有项目技能的，最多列 8 个，缓存 10 分钟；
  - 一句行动契约。
- 同一份数据从三个出口给，各自裁剪：
  - MCP instructions 只有一句，没有钉住的名字，因为那里 4096 字符的空间要留给密钥名。
  - 云端开场 hook 的技能段给检出的项目技能，再加文字版地图，约 2–3 KB，替换原来约 9 KB 的平铺。
  - `context()` 给 `skill_map`。
- `context(path)` 的 `skills` 只给这个项目自己的技能：前 10 个带描述，其余进 `skills_more`。`skills_note` 写出带真实 path 的调用。
- 领域靠关键词打分，取最高的一两个，主人可以在 `[context] skill_areas` 里按 id 或名字改正。astra 在 t22 实测过会分错：`template-creator` 被分进文档，`eide-builder` 被分进代码。所以地图只用来提醒，不用来决定该用哪个技能。
- 常驻定为 smart-search-cli、officecli、gh-github-cli、tool-routing、windows-disk-cleanup，写在本机 `config.toml`。依据是 2026-11-23 的使用统计（见 issue 005），以及主人点名常用的几个。使用次数不自动改地图。

### 要用时搜：skill(query=…)

- query 是字符串，或 1–4 个中英文词。
- 一张内置双语别名表，两个方向都能扩：烧录 → flash firmware，disk → 磁盘。
- 打分权重：名字最高，其次描述、id、领域；命中的词越多，分越高。
- 范围：path 所在的项目、用户层、插件层，再加地图上其他项目的项目技能。
- 当前项目最多占一半名额，剩下的留给全局得分最高的。
- path 解析不出来时退回全机检索，并给出 `scope_warning`。
- 每条结果带 `load`，即 `name` 和项目的 `path`。别的项目的技能标 `cross_project`。
- 只给 `area` 时，列出那个领域里的技能。

### 报告

`winhand skills` 给主人看这些：各来源的个数、领域、没归到领域的技能、描述不到 40 字的技能、被宿主关掉的技能，以及 winhand 日志里每个技能被 `skill()` 调用的天数。报告不改任何东西。

## Alternatives considered

- **整体换成 pi-bridge，或在 winhand 里接 Pi SDK 的 resourceLoader。**
  - 最强的理由：Pi 的发现逻辑现成、成熟，还能装 Pi 包。
  - 否掉的原因：
    - pi-bridge 的技能机制和 winhand 一样，都是先列名字和描述，再读 SKILL.md。Pi 也不读 Claude 和 Codex 的插件目录，这台电脑上也没装 Pi。
    - 接 SDK 要起 Node 子进程。而 `reload()` 碰到缺的包会自己跑 `npm install` 或 `git clone`，这会破坏 `context()` 纯本地、秒回的性质。
    - pi-bridge df822c7 也没有 Pi 的信任门控：它没传 settingsManager，Pi 默认按 `projectTrusted = true` 处理。
  - 迁移本身的取舍见 [stay-on-winhand](../architecture/2026-11-17-stay-on-winhand-not-pi-bridge.md)。
- **继续平铺全部技能，只把预算调大。**
  - 最强的理由：AI 一眼能看全，不会漏。
  - 否掉的原因：主人明确不要；150 个平级的技能，正是「乱」的来源。各家都给第一层设了预算：Claude 是上下文的 1%，Codex 是 2%。
- **每次 run、fs_read 都顺带附一行「这个项目的技能」。**
  - 最强的理由：和 Claude Code「读到子目录的文件才加载那里的嵌套技能」一致。
  - 否掉的原因：
    - 无会话的客户端判断不了「第一次」，一次排错 30 次调用就会贴 30 遍。
    - AI 读文件之前往往已经在规划了，附在读文件的结果上太晚。
  - 现有工具说明本来就要求进文件夹前先调 `context(path)`，这就够当绊线。
- **按使用次数自动挑选常驻技能。**
  - 最强的理由：零配置，常用的自动浮上来。
  - 否掉的原因：排序会漂移，击穿缓存，还会出现马太效应（agy-neon 在 t22 提出）。使用次数只进报告。
- **自动继承宿主开关，在 winhand 里也隐藏。**
  - 最强的理由：主人在 Claude 里关掉的，多半就是不想让 Claude 看见。
  - 否掉的原因：
    - 隐藏以后，AI 连 `host_override` 这个标注都看不到。
    - 两个宿主一开一关时，默认值无从定义（agy-neon 在 t22 提出）。
- **按 Codex app-server 的实时结果判断插件是否启用。**
  - 最强的理由：和 Codex 自己的判断 100% 一致。
  - 否掉的原因：要联网，要 1.2 秒以上，断网时会丢掉远端插件。本地标记规则在本机命中了全部 55 个技能。风险是这个标记没有文档，记在 Consequences 里。
- **skill() 不带 path 时，按最近一次 context(path) 的项目兜底。**
  - 最强的理由：模型常常忘记带 path。
  - 否掉的原因：新版无会话的客户端没有 `Mcp-Session-Id`（`history.py:48-52`），所以分不清是谁在调用，并发时会串到别人的项目；同一个仓库里起点不同，结果也不同（astra 在 t21 提出）。现在的做法是：`skills_note` 写出带真实 path 的调用，项目的 id 漏了 path 就报错。
- **默认 strict：未受信项目只列名字，读正文要先授权。**
  - 最强的理由：第三方仓库的技能可能指使 AI 跑脚本。
  - 否掉的原因：
    - winhand 给项目规矩（AGENTS.md、CLAUDE.md）时本来就不看信任，技能和规矩都是文字说明，标准应该一致；执行还有守卫兜底。
    - md2visio 是主人自己的项目，却不在任何信任列表里，strict 会让第一个验收样本失败。
  - 想要严格时可以设 `project_skills = "trusted"`。

## Consequences

- **收益**：
  - 本机能找到的技能：用户层 66 个，Claude 插件 31 个，Codex 插件 55 个，再加上各个项目的项目技能。
  - 开场和 `context()` 的技能部分只剩几 KB；不传 path 时，`context()` 也不再平铺用户技能。
  - AI 用中文提问，也能搜到英文描述的技能。
  - 同名技能和串仓都有 id 和报错兜底。
- **代价**：
  - 不在地图上的技能，要靠 AI 想到去搜。行动契约写在三个出口里，但模型仍可能偷懒，需要按入口实测。
  - 云端 Claude Code、claude.ai 网页、本机 stdio 能看到的内容不同：本机 stdio 不显示 context 提醒；多仓的云端会话不读仓库的 hook。
  - Codex 远端插件靠一个没有文档的标记文件。Codex 升级后，要用 app-server 的 `skills/list` 核对一次；本机 t18 的基准是 55 项。
  - 领域分类靠关键词，会分错，要靠主人纠正。
  - MCP instructions 里的技能说明压成了一句，没有钉住的名字。为此删掉了 GUIDE 里两句话：「Waiting then costs no turns.」和「garbled text, a failing shim」的举例。
- **推后**：`--add-dir`；受管、系统（`$CODEX_HOME/skills/.system`）和 claude.ai 同步的技能；Pi 包；按调用方过滤；项目 `.codex/config.toml` 里的插件开关。
- **后续修订（2026-11-25）**：读取预算的时间改按调用线程自己的 CPU 时间算，总时间只留 10 秒上限兜底。
  - 起因：本机实测，同时发出 4 个 `context()`，每个要 3 秒（单独跑 0.5 秒）。原来的 2 秒是总时间，在读到 Codex 插件之前就用完了：地图只数到 109 条，少了 Codex 插件的 56 条，还只留一句「太大没读全」。
  - 否掉的做法：
    - 只把总时间上限放宽：并发再多一点还会超，结果仍取决于同时有几个调用。
    - 缓存读到的技能清单：能让并发的调用更快，但要管失效，而且这次要修的是结果不对。推后。
  - 读不完时，`context()` 的 `skills_note` 和 `skill(query=…)` 的 `incomplete` 现在都会说「可能不全」；以前检索里一个字都没有。
- **验证**：
  - `agent/tests/test_skills.py` 覆盖：项目链、四种目录、递归、id、去重、信任三种模式、Claude 和 Codex 插件、宿主开关、地图、中文检索、配额、跨项目加载、子目录、报告、读取预算。
  - 旧测试改为新的语义，全套测试通过。
  - 本机在 md2visio、Trellis、dev-board 上的实测，记在 `codestable/issues/005-x-技能分层发现与按需展示.md` 的执行记录里。
