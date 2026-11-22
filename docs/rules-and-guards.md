# 本机规则与守卫

> 从原 README 挪来。部署和接入见 [README](../README.md)，其他文档：[使用指南](guide.md) · [规矩与守卫](rules-and-guards.md) · [本机的其他 MCP 服务](local-mcp.md) · [中转](relay.md) · [开发](development.md)。

任何客户端连上 winhand，都能读到这台电脑写好的规矩和技能，并受这台电脑上的规则约束。这一层与客户端无关，只用标准 MCP；没写规则时什么都不拦。

## context：规矩和技能

`context(path)` 按 Codex 和 Claude Code 各自的读法返回规矩，和它们在这台电脑上实际读到的一致：

- **Codex**：
  - 全局 `~/.codex/AGENTS.override.md`，没有时读 `~/.codex/AGENTS.md`（设了 `CODEX_HOME` 时，从那里读）；
  - 项目根（最近的 `.git` 所在目录）到 `path` 沿途每一级，各取一份：`AGENTS.override.md` 优先，没有时取 `AGENTS.md`。
- **Claude Code**：
  - 管理员策略文件，以及 `~/.claude/CLAUDE.md` 和 `~/.claude/rules/`；
  - 从磁盘根目录到 `path` 沿途每一级的 `CLAUDE.md`、`.claude/CLAUDE.md`、`CLAUDE.local.md`；
  - 项目的 `.claude/rules/`：写了 `paths` 的规则只在路径匹配时生效，不生效的列在 `rules_not_applied` 里；
  - 文件里的 `@路径` 引用会展开，最多 4 跳。

同一个文件只返回一次。比如 CLAUDE.md 里写了 `@AGENTS.md`，AGENTS.md 只出现一次。每份规则都标出来自哪种格式（`format`）。

除了规矩，还返回：
- **技能**：只给这个项目自己的技能（10 个以内带描述，其余只给 id 和名字），以及一张技能地图（`skill_map`），见下面的「技能：找全、摆少、要用时搜」；
- 对该目录生效的守卫规则；
- 本机 MCP 服务。

每次回答都带一个 `digest`。两种省字数的用法：

- `brief=true`：技能只给 id 和名字，地图不带例子；规矩和守卫照旧给全。
- `since=<上次的 digest>`：内容没变时只回 `unchanged`；变了时按文件给：没变的文件只给路径和指纹（`unchanged: true`），只在末尾加了内容的文件只给新加的行（`appended_after_line`）。只在上次的回答还在对话里时用；压缩上下文以后不要带，回答里也这样提醒。

文件都是现读的，改完规矩，下一次调用就生效。

输出经过脱敏，按脱敏后的长度控制在约 6 万字符以内。规矩太长时，给 `#` 标题目录（最多 60 条）加文件的最后一段，`truncated` 写明给了哪几行、前面的用哪条 `fs_read` 读。以前截掉的是后面，正好是最新追加的那几段。

## 技能：找全、摆少、要用时搜

技能多了以后，全部平铺给模型反而难用。winhand 把三件事分开：

- **找全**：一张来源表，按每家工具自己的读法读。
  - **项目**：从当前目录逐级往上到项目根（最近的 `.git`）。
    - 每一级看 `.agents/skills`、`.claude/skills`、`.codex/skills`；
    - `.pi/skills` 只看当前目录；
    - 没有 `.git` 时只看当前目录本身。
  - **用户**：`~/.agents/skills`、Claude Code 的 `skills`（`CLAUDE_CONFIG_DIR`，默认 `~/.claude`）、Codex 的 `skills`（`CODEX_HOME`，默认 `~/.codex`）、`~/.pi/agent/skills`。设了 `[context] skills` 时，改为读设的那些。
  - **插件**：
    - Claude Code：`installed_plugins.json` 加上用户、项目、本地三层设置里 `enabledPlugins` 打开的插件，读它声明的技能目录，默认读 `skills/`；
    - Codex：`config.toml` 里 `enabled = true` 的插件，以及从主人账号装的插件（带 `.codex-remote-plugin-install.json`，且没有被显式关掉）。有多个版本时，优先 `local`，其次版本号最大的。
  - **遍历方式**：
    - Codex 式目录：往下 6 层，收集每一个 SKILL.md；
    - Claude Code 式目录：只看第一层；
    - Pi：碰到 SKILL.md 就停。
    - 点开头的目录和 `synced` 一律跳过；一次调用最多走 4000 个目录、2 秒。
