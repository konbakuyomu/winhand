# Outcome

任何 MCP 客户端连上 winhand 后，都能做到以下几点，而且这一层不依赖任何特定客户端，也不改变终端核心的行为：

- 读到这台电脑和项目里写好的规矩（AGENTS.md）和技能（SKILL.md）；
- 被这台电脑上写好的规则拦下危险操作，或在执行前请人确认；
- 在 Claude Code 里看到完整、不被截断的 winhand 使用指引。

对应设计文档 `docs/workspace-layer.md` 的 P0。

# Scope

- **mcp-guide**：把 MCP instructions 压到 4096 字符以内，加入指向 `context()` 和守卫的说明；完整指南改由 `help()` 返回。
- **action-guard**：
  - 把会读写这台机器的工具调用统一描述成“动作”；
  - 守卫中间件按 winhand 自有规则文件 `guards.toml`（全局和项目两级）给出放行、拒绝、请人确认或附注；
  - 覆盖经 `call()` 转发的调用，以及交互式 shell 会话里提交的整行（待 Q5）。
- **workspace-context**：
  - 新工具 `context(path)`，返回全局规矩、项目根到 path 沿途的 AGENTS.md、技能列表、生效的守卫规则摘要和本机 MCP 服务名；
  - 配置项 `[context]` 能被桌面端保存时保留。

## Source coverage

来源：`docs/workspace-layer.md`（d4cbe32，complete）。

| 来源条目与位置 | 读取状态 | 需要保留的内容 | Spec 位置 | 验收 ID | 覆盖状态 | 理由或替代关系 |
|---|---|---|---|---|---|---|
| S1：定位·通用 | complete | 只用标准 MCP，任何客户端可用 | 三个 spec 的 Purpose | A1、A10 | covered | P0 只用 tools 和 instructions |
| S2：定位·Claude Code 只是来源之一 | complete | 客户端配置由适配器读取 | — | — | non-goal | P1 `client-adapters` |
| S3：定位·终端能力扩展点 | complete | 交互式会话的输入也过守卫 | specs/action-guard 的 R9 | A8 | needs-clarification | 待 Q5 |
| S4：§1 动作表 | complete | 各工具对应的动作类型 | specs/action-guard 的 R1 | A2、A3、A7、A8 | covered | |
| S5：§1 四种结论 | complete | 放行、拒绝、请人确认、附注 | specs/action-guard 的 R4–R7 | A2、A5、A6 | covered | |
| S6：§1 中间件位置 | complete | 所有客户端、所有调用都经过，含 `call()` | specs/action-guard 的 R1、R8 | A7 | covered | |
| S7：§1 来源 1·winhand 自有规则 | complete | 全局和项目 `guards.toml`，按动作类型、路径、正则匹配 | specs/action-guard 的 R2、R3 | A2、A3、A4 | needs-clarification | 位置和格式待 Q1 |
| S8：§1 来源 1·“或交给一个命令去判断” | complete | 外部命令判断 | — | — | non-goal | 外部命令协议随 P1 的 hooks 适配一起做 |
| S9：§1 来源 2·Claude Code / Codex hooks | complete | hooks 翻译 | — | — | non-goal | P1 |
| S10：§1 来源 3·其他客户端 hooks | complete | 以后的适配器 | — | — | non-goal | P1 之后 |
| S11：§1 信任 | complete | 外部 hooks 只运行已信任的 | — | — | non-goal | P1。P0 的规则只能增加限制，不需要信任机制（见 Constraints） |
| S12：§1 终端特有 | complete | 只检查 shell 类会话的命令行，不检查密码和 gdb、串口输入 | specs/action-guard 的 R9 | A8 | needs-clarification | 待 Q5 |
| S13：§2 当前目录（目标路径向上找项目根） | complete | 项目根判定 | specs/workspace-context 的 R2 | A10 | covered | |
| S14：§2 当前目录（仓库名 → 本机检出目录） | complete | `repo` 映射 | — | — | non-goal | P1 |
| S15：§2 来源表·通用标准 | complete | AGENTS.md、SKILL.md | specs/workspace-context 的 R2、R4 | A10 | covered | |
| S16：§2 来源表·winhand 自有 | complete | `guards.toml` 的说明；`.winhand/profiles/` | specs/workspace-context 的 R5 | A10 | covered | 项目自带 profile 推迟到 P1（见 Non-goals） |
| S17：§2 来源表·Claude Code、Codex 行 | complete | 它们的规则语义和配置 | specs/workspace-context 的 R3、R4 | A10 | needs-clarification | P0 只把它们当普通文件和目录读，见 Q6、Q7；语义适配在 P1 |
| S18：§2 合并规则 | complete | CLAUDE.md 优先级、`@import` | — | — | non-goal | P1 |
| S19：§2 出口·工具 `context()` | complete | 主通道 | specs/workspace-context 的 R1 | A10 | covered | |
| S20：§2 出口·instructions | complete | 一句指引，4096 限制 | specs/mcp-guide 的 R1 | A1 | covered | |
| S21：§2 出口·resources、prompts | complete | | — | — | non-goal | P3 |
| S22：§2 脱敏和分页 | complete | `RedactOutput`、大小预算 | specs/workspace-context 的 R6 | A11 | covered | |
| S23：§2 本机客户端可关掉指引 | complete | stdio 客户端 | specs/mcp-guide 的 R3 | A14 | needs-clarification | 待 Q8 |
| S24：§3 生命周期桥 | complete | | — | — | non-goal | P2 |
| S25：对终端能力的意义 | complete | 安全底座；项目自带终端配置；新功能自动受益 | specs/action-guard 的 R1 | A2、A8 | covered | 项目自带 profile 推迟到 P1 |
| S26：各客户端能得到什么 | complete | | — | — | background | 说明性内容 |
| S27：依据 | complete | | — | — | background | 其中 4096 截断已由 S20 覆盖 |
| S28：实施顺序 P0 | complete | 截断修复、守卫、`guards.toml`、通用 context | 三个 spec | A1–A14 | covered | |
| S29：实施顺序 P1–P3 | complete | | — | — | non-goal | 后续 change |
| S30：待定·guards.toml 格式 | complete | | specs/action-guard 的 R2 | A2 | needs-clarification | Q1 |
| S31：待定·外部 hooks 默认开关 | complete | | — | — | non-goal | P1 |
| S32：待定·stdio 指引 | complete | | specs/mcp-guide 的 R3 | A14 | needs-clarification | Q8 |