- **命名**：每个技能有一个说明它在哪里的 id：

  - `project:.codex/skills/openspec-flow`
  - `user:~/.codex/skills/officecli/officecli-docx`
  - `plugin:figma@claude-plugins-official/figma-use`

  同一个文件经链接出现在两处时，只算一个，`also` 里列出另一个 id。只给名字时的查找顺序：离当前目录最近的项目、用户、插件。插件技能也可以写成 `figma:figma-use`。
- **摆少**：
  - **能力地图**：主人钉住的技能（`pinned_skills`），8 个中英双语领域的计数和例子，带专属技能的项目，再加一句「要用专门工具时先搜」。
  - 它出现在三处：`context()`、云端会话开场 hook 的技能段、MCP instructions（只有一句）。
  - `context(path)` 另外列出这个项目自己的技能。
- **要用时搜**：`skill(query=['烧录固件', 'flash firmware'])`。
  - 搜索范围：当前项目、用户目录、插件，以及 Codex 和 Claude Code 记录的其他项目里的技能。
  - 中英文都能搜：内置一张别名表（烧录↔flash 等）。
  - 当前项目的结果最多占一半，其余位置留给别处得分最高的。
  - 每个结果给出 `load`（`name`，项目技能还有 `path`），照着调用 `skill(name=..., path=...)` 即可。`skill(area='hardware')` 列出一个领域的技能。
  - 别的项目的技能带 `cross_project`；加载时会提醒，先核对路径，再运行它的脚本。

`skill(name)` 取 SKILL.md 正文、技能文件夹和其中的文件清单，SKILL.md 同样先过守卫的 `read` 规则；脚本用 `run` 在这台电脑上执行。项目技能要带 `path`，即它所在的目录；不带 `path` 的 `project:` id 会直接报错，不会去猜。

**信任**：项目技能来自仓库，刚克隆的仓库可能并不可信。

- `project_skills = "warn"`（默认）：照常列出，没有受信的项目标上 `trusted: false`，加载时附一句提醒。这样的技能不能顶替同名的用户技能，但能用 id 指定加载。
- `"trusted"`：只列受信项目的技能。
- `"off"`：不列项目技能。

受信的依据，依次是：

1. Codex 的 `trust_level`。一旦显式写了 `untrusted`，任何模式下都不列，`trusted_roots` 也放不开；
2. `[context] trusted_roots`；
3. Claude Code 接受过工作区信任。

**宿主开关**：Claude Code 的 `skillOverrides` 设为 `off` 或 `user-invocable-only`、Codex 的 `[[skills.config]] enabled = false` 时，winhand 不隐藏这个技能，因为那是各家给自己的设置。这样的技能标上 `host_off`，不放进地图的例子，搜索时排在后面。

**主人的报告**：`winhand skills` 列出这些内容：
- 各来源的个数；
- 每个技能落在哪个领域；
- 没归到任何领域的、描述太短的、被宿主关掉的技能；
- winhand 日志里每个技能被 `skill()` 调用的天数。

领域分错了，就在 `[context] skill_areas` 里按 id 或名字改正。

**只知道仓库名时**（比如云端会话只知道 `owner/name`），用 `context(repo="owner/name")`。它在以下位置找这个仓库在本机的检出目录：
- Codex 记录的项目；
- Claude Code 记录的项目；
- `repo_roots` 里列出的文件夹。

再给 `path="src"` 时，`path` 相对于检出目录。

**安全**：`context` 读的每个文件，都先过守卫的 `read` 规则，被拒绝的不读。项目里的规矩引用项目外的文件（例如 `@~/.ssh/id_rsa`）时，只有两种情况才会展开：
- Claude Code 已为这个项目批准过外部引用；
- 设置了 `external_imports = true`。