# Non-goals

- Claude Code / Codex 适配：CLAUDE.md 优先级和 `@import`、`.claude/rules`、它们的 hooks 与信任机制、`context(repo=…)` 映射（P1）。
- 交给外部命令判断的守卫规则（P1，随 hooks 协议一起做）。
- 项目自带的 `.winhand/profiles/`（P1）。
- 生命周期桥、中转新入口（P2）。
- MCP resources / prompts 出口、跟随本机 MCP 配置、`mcp()` 元工具、GitHub 导出（P3）。
- 桌面类工具（`input`、`ui`、`clipboard`、`screenshot`、`window`）的守卫；桌面端的规则设置界面。
- 版本号和发布。

# Acceptance examples

验收项编号以本节为准；spec 中的每个 `Scenario:` 都标注了它对应的编号，不另外产生新的验收项。

- **A1** instructions 不超过 4096 字符，包含指向 `context()` 和守卫的说明；`help()` 返回完整指南，包括现在被截掉的桌面、`job_start`、`sys_info`、`call()`、本机 MCP 几段。
- **A2** 全局 `guards.toml` 有一条 `on = "command"` 的 deny 规则，`match` 能命中 `Remove-Item -Recurse` 和 `rm -rf`。这时调用 `run(command="Remove-Item -Recurse C:\\tmp\\x")`：命令不会执行，返回工具错误，写明规则文件、第几条和理由；`history()` 里能看到这次调用。
- **A3** 一条 `on = "read"`、`path = ["**/.env"]` 的 deny 规则，会拒绝对任意 `.env` 文件的 `fs_read`、`fs_send` 和 `fs_download_url`。
- **A4** 项目 `.winhand/guards.toml` 只对该目录及其子目录下的动作生效，对其他目录的同类动作不起作用。
- **A5** 一条 ask 规则命中时，电脑上弹出确认框：选“是”就照常执行；选“否”、超时或无法弹窗时，按 Q4 处理，并说明原因。
- **A6** 一条 note 规则命中时，动作照常执行，结果里带上这条附注。
- **A7** 通过 `call("fs_write", {...})` 转发的写入，与直接调用 `fs_write` 受同样的规则约束。
- **A8**（待 Q5）在 shell 类会话里提交一整行、且命中 `input` 规则时，这一行不会发给程序，会话不受影响；gdb、串口等非 shell 会话的输入不检查。
- **A9** 规则文件解析失败时，按 Q3 处理，结果里写明文件路径和解析错误。
- **A10** `context(path)` 返回：
  - 全局规矩；
  - 从项目根到 path 沿途的每份 AGENTS.md（路径和正文）；
  - 技能列表：名称、描述、SKILL.md 路径，同名时项目级优先；
  - 对该路径生效的守卫规则摘要；
  - 本机 MCP 服务名。

  不传 path 时只返回全局部分。
- **A11** `context()` 的输出经过 `[redact]` 脱敏；超出大小预算时截断，并写明被截断的文件和继续读的方法（`fs_read`）。
- **A12** `config.toml` 里的 `[context]` 设置在 `config.save()` 之后仍然保留，桌面端保存设置不会把它丢掉。
- **A13** 没有任何规则文件、也没有 `[context]` 配置时，现有工具的行为不变：原有测试全部通过，`ruff check` 和 `ruff format --check` 通过。
- **A14**（待 Q8）本机 stdio 入口默认不显示规则指引，配置打开后显示；远程入口（http、中转）始终显示。

# Constraints and invariants

- 终端核心的行为不变；新增能力全部是可选的，没有规则文件时不产生任何拦截。
- 规则只有 deny、ask、note 三种结论，没有 allow。项目规则只能增加限制，不能放宽全局规则。所以读取项目规则不需要信任机制。
- 守卫只看工具调用的参数，不读取文件内容，也不把规则文件以外的内容交给模型。
- 拒绝时工具不执行，不产生任何副作用。
- 守卫的拒绝和附注同样经过 `[redact]` 脱敏，并记录到 `history()` 和桌面端时间线。
- Windows 上路径匹配不区分大小写，`/` 与 `\` 等价。
- Linux 和 Windows CI 都要通过。

# Decisions

- D1：这一层与客户端无关，只用标准 MCP；Claude Code / Codex 的配置只是以后的来源之一（用户确认）。
- D2：本次只做 P0，P1–P3 各自另开 change（用户确认：“按刚刚的那个开始”）。
- D3：在云端开发，Comet 只作为方法参考，不在用户电脑上运行 Comet CLI（用户确认）。
- D4：结论只有 deny、ask、note 三种，项目规则只能增加限制（见 Constraints；理由：不需要信任机制，也不会被项目文件削弱全局规则）。
- D5：拒绝以工具错误返回，模型能看到规则文件、第几条和理由。

# Open questions

- [blocking] Q1: 规则文件放在哪？推荐：独立文件 `~/.winhand/guards.toml` 加项目 `.winhand/guards.toml`；备选：放进 `config.toml` 的 `[guards]` 段。
- [blocking] Q2: 是否自带默认规则？推荐：不带，文档给示例；备选：默认开启“禁止批量删除”。
- [blocking] Q3: 规则文件解析失败时怎么办？推荐：拒绝它管辖范围内的动作（失败即关闭）；备选：忽略该文件并附警告（失败即放行）。
- [blocking] Q4: ask 规则没人应答或无法弹窗时怎么办？推荐：视为拒绝；备选：视为放行。
- [blocking] Q5: 交互式会话输入的检查 P0 做不做？推荐：做，只检查 shell 类会话提交的整行；备选：推迟到 P1。
- [blocking] Q6: `context()` 默认读哪些全局规矩文件？推荐：自动包含已存在的 `~/.codex/AGENTS.md`、`~/.claude/CLAUDE.md`；备选：默认不读，由配置列出。
- [blocking] Q7: 技能目录默认扫哪些？推荐：`~/.agents/skills`、`~/.claude/skills`、`~/.codex/skills`，以及项目里的 `.agents/skills`、`.claude/skills`；备选：只扫 `.agents/skills` 两级。
- [blocking] Q8: 本机 stdio 客户端是否显示规则指引？推荐：默认不显示，可配置打开；备选：始终显示。

# Verification expectations

- 正式检查（在 `agent/` 下）：`uv sync --locked`、`uv run ruff check src tests`、`uv run ruff format --check src tests`、`uv run pytest -q`。
- 对本分支手动触发 GitHub Actions `agent.yml`，ubuntu 和 windows 都要通过。
- 本机冒烟（经 winhand，在临时目录、临时 `WINHAND_HOME` 下运行，结束后删除）：
  - 对真实项目目录调用 `context()`；
  - 一条 deny 规则能拦住命令；
  - instructions 不超过 4096 字符。
- 每轮由新的只读 Verifier 独立判断 A1–A14，每项给出 passed、failed 或 blocked；结论写入 `verification.md`。