项目里指向项目外的符号链接（例如 `CLAUDE.md` 链接到别处）也一样。这样，别人的仓库没法借规矩文件读走你的私密文件。

instructions 会提醒模型：开始在某个目录工作前，以及上下文被压缩后，调用 `context`。经中转和 `winhand http` 连接的客户端会看到这句提醒；本机 stdio 客户端默认不显示，因为它们自己会读规则。读哪些文件可以在 `config.toml` 里改：

```toml
[context]
formats = ['codex', 'claude-code']   # 按谁的读法读规矩；['claude-code'] 时与 Claude Code 默认一致
rules = ['D:\notes\agents.md']       # 写了就代替两种格式的全局规则文件；[] = 不读
skills = ['~/.agents/skills', '~/.claude/skills', '~/.codex/skills', '~/.pi/agent/skills']  # 不写时就是这四个
pinned_skills = ['smart-search-cli', 'officecli']   # 地图里常驻、带描述的技能（id 或名字）
project_skills = 'warn'               # 'warn' | 'trusted' | 'off'，见上文「信任」
trusted_roots = ['D:\Dev\20_个人项目'] # 这些文件夹下的项目算受信（Codex 显式 untrusted 的除外）
skill_areas = { 'template-creator' = ['agents'] }  # 改正领域：hardware docs visual code agents search project ops
repo_roots = ['D:\code']              # context(repo=...) 还会在这些文件夹和它们的子文件夹里找
external_imports = false             # true：项目规矩引用项目外文件时，不用等 Claude Code 批准
stdio_hint = false                   # true：本机 stdio 客户端也显示 context 提醒
```

Claude Code 只保留 MCP instructions 的前 4096 个字符，所以 instructions 只放要点，完整指南由 `help()` 返回。

## 守卫：动作前先看规则

会读写这台电脑的调用，执行前都会被描述成一个“动作”，按规则裁决。经 `call()` 转发的调用也一样。

| 动作 | 来自 |
|---|---|
| `command` | `run`（含 `stdin` 送进去的内容）、`job_start`、`session_start`、`remote_run`（含参数，`host` 为远程主机） |
| `input` | shell 类会话（pwsh、powershell、cmd、bash、sh、zsh、ssh，或没指定程序的默认 shell）里真正被执行的每一行，见下文。gdb、串口等会话不检查 |
| `write` | `fs_write`、`fs_write_bytes`、`fs_upload_url`、`secret_prompt` 的 `save_to`、`fs_op` 的 copy/move 目标（目标是已有文件夹时，按放进去的那个文件判断）和 mkdir |
| `edit` | `fs_edit`、`fs_apply_patch`（补丁涉及的每个文件）。编辑需要看到内容，所以它们同时算对这些文件的 `read`；补丁文件本身也算 `read` |
| `delete` / `move` | `fs_op`；delete 带 `recursive=true`（整个文件夹）时，还按 `command` 判断，看成 `Remove-Item -Recurse -LiteralPath '<路径>'`，所以禁止批量删除的命令规则也管得到它 |
| `read` | `fs_read`、`fs_send`、`fs_download_url`、`fs_search`、`fs_op` 的 copy 和 move 来源，以及上面 `edit` 的目标 |

`fs_search` 搜索文件夹时，读取规则拒绝或要求确认的文件不会出现在结果里，结果中的 `guard_hidden` 写明略去了几个文件。

**shell 会话里的一行**：winhand 记住每个 shell 会话里“已输入、还没执行”的内容。之后换行、Enter 键、`submit`、`session_stop`（会输入退出命令并回车）或 `session_prompt_user`（会输入口令并回车）让这一行执行时，守卫判断的是整行。
- **续行**：可能续行的行（以 `\`、反引号、`^`、`|` 等结尾，或引号、括号没闭合），既单独判断，也和后面的行连起来判断，连起来时分别试“直接相连”和“加一个空格”两种拼法；
- **删除和清行**：Backspace 删掉最后一个字符；Ctrl+C 清掉这一行（仅限终端会话，管道会话里它只是一个字符）；
- **无法推断的行**：光标移动、历史记录和历史展开（bash 的 `!!`、`^a^b`）、Tab 补全会让最终执行的内容无法从输入推断。工具出错、按键名写错、口令是否送达不确定、同一会话的输入互相重叠时，winhand 也不再知道已输入了什么。这样的行，任何带 `match` 的 `input` 规则都视为命中，拒绝信息会提示先用 Ctrl+C 清行；
- **排队**：存在 `input` 规则时，同一 shell 会话的输入按顺序判断和发送。没有 `input` 规则的会话、单独的 Ctrl+C 和 `session_stop(force=true)` 不排队。

规则写在 `~/.winhand/guards.toml`（全局），或项目目录下的 `.winhand/guards.toml`（只管该目录及子目录）。winhand 不自带任何规则，下面是示例：

```toml
[[guard]]
on = ["command", "input"]
match = '(?i)(\brm\b[^|;&\n]*\s-\w*r|\b(remove-item|ri|rm|rmdir|rd|del|erase)\b[^|;&\n]*\s-r(e(c(u(r(s(e)?)?)?)?)?)?\b|\b(rd|rmdir|del|erase)\b[^|;&\n]*\s/s\b)'
decision = "deny"
reason = "禁止批量删除：一次只删一个明确路径的文件，或请用户手动删除"

[[guard]]
on = "read"
path = ["**/.env", "**/.env.*", "**/secrets/**"]
decision = "deny"

[[guard]]
on = "delete"
decision = "ask"          # 在电脑上弹窗确认
reason = "删除前先问我"
timeout_s = 40

[[guard]]
on = "edit"
path = ["**/*.c", "**/*.h"]
decision = "note"         # 照常执行，结果里附上这句提醒
reason = "改完记得跑单元测试"

[[guard]]
on = ["write", "edit", "delete", "move"]
path = ["**/.winhand/guards.toml"]
decision = "ask"          # 规则文件本身也值得保护
reason = "要改守卫规则"
```

- **字段**：
  - `on`：动作类型：`command`、`input`、`write`、`edit`、`delete`、`move`、`read`、`mcp`。`mcp` 是经 `mcp_call` 调用本机 MCP 服务的工具，`match` 看到的文字是 `<服务>.<工具> <参数 JSON>`，例如 `match = '^pyocd-debug\.(flash|erase) '`；
  - `match`：在命令行或输入的这一行里搜索的正则；
  - `path`：glob 列表，支持 `**`，Windows 上不区分大小写。项目文件里的相对模式以该项目目录为基准；全局文件里的相对模式匹配任意位置（`*.pem` 等于 `**/*.pem`）；
  - `decision`：`deny`、`ask` 或 `note`；
  - `reason`：拒绝、确认或提醒时显示的说明；
  - `timeout_s`：ask 等待确认的秒数，默认 40，最多 50。客户端等一个工具调用大约 60 秒就放弃，再晚的“是”会让 AI 以为失败的操作其实执行了。
- **裁决**：deny 优先于 ask，ask 优先于 note。没有 allow，所以项目规则只能增加限制，不能放宽全局规则。
- **拒绝**：工具不执行，以工具错误返回，第一行就写明规则文件、第几条和理由。这次调用仍出现在 `history` 和桌面端时间线里。
- **引号里的搜索模式不算命令**：判断 `command`、`input` 规则前，`Select-String -Pattern '…'`、`rg`/`grep`/`findstr` 的模式和 `-e` 后的引号内容换成 `x`，所以 `-Pattern '…|rm'` 这样的搜索不会被当成删除。双引号里带 `$(` 或反引号的、`rg --pre` 之后的照旧判断。
- **内置保护登录信息**（`winhand (built in)` 第 7 条）：colab、gcloud、git、docker、gh 的登录文件，`~/.ssh` 的私钥，Chrome、Edge、Firefox 的用户资料和 1Password 的本地目录，读、写、改、删、移都拒；规矩文件引用它们也不读。
- **请人确认**：在电脑上弹出置顶的确认框，写明工具、动作和理由。默认按钮是“否”，正在别处打字时误按回车也不会放行。没人应答、点“否”或弹不出窗口，都不执行。
- **规则文件写错**：解析失败、字段不认识、正则写错时，这个文件管辖的动作一律拒绝，错误里写明文件和位置；改好后下一次调用即恢复。
- **文件夹**：`secrets/**` 这样的模式同时覆盖文件夹本身，复制或移动整个文件夹也会被拦。
- **路径**：按工具自己的方式解析路径（`fs_*` 不展开环境变量，一次性链接工具会展开），并同时按真实路径判断：符号链接、目录联接、8.3 短名、`\\?\` 前缀、`::$DATA` 这类写法不会绕过路径规则。

参数格式不对的调用不经守卫判断，直接由工具自己报错，什么也不会执行。

## 本机的 Codex、Claude Code hooks

在本机用 Codex 或 Claude Code 时会运行的 PreToolUse hooks，经 winhand 的调用也会运行。例如项目里 `.codex/hooks.json` 中“这个阶段不许改 src/”的守卫，云端会话通过 winhand 改文件时同样生效。

- **只运行这两个工具自己会运行的**：
  - Codex：在 Codex 里审核过（`/hooks`，`trusted_hash` 与现在的内容一致）的 hook，项目里的还要求项目被信任；
  - Claude Code：项目已接受工作区信任时才运行；
  - 管理员下发的 hooks 总是运行。

  没信任、改过、被关掉的不运行；Codex 自己读不了的 hooks 文件、Codex 的 `[features] hooks = false` 也一样。`context(path)` 的 `hooks` 一项列出每个 hook 的状态和原因，以及读不了的文件。
- **hook 看到什么**：动作按那个工具自己的样子交给它。
  - 命令、shell 会话里提交的一行，都是 `Bash`；
  - 写文件：Claude Code 是 `Write`、`Edit`，Codex 是 `apply_patch`；
  - 读文件：Claude Code 是 `Read`。Codex 没有读文件工具，读文件不交给 Codex 的 hook。
- **结论**：
  - 退出码 2 或 deny：拒绝；
  - Claude Code 的 ask：弹窗请你确认；
  - `additionalContext`：作为附注给模型；
  - hook 想改写这次调用（`updatedInput`）：拒绝，并告诉模型按 hook 的意思改了再发；
  - hook 超时、出错：照常执行并附注，与两个工具的做法一致。
- hook 的结论和 `guards.toml` 一起裁决，只会更严：hook 说 allow 也放不过 `guards.toml` 拒绝的动作。
- **Windows 上的退出码**：Codex 用 `pwsh -NoProfile -Command <命令>` 运行 hook，PowerShell 会把程序的退出码 2 变成 1，Codex 就当 hook 出错而放行，靠退出码 2 拒绝的 hook 在 Codex 里其实拦不住。winhand 也用 PowerShell 运行，但会把程序的退出码传出来，让这类拒绝照常生效。这一点比 Codex 严格，符合 hook 的本意。想让 hook 在 Codex 里也生效，可在命令末尾加 `; exit $LASTEXITCODE`，或改为输出 JSON 的 deny。

`guards.toml` 也可以把判断交给一个命令，协议与 Claude Code 的 PreToolUse hook 相同：

```toml
[[guard]]
on = ["command", "input"]
command = 'python C:\tools\check_command.py'   # 读 stdin 的 JSON，退出码 2 或输出 deny 即拒绝
```

`command` 只能写在全局的 `~/.winhand/guards.toml` 里。项目里的文件写了，按“写错了”处理：仓库里的文件不能让 winhand 执行命令。

在 `config.toml` 里可以关掉或调整：

```toml
[hooks]
sources = ['codex', 'claude-code']   # [] = 这两个工具的 hook 一个都不运行（guards.toml 的 command 规则照常）
timeout_s = 30                       # 每个 hook 最多等多久
```

守卫按工具的参数判断，**不是沙箱**。它适合拦住善意的 AI 犯错，挡不住刻意绕路：`run` 里的程序自己去读写什么（例如 `run("type .env")`）、正则没覆盖到的写法、`proc_kill` 和桌面工具，它都管不到。真正需要隔离时，用系统权限或单独的账户。

## 云端会话开场就带上本机规矩

云端 Claude Code 会话本来要靠模型自己想起来调用 `context()`。在仓库里加 SessionStart hook，会话一开始（以及 `/clear`、恢复、压缩上下文之后）就会自动带上这台电脑上的三段内容，每段是一个单独的 hook，各自不超过 9,000 字符（Claude Code 对每个 hook 的上限是 10,000）：

- **规矩**：全局规矩（与 `context()` 的读法相同），以及这个仓库在本机的检出目录里、**没提交到仓库**的规则文件，例如 `CLAUDE.local.md`。已提交的文件云端仓库里有，不重复。放不下的部分列出路径，提示用 `context()` 读；
- **技能**：这个仓库在本机检出里的项目技能（带 id 和描述），以及技能地图：钉住的技能、各领域的计数和例子、带专属技能的项目，还有一句「要用专门工具时先 `skill(query=…)` 搜」。其余技能不平铺，搜得到。总共几 KB，以前是把全部技能平铺成约 9 KB；
- **本机 MCP 工具**：每个启用的本机 MCP 服务的说明和工具清单，每个工具一行说明。工具多、放不下时，所有说明均匀缩短到同一长度（最短 40 字符），每个工具都留着说明；仍放不下时，最后几个服务只列工具名。内容来自电脑上的工具清单缓存，开场**不会启动任何服务**；从没列出过工具的服务只写“尚未列出”。调用用 `mcp_call`，完整参数用 `mcp_tools(server)`。

电脑不在线时，每段只有一句说明。0.4.7 生成的单个 hook 仍然可用（规矩加技能名单）。升级时先把电脑上的 winhand 更新到 0.4.8，再重新执行 `winhand bridge init --write` 换成三段：电脑还是 0.4.7 时，三段 hook 会各自拿到一份完整的规矩。

设置三步：

1. 生成 hook 令牌，设到中转上：

   ```
   winhand bridge token                 # 生成令牌，只显示一次
   npx wrangler secret put HOOK_TOKEN   # 在中转目录执行，粘贴令牌
   npx wrangler deploy
   ```

2. 把 hook 放进仓库：`winhand bridge init --write <仓库目录>`，然后提交并推送。它会合并进 `.claude/settings.json`，保留已有内容；只想看片段时去掉 `--write`。所有仓库用同一份配置：仓库由云端的 `git remote get-url origin` 决定，本机按仓库名找到检出目录（同 `context(repo=…)`）。
3. 在云端环境里放令牌（会话标题栏的云环境菜单 → Edit），`winhand bridge init` 会打印具体做法：
   - 推荐用 Network secrets（Pro/Max 才有）：网站填中转的域名，Path prefixes 留空（只填 `/hook/` 会挡住一次性文件链接），自定义请求头 `X-Winhand-Hook-Token`，前缀留空。令牌由代理在请求离开容器后加上，不进容器；
   - 没有 Network secrets 时：Network access 选 Custom，在 Allowed domains 里加上中转的域名并勾上 Also include default list of common package managers，再设环境变量 `WINHAND_HOOK_TOKEN`。使用这个环境的人都能看到这个变量。

hook 令牌只能取开场上下文：中转只为 `/hook/session-start` 认它，不能调用任何工具，也打不开 `/mcp`。生成的配置里写着中转的域名，提交后看得到仓库的人都能看到它，建议只用于私有仓库；没有令牌的请求会被拒绝。内容与 `context()` 一样经过守卫的 `read` 规则和脱敏。本机的 Claude Code 不受影响：hook 只在 `CLAUDE_CODE_REMOTE=true`（云端）时运行，出任何错都安静退出，不打断会话。多仓库的云端会话不读仓库的 hooks，这个功能不生效。
